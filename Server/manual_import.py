"""Manual folder import — a fallback ingestion path when RabbitMQ is unavailable.

Operators can drop payload files into a watched directory and the server will
import them into MySQL, independent of the broker. Supports:
- plain ``.json`` files (a single payload), including the collector's spool
  files (``msg_*.json``) and archived payloads, and
- ``.zip`` files (e.g. the collector's daily archive zips) — every ``*.json``
  member inside is imported.

Successfully imported files are moved to a ``processed`` sub-folder (or deleted
if configured); files that fail are moved to ``failed`` for inspection. The
importer runs in its own thread with its OWN MySQL connection (PyMySQL
connections are not thread-safe to share), so it keeps working even while the
RabbitMQ consumer is stuck retrying a dead broker.
"""
import json
import os
import shutil
import threading
import time
import zipfile

from db_mysql import Database


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class ManualImporter:
    """Watch a directory and import dropped JSON/ZIP payload files into MySQL."""

    def __init__(self, logger, manual_config, mysql_config, script_dir):
        self.logger = logger
        self.cfg = manual_config
        self.enabled = _truthy(manual_config.get("Enable", 0))
        self.scan_interval = max(2, int(manual_config.get("Scan_Interval", 30)))
        self.min_age = max(0, int(manual_config.get("Min_Age_Seconds", 5)))
        self.delete_after = _truthy(manual_config.get("Delete_After", 0))

        directory = manual_config.get("Directory", "manual")
        if not os.path.isabs(directory):
            directory = os.path.join(script_dir, directory)
        self.directory = directory
        self.processed_dir = os.path.join(directory, manual_config.get("Processed_Subdir", "processed"))
        self.failed_dir = os.path.join(directory, manual_config.get("Failed_Subdir", "failed"))

        # Dedicated DB connection for this thread.
        self.db = Database(logger, mysql_config)

        self._stop = threading.Event()
        self._thread = None

    # ------------------------------ Lifecycle ----------------------------
    def start(self):
        """Spawn the background scan thread (no-op if disabled)."""
        if not self.enabled:
            self.logger.debug("Manual import disabled in config.")
            return
        for d in (self.directory, self.processed_dir, self.failed_dir):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as exc:
                self.logger.error("Could not create manual dir '{}': {}", d, exc)
                return
        self.logger.info(
            "Manual import enabled: watching '{}' every {}s.", self.directory, self.scan_interval
        )
        self._thread = threading.Thread(target=self._run, name="manual-import", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.db.close()

    # ------------------------------- Loop --------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as exc:
                self.logger.exception("Manual import scan error: {}", exc)
            # Sleep in short slices so stop() is responsive.
            for _ in range(self.scan_interval):
                if self._stop.is_set():
                    break
                time.sleep(1)

    def _scan_once(self):
        """Process every eligible file currently in the watch directory."""
        try:
            names = sorted(os.listdir(self.directory))
        except OSError as exc:
            self.logger.warning("Could not list manual dir '{}': {}", self.directory, exc)
            return

        # If MySQL is unreachable, leave files in place and retry next scan
        # instead of wrongly moving valid payloads to failed/ (the manual folder
        # is the fallback for exactly the outage scenario where the DB may also
        # be briefly down).
        if not self.db.connect():
            self.logger.warning("Manual import: MySQL unreachable; deferring scan (files left in place).")
            return

        now = time.time()
        for name in names:
            path = os.path.join(self.directory, name)
            if not os.path.isfile(path):
                continue  # skip processed/ and failed/ sub-dirs
            lower = name.lower()
            if not (lower.endswith(".json") or lower.endswith(".zip")):
                continue
            # Skip files that are still being written (recently modified).
            try:
                if self.min_age and (now - os.path.getmtime(path)) < self.min_age:
                    self.logger.debug("Skipping '{}' (too recently modified).", name)
                    continue
            except OSError:
                continue

            if lower.endswith(".zip"):
                ok = self._process_zip(path)
            else:
                ok = self._process_json_file(path)
            self._dispose(path, ok)

    # ----------------------------- Processing ----------------------------
    def _process_json_file(self, path):
        """Import a single .json payload file. Returns True on success."""
        try:
            with open(path, "rb") as fh:
                payload = json.loads(fh.read().decode("utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            self.logger.error("Manual import: bad JSON '{}': {}", os.path.basename(path), exc)
            return False

        if self.db.store_payload(payload):
            self.logger.info("Manual import: stored '{}'.", os.path.basename(path))
            return True
        self.logger.warning("Manual import: DB store failed for '{}'.", os.path.basename(path))
        return False

    def _process_zip(self, path):
        """Import every .json member of a zip. Returns True only if ALL succeed."""
        name = os.path.basename(path)
        try:
            with zipfile.ZipFile(path) as zf:
                members = [m for m in zf.namelist() if m.lower().endswith(".json")]
                if not members:
                    self.logger.warning("Manual import: zip '{}' has no .json members.", name)
                    return False
                all_ok = True
                for member in members:
                    try:
                        payload = json.loads(zf.read(member).decode("utf-8", errors="replace"))
                    except (ValueError, KeyError) as exc:
                        self.logger.error("Manual import: bad member '{}' in '{}': {}", member, name, exc)
                        all_ok = False
                        continue
                    if not self.db.store_payload(payload):
                        self.logger.warning("Manual import: DB store failed for '{}' in '{}'.", member, name)
                        all_ok = False
                self.logger.info(
                    "Manual import: zip '{}' -> {} member(s), all_ok={}.", name, len(members), all_ok
                )
                return all_ok
        except (OSError, zipfile.BadZipFile) as exc:
            self.logger.error("Manual import: bad zip '{}': {}", name, exc)
            return False

    def _dispose(self, path, ok):
        """Move a processed file to processed/ or failed/ (or delete on success)."""
        name = os.path.basename(path)
        if ok and self.delete_after:
            try:
                os.remove(path)
                self.logger.debug("Manual import: deleted '{}' after import.", name)
            except OSError as exc:
                self.logger.warning("Could not delete '{}': {}", name, exc)
            return

        dest_dir = self.processed_dir if ok else self.failed_dir
        dest = os.path.join(dest_dir, name)
        # Avoid clobbering an existing file of the same name.
        if os.path.exists(dest):
            dest = os.path.join(dest_dir, f"{int(time.time()*1000)}_{name}")
        try:
            shutil.move(path, dest)
            self.logger.debug("Manual import: moved '{}' -> {}.", name, "processed" if ok else "failed")
        except OSError as exc:
            self.logger.warning("Could not move '{}' to {}: {}", name, dest_dir, exc)
