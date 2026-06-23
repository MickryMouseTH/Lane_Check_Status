"""Lane_Check_Server — RabbitMQ -> MySQL ingestion service.

Consumes Lane_Check_Status JSON payloads from RabbitMQ and stores each one,
split across per-section MySQL tables (see db_mysql.py). Resilient by design:
- reconnects to RabbitMQ with a backoff if the broker drops,
- a failed DB write nacks the message back to the queue (requeue) so nothing is
  lost while MySQL is briefly unavailable,
- a message that cannot be parsed at all is dropped (nack, no requeue) so a
  poison message does not loop forever.

Uses LogLibrary for config + logging (same style as the collector); the
RabbitMQ and MySQL passwords are encrypted on disk automatically.

Build standalone:
    pyinstaller --onefile --name Lane_Check_Server --paths . \
        --hidden-import pika --hidden-import pymysql --hidden-import loguru \
        --hidden-import cryptography.fernet --hidden-import db_mysql server_consumer.py
"""
import json
import sys
import time

from LogLibrary import Load_Config, Loguru_Logging, script_dir

from db_mysql import Database
from json_archive import JsonArchive
from manual_import import ManualImporter

try:
    import pika
    _HAS_PIKA = True
except ImportError:
    _HAS_PIKA = False

# ----------------------- Configuration Values -----------------------
Program_Name = "Lane_Check_Server"
Program_Version = "1.0.6"
# ---------------------------------------------------------------------

default_config = {
    # ---- LogLibrary core keys ----
    "log_Level": "DEBUG",
    "Log_Console": 1,
    "log_Backup": 90,
    "Log_Size": "100 MB",

    # ---- RabbitMQ source (must match the collector's queue) ----
    "RabbitMQ": {
        "Host": "localhost",
        "Port": 5672,
        "VHost": "/",
        "Username": "guest",
        "Password": "guest",
        "Queue": "system_status",
        "Durable": 1,
        "Prefetch": 20,
        "Connection_Timeout": 10,
        "Reconnect_Delay": 5,
    },

    # ---- Save received payloads to disk (per-day .json, zipped daily) ----
    "Received_Files": {
        "Enable": 1,                # 1 = save each received payload to a .json file.
        "Directory": "received",    # relative to executable/script dir.
        "Retention_Days": 30,       # delete archives older than this (0 = keep forever).
        "Daily_Zip": 1,             # roll up each finished day into received/YYYY-MM-DD.zip.
        "Daily_Zip_Time": "00:01",  # HH:MM at/after which the previous day is zipped.
    },

    # ---- Manual folder import (fallback when RabbitMQ is unavailable) ----
    # Drop .json or .zip payload files here and they are imported into MySQL.
    "Manual_Import": {
        "Enable": 1,
        "Directory": "manual",           # watch dir (relative to executable/script).
        "Processed_Subdir": "processed",  # imported files moved here.
        "Failed_Subdir": "failed",        # files that failed moved here.
        "Scan_Interval": 30,              # seconds between folder scans.
        "Min_Age_Seconds": 5,             # ignore files modified within this window.
        "Delete_After": 0,                # 1 = delete on success instead of moving.
    },

    # ---- MySQL destination ----
    "MySQL": {
        "Host": "localhost",
        "Port": 3306,
        "User": "lane_check",
        "Password": "change_me",
        "Database": "lane_check",
        "Charset": "utf8mb4",
        "Connection_Timeout": 10,
        "Reconnect_Delay": 5,
        "Table_Prefix": "",
    },
}


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _on_message(logger, db, archive, channel, method, _properties, body):
    """Handle one delivered message: parse -> archive -> store -> ack (or nack)."""
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.error("Dropping unparseable message ({} bytes): {}", len(body), exc)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    logger.debug("Received payload: {}", payload)

    # Archive the raw received payload first (named by source hostname), then run
    # daily-zip housekeeping. This keeps a local copy even if the DB write fails.
    archive.write(payload, name_prefix=payload.get("hostname"))
    archive.maintain()

    if db.store_payload(payload):
        channel.basic_ack(delivery_tag=method.delivery_tag)
    else:
        # DB write failed (e.g. MySQL down) — requeue and let it retry later.
        logger.warning("DB store failed; requeueing message for retry.")
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        # Brief pause so we don't hot-loop while MySQL is unavailable.
        time.sleep(int(db.cfg.get("Reconnect_Delay", 5)))


def _consume_forever(logger, config, db, archive):
    """Connect to RabbitMQ and consume, reconnecting on failure until stopped."""
    rmq = config.get("RabbitMQ", {})
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
                on_message_callback=lambda ch, m, p, b: _on_message(logger, db, archive, ch, m, p, b),
                auto_ack=False,
            )
            logger.info("Connected. Consuming queue '{}'. Press Ctrl+C to stop.", queue)
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

    db = Database(logger, config.get("MySQL", {}))
    archive = JsonArchive(logger, config.get("Received_Files", {}), script_dir, Program_Name)

    # Ensure the schema exists before consuming, retrying until MySQL is up.
    while not db.ensure_schema():
        delay = int(config.get("MySQL", {}).get("Reconnect_Delay", 5))
        logger.warning("Could not initialise MySQL schema; retrying in {}s ...", delay)
        time.sleep(delay)

    # Start the manual-folder importer (fallback path; independent of RabbitMQ).
    manual = ManualImporter(logger, config.get("Manual_Import", {}), config.get("MySQL", {}), script_dir)
    manual.start()

    try:
        _consume_forever(logger, config, db, archive)
    finally:
        manual.stop()
        db.close()
        logger.info("Lane_Check_Server stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
