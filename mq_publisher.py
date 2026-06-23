"""RabbitMQ publisher with an on-disk spool for store-and-forward delivery.

Design goals:
- Never lose a payload because the broker is unreachable. If publishing fails,
  the JSON message is written to a spool directory and retried on later cycles.
- Flush the spool (oldest first) whenever the connection is healthy, so backed-up
  messages drain in order once RabbitMQ returns.
- Keep a single long-lived connection, reconnecting lazily on demand.

The spool is a directory of timestamped ``*.json`` files. Each file is one
message body. Files are deleted only after the broker confirms publication
(publisher confirms), guaranteeing at-least-once delivery.
"""
import json
import os
import time

try:
    import pika
    _HAS_PIKA = True
except ImportError:  # allows the module to import even before deps are installed
    _HAS_PIKA = False


class MQPublisher:
    """Publish JSON messages to RabbitMQ, spooling to disk on failure."""

    def __init__(self, logger, mq_config, spool_config, script_dir):
        self.logger = logger
        self.cfg = mq_config
        self.spool_enabled = _truthy(spool_config.get("Enable", 1))
        self.max_spool_files = int(spool_config.get("Max_Files", 10000))
        self.script_dir = script_dir

        spool_dir = spool_config.get("Directory", "spool")
        if not os.path.isabs(spool_dir):
            spool_dir = os.path.join(script_dir, spool_dir)
        self.spool_dir = spool_dir
        os.makedirs(self.spool_dir, exist_ok=True)

        self._connection = None
        self._channel = None

        if not _HAS_PIKA:
            self.logger.error("pika is not installed; messages will only be spooled. Run: pip install pika")

    # ----------------------------- Connection ----------------------------
    def _connect(self):
        """Establish (or reuse) a connection + channel with publisher confirms."""
        if self._channel is not None and self._connection is not None \
                and self._connection.is_open and self._channel.is_open:
            return True

        if not _HAS_PIKA:
            return False

        host = self.cfg.get("Host", "localhost")
        port = int(self.cfg.get("Port", 5672))
        vhost = self.cfg.get("VHost", "/")
        username = self.cfg.get("Username", "guest")
        password = self.cfg.get("Password", "guest")
        timeout = int(self.cfg.get("Connection_Timeout", 10))

        self.logger.debug("Connecting to RabbitMQ at {}:{} vhost='{}' user='{}'.", host, port, vhost, username)
        try:
            credentials = pika.PlainCredentials(username, password)
            params = pika.ConnectionParameters(
                host=host,
                port=port,
                virtual_host=vhost,
                credentials=credentials,
                socket_timeout=timeout,
                blocked_connection_timeout=timeout,
                heartbeat=30,
            )
            self._connection = pika.BlockingConnection(params)
            self._channel = self._connection.channel()
            self._channel.confirm_delivery()  # enable publisher confirms

            # Declare a durable queue so messages survive a broker restart.
            queue = self.cfg.get("Queue", "")
            if queue:
                self._channel.queue_declare(
                    queue=queue,
                    durable=_truthy(self.cfg.get("Durable", 1)),
                )
            self.logger.info("Connected to RabbitMQ at {}:{}.", host, port)
            return True
        except Exception as exc:  # pika raises a broad set of exceptions
            self.logger.warning("RabbitMQ connection failed: {}", exc)
            self._connection = None
            self._channel = None
            return False

    def close(self):
        """Close the broker connection cleanly (best effort)."""
        try:
            if self._connection is not None and self._connection.is_open:
                self._connection.close()
                self.logger.debug("RabbitMQ connection closed.")
        except Exception as exc:
            self.logger.debug("Error closing RabbitMQ connection: {}", exc)
        finally:
            self._connection = None
            self._channel = None

    # ----------------------------- Publishing ----------------------------
    def _publish_raw(self, body_bytes):
        """Publish raw bytes once. Returns True only on a confirmed delivery."""
        if not self._connect():
            return False

        exchange = self.cfg.get("Exchange", "")
        routing_key = self.cfg.get("Routing_Key") or self.cfg.get("Queue", "")
        try:
            self._channel.basic_publish(
                exchange=exchange,
                routing_key=routing_key,
                body=body_bytes,
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2,  # persistent message
                ),
                mandatory=True,
            )
            self.logger.debug("Published message to exchange='{}' routing_key='{}'.", exchange, routing_key)
            return True
        except Exception as exc:
            self.logger.warning("Publish failed ({}); will spool/retry.", exc)
            # Drop the channel so the next attempt reconnects fresh.
            self.close()
            return False

    def publish(self, message):
        """Publish a message dict, falling back to the spool on failure.

        The flow each cycle is:
          1. Try to drain any previously spooled messages (oldest first).
          2. Try to publish the new message directly.
          3. If the broker is down, spool the new message for a later retry.

        Returns True if the new message was published immediately, False if it
        was spooled.
        """
        # Always attempt to flush the backlog first so ordering is preserved.
        self.flush_spool()

        body = json.dumps(message, ensure_ascii=False).encode("utf-8")

        if self._publish_raw(body):
            self.logger.info("Message published to RabbitMQ.")
            return True

        self._spool_message(body)
        return False

    # ------------------------------- Spool -------------------------------
    def _spool_message(self, body_bytes):
        """Write a message body to the spool directory for later delivery."""
        if not self.spool_enabled:
            self.logger.error("Spool disabled and broker unreachable; message DROPPED.")
            return

        existing = self._spool_files()
        if len(existing) >= self.max_spool_files:
            # Drop the oldest to bound disk usage; warn loudly because this is data loss.
            oldest = existing[0]
            try:
                os.remove(oldest)
                self.logger.warning("Spool full ({}); dropped oldest '{}'.", self.max_spool_files, oldest)
            except OSError:
                pass

        # Monotonic-ish unique filename: nanosecond timestamp + pid.
        fname = f"msg_{time.time_ns()}_{os.getpid()}.json"
        fpath = os.path.join(self.spool_dir, fname)
        tmp = f"{fpath}.tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(body_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, fpath)
            self.logger.info("Message spooled to '{}' (backlog now {}).", fpath, len(self._spool_files()))
        except OSError as exc:
            self.logger.error("Failed to spool message: {}", exc)

    def _spool_files(self):
        """Return spooled message paths sorted oldest-first (by filename)."""
        try:
            names = [n for n in os.listdir(self.spool_dir) if n.startswith("msg_") and n.endswith(".json")]
        except OSError:
            return []
        names.sort()  # filenames embed a nanosecond timestamp, so name sort == time sort
        return [os.path.join(self.spool_dir, n) for n in names]

    def flush_spool(self):
        """Try to publish every spooled message, oldest first.

        Stops at the first failure (broker likely down) to avoid hammering an
        unavailable broker and to keep strict ordering.
        """
        files = self._spool_files()
        if not files:
            return

        self.logger.debug("Flushing spool: {} message(s) pending.", len(files))
        sent = 0
        for fpath in files:
            try:
                with open(fpath, "rb") as fh:
                    body = fh.read()
            except OSError as exc:
                self.logger.warning("Could not read spooled file '{}': {}", fpath, exc)
                continue

            if self._publish_raw(body):
                try:
                    os.remove(fpath)
                except OSError:
                    pass
                sent += 1
            else:
                self.logger.debug("Broker still unreachable; {} message(s) remain spooled.", len(files) - sent)
                break

        if sent:
            self.logger.info("Flushed {} spooled message(s) to RabbitMQ.", sent)


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)
