"""Write each outgoing JSON payload to a dated file; zip each finished day.

This is independent of RabbitMQ delivery: the archive keeps a local copy of
exactly what was produced each cycle, regardless of whether it was published or
spooled.

Storage model:
- During the day, payloads are written as plain ``.json`` files under a daily
  folder: ``<dir>/YYYY-MM-DD/<prefix>_YYYYmmdd_HHMMSS_xxx.json``.
- Once a day has finished, the whole folder is rolled up into a single
  ``<dir>/YYYY-MM-DD.zip`` and the folder is removed. The rollup runs on the
  first cycle at/after the configured time of day (default 00:01), so it zips
  *past* days only — never the day still being written.
- Retention deletes day zips (and any leftover files/folders) older than N days.

All behaviour is configurable and the whole feature can be turned off.
"""
import json
import os
import re
import time
import zipfile
from datetime import date, datetime


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


# A finished-day folder name, e.g. "2026-06-23".
_DAY_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class JsonArchive:
    """Persist outgoing payloads as plain JSON, zipped per finished day."""

    def __init__(self, logger, archive_config, script_dir, program_name):
        self.logger = logger
        self.enabled = _truthy(archive_config.get("Enable", 0))
        self.retention_days = int(archive_config.get("Retention_Days", 30))
        self.daily_zip = _truthy(archive_config.get("Daily_Zip", 1))
        self.program_name = program_name or "app"

        # Time of day (HH:MM) at/after which a finished day is rolled up to zip.
        self.zip_hour, self.zip_minute = _parse_hhmm(archive_config.get("Daily_Zip_Time", "00:01"))

        directory = archive_config.get("Directory", "output")
        if not os.path.isabs(directory):
            directory = os.path.join(script_dir, directory)
        self.directory = directory

        if self.enabled:
            os.makedirs(self.directory, exist_ok=True)
            self.logger.info(
                "JSON archive enabled: dir='{}', daily_zip={} at {:02d}:{:02d}, retention={} day(s).",
                self.directory, self.daily_zip, self.zip_hour, self.zip_minute, self.retention_days,
            )
        else:
            self.logger.debug("JSON archive disabled in config.")

    # ------------------------------ Writing ------------------------------
    def write(self, payload, name_prefix=None):
        """Write one payload as a plain .json file under today's folder.

        `name_prefix` overrides the filename prefix (defaults to program_name).
        On the server side we pass the source hostname so received files are
        identifiable per host. No-op (returns None) when archiving is disabled.
        Failures are logged but never raised — archiving must not break the loop.
        """
        if not self.enabled:
            return None

        now = datetime.now()
        target_dir = os.path.join(self.directory, now.strftime("%Y-%m-%d"))
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as exc:
            self.logger.error("Failed to create archive day dir '{}': {}", target_dir, exc)
            return None

        # Nanosecond suffix keeps filenames unique even within the same second.
        prefix = _safe_prefix(name_prefix) or self.program_name
        base = f"{prefix}_{now.strftime('%Y%m%d_%H%M%S')}_{time.time_ns() % 1000:03d}"
        fpath = os.path.join(target_dir, base + ".json")
        tmp = fpath + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, fpath)
            self.logger.info("Archived payload to '{}'.", fpath)
        except OSError as exc:
            self.logger.error("Failed to archive payload to '{}': {}", fpath, exc)
            return None
        return fpath

    # ------------------------ Daily zip rollup ---------------------------
    def maintain(self, now=None):
        """Run periodic housekeeping: roll up finished days, then prune.

        Call this once per cycle. Cheap when there is nothing to do.
        """
        if not self.enabled:
            return
        if now is None:
            now = datetime.now()
        if self.daily_zip:
            self._rollup_finished_days(now)
        self._prune()

    def _rollup_finished_days(self, now):
        """Zip every past-day folder into ``<dir>/<day>.zip`` and remove it.

        Only runs once the current time is at/after the configured rollup time,
        and only touches folders dated strictly before today — so the day being
        written is never zipped mid-stream.
        """
        # Before the rollup time on the current day, do nothing.
        if (now.hour, now.minute) < (self.zip_hour, self.zip_minute):
            return

        today = now.date()
        try:
            entries = sorted(os.listdir(self.directory))
        except OSError as exc:
            self.logger.warning("Could not list archive dir '{}': {}", self.directory, exc)
            return

        for name in entries:
            if not _DAY_DIR_RE.match(name):
                continue
            sub = os.path.join(self.directory, name)
            if not os.path.isdir(sub):
                continue
            try:
                day = date.fromisoformat(name)
            except ValueError:
                continue
            if day >= today:
                continue  # don't zip today's (or future) folder
            self._zip_day_folder(name, sub)

    def _zip_day_folder(self, day_name, sub):
        """Zip one finished-day folder atomically, then delete the folder."""
        zip_path = os.path.join(self.directory, day_name + ".zip")
        tmp_zip = zip_path + ".tmp"
        try:
            files = sorted(
                f for f in os.listdir(sub)
                if f.endswith(".json") and os.path.isfile(os.path.join(sub, f))
            )
            if not files:
                # Empty/spurious folder — just remove it.
                try:
                    os.rmdir(sub)
                except OSError:
                    pass
                return

            with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for f in files:
                    zf.write(os.path.join(sub, f), arcname=f)
            os.replace(tmp_zip, zip_path)
            self.logger.info("Rolled up {} file(s) of {} into '{}'.", len(files), day_name, zip_path)

            # Remove the source folder now that it is safely zipped.
            for f in files:
                try:
                    os.remove(os.path.join(sub, f))
                except OSError:
                    pass
            try:
                os.rmdir(sub)
            except OSError:
                self.logger.debug("Day folder '{}' not empty after rollup; left in place.", sub)
        except OSError as exc:
            self.logger.error("Failed to roll up day '{}': {}", day_name, exc)
            if os.path.exists(tmp_zip):
                try:
                    os.remove(tmp_zip)
                except OSError:
                    pass

    # ------------------------------ Pruning ------------------------------
    def _prune(self):
        """Delete day zips and any leftover files older than the retention window.

        Retention is measured by file mtime. Retention_Days <= 0 keeps forever.
        Emptied day folders are removed too.
        """
        if self.retention_days <= 0:
            return

        cutoff = time.time() - (self.retention_days * 86400)
        removed = 0

        # 1) Day zips at the top level (e.g. "2026-05-01.zip").
        try:
            for name in os.listdir(self.directory):
                path = os.path.join(self.directory, name)
                is_day_zip = name.endswith(".zip") and _DAY_DIR_RE.match(name[:-4])
                if is_day_zip and os.path.isfile(path):
                    try:
                        if os.path.getmtime(path) < cutoff:
                            os.remove(path)
                            removed += 1
                    except OSError:
                        continue
        except OSError as exc:
            self.logger.warning("Could not list archive dir '{}': {}", self.directory, exc)

        # 2) Any leftover loose .json inside (un-rolled) day folders. Files are
        #    prefixed by source hostname here, so match by extension only.
        for root, _dirs, files in os.walk(self.directory):
            for name in files:
                if not name.endswith(".json"):
                    continue
                path = os.path.join(root, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed += 1
                except OSError:
                    continue

        self._remove_empty_day_dirs()

        if removed:
            self.logger.info(
                "Pruned {} archive item(s) older than {} day(s).", removed, self.retention_days
            )

    def _remove_empty_day_dirs(self):
        """Remove now-empty ``YYYY-MM-DD`` sub-directories under the archive dir."""
        try:
            for name in os.listdir(self.directory):
                if not _DAY_DIR_RE.match(name):
                    continue
                sub = os.path.join(self.directory, name)
                if os.path.isdir(sub) and not os.listdir(sub):
                    try:
                        os.rmdir(sub)
                    except OSError:
                        pass
        except OSError:
            pass


def _safe_prefix(value):
    """Sanitise a filename prefix (e.g. a hostname) to safe characters.

    Keeps alphanumerics, dot, dash and underscore; everything else becomes '_'.
    Returns None for empty/invalid input so the caller can fall back.
    """
    if not value:
        return None
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(value))
    return cleaned[:80] or None


def _parse_hhmm(value, default=(0, 1)):
    """Parse an 'HH:MM' string into (hour, minute), falling back to default."""
    try:
        hh, mm = str(value).strip().split(":")
        h, m = int(hh), int(mm)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (ValueError, AttributeError):
        pass
    return default
