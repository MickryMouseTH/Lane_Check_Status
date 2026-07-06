"""Database retention cleaner — periodically purges old rows from MySQL.

Deletes rows whose ``timestamp_utc`` is older than ``Retention_Days`` across every
per-cycle table (see ``db_mysql._ALL_TABLES``), so the database does not grow
without bound. Runs in its own thread with its OWN MySQL connection (PyMySQL
connections are not thread-safe to share with the consumer / manual importer),
mirroring the ManualImporter design.

A failed purge (e.g. MySQL briefly down) is logged and simply retried on the next
interval — it never crashes the server.
"""
import threading
import time

from db_mysql import Database


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class RetentionCleaner:
    """Background thread that prunes DB rows older than a retention window."""

    def __init__(self, logger, retention_config, mysql_config):
        self.logger = logger
        self.cfg = retention_config or {}
        self.enabled = _truthy(self.cfg.get("Enable", 0))
        self.retention_days = int(self.cfg.get("Retention_Days", 30) or 0)
        # How often to run the purge (hours -> seconds; min 1 hour).
        self.interval_seconds = max(1, int(self.cfg.get("Cleanup_Interval_Hours", 24))) * 3600
        self.run_on_startup = _truthy(self.cfg.get("Run_On_Startup", 1))

        # Dedicated DB connection for this thread.
        self.db = Database(logger, mysql_config)

        self._stop = threading.Event()
        self._thread = None

    # ------------------------------ Lifecycle ----------------------------
    def start(self):
        """Spawn the background cleanup thread (no-op if disabled)."""
        if not self.enabled:
            self.logger.debug("DB retention cleanup disabled in config.")
            return
        if self.retention_days <= 0:
            self.logger.info("DB retention cleanup enabled but Retention_Days<=0; nothing will be purged.")
            return
        self.logger.info(
            "DB retention cleanup enabled: purge rows older than {} day(s), every {}h.",
            self.retention_days, self.interval_seconds // 3600,
        )
        self._thread = threading.Thread(target=self._run, name="db-cleanup", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.db.close()

    # ------------------------------- Loop --------------------------------
    def _run(self):
        # Optionally purge once at startup, then on the configured interval.
        if not self.run_on_startup:
            self._sleep(self.interval_seconds)
        while not self._stop.is_set():
            try:
                self.db.purge_old(self.retention_days)
            except Exception as exc:
                self.logger.exception("DB retention cleanup error: {}", exc)
            self._sleep(self.interval_seconds)

    def _sleep(self, seconds):
        """Sleep in short slices so stop() stays responsive."""
        for _ in range(int(seconds)):
            if self._stop.is_set():
                break
            time.sleep(1)
