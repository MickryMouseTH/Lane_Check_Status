"""MySQL persistence for Lane_Check_Status payloads.

Each functional section of a status payload is stored in its OWN table, all
keyed by the composite primary key (timestamp_utc, hostname). Sections that
carry multiple rows per host (disk paths, SMART devices/attributes, program
logs/lines) extend the key with a discriminator (path / device / attr_id /
name / line_no).

Tables (with optional Table_Prefix):
    host                (PK: timestamp_utc, hostname)
    cpu                 (PK: timestamp_utc, hostname)
    memory              (PK: timestamp_utc, hostname)
    disk_usage          (PK: timestamp_utc, hostname, path)
    smart               (PK: timestamp_utc, hostname, device)
    smart_attributes    (PK: timestamp_utc, hostname, device, attr_id)
    services_systemd    (PK: timestamp_utc, hostname, unit)
    services_process    (PK: timestamp_utc, hostname, name)
    program_logs        (PK: timestamp_utc, hostname, name)
    program_log_lines   (PK: timestamp_utc, hostname, program_name, line_no)

Rows are written with INSERT ... ON DUPLICATE KEY UPDATE so re-delivered
messages (RabbitMQ is at-least-once) update in place instead of erroring.
"""
import json
from datetime import datetime, timezone

try:
    import pymysql
    _HAS_PYMYSQL = True
except ImportError:
    _HAS_PYMYSQL = False


def _parse_ts(ts):
    """Parse an ISO-8601 timestamp into a naive UTC datetime for DATETIME(6).

    The collector emits e.g. "2026-06-23T07:05:09.482113+00:00". We normalise to
    UTC and drop the tzinfo so it stores cleanly in a MySQL DATETIME(6) column.
    Falls back to "now" if the field is missing/unparseable.
    """
    if ts:
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Database:
    """Thin MySQL wrapper that creates the schema and stores payloads."""

    def __init__(self, logger, mysql_config):
        self.logger = logger
        self.cfg = mysql_config
        self.prefix = mysql_config.get("Table_Prefix", "") or ""
        self._conn = None
        if not _HAS_PYMYSQL:
            self.logger.error("PyMySQL is not installed. Run: pip install PyMySQL")

    def _t(self, name):
        """Return the prefixed, back-quoted table name."""
        return f"`{self.prefix}{name}`"

    # ----------------------------- Connection ----------------------------
    def connect(self):
        """Open (or reuse) a MySQL connection. Returns True on success."""
        if self._conn is not None:
            try:
                self._conn.ping(reconnect=True)
                return True
            except Exception:
                self._conn = None

        if not _HAS_PYMYSQL:
            return False

        try:
            self._conn = pymysql.connect(
                host=self.cfg.get("Host", "localhost"),
                port=int(self.cfg.get("Port", 3306)),
                user=self.cfg.get("User", "root"),
                password=self.cfg.get("Password", ""),
                database=self.cfg.get("Database", "lane_check"),
                charset=self.cfg.get("Charset", "utf8mb4"),
                connect_timeout=int(self.cfg.get("Connection_Timeout", 10)),
                autocommit=False,
            )
            self.logger.info(
                "Connected to MySQL {}:{}/{}.",
                self.cfg.get("Host"), self.cfg.get("Port"), self.cfg.get("Database"),
            )
            return True
        except Exception as exc:
            self.logger.warning("MySQL connection failed: {}", exc)
            self._conn = None
            return False

    def close(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        finally:
            self._conn = None

    # ------------------------------- Schema ------------------------------
    def ensure_schema(self):
        """Create all tables if they do not exist. Returns True on success."""
        if not self.connect():
            return False

        statements = [
            f"""CREATE TABLE IF NOT EXISTS {self._t('host')} (
                timestamp_utc  DATETIME(6)  NOT NULL,
                hostname       VARCHAR(150) NOT NULL,
                program        VARCHAR(100),
                version        VARCHAR(40),
                timestamp_epoch BIGINT,
                os_system      VARCHAR(60),
                os_release     VARCHAR(120),
                os_platform    VARCHAR(255),
                received_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (timestamp_utc, hostname)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

            f"""CREATE TABLE IF NOT EXISTS {self._t('cpu')} (
                timestamp_utc  DATETIME(6)  NOT NULL,
                hostname       VARCHAR(150) NOT NULL,
                percent        DOUBLE,
                core_count     INT,
                load_avg_1m    DOUBLE,
                load_avg_5m    DOUBLE,
                load_avg_15m   DOUBLE,
                per_core_percent JSON,
                PRIMARY KEY (timestamp_utc, hostname)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

            f"""CREATE TABLE IF NOT EXISTS {self._t('memory')} (
                timestamp_utc     DATETIME(6)  NOT NULL,
                hostname          VARCHAR(150) NOT NULL,
                ram_total_kb      BIGINT,
                ram_available_kb  BIGINT,
                ram_used_kb       BIGINT,
                ram_percent       DOUBLE,
                swap_total_kb     BIGINT,
                swap_used_kb      BIGINT,
                swap_percent      DOUBLE,
                PRIMARY KEY (timestamp_utc, hostname)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

            f"""CREATE TABLE IF NOT EXISTS {self._t('disk_usage')} (
                timestamp_utc  DATETIME(6)  NOT NULL,
                hostname       VARCHAR(150) NOT NULL,
                path           VARCHAR(400) NOT NULL,
                total_kb       BIGINT,
                used_kb        BIGINT,
                free_kb        BIGINT,
                percent        DOUBLE,
                error          VARCHAR(255),
                PRIMARY KEY (timestamp_utc, hostname, path)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

            f"""CREATE TABLE IF NOT EXISTS {self._t('smart')} (
                timestamp_utc     DATETIME(6)  NOT NULL,
                hostname          VARCHAR(150) NOT NULL,
                device            VARCHAR(120) NOT NULL,
                model_name        VARCHAR(150),
                serial_number     VARCHAR(120),
                firmware_version  VARCHAR(60),
                smart_passed      TINYINT,
                temperature_c     INT,
                power_on_hours    BIGINT,
                power_cycle_count BIGINT,
                error             VARCHAR(255),
                PRIMARY KEY (timestamp_utc, hostname, device)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

            f"""CREATE TABLE IF NOT EXISTS {self._t('smart_attributes')} (
                timestamp_utc  DATETIME(6)  NOT NULL,
                hostname       VARCHAR(150) NOT NULL,
                device         VARCHAR(120) NOT NULL,
                attr_id        INT          NOT NULL,
                name           VARCHAR(120),
                value          INT,
                worst          INT,
                thresh         INT,
                raw            BIGINT,
                raw_string     VARCHAR(120),
                type           VARCHAR(40),
                when_failed    VARCHAR(40),
                PRIMARY KEY (timestamp_utc, hostname, device, attr_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

            f"""CREATE TABLE IF NOT EXISTS {self._t('raid')} (
                timestamp_utc  DATETIME(6)  NOT NULL,
                hostname       VARCHAR(150) NOT NULL,
                available      TINYINT,
                raid_detected  TINYINT,
                command        VARCHAR(255),
                returncode     INT,
                output         JSON,
                stderr         VARCHAR(2000),
                error          VARCHAR(255),
                collected_at   VARCHAR(40),
                PRIMARY KEY (timestamp_utc, hostname)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

            f"""CREATE TABLE IF NOT EXISTS {self._t('services_systemd')} (
                timestamp_utc  DATETIME(6)  NOT NULL,
                hostname       VARCHAR(150) NOT NULL,
                unit           VARCHAR(200) NOT NULL,
                load_state     VARCHAR(40),
                active_state   VARCHAR(40),
                sub_state      VARCHAR(40),
                enabled        VARCHAR(40),
                main_pid       BIGINT,
                ok             TINYINT,
                error          VARCHAR(255),
                PRIMARY KEY (timestamp_utc, hostname, unit)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

            f"""CREATE TABLE IF NOT EXISTS {self._t('services_process')} (
                timestamp_utc  DATETIME(6)  NOT NULL,
                hostname       VARCHAR(150) NOT NULL,
                name           VARCHAR(150) NOT NULL,
                pattern        VARCHAR(255),
                running        TINYINT,
                count          INT,
                pids           JSON,
                rss_kb         BIGINT,
                uptime_seconds BIGINT,
                ok             TINYINT,
                error          VARCHAR(255),
                PRIMARY KEY (timestamp_utc, hostname, name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

            f"""CREATE TABLE IF NOT EXISTS {self._t('program_logs')} (
                timestamp_utc     DATETIME(6)  NOT NULL,
                hostname          VARCHAR(150) NOT NULL,
                name              VARCHAR(150) NOT NULL,
                log_path_pattern  VARCHAR(512),
                log_path          VARCHAR(512),
                new_lines_total   INT,
                matched_count     INT,
                output_truncated  TINYINT,
                error             VARCHAR(255),
                PRIMARY KEY (timestamp_utc, hostname, name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

            f"""CREATE TABLE IF NOT EXISTS {self._t('program_log_lines')} (
                timestamp_utc  DATETIME(6)  NOT NULL,
                hostname       VARCHAR(150) NOT NULL,
                program_name   VARCHAR(150) NOT NULL,
                line_no        INT          NOT NULL,
                line           TEXT,
                PRIMARY KEY (timestamp_utc, hostname, program_name, line_no)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        ]

        try:
            with self._conn.cursor() as cur:
                for stmt in statements:
                    cur.execute(stmt)
            self._conn.commit()
            self.logger.info("MySQL schema ensured ({} table(s), prefix='{}').", len(statements), self.prefix)
            return True
        except Exception as exc:
            self.logger.error("Failed to ensure schema: {}", exc)
            self._conn.rollback()
            return False

    # ------------------------------ Storage ------------------------------
    @staticmethod
    def _bool_int(value):
        """Map True/False/None to 1/0/None for TINYINT columns."""
        if value is None:
            return None
        return 1 if value else 0

    def _upsert(self, cur, table, columns, values):
        """Run an INSERT ... ON DUPLICATE KEY UPDATE for one row."""
        col_list = ", ".join(f"`{c}`" for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        updates = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in columns)
        sql = f"INSERT INTO {self._t(table)} ({col_list}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}"
        cur.execute(sql, values)

    def store_payload(self, payload):
        """Persist one full payload across all section tables in a transaction.

        Returns True on commit, False on failure (caller decides ack/requeue).
        """
        if not self.connect():
            return False

        ts = _parse_ts(payload.get("timestamp_utc"))
        host = payload.get("hostname", "unknown")

        try:
            with self._conn.cursor() as cur:
                self._store_host(cur, ts, host, payload)
                self._store_cpu(cur, ts, host, payload.get("cpu", {}) or {})
                self._store_memory(cur, ts, host, payload.get("memory", {}) or {})
                self._store_disks(cur, ts, host, payload.get("disk_usage", []) or [])
                self._store_smart(cur, ts, host, payload.get("smart", []) or [])
                self._store_raid(cur, ts, host, payload.get("raid", {}) or {},
                                 payload.get("raid_collected_at"))
                self._store_services(cur, ts, host, payload.get("services", {}) or {})
                self._store_program_logs(cur, ts, host, payload.get("program_logs", []) or [])
            self._conn.commit()
            self.logger.info("Stored payload for {} @ {} into MySQL.", host, ts.isoformat())
            return True
        except Exception as exc:
            self.logger.exception("Failed to store payload for {} @ {}: {}", host, ts, exc)
            try:
                self._conn.rollback()
            except Exception:
                pass
            return False

    def _store_host(self, cur, ts, host, payload):
        os_info = payload.get("os", {}) or {}
        self._upsert(cur, "host",
            ["timestamp_utc", "hostname", "program", "version", "timestamp_epoch",
             "os_system", "os_release", "os_platform"],
            [ts, host, payload.get("program"), payload.get("version"),
             payload.get("timestamp_epoch"), os_info.get("system"),
             os_info.get("release"), os_info.get("platform")])

    def _store_cpu(self, cur, ts, host, cpu):
        if not cpu:
            return
        self._upsert(cur, "cpu",
            ["timestamp_utc", "hostname", "percent", "core_count",
             "load_avg_1m", "load_avg_5m", "load_avg_15m", "per_core_percent"],
            [ts, host, cpu.get("percent"), cpu.get("core_count"),
             cpu.get("load_avg_1m"), cpu.get("load_avg_5m"), cpu.get("load_avg_15m"),
             json.dumps(cpu.get("per_core_percent"))])

    def _store_memory(self, cur, ts, host, memory):
        if not memory:
            return
        ram = memory.get("ram", {}) or {}
        swap = memory.get("swap", {}) or {}
        self._upsert(cur, "memory",
            ["timestamp_utc", "hostname", "ram_total_kb", "ram_available_kb",
             "ram_used_kb", "ram_percent", "swap_total_kb", "swap_used_kb", "swap_percent"],
            [ts, host, ram.get("total_kb"), ram.get("available_kb"), ram.get("used_kb"),
             ram.get("percent"), swap.get("total_kb"), swap.get("used_kb"), swap.get("percent")])

    def _store_disks(self, cur, ts, host, disks):
        for d in disks:
            path = d.get("path")
            if not path:
                continue
            self._upsert(cur, "disk_usage",
                ["timestamp_utc", "hostname", "path", "total_kb", "used_kb",
                 "free_kb", "percent", "error"],
                [ts, host, path, d.get("total_kb"), d.get("used_kb"),
                 d.get("free_kb"), d.get("percent"), d.get("error")])

    def _store_smart(self, cur, ts, host, smart):
        for s in smart:
            device = s.get("device")
            if not device:
                # Error-only entries without a device (e.g. smartctl missing).
                device = "_global"
            self._upsert(cur, "smart",
                ["timestamp_utc", "hostname", "device", "model_name", "serial_number",
                 "firmware_version", "smart_passed", "temperature_c",
                 "power_on_hours", "power_cycle_count", "error"],
                [ts, host, device, s.get("model_name"), s.get("serial_number"),
                 s.get("firmware_version"), self._bool_int(s.get("smart_passed")),
                 s.get("temperature_c"), s.get("power_on_hours"),
                 s.get("power_cycle_count"), s.get("error")])

            for attr in s.get("attributes", []) or []:
                attr_id = attr.get("id")
                if attr_id is None:
                    continue
                self._upsert(cur, "smart_attributes",
                    ["timestamp_utc", "hostname", "device", "attr_id", "name", "value",
                     "worst", "thresh", "raw", "raw_string", "type", "when_failed"],
                    [ts, host, device, attr_id, attr.get("name"), attr.get("value"),
                     attr.get("worst"), attr.get("thresh"), attr.get("raw"),
                     attr.get("raw_string"), attr.get("type"), attr.get("when_failed")])

    def _store_raid(self, cur, ts, host, raid, collected_at):
        if not raid:
            return
        output = raid.get("output")
        self._upsert(cur, "raid",
            ["timestamp_utc", "hostname", "available", "raid_detected", "command",
             "returncode", "output", "stderr", "error", "collected_at"],
            [ts, host, self._bool_int(raid.get("available")),
             self._bool_int(raid.get("raid_detected")), raid.get("command"),
             raid.get("returncode"),
             json.dumps(output) if output is not None else None,
             raid.get("stderr"), raid.get("error"), collected_at])

    def _store_services(self, cur, ts, host, services):
        if not services:
            return
        for s in services.get("systemd", []) or []:
            unit = s.get("unit")
            if not unit:
                continue
            self._upsert(cur, "services_systemd",
                ["timestamp_utc", "hostname", "unit", "load_state", "active_state",
                 "sub_state", "enabled", "main_pid", "ok", "error"],
                [ts, host, unit, s.get("load_state"), s.get("active_state"),
                 s.get("sub_state"), s.get("enabled"), s.get("main_pid"),
                 self._bool_int(s.get("ok")), s.get("error")])

        for p in services.get("processes", []) or []:
            name = p.get("name")
            if not name:
                continue
            pids = p.get("pids")
            self._upsert(cur, "services_process",
                ["timestamp_utc", "hostname", "name", "pattern", "running", "count",
                 "pids", "rss_kb", "uptime_seconds", "ok", "error"],
                [ts, host, name, p.get("pattern"), self._bool_int(p.get("running")),
                 p.get("count"), json.dumps(pids) if pids is not None else None,
                 p.get("rss_kb"), p.get("uptime_seconds"),
                 self._bool_int(p.get("ok")), p.get("error")])

    def _store_program_logs(self, cur, ts, host, programs):
        for p in programs:
            name = p.get("name", "unknown")
            self._upsert(cur, "program_logs",
                ["timestamp_utc", "hostname", "name", "log_path_pattern", "log_path",
                 "new_lines_total", "matched_count", "output_truncated", "error"],
                [ts, host, name, p.get("log_path_pattern"), p.get("log_path"),
                 p.get("new_lines_total"), p.get("matched_count"),
                 self._bool_int(p.get("output_truncated")), p.get("error")])

            for i, line in enumerate(p.get("lines", []) or []):
                self._upsert(cur, "program_log_lines",
                    ["timestamp_utc", "hostname", "program_name", "line_no", "line"],
                    [ts, host, name, i, line])
