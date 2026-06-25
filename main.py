"""Lane_Check_Status — Ubuntu host status collector.

Each cycle the program:
  1. Collects CPU, RAM and per-path disk usage.
  2. Collects S.M.A.R.T. health for every disk (smartmontools).
  3. Reads and filters new lines from each configured program log.
  4. Assembles a single JSON document.
  5. Publishes it to RabbitMQ, spooling to disk if the broker is unreachable.

Configuration and logging are provided by LogLibrary.py (config style, secret
encryption, and Loguru sinks). Logging is verbose per the configured log level
and dumps the collected data at DEBUG.
"""
import os
import platform
import socket
import sys
import time
from datetime import datetime, timezone

from LogLibrary import Load_Config, Loguru_Logging, script_dir

import system_metrics
import smart_collector
import raid_collector
import service_collector
import log_collector
from mq_publisher import MQPublisher
from json_archive import JsonArchive

# ----------------------- Configuration Values -----------------------
Program_Name = "Lane_Check_Status"   # Program name for identification and logging.
Program_Version = "1.1.0"             # Program version used for file naming and logging.
# ---------------------------------------------------------------------

default_config = {
    # ---- LogLibrary core keys ----
    "log_Level": "DEBUG",
    "Log_Console": 1,          # 1/true to enable console logging, 0/false to disable.
    "log_Backup": 90,          # Log retention duration in days.
    "Log_Size": "10 MB",       # Max log file size before rotation.

    # ---- Collector loop ----
    "Interval_Seconds": 60,    # Seconds between collection cycles.
    "Hostname_Override": "",   # Use this name instead of the OS hostname (optional).
    # Low-impact controls: keep this agent out of the way of the real workload.
    "Nice_Level": 10,          # Lower CPU scheduling priority (0..19, higher = nicer). 0 to disable.
    "IO_Nice_Idle": 1,         # 1 = best-effort/idle disk I/O class via ionice (Linux only).

    # ---- RabbitMQ destination ----
    # NOTE: the key "Password" contains "pass", so LogLibrary encrypts it on disk
    # automatically after the first run (value becomes "ENC:...").
    "RabbitMQ": {
        "Enable": 1,                    # 1 = publish to RabbitMQ; 0 = skip sending entirely.
        "Host": "localhost",
        "Port": 5672,
        "VHost": "/",
        "Username": "guest",
        "Password": "guest",
        "Exchange": "",                 # "" = default exchange (publish straight to a queue).
        "Routing_Key": "system.status",
        "Queue": "system_status",       # declared durable; also used as routing key when Exchange is "".
        "Durable": 1,
        "Connection_Timeout": 10,
    },

    # ---- Local JSON archive of every outgoing payload ----
    "Output_Files": {
        "Enable": 1,                    # 1 = save each payload to a .json file; 0 = don't.
        "Directory": "output",          # relative to the executable/script dir.
        "Retention_Days": 30,           # delete archives older than this (0 = keep forever).
        # Each payload is a plain .json under output/YYYY-MM-DD/. After a day
        # finishes it is zipped into output/YYYY-MM-DD.zip and the folder removed.
        "Daily_Zip": 1,                 # 1 = roll up each finished day into a single zip.
        "Daily_Zip_Time": "00:01",      # HH:MM at/after which the previous day is zipped.
    },

    # ---- Disk spool (store-and-forward when broker is down) ----
    "Spool": {
        "Enable": 1,
        "Directory": "spool",           # relative to the executable/script dir.
        "Max_Files": 10000,             # oldest dropped beyond this (bounds disk use).
        # Background sweeper: keeps retrying spooled files on its own cadence so
        # they resend promptly once RabbitMQ recovers (independent of the
        # collection interval).
        "Sweeper_Enable": 1,
        "Flush_Interval": 30,           # seconds between background sweep attempts.
    },

    # ---- Per-path disk usage ----
    "Disk_Paths": ["/", "/home"],

    # ---- smartmontools ----
    "Smart": {
        "Enable": 1,
        "Smartctl_Path": "smartctl",    # absolute path if not on PATH.
        "Devices": [],                  # [] = auto-scan; or e.g. ["/dev/sda", "/dev/nvme0"].
        # smartctl wakes disks and does I/O, so run it less often than the main
        # loop. SMART is collected every Nth cycle; cached in between.
        "Interval_Cycles": 15,
        # Label vendor-specific attributes that smartctl reports as
        # "Unknown_Attribute". Keys are attribute IDs (as strings). Consult the
        # SSD datasheet for the correct meaning of each ID on your drive.
        "Attribute_Names": {
            "148": "SanDisk_Vendor_148",
            "149": "SanDisk_Vendor_149",
            "150": "SanDisk_Vendor_150",
            "151": "SanDisk_Vendor_151"
        },
    },

    # ---- RAID metadata (dmraid -n) ----
    # Captures ATARAID / fakeRAID / BIOS RAID on-disk metadata. Requires root
    # (the service runs as root) and the `dmraid` package installed.
    "Raid": {
        "Enable": 1,
        "Dmraid_Path": "dmraid",        # absolute path if not on PATH.
        # RAID config is essentially static; probe it infrequently and cache
        # the result between refreshes (like SMART).
        "Interval_Cycles": 60,
        "Timeout_Seconds": 20,
    },

    # ---- Service / process health checks ----
    # Two independent checks, both optional:
    #   Systemd_Units — queried with `systemctl show` (healthy = ActiveState=active).
    #   Processes     — matched in the live process table by name/cmdline substring
    #                   (for workloads that are NOT systemd units, e.g. app-launched
    #                   or mono/.NET binaries). Healthy = at least one instance up.
    "Services": {
        "Enable": 1,
        "Systemctl_Path": "systemctl",  # absolute path if not on PATH.
        # Service state can flap, so check it more often than SMART/RAID. Default
        # is every cycle; raise Interval_Cycles to probe less frequently.
        "Interval_Cycles": 1,
        "Timeout_Seconds": 10,          # per-systemctl-call timeout.
        "Systemd_Units": [],            # e.g. ["nginx.service", "rabbitmq-server.service"]
        "Processes": [],                # e.g. [{"Name": "tct_app", "Pattern": "TCT_App.exe"}]
    },

    # ---- Application logs to tail & filter ----
    # Log_Path supports date tokens: yyyy yy mm dd HH MM SS
    #   e.g. "/tct/yyyy-mm/tct_app_ddmmyy.log" -> "/tct/2026-06/tct_app_230626.log"
    "Programs": [
        {
            "Name": "example_app",
            "Log_Path": "/var/log/example_app/app.log",
            "Include_Patterns": ["ERROR", "WARN", "CRITICAL", "Exception"],
            "Exclude_Patterns": ["DEBUG heartbeat"],
            "Max_Lines": 200,
        },
        {
            "Name": "tct_app",
            "Log_Path": "/tct/yyyy-mm/tct_app_ddmmyy.log",
            "Include_Patterns": [],          # empty = keep every new line
            "Exclude_Patterns": [],
            "Max_Lines": 500,
        },
    ],
}


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def apply_low_impact(logger, config):
    """Lower this agent's CPU and disk-I/O priority so it stays out of the way.

    Both calls are best-effort and Linux-specific; failures are logged but never
    fatal. This is what keeps the collector from competing with the real
    workload on a busy host.
    """
    nice_level = int(config.get("Nice_Level", 0) or 0)
    if nice_level > 0 and hasattr(os, "nice"):
        try:
            new_nice = os.nice(nice_level)
            logger.info("Lowered CPU priority: process niceness now {}.", new_nice)
        except OSError as exc:
            logger.debug("Could not set niceness: {}", exc)

    if _truthy(config.get("IO_Nice_Idle", 0)):
        # ionice is not exposed by the stdlib; shell out best-effort.
        import shutil
        import subprocess
        if shutil.which("ionice"):
            try:
                # class 3 = idle: only uses disk when nothing else needs it.
                subprocess.run(
                    ["ionice", "-c", "3", "-p", str(os.getpid())],
                    capture_output=True, check=False, timeout=5,
                )
                logger.info("Set disk I/O scheduling class to idle (ionice -c3).")
            except (OSError, subprocess.SubprocessError) as exc:
                logger.debug("Could not set ionice: {}", exc)
        else:
            logger.debug("ionice not available; skipping I/O priority tuning.")


def build_payload(logger, config, log_state, smart_cache, raid_cache, service_cache):
    """Collect every metric and assemble the JSON-ready payload dict."""
    hostname = config.get("Hostname_Override") or socket.gethostname()
    now = datetime.now(timezone.utc)

    payload = {
        "program": Program_Name,
        "version": Program_Version,
        "hostname": hostname,
        "timestamp_utc": now.isoformat(),
        "timestamp_epoch": int(now.timestamp()),
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "platform": platform.platform(),
        },
    }

    # 1) CPU / RAM / Disk
    payload["cpu"] = system_metrics.collect_cpu(logger)
    payload["memory"] = system_metrics.collect_memory(logger)
    payload["disk_usage"] = system_metrics.collect_disk_usage(
        logger, config.get("Disk_Paths", [])
    )

    # 2) SMART — collected only on due cycles (heavier; wakes disks). Between
    #    refreshes the last result is reused from smart_cache.
    smart_cfg = config.get("Smart", {})
    if _truthy(smart_cfg.get("Enable", 1)):
        if smart_cache.get("due"):
            smart_cache["data"] = smart_collector.collect_smart(
                logger,
                smartctl_path=smart_cfg.get("Smartctl_Path", "smartctl"),
                devices=smart_cfg.get("Devices", []),
                attribute_names=smart_cfg.get("Attribute_Names", {}),
            )
            smart_cache["collected_at"] = now.isoformat()
        else:
            logger.debug("SMART not due this cycle; reusing cached result.")
        payload["smart"] = smart_cache.get("data", [])
        payload["smart_collected_at"] = smart_cache.get("collected_at")
    else:
        logger.debug("SMART collection disabled in config.")
        payload["smart"] = []

    # 2b) RAID metadata (dmraid -n) — static-ish, cached like SMART.
    raid_cfg = config.get("Raid", {})
    if _truthy(raid_cfg.get("Enable", 1)):
        if raid_cache.get("due"):
            raid_cache["data"] = raid_collector.collect_raid(
                logger,
                dmraid_path=raid_cfg.get("Dmraid_Path", "dmraid"),
                timeout=int(raid_cfg.get("Timeout_Seconds", 20)),
            )
            raid_cache["collected_at"] = now.isoformat()
        else:
            logger.debug("RAID not due this cycle; reusing cached result.")
        payload["raid"] = raid_cache.get("data", {})
        payload["raid_collected_at"] = raid_cache.get("collected_at")
    else:
        logger.debug("RAID collection disabled in config.")
        payload["raid"] = {}

    # 2c) Service / process health — cheap, but cached on its own cadence so it
    #     can be throttled independently of the main loop if desired.
    service_cfg = config.get("Services", {})
    if _truthy(service_cfg.get("Enable", 1)):
        if service_cache.get("due"):
            service_cache["data"] = service_collector.collect_services(
                logger,
                systemctl_path=service_cfg.get("Systemctl_Path", "systemctl"),
                systemd_units=service_cfg.get("Systemd_Units", []),
                processes=service_cfg.get("Processes", []),
                timeout=int(service_cfg.get("Timeout_Seconds", 10)),
            )
            service_cache["collected_at"] = now.isoformat()
        else:
            logger.debug("Service check not due this cycle; reusing cached result.")
        payload["services"] = service_cache.get("data", {})
        payload["services_collected_at"] = service_cache.get("collected_at")
    else:
        logger.debug("Service collection disabled in config.")
        payload["services"] = {}

    # 3) Program logs (offset state is mutated in place)
    payload["program_logs"] = log_collector.collect_program_logs(
        logger, config.get("Programs", []), log_state
    )

    logger.debug("Assembled payload: {}", payload)
    return payload


def run_once(logger, config, publisher, archive, log_state, log_state_path, smart_cache, raid_cache, service_cache):
    """Execute a single collection-and-publish cycle."""
    logger.info("=== Collection cycle started ===")
    start = time.monotonic()

    payload = build_payload(logger, config, log_state, smart_cache, raid_cache, service_cache)
    log_collector.save_state(logger, log_state_path, log_state)

    # 4a) Archive a local copy of the payload (independent of MQ delivery),
    #     then run housekeeping (daily zip rollup + retention pruning).
    archive.write(payload)
    archive.maintain()

    # 4b) Publish to RabbitMQ, unless sending is disabled in config.
    if publisher is None:
        logger.info("RabbitMQ sending disabled (RabbitMQ.Enable=0); payload not published.")
    else:
        published = publisher.publish(payload)
        if published:
            logger.info("Cycle published directly to RabbitMQ.")
        else:
            logger.warning("Cycle spooled to disk (broker unreachable).")

    elapsed = time.monotonic() - start
    logger.info("=== Collection cycle finished in {:.2f}s ===", elapsed)


def main():
    config = Load_Config(default_config, Program_Name)
    logger = Loguru_Logging(config, Program_Name, Program_Version)

    logger.info("Effective configuration: {}", _redact(config))

    # Keep the agent low-impact relative to the existing workload on the host.
    apply_low_impact(logger, config)

    interval = max(5, int(config.get("Interval_Seconds", 60)))
    smart_interval_cycles = max(1, int(config.get("Smart", {}).get("Interval_Cycles", 15)))
    raid_interval_cycles = max(1, int(config.get("Raid", {}).get("Interval_Cycles", 60)))
    service_interval_cycles = max(1, int(config.get("Services", {}).get("Interval_Cycles", 1)))
    log_state_path = os.path.join(script_dir, f"{Program_Name}_log_state.json")
    log_state = log_collector.load_state(logger, log_state_path)
    smart_cache = {"data": [], "collected_at": None, "due": True}
    raid_cache = {"data": {}, "collected_at": None, "due": True}
    service_cache = {"data": {}, "collected_at": None, "due": True}

    archive = JsonArchive(
        logger,
        config.get("Output_Files", {}),
        script_dir,
        Program_Name,
    )

    # Only build the publisher (and open spool handling) when sending is enabled.
    if _truthy(config.get("RabbitMQ", {}).get("Enable", 1)):
        publisher = MQPublisher(
            logger,
            config.get("RabbitMQ", {}),
            config.get("Spool", {}),
            script_dir,
        )
        # Start the background spool sweeper (resends spooled files when the
        # broker recovers, independent of the collection loop).
        spool_cfg = config.get("Spool", {})
        if _truthy(spool_cfg.get("Sweeper_Enable", 1)):
            publisher.start_sweeper(int(spool_cfg.get("Flush_Interval", 30)))
    else:
        publisher = None
        logger.info("RabbitMQ.Enable=0: running in archive-only mode (no MQ connection).")

    logger.info(
        "Entering main loop (interval={}s, SMART every {} cycle(s)). Press Ctrl+C to stop.",
        interval, smart_interval_cycles,
    )
    cycle = 0
    try:
        while True:
            # SMART is due on the first cycle and every Nth cycle thereafter.
            smart_cache["due"] = (cycle % smart_interval_cycles == 0)
            # RAID metadata is static-ish; refresh it on its own (slower) cadence.
            raid_cache["due"] = (cycle % raid_interval_cycles == 0)
            # Service health flaps; refresh on its own (usually faster) cadence.
            service_cache["due"] = (cycle % service_interval_cycles == 0)
            try:
                run_once(logger, config, publisher, archive, log_state, log_state_path, smart_cache, raid_cache, service_cache)
            except Exception as exc:
                # One bad cycle must not kill the daemon.
                logger.exception("Unhandled error during collection cycle: {}", exc)
            cycle += 1
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("Stop requested (Ctrl+C); shutting down.")
    finally:
        if publisher is not None:
            publisher.stop_sweeper()
            publisher.close()
        logger.info("Lane_Check_Status stopped.")


def _redact(config):
    """Return a shallow copy of config with secret values masked for logging."""
    import copy
    safe = copy.deepcopy(config)
    rmq = safe.get("RabbitMQ")
    if isinstance(rmq, dict) and rmq.get("Password"):
        rmq["Password"] = "***"
    return safe


if __name__ == "__main__":
    sys.exit(main())
