"""Seed the Lane_Check MySQL database with realistic demo data.

Generates a coherent multi-host, multi-day time series covering EVERY payload
section (cpu / memory / disk / smart / raid / services / ping / usb /
program_logs) and stores it through the normal ``db_mysql.Database`` path — so
what lands in MySQL is exactly what the live server would write.

Intended for building / testing dashboards against a local DB. Values wander
over time (CPU load, RAM creep, disk filling) and include occasional problems
(a ping timeout, a failed systemd unit, an unplugged USB device) so charts and
alerts have something to show.

Note on re-running: cycle timestamps are anchored to "now", so running this again
at a later time produces a NEW time-shifted series (it does not overwrite the
previous run — the (timestamp_utc, hostname) keys differ). To reset a host's data,
delete its rows first (``DELETE ... WHERE hostname IN (...)`` across every table)
and then seed once.

Usage (from the Server/ directory):
    python3 seed_demo_data.py                 # defaults: 3 hosts, 7 days, hourly
    python3 seed_demo_data.py --days 14 --step-minutes 30
    python3 seed_demo_data.py --hosts DD13,DD14 --days 3
"""
import argparse
import math
import random
import sys
from datetime import datetime, timedelta, timezone

from LogLibrary import Load_Config
from db_mysql import Database
from server_consumer import default_config, Program_Name


# --------------------------------------------------------------------------
# Static per-host inventory (kept stable across cycles so the dashboard sees a
# consistent fleet). Ping targets / USB devices mirror the collector config.
# --------------------------------------------------------------------------
PING_TARGETS = [
    {"Hostname": "Gateway", "Address": "192.168.1.1"},
    {"Hostname": "Google DNS", "Address": "8.8.8.8"},
    {"Hostname": "TPS Server", "Address": "10.0.0.42"},
]

USB_INVENTORY = [
    {"name": "POS Keyboard (Cherry SPOS)", "vendor_id": "046a", "product_id": "0038",
     "manufacturer": "Cherry", "product": "SPOS", "serial": None},
    {"name": "NFC Reader (SL600)", "vendor_id": "0471", "product_id": "a112",
     "manufacturer": "HighTech Reader Co. Ltd.", "product": "SL600-NFC Reader",
     "serial": "HTR3310000000000"},
    {"name": "Touchscreen (eGalax)", "vendor_id": "0eef", "product_id": "c002",
     "manufacturer": "eGalax Inc.", "product": "eGalaxTouch P80H84", "serial": None},
    {"name": "Receipt Printer (EPSON TM-T88VI)", "vendor_id": "04b8", "product_id": "0202",
     "manufacturer": "EPSON", "product": "TM-T88VI", "serial": "583658430024590000"},
]

SYSTEMD_UNITS = ["rabbitmq-server.service", "nginx.service"]
PROCESSES = [
    {"name": "bangkok_tps", "pattern": "bangkoktps.linux"},
    {"name": "tct_app", "pattern": "TCT_App.exe"},
]


def _iso(dt):
    return dt.isoformat()


def _cpu(rng, t):
    """CPU that oscillates with a daily rhythm plus noise."""
    base = 25 + 15 * math.sin(t / 6.0) + rng.uniform(-6, 6)
    base = max(1.0, min(98.0, base))
    cores = 4
    per_core = [max(0.0, min(100.0, base + rng.uniform(-10, 10))) for _ in range(cores)]
    load = round(base / 100 * cores, 2)
    return {
        "percent": round(base, 1),
        "per_core_percent": [round(c, 1) for c in per_core],
        "core_count": cores,
        "load_avg_1m": load,
        "load_avg_5m": round(load * 0.9, 2),
        "load_avg_15m": round(load * 0.8, 2),
    }


def _memory(rng, creep):
    """RAM usage with a slow upward creep (so a leak-like trend is visible)."""
    total = 16210172
    pct = min(92.0, 30.0 + creep * 0.5 + rng.uniform(-3, 3))
    used = int(total * pct / 100)
    return {
        "ram": {"total_kb": total, "available_kb": total - used, "used_kb": used, "percent": round(pct, 1)},
        "swap": {"total_kb": 2097148, "used_kb": int(rng.uniform(0, 50000)), "percent": round(rng.uniform(0, 3), 1)},
    }


def _disk(day_index):
    """Disk usage that slowly fills over the run (root + home)."""
    root_pct = min(95.0, 38.0 + day_index * 0.4)
    home_pct = min(95.0, 60.0 + day_index * 0.6)
    def entry(path, total, pct):
        used = int(total * pct / 100)
        return {"path": path, "total_kb": total, "used_kb": used,
                "free_kb": total - used, "percent": round(pct, 1)}
    return [entry("/", 491134084, root_pct), entry("/home", 976762584, home_pct)]


def _smart(rng, hours):
    """Two disks with a handful of attributes; temperature wanders a bit."""
    def dev(name, model, serial, poh, base_temp):
        temp = int(base_temp + rng.uniform(-2, 3))
        attrs = [
            {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "worst": 100, "thresh": 10,
             "raw": 0, "raw_string": "0", "type": "Pre-fail", "when_failed": "-"},
            {"id": 9, "name": "Power_On_Hours", "value": 99, "worst": 99, "thresh": 0,
             "raw": poh, "raw_string": str(poh), "type": "Old_age", "when_failed": "-"},
            {"id": 12, "name": "Power_Cycle_Count", "value": 99, "worst": 99, "thresh": 0,
             "raw": 142, "raw_string": "142", "type": "Old_age", "when_failed": "-"},
            {"id": 194, "name": "Temperature_Celsius", "value": 100, "worst": 100, "thresh": 0,
             "raw": temp, "raw_string": str(temp), "type": "Old_age", "when_failed": "-"},
        ]
        return {"device": name, "model_name": model, "serial_number": serial,
                "firmware_version": "SVT02B6Q", "smart_passed": True,
                "temperature_c": temp, "power_on_hours": poh, "power_cycle_count": 142,
                "attributes": attrs}
    return [
        dev("/dev/sda", "Samsung SSD 870 EVO 500GB", "S5Y2NJ0R123456", 8421 + hours, 34),
        dev("/dev/sdb", "WD Blue SA510 1TB", "WD-WX11A80C7890", 3120 + hours, 39),
    ]


def _raid():
    return {
        "available": True, "raid_detected": True, "command": "dmraid -n",
        "returncode": 0,
        "output": [
            "/dev/sda (isw):",
            "raid set \"isw_raid_Volume0\": level 1, 2 disks, state OK",
        ],
    }


def _services(rng):
    """systemd units + named processes; nginx occasionally flaps down."""
    nginx_ok = rng.random() > 0.08
    systemd = [
        {"unit": "rabbitmq-server.service", "load_state": "loaded", "active_state": "active",
         "sub_state": "running", "enabled": "enabled", "main_pid": 5268, "ok": True},
        {"unit": "nginx.service", "load_state": "loaded",
         "active_state": "active" if nginx_ok else "failed",
         "sub_state": "running" if nginx_ok else "failed",
         "enabled": "enabled", "main_pid": 4120 if nginx_ok else None, "ok": nginx_ok},
    ]
    tps_up = rng.random() > 0.05
    processes = [
        {"name": "bangkok_tps", "pattern": "bangkoktps.linux", "running": tps_up,
         "count": 1 if tps_up else 0, "pids": [5830] if tps_up else [],
         "rss_kb": int(2674048 + rng.uniform(-50000, 80000)) if tps_up else 0,
         "vms_kb": int(5276972 + rng.uniform(-50000, 120000)) if tps_up else 0,
         "num_threads": int(45 + rng.uniform(-3, 6)) if tps_up else 0,
         "uptime_seconds": 1814400 if tps_up else None, "ok": tps_up},
        {"name": "tct_app", "pattern": "TCT_App.exe", "running": True, "count": 1,
         "pids": [3275], "rss_kb": int(28068 + rng.uniform(-2000, 4000)),
         "vms_kb": int(1049280 + rng.uniform(-5000, 9000)),
         "num_threads": int(24 + rng.uniform(-2, 4)), "uptime_seconds": 430000, "ok": True},
    ]
    return {"systemd": systemd, "processes": processes}


def _ping(rng):
    """Ping each target; TPS server occasionally times out, DNS latency varies."""
    out = []
    for t in PING_TARGETS:
        # Gateway is LAN (sub-ms), DNS is WAN (~10-20ms), TPS server sometimes down.
        if t["Address"] == "192.168.1.1":
            rtt, up = round(rng.uniform(0.2, 1.2), 3), True
        elif t["Address"] == "8.8.8.8":
            rtt, up = round(rng.uniform(8, 22), 1), True
        else:
            up = rng.random() > 0.12
            rtt = round(rng.uniform(15, 60), 1) if up else None
        out.append({
            "hostname": t["Hostname"], "address": t["Address"],
            "reachable": up, "rtt_ms": rtt,
            "packet_loss_percent": 0.0 if up else 100.0, "ok": up,
        })
    return out


def _usb(rng):
    """Expected USB inventory; the printer occasionally goes missing."""
    out = []
    for d in USB_INVENTORY:
        present = True
        if d["product_id"] == "0202":       # printer sometimes unplugged / off
            present = rng.random() > 0.1
        out.append({
            "name": d["name"], "vendor_id": d["vendor_id"], "product_id": d["product_id"],
            "present": present, "count": 1 if present else 0,
            "manufacturer": d["manufacturer"] if present else None,
            "product": d["product"] if present else None,
            "serial": d["serial"] if present else None,
            "ok": present,
        })
    return out


def _http_probe(rng):
    """Simulate a Q-Free RSE651 HTTP status page probe (serial/MAC extract)."""
    up = rng.random() > 0.05
    if up:
        return [{
            "name": "RSE651 mra242", "url": "http://10.0.0.15:1337",
            "ok": True, "status_code": 200,
            "response_ms": round(rng.uniform(20, 120), 1),
            "fields": {"serial_number": "RSE1003032", "mac": "00:12:8e:04:2f:db",
                       "host": "mra242"},
        }]
    return [{
        "name": "RSE651 mra242", "url": "http://10.0.0.15:1337",
        "ok": False, "status_code": None,
        "response_ms": round(rng.uniform(9000, 10000), 1),
        "fields": {}, "error": "timed out",
    }]


def _program_logs(rng, ts):
    matched = int(rng.uniform(0, 4))
    lines = [
        f"{ts:%Y-%m-%d %H:%M:%S} ERROR worker-{i} order_service timeout on reserve"
        for i in range(matched)
    ]
    return [{
        "name": "TCT_EventIO",
        "log_path_pattern": "/var/log/tct/EventIO_yyyy-mm-dd.log",
        "log_path": f"/var/log/tct/EventIO_{ts:%Y-%m-%d}.log",
        "new_lines_total": int(rng.uniform(50, 400)),
        "matched_count": matched, "output_truncated": False, "lines": lines,
    }]


def build_payload(host, ts, cycle, day_index, rng):
    hours = cycle // 60
    return {
        "program": "Lane_Check_Status", "version": "1.3.0", "hostname": host,
        "timestamp_utc": _iso(ts), "timestamp_epoch": int(ts.timestamp()),
        "os": {"system": "Linux", "release": "6.8.0-40-generic",
               "platform": "Linux-6.8.0-40-generic-x86_64-with-glibc2.39"},
        "cpu": _cpu(rng, cycle),
        "memory": _memory(rng, cycle % 48),
        "disk_usage": _disk(day_index),
        "smart": _smart(rng, hours), "smart_collected_at": _iso(ts),
        "raid": _raid(), "raid_collected_at": _iso(ts),
        "services": _services(rng), "services_collected_at": _iso(ts),
        "ping": _ping(rng), "ping_collected_at": _iso(ts),
        "usb": _usb(rng), "usb_collected_at": _iso(ts),
        "http_probe": _http_probe(rng), "http_probe_collected_at": _iso(ts),
        "program_logs": _program_logs(rng, ts),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Seed Lane_Check MySQL with demo data.")
    ap.add_argument("--hosts", default="DD13,DD14,DD15",
                    help="comma-separated hostnames (default DD13,DD14,DD15)")
    ap.add_argument("--days", type=int, default=7, help="days of history (default 7)")
    ap.add_argument("--step-minutes", type=int, default=60,
                    help="minutes between cycles (default 60)")
    ap.add_argument("--seed", type=int, default=1337, help="RNG seed for reproducibility")
    args = ap.parse_args(argv)

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    step = max(1, args.step_minutes)
    cycles = max(1, (args.days * 24 * 60) // step)

    # Minimal console logger compatible with db_mysql's logger.info/.warning/.error.
    class _Log:
        def _fmt(self, m, a):
            try:
                return m.format(*a) if a else m
            except Exception:
                return m
        def debug(self, m, *a):
            pass
        def info(self, m, *a):
            # Per-payload "Stored payload" logs are too noisy for a bulk seed;
            # the script prints its own progress instead.
            pass
        def warning(self, m, *a):
            print("WARN ", self._fmt(m, a))
        def error(self, m, *a):
            print("ERROR", self._fmt(m, a))
        def exception(self, m, *a):
            print("EXC  ", self._fmt(m, a))

    logger = _Log()
    config = Load_Config(default_config, "Lane_Check_Server")
    db = Database(logger, config.get("MySQL", {}))

    if not db.ensure_schema():
        print("Failed to ensure schema; aborting.", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).replace(microsecond=0)
    total = len(hosts) * cycles
    print(f"Seeding {total} cycles: {len(hosts)} host(s) x {cycles} cycles "
          f"({args.days}d @ {step}min), ending now.")

    written = failed = 0
    for host in hosts:
        rng = random.Random(f"{args.seed}:{host}")
        for i in range(cycles):
            # Oldest first so timestamps march forward to 'now'.
            ts = now - timedelta(minutes=step * (cycles - 1 - i))
            day_index = (i * step) // (24 * 60)
            payload = build_payload(host, ts, i, day_index, rng)
            if db.store_payload(payload):
                written += 1
            else:
                failed += 1
        print(f"  {host}: done ({cycles} cycles).")

    db.close()
    print(f"Seed complete: {written} payload(s) stored, {failed} failed.")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
