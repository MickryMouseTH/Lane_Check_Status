"""Collect S.M.A.R.T. health data for every disk using smartmontools.

smartctl >= 7.0 supports JSON output via ``-j``, which we parse directly. Older
smartmontools (e.g. 6.6 on older Ubuntu) does NOT understand ``-j`` and prints a
plain-text error instead — which is why a naive JSON parse fails at "char 0".

This collector therefore:
- detects whether the installed smartctl supports JSON (by version), and
- falls back to parsing classic text output when it does not,
so it works on both old and new hosts. Device names are normalised to ``/dev/*``
and any raw stdout/stderr is logged on failure for easy diagnosis.

smartmontools usually needs root; permission errors are recorded per-device
rather than failing the whole cycle.
"""
import json
import re
import shutil
import subprocess


def _snippet(text, limit=400):
    """Return a trimmed one-line snippet of command output for logging."""
    if not text:
        return ""
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _normalise_device(name):
    """Ensure a device name is an absolute path (prepend /dev/ when missing).

    Fixes configs/scan output like "sda" -> "/dev/sda" so smartctl can open it.
    """
    if not name:
        return name
    if name.startswith("/"):
        return name
    return "/dev/" + name


def detect_json_support(logger, smartctl_path):
    """Return True if this smartctl build supports JSON (``-j``) output.

    JSON output was introduced in smartmontools 7.0. We parse the version banner
    from ``smartctl --version``; on any doubt we assume no JSON and use the
    text-mode path, which always works.
    """
    try:
        proc = subprocess.run(
            [smartctl_path, "--version"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Could not run 'smartctl --version' ({}); assuming no JSON support.", exc)
        return False

    # Banner looks like: "smartctl 7.2 2020-12-30 r5155 ..."
    m = re.search(r"smartctl\s+(\d+)\.(\d+)", proc.stdout)
    if not m:
        logger.warning("Could not parse smartctl version from: {}", _snippet(proc.stdout))
        return False

    major, minor = int(m.group(1)), int(m.group(2))
    supported = major >= 7
    logger.info("Detected smartctl {}.{} (JSON {}).", major, minor, "supported" if supported else "NOT supported")
    return supported


# --------------------------------------------------------------------------
# JSON mode (smartctl >= 7.0)
# --------------------------------------------------------------------------
def _run_smartctl_json(logger, smartctl_path, args, timeout=30):
    """Run ``smartctl -j ...`` and return parsed JSON (or None on failure)."""
    cmd = [smartctl_path, "-j"] + args
    logger.debug("Running smartctl: {}", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("smartctl execution failed for {}: {}", args, exc)
        return None

    if not proc.stdout.strip():
        logger.warning(
            "smartctl returned no output for {} (exit={}, stderr: {}).",
            args, proc.returncode, _snippet(proc.stderr),
        )
        return None

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        logger.error(
            "smartctl JSON parse error for {}: {} | stdout: {} | stderr: {}",
            args, exc, _snippet(proc.stdout), _snippet(proc.stderr),
        )
        return None

    logger.debug("smartctl raw JSON for {}: {}", args, parsed)
    return parsed


def _scan_devices_json(logger, smartctl_path):
    """Discover devices via ``smartctl --scan -j``."""
    parsed = _run_smartctl_json(logger, smartctl_path, ["--scan"])
    devices = []
    if parsed:
        for dev in parsed.get("devices", []):
            devices.append((_normalise_device(dev.get("name")), dev.get("type")))
    logger.debug("smartctl discovered devices (JSON): {}", devices)
    return devices


def _extract_summary_json(device_name, parsed):
    """Pull the operationally useful fields out of a full JSON payload."""
    summary = {
        "device": device_name,
        "model_name": parsed.get("model_name"),
        "serial_number": parsed.get("serial_number"),
        "firmware_version": parsed.get("firmware_version"),
    }
    summary["smart_passed"] = parsed.get("smart_status", {}).get("passed")
    summary["temperature_c"] = parsed.get("temperature", {}).get("current")
    summary["power_on_hours"] = parsed.get("power_on_time", {}).get("hours")
    summary["power_cycle_count"] = parsed.get("power_cycle_count")

    attributes = {}
    for attr in parsed.get("ata_smart_attributes", {}).get("table", []):
        name = attr.get("name")
        if name in (
            "Reallocated_Sector_Ct", "Current_Pending_Sector", "Offline_Uncorrectable",
            "Wear_Leveling_Count", "Media_Wearout_Indicator", "Percent_Lifetime_Remain",
        ):
            attributes[name] = {"value": attr.get("value"), "raw": attr.get("raw", {}).get("value")}
    if attributes:
        summary["key_attributes"] = attributes

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


# --------------------------------------------------------------------------
# Text mode (smartctl < 7.0, no JSON)
# --------------------------------------------------------------------------
def _run_smartctl_text(logger, smartctl_path, args, timeout=30):
    """Run ``smartctl ...`` (no -j) and return (stdout, returncode) or (None, None)."""
    cmd = [smartctl_path] + args
    logger.debug("Running smartctl (text): {}", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("smartctl execution failed for {}: {}", args, exc)
        return None, None

    if proc.stderr.strip():
        logger.debug("smartctl stderr for {}: {}", args, _snippet(proc.stderr))
    return proc.stdout, proc.returncode


def _scan_devices_text(logger, smartctl_path):
    """Discover devices via ``smartctl --scan`` (text)."""
    stdout, _rc = _run_smartctl_text(logger, smartctl_path, ["--scan"])
    devices = []
    if stdout:
        # Lines look like: "/dev/sda -d scsi # /dev/sda, SCSI device"
        for line in stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if not parts:
                continue
            name = _normalise_device(parts[0])
            dev_type = None
            if "-d" in parts:
                idx = parts.index("-d")
                if idx + 1 < len(parts):
                    dev_type = parts[idx + 1]
            devices.append((name, dev_type))
    logger.debug("smartctl discovered devices (text): {}", devices)
    return devices


_TEXT_PATTERNS = {
    "model_name": re.compile(r"^(?:Device Model|Model Number|Product):\s*(.+)$", re.M),
    "serial_number": re.compile(r"^Serial Number:\s*(.+)$", re.M),
    "firmware_version": re.compile(r"^(?:Firmware Version|Revision):\s*(.+)$", re.M),
}


def _extract_summary_text(device_name, stdout):
    """Parse a classic ``smartctl -a`` text report into the same summary shape."""
    summary = {"device": device_name, "model_name": None, "serial_number": None, "firmware_version": None}

    for key, rx in _TEXT_PATTERNS.items():
        m = rx.search(stdout)
        if m:
            summary[key] = m.group(1).strip()

    # Overall health line.
    m = re.search(r"SMART overall-health self-assessment test result:\s*(\w+)", stdout)
    if not m:
        m = re.search(r"SMART Health Status:\s*(\w+)", stdout)  # SCSI wording
    if m:
        summary["smart_passed"] = m.group(1).upper() in ("PASSED", "OK")

    # ATA attribute table rows: "  9 Power_On_Hours  0x0032 099 099 000 Old_age Always - 8421"
    attrs = {}
    for line in stdout.splitlines():
        m = re.match(r"\s*\d+\s+(\w+)\s+0x[0-9a-fA-F]+\s+(\d+)\s+\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+(\d+)", line)
        if not m:
            continue
        name, value, raw = m.group(1), int(m.group(2)), int(m.group(3))
        if name in ("Temperature_Celsius", "Airflow_Temperature_Cel"):
            summary.setdefault("temperature_c", raw)
        elif name == "Power_On_Hours":
            summary["power_on_hours"] = raw
        elif name == "Power_Cycle_Count":
            summary["power_cycle_count"] = raw
        elif name in (
            "Reallocated_Sector_Ct", "Current_Pending_Sector", "Offline_Uncorrectable",
            "Wear_Leveling_Count", "Media_Wearout_Indicator", "Percent_Lifetime_Remain",
        ):
            attrs[name] = {"value": value, "raw": raw}
    if attrs:
        summary["key_attributes"] = attrs

    # Fallback temperature line: "Current Temperature: 34 Celsius"
    if "temperature_c" not in summary:
        m = re.search(r"(?:Current Temperature|Temperature):\s*(\d+)", stdout)
        if m:
            summary["temperature_c"] = int(m.group(1))

    return summary


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def collect_smart(logger, smartctl_path="smartctl", devices=None):
    """Collect SMART summaries for all disks (JSON or text mode automatically).

    Args:
        logger: Loguru logger.
        smartctl_path: Path to / name of the smartctl binary.
        devices: Optional explicit list of device paths (e.g. ["/dev/sda"] or
                 even ["sda"] — normalised to /dev/sda). Empty/None = auto-scan.

    Returns:
        list[dict]: One summary per device (or an error entry per device).
    """
    logger.debug("Starting SMART collection (smartctl='{}', devices={})", smartctl_path, devices)

    if shutil.which(smartctl_path) is None:
        logger.warning("smartctl not found at '{}'; skipping SMART collection.", smartctl_path)
        return [{"error": f"smartctl not found at '{smartctl_path}'"}]

    use_json = detect_json_support(logger, smartctl_path)

    # Resolve the device list (config override or scan), normalising names.
    if devices:
        target_devices = [(_normalise_device(d), None) for d in devices]
    elif use_json:
        target_devices = _scan_devices_json(logger, smartctl_path)
    else:
        target_devices = _scan_devices_text(logger, smartctl_path)

    if not target_devices:
        logger.warning("No SMART-capable devices discovered.")
        return []

    results = []
    for name, dev_type in target_devices:
        if not name:
            continue
        base_args = (["-d", dev_type] if dev_type else []) + ["-a", name]

        if use_json:
            parsed = _run_smartctl_json(logger, smartctl_path, base_args)
            if parsed is None:
                results.append({"device": name, "error": "smartctl JSON query failed (see log)"})
                continue
            summary = _extract_summary_json(name, parsed)
        else:
            stdout, rc = _run_smartctl_text(logger, smartctl_path, base_args)
            if not stdout or not stdout.strip():
                results.append({"device": name, "error": f"smartctl text query failed (exit={rc})"})
                continue
            # smartctl exit bit 1 (value 2) = device open failed (e.g. permission).
            if rc is not None and (rc & 2):
                logger.warning("smartctl could not open '{}' (exit={}); likely needs root.", name, rc)
            summary = _extract_summary_text(name, stdout)

        logger.debug("SMART summary for '{}': {}", name, summary)
        results.append(summary)

    logger.info("SMART collection complete for {} device(s).", len(results))
    return results
