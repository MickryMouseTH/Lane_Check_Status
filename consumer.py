"""Lane_Check_Consumer — RabbitMQ consumer for Lane_Check_Status payloads.

Connects to the same queue the collector publishes to, decodes each JSON
message, logs a readable health summary, and optionally saves the raw payload to
disk. Designed to be resilient: if the broker is unavailable or drops the
connection, it keeps retrying with a backoff instead of exiting.

Reuses LogLibrary for the config file + Loguru logging (same style as the
collector), so the password is encrypted on disk automatically.

Build standalone the same way as the collector:
    pyinstaller --onefile --name Lane_Check_Consumer \
        --hidden-import pika --hidden-import loguru \
        --hidden-import cryptography.fernet consumer.py
"""
import json
import os
import sys
import time
from datetime import datetime

from LogLibrary import Load_Config, Loguru_Logging, script_dir

try:
    import pika
    _HAS_PIKA = True
except ImportError:
    _HAS_PIKA = False

# ----------------------- Configuration Values -----------------------
Program_Name = "Lane_Check_Consumer"
Program_Version = "1.1.0"
# ---------------------------------------------------------------------

default_config = {
    # ---- LogLibrary core keys ----
    "log_Level": "DEBUG",
    "Log_Console": 1,
    "log_Backup": 90,
    "Log_Size": "10 MB",

    # ---- RabbitMQ source (must match the collector's queue) ----
    "RabbitMQ": {
        "Host": "localhost",
        "Port": 5672,
        "VHost": "/",
        "Username": "guest",
        "Password": "guest",
        "Queue": "system_status",
        "Durable": 1,
        "Prefetch": 20,             # max unacked messages handed to us at once.
        "Connection_Timeout": 10,
        "Reconnect_Delay": 5,       # seconds to wait before reconnecting.
    },

    # ---- Save received payloads to disk (optional) ----
    "Save_Received": {
        "Enable": 1,
        "Directory": "received",    # relative to executable/script dir.
        "Daily_Subdir": 1,          # store under received/YYYY-MM-DD/.
    },
}


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _save_received(logger, save_cfg, payload, raw_bytes):
    """Persist a received payload to disk (best effort)."""
    if not _truthy(save_cfg.get("Enable", 0)):
        return

    directory = save_cfg.get("Directory", "received")
    if not os.path.isabs(directory):
        directory = os.path.join(script_dir, directory)

    now = datetime.now()
    if _truthy(save_cfg.get("Daily_Subdir", 1)):
        directory = os.path.join(directory, now.strftime("%Y-%m-%d"))
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        logger.error("Could not create receive dir '{}': {}", directory, exc)
        return

    host = (payload or {}).get("hostname", "unknown")
    fname = f"{host}_{now.strftime('%Y%m%d_%H%M%S')}_{time.time_ns() % 1000:03d}.json"
    fpath = os.path.join(directory, fname)
    try:
        with open(fpath, "wb") as fh:
            fh.write(raw_bytes)
        logger.debug("Saved received payload to '{}'.", fpath)
    except OSError as exc:
        logger.error("Failed to save received payload '{}': {}", fpath, exc)


def _log_summary(logger, payload):
    """Log a concise, human-readable summary of one status payload."""
    host = payload.get("hostname", "?")
    ts = payload.get("timestamp_utc", "?")
    cpu = payload.get("cpu", {}).get("percent")
    ram = payload.get("memory", {}).get("ram", {}).get("percent")

    disks = payload.get("disk_usage", []) or []
    disk_str = ", ".join(
        f"{d.get('path')}={d.get('percent')}%" if "percent" in d else f"{d.get('path')}=ERR"
        for d in disks
    )

    smart = payload.get("smart", []) or []
    smart_bad = [
        s.get("device") for s in smart
        if s.get("smart_passed") is False or "error" in s
    ]
    smart_str = f"{len(smart)} disk(s)"
    if smart_bad:
        smart_str += f", ATTENTION: {smart_bad}"

    logs = payload.get("program_logs", []) or []
    log_hits = sum(int(l.get("matched_count", 0)) for l in logs)

    logger.info(
        "STATUS from {} @ {} | CPU {}% | RAM {}% | Disk[{}] | SMART {} | log matches {}",
        host, ts, cpu, ram, disk_str, smart_str, log_hits,
    )

    # Surface any matched log lines so alerts are visible without digging in.
    for prog in logs:
        for line in prog.get("lines", []) or []:
            logger.warning("[{}@{}] {}", prog.get("name"), host, line)

    # Dump the full payload at DEBUG for traceability.
    logger.debug("Full received payload: {}", payload)


def _on_message(logger, save_cfg, channel, method, _properties, body):
    """Callback for each delivered message: parse, summarise, save, ack."""
    try:
        text = body.decode("utf-8", errors="replace")
        payload = json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        logger.error("Dropping unparseable message ({} bytes): {}", len(body), exc)
        # Reject without requeue so a poison message doesn't loop forever.
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    try:
        _log_summary(logger, payload)
        _save_received(logger, save_cfg, payload, body)
        channel.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as exc:
        logger.exception("Error handling message; requeueing: {}", exc)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def _consume_forever(logger, config):
    """Connect and consume, reconnecting on any failure until interrupted."""
    rmq = config.get("RabbitMQ", {})
    save_cfg = config.get("Save_Received", {})
    reconnect_delay = int(rmq.get("Reconnect_Delay", 5))

    while True:
        try:
            credentials = pika.PlainCredentials(
                rmq.get("Username", "guest"), rmq.get("Password", "guest")
            )
            params = pika.ConnectionParameters(
                host=rmq.get("Host", "localhost"),
                port=int(rmq.get("Port", 5672)),
                virtual_host=rmq.get("VHost", "/"),
                credentials=credentials,
                socket_timeout=int(rmq.get("Connection_Timeout", 10)),
                heartbeat=30,
            )
            logger.info("Connecting to RabbitMQ at {}:{} ...", params.host, params.port)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()

            queue = rmq.get("Queue", "system_status")
            channel.queue_declare(queue=queue, durable=_truthy(rmq.get("Durable", 1)))
            channel.basic_qos(prefetch_count=int(rmq.get("Prefetch", 20)))

            channel.basic_consume(
                queue=queue,
                on_message_callback=lambda ch, m, p, b: _on_message(logger, save_cfg, ch, m, p, b),
                auto_ack=False,
            )
            logger.info("Connected. Waiting for messages on queue '{}'. Press Ctrl+C to stop.", queue)
            channel.start_consuming()

        except KeyboardInterrupt:
            logger.info("Stop requested (Ctrl+C); shutting down.")
            try:
                channel.stop_consuming()
                connection.close()
            except Exception:
                pass
            return
        except Exception as exc:
            logger.warning("Consumer connection error: {}. Reconnecting in {}s ...", exc, reconnect_delay)
            time.sleep(reconnect_delay)


def main():
    config = Load_Config(default_config, Program_Name)
    logger = Loguru_Logging(config, Program_Name, Program_Version)

    if not _HAS_PIKA:
        logger.error("pika is not installed. Run: pip install pika")
        return 1

    _consume_forever(logger, config)
    logger.info("Lane_Check_Consumer stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
