"""USB device presence collector.

Verifies that the USB peripherals a lane is supposed to have (NFC reader, receipt
printer, touchscreen, POS keyboard, ...) are actually enumerated on the bus. Each
configured device is matched by its USB Vendor:Product ID (and, optionally, a
serial number) against the live device list.

Devices are enumerated with ``usb-devices`` (from the usbutils package) which is
rich enough to also surface Manufacturer / Product / SerialNumber. If that binary
is unavailable we fall back to ``lsusb``. Match by VID:PID rather than a /dev
path because these peripherals (HID readers, usblp printers, touch controllers)
do not all expose a stable /dev node.

Like the other collectors this module never raises: any failure is captured in
the returned structure so one bad probe cannot abort a collection cycle. USB
topology is essentially static, so a cache cadence is supported via the caller
(see ``USB.Interval_Cycles``).
"""
import re
import shutil
import subprocess


# usb-devices block fields.
_UD_VENDOR_RE = re.compile(r"Vendor=([0-9a-fA-F]{4})\s+ProdID=([0-9a-fA-F]{4})")
_UD_STRING_RE = re.compile(r"^S:\s+(\w+)=(.*)$")
# lsusb line: "Bus 001 Device 003: ID 0471:a112 HighTech Reader ... SL600-NFC".
_LSUSB_RE = re.compile(
    r"ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)$", re.IGNORECASE
)


def _parse_usb_devices(text):
    """Parse ``usb-devices`` output into a list of device dicts.

    Blocks are separated by blank lines; we pull Vendor/ProdID from the ``P:``
    line and Manufacturer/Product/SerialNumber from the ``S:`` lines.
    """
    devices = []
    current = {}

    def flush():
        if current.get("vendor_id") and current.get("product_id"):
            devices.append(dict(current))

    for line in text.splitlines():
        if not line.strip():
            flush()
            current.clear()
            continue
        m = _UD_VENDOR_RE.search(line)
        if m:
            current["vendor_id"] = m.group(1).lower()
            current["product_id"] = m.group(2).lower()
            continue
        m = _UD_STRING_RE.match(line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            if key == "Manufacturer":
                current["manufacturer"] = value
            elif key == "Product":
                current["product"] = value
            elif key == "SerialNumber":
                current["serial"] = value
    flush()
    return devices


def _parse_lsusb(text):
    """Parse ``lsusb`` output into a list of device dicts (no serial available)."""
    devices = []
    for line in text.splitlines():
        m = _LSUSB_RE.search(line)
        if not m:
            continue
        devices.append({
            "vendor_id": m.group(1).lower(),
            "product_id": m.group(2).lower(),
            "product": (m.group(3) or "").strip() or None,
        })
    return devices


def _enumerate(logger, command, timeout):
    """Return (list_of_devices, source_name, error). Never raises."""
    # Preferred: usb-devices (has serial numbers). Fall back to lsusb.
    for cmd_name, parser in ((command, _parse_usb_devices), ("lsusb", _parse_lsusb)):
        if not cmd_name or shutil.which(cmd_name) is None:
            continue
        try:
            proc = subprocess.run(
                [cmd_name], capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("USB enumeration via '{}' failed: {}", cmd_name, exc)
            continue
        devices = parser(proc.stdout or "")
        logger.debug("USB: '{}' enumerated {} device(s).", cmd_name, len(devices))
        return devices, cmd_name, None

    return [], None, "no USB enumeration tool found (need usb-devices or lsusb)"


def collect_usb(logger, devices, command="usb-devices", timeout=10):
    """Check that each configured USB device is present on the bus.

    Args:
        logger: Loguru logger.
        devices: list of dicts describing expected devices. Each is matched by
            ``VendorID`` + ``ProductID`` (4-hex-digit strings, case-insensitive);
            an optional ``SerialNumber`` further narrows the match. ``Name`` is a
            friendly label.
        command: enumeration binary to prefer (default ``usb-devices``); falls
            back to ``lsusb`` if unavailable.
        timeout: subprocess timeout in seconds.

    Returns:
        list of dicts, each with ``name``, ``vendor_id``, ``product_id``,
        ``present``, ``count``, matched ``manufacturer`` / ``product`` /
        ``serial`` (from the first match) and an ``ok`` boolean for alerting.
    """
    devices = devices or []
    logger.debug("Checking {} expected USB device(s).", len(devices))

    present, source, enum_error = _enumerate(logger, command, timeout)

    results = []
    for spec in devices:
        name = spec.get("Name") or "unknown"
        vid = (spec.get("VendorID") or "").strip().lower()
        pid = (spec.get("ProductID") or "").strip().lower()
        want_serial = (spec.get("SerialNumber") or "").strip()
        entry = {
            "name": name,
            "vendor_id": vid,
            "product_id": pid,
            "present": False,
            "count": 0,
            "manufacturer": None,
            "product": None,
            "serial": None,
            "ok": False,
        }
        if want_serial:
            entry["expected_serial"] = want_serial

        if not vid or not pid:
            entry["error"] = "VendorID/ProductID not configured"
            logger.warning("USB device '{}' missing VendorID/ProductID; skipping.", name)
            results.append(entry)
            continue

        if enum_error:
            # Could not enumerate at all — report the tool error against each spec.
            entry["error"] = enum_error
            results.append(entry)
            continue

        matches = [
            d for d in present
            if d.get("vendor_id") == vid and d.get("product_id") == pid
            and (not want_serial or (d.get("serial") or "") == want_serial)
        ]
        if matches:
            first = matches[0]
            entry["present"] = True
            entry["count"] = len(matches)
            entry["manufacturer"] = first.get("manufacturer")
            entry["product"] = first.get("product")
            entry["serial"] = first.get("serial")
            entry["ok"] = True
        logger.debug("USB device '{}' ({}:{}): {}", name, vid, pid, entry)
        results.append(entry)

    missing = [e["name"] for e in results if not e.get("ok")]
    if missing:
        logger.warning("USB check: {} device(s) missing/failed: {}", len(missing), missing)
    else:
        logger.info("USB check OK ({} device(s), via {}).", len(results), source or "n/a")
    return results
