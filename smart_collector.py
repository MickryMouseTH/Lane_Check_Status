"""Collect S.M.A.R.T. health data for every disk using smartmontools.

smartctl (>= 7.0) supports JSON output via `-j`, which we parse directly. The
collector first discovers devices (`smartctl --scan -j`) unless an explicit
device list is configured, then queries each one. smartmontools usually needs
root; when a query fails we record the error per-device rather than failing the
whole cycle.
"""
import json
import shutil
import subprocess


def _run_smartctl(logger, smartctl_path, args, timeout=30):
    """Run smartctl and return parsed JSON (or None on failure).

    smartctl uses its exit code as a bitmask — a non-zero code does NOT always
    mean failure (e.g. bit 0 = command line did not parse, higher bits flag
    disk conditions). We therefore rely on whether JSON parsed, not the code.
    """
    cmd = [smartctl_path, "-j"] + args
    logger.debug("Running smartctl: {}", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("smartctl execution failed for {}: {}", args, exc)
        return None

    if not proc.stdout.strip():
        logger.warning("smartctl returned no output for {} (stderr: {})", args, proc.stderr.strip())
        return None

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        logger.error("smartctl JSON parse error for {}: {}", args, exc)
        return None

    logger.debug("smartctl raw output for {}: {}", args, parsed)
    return parsed


def _scan_devices(logger, smartctl_path):
    """Return a list of (device_name, device_type) tuples discovered on the host."""
    parsed = _run_smartctl(logger, smartctl_path, ["--scan"])
    devices = []
    if parsed:
        for dev in parsed.get("devices", []):
            devices.append((dev.get("name"), dev.get("type")))
    logger.debug("smartctl discovered devices: {}", devices)
    return devices


def _extract_summary(device_name, parsed):
    """Pull the operationally useful fields out of a full smartctl payload.

    Keeps the response compact for the message bus while retaining the most
    important health signals (overall PASS/FAIL, temperature, power-on hours,
    reallocated sectors, etc.).
    """
    summary = {
        "device": device_name,
        "model_name": parsed.get("model_name"),
        "serial_number": parsed.get("serial_number"),
        "firmware_version": parsed.get("firmware_version"),
    }

    status = parsed.get("smart_status", {})
    summary["smart_passed"] = status.get("passed")

    temp = parsed.get("temperature", {})
    summary["temperature_c"] = temp.get("current")

    poh = parsed.get("power_on_time", {})
    summary["power_on_hours"] = poh.get("hours")
    summary["power_cycle_count"] = parsed.get("power_cycle_count")

    # Surface a few headline ATA attributes when present.
    attributes = {}
    for attr in parsed.get("ata_smart_attributes", {}).get("table", []):
        name = attr.get("name")
        if name in (
            "Reallocated_Sector_Ct",
            "Current_Pending_Sector",
            "Offline_Uncorrectable",
            "Wear_Leveling_Count",
            "Media_Wearout_Indicator",
            "Percent_Lifetime_Remain",
        ):
            attributes[name] = {
                "value": attr.get("value"),
                "raw": attr.get("raw", {}).get("value"),
            }
    if attributes:
        summary["key_attributes"] = attributes

    # NVMe drives expose a different health log.
    nvme = parsed.get("nvme_smart_health_information_log")
    if nvme:
        summary["nvme_health"] = {
            "percentage_used": nvme.get("percentage_used"),
            "available_spare": nvme.get("available_spare"),
            "media_errors": nvme.get("media_errors"),
            "critical_warning": nvme.get("critical_warning"),
            "data_units_written": nvme.get("data_units_written"),
        }

    return summary


def collect_smart(logger, smartctl_path="smartctl", devices=None):
    """Collect SMART summaries for all disks.

    Args:
        logger: Loguru logger.
        smartctl_path: Path to / name of the smartctl binary.
        devices: Optional explicit list of device paths (e.g. ["/dev/sda"]).
                 When empty/None, devices are auto-discovered via --scan.

    Returns:
        list[dict]: One summary per device (or an error entry per device).
    """
    logger.debug("Starting SMART collection (smartctl='{}', devices={})", smartctl_path, devices)

    if shutil.which(smartctl_path) is None:
        logger.warning("smartctl not found at '{}'; skipping SMART collection.", smartctl_path)
        return [{"error": f"smartctl not found at '{smartctl_path}'"}]

    if devices:
        target_devices = [(d, None) for d in devices]
    else:
        target_devices = _scan_devices(logger, smartctl_path)

    if not target_devices:
        logger.warning("No SMART-capable devices discovered.")
        return []

    results = []
    for name, dev_type in target_devices:
        if not name:
            continue
        args = ["-a", name]
        if dev_type:
            args = ["-d", dev_type] + args
        parsed = _run_smartctl(logger, smartctl_path, args)
        if parsed is None:
            results.append({"device": name, "error": "smartctl query failed (see log)"})
            continue
        summary = _extract_summary(name, parsed)
        logger.debug("SMART summary for '{}': {}", name, summary)
        results.append(summary)

    logger.info("SMART collection complete for {} device(s).", len(results))
    return results
