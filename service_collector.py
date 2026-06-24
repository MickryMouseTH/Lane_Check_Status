"""Service / process health collector.

Two complementary checks, both optional and config-driven:

1. **systemd units** — for anything managed by systemd we ask
   ``systemctl show <unit>`` for its LoadState / ActiveState / SubState /
   MainPID / UnitFileState. A unit is considered healthy when ActiveState is
   ``active``. Works inside a container as long as that container runs its own
   systemd (the common LXD/Docker-with-systemd case).

2. **named processes** — for workloads that are NOT systemd units (started by an
   app supervisor, launched from a shell, mono/.NET binaries, etc.) we scan the
   live process table with psutil and match each configured pattern against the
   process name and full command line. We report whether it is running, how many
   instances, their PIDs, and a lightweight resource snapshot (RSS + uptime).

Like the SMART and RAID collectors this module never raises: any failure is
captured in the returned structure so one bad probe cannot abort a cycle. The
result is cheap enough to run every cycle, but a cache cadence is supported via
the caller (see ``Services.Interval_Cycles``).
"""
import shutil
import subprocess
import time

import psutil


# --------------------------------------------------------------------------
# systemd units
# --------------------------------------------------------------------------
# Properties pulled in a single `systemctl show` call per unit. Keeping the set
# small keeps the output easy to parse and the call fast.
_SHOW_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "MainPID",
)


def _check_systemd_unit(logger, systemctl_path, unit, timeout):
    """Return a health dict for one systemd unit via ``systemctl show``."""
    entry = {"unit": unit}
    cmd = [
        systemctl_path, "show", unit,
        "--no-pager",
        "--property=" + ",".join(_SHOW_PROPERTIES),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("systemctl show failed for '{}': {}", unit, exc)
        entry["error"] = str(exc)
        entry["ok"] = False
        return entry

    # `systemctl show` prints KEY=VALUE lines; parse them into a dict.
    props = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()

    load_state = props.get("LoadState")
    active_state = props.get("ActiveState")

    # A typo'd / absent unit reports LoadState=not-found. On systemd it still
    # fills ActiveState=inactive, so we must key off LoadState, not "no output".
    if load_state == "not-found":
        entry.update({"load_state": load_state, "active_state": active_state,
                      "error": "unit not found", "ok": False})
        return entry

    # No parseable state at all (systemctl errored, e.g. no privilege / no PID 1).
    if not load_state and not active_state:
        entry.update({"error": f"no state from systemctl (exit={proc.returncode})", "ok": False})
        return entry

    main_pid = props.get("MainPID")
    try:
        main_pid = int(main_pid)
    except (TypeError, ValueError):
        main_pid = None

    entry.update({
        "load_state": load_state,
        "active_state": active_state,
        "sub_state": props.get("SubState"),
        "enabled": props.get("UnitFileState"),
        "main_pid": main_pid if main_pid else None,
        # Healthy = systemd reports the unit as active (running/exited/etc).
        "ok": active_state == "active",
    })
    logger.debug("systemd unit '{}': {}", unit, entry)
    return entry


# --------------------------------------------------------------------------
# Named processes (non-systemd workloads)
# --------------------------------------------------------------------------
def _match_processes(logger, specs):
    """Match configured process specs against the live process table.

    A single pass over ``psutil.process_iter`` so we touch /proc once regardless
    of how many patterns are configured. Each spec is matched against both the
    (possibly truncated) process name and the full command line, case-insensitively.
    """
    # Pre-build a result accumulator per spec, preserving config order.
    results = []
    spec_state = []
    for spec in specs:
        name = spec.get("Name") or spec.get("Pattern") or "unknown"
        pattern = (spec.get("Pattern") or "").strip()
        entry = {
            "name": name,
            "pattern": pattern,
            "running": False,
            "count": 0,
            "pids": [],
            "rss_kb": 0,
            "uptime_seconds": None,
        }
        results.append(entry)
        spec_state.append(pattern.lower())

    now = time.time()
    for proc in psutil.process_iter(["pid", "name", "cmdline", "memory_info", "create_time"]):
        try:
            info = proc.info
            pname = (info.get("name") or "").lower()
            cmdline = " ".join(info.get("cmdline") or []).lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

        for i, pat in enumerate(spec_state):
            if not pat:
                continue
            if pat in pname or pat in cmdline:
                entry = results[i]
                entry["running"] = True
                entry["count"] += 1
                entry["pids"].append(info.get("pid"))
                mem = info.get("memory_info")
                if mem is not None:
                    entry["rss_kb"] += int(getattr(mem, "rss", 0)) // 1024
                # Track the oldest matching instance's uptime (most representative).
                ctime = info.get("create_time")
                if ctime:
                    up = int(now - ctime)
                    if entry["uptime_seconds"] is None or up > entry["uptime_seconds"]:
                        entry["uptime_seconds"] = up

    for entry in results:
        # Healthy = at least one instance is running.
        entry["ok"] = entry["count"] > 0
        logger.debug("process '{}' (pattern '{}'): {}", entry["name"], entry["pattern"], entry)

    return results


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def collect_services(logger, systemctl_path="systemctl", systemd_units=None,
                     processes=None, timeout=10):
    """Collect health for configured systemd units and named processes.

    Args:
        logger: Loguru logger.
        systemctl_path: Path to / name of the systemctl binary.
        systemd_units: List of unit names (e.g. ["nginx.service"]). Empty/None
            skips the systemd check entirely.
        processes: List of dicts ``{"Name": ..., "Pattern": ...}`` to look for in
            the process table. Empty/None skips the process check.
        timeout: Per-``systemctl`` call timeout in seconds.

    Returns:
        dict with keys ``systemd`` (list) and ``processes`` (list). Each entry
        carries an ``ok`` boolean for quick alerting.
    """
    systemd_units = systemd_units or []
    processes = processes or []
    logger.debug(
        "Collecting service health ({} systemd unit(s), {} process spec(s)).",
        len(systemd_units), len(processes),
    )

    result = {"systemd": [], "processes": []}

    if systemd_units:
        if shutil.which(systemctl_path) is None:
            logger.warning("systemctl not found at '{}'; skipping systemd checks.", systemctl_path)
            result["systemd"] = [
                {"unit": u, "error": f"systemctl not found at '{systemctl_path}'", "ok": False}
                for u in systemd_units
            ]
        else:
            for unit in systemd_units:
                if unit:
                    result["systemd"].append(
                        _check_systemd_unit(logger, systemctl_path, unit, timeout)
                    )

    if processes:
        try:
            result["processes"] = _match_processes(logger, processes)
        except Exception as exc:  # psutil can raise assorted OS errors
            logger.warning("Process scan failed: {}", exc)
            result["processes"] = [
                {"name": p.get("Name", "unknown"), "pattern": p.get("Pattern", ""),
                 "error": str(exc), "ok": False}
                for p in processes
            ]

    down = [s["unit"] for s in result["systemd"] if not s.get("ok")] + \
           [p["name"] for p in result["processes"] if not p.get("ok")]
    if down:
        logger.warning("Service check: {} not healthy: {}", len(down), down)
    else:
        logger.info(
            "Service check OK ({} unit(s), {} process(es)).",
            len(result["systemd"]), len(result["processes"]),
        )
    return result
