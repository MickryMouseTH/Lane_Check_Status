"""HTTP probe collector — fetch a URL (like ``curl``) and extract fields.

For appliances that expose a status page over HTTP (e.g. a Q-Free RSE service
interface at ``http://10.0.0.15:1337``) this collector does a plain GET and pulls
out whatever fields the operator configures via regex — serial number, MAC,
firmware, uptime, etc. It is deliberately generic: each endpoint carries its own
``Extract`` map of ``field_name -> regex`` so the same collector works for any
device without code changes.

Uses only the standard library (``urllib``) so it adds no dependency and bundles
cleanly with PyInstaller. Like the other collectors it never raises: connection
errors, timeouts and non-2xx responses are captured in the returned structure so
one unreachable device cannot abort a collection cycle. HTTP identity data is
static-ish, so a cache cadence is supported via the caller
(see ``Http_Probe.Interval_Cycles``).
"""
import re
import time
import urllib.error
import urllib.request


# Cap how much of the response we read so a misbehaving endpoint can't make the
# agent read an unbounded body. 1 MiB is plenty for a status page.
_MAX_BYTES = 1024 * 1024


def _normalize_url(raw):
    """Accept 'host:port' or 'host' and default to http:// like curl does."""
    url = (raw or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    return url


def _extract_fields(logger, body, extract_map):
    """Run each configured regex against the body. Returns (fields, missing)."""
    fields = {}
    missing = []
    for field_name, pattern in (extract_map or {}).items():
        try:
            m = re.search(pattern, body, re.IGNORECASE | re.MULTILINE)
        except re.error as exc:
            logger.warning("HTTP probe: bad regex for field '{}': {}", field_name, exc)
            missing.append(field_name)
            continue
        if not m:
            missing.append(field_name)
            continue
        # Prefer the first capture group; fall back to the whole match.
        value = m.group(1) if m.groups() else m.group(0)
        fields[field_name] = value.strip()
    return fields, missing


def _probe_one(logger, endpoint, timeout):
    """Fetch one endpoint and extract its fields. Never raises."""
    name = endpoint.get("Name") or endpoint.get("URL") or "unknown"
    url = _normalize_url(endpoint.get("URL") or endpoint.get("Address"))
    entry = {
        "name": name,
        "url": url,
        "ok": False,
        "status_code": None,
        "response_ms": None,
        "fields": {},
    }
    if not url:
        entry["error"] = "no URL configured"
        logger.warning("HTTP probe '{}' has no URL; skipping.", name)
        return entry

    start = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Lane_Check_Status"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            entry["status_code"] = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read(_MAX_BYTES)
    except urllib.error.HTTPError as exc:
        # Server answered with a non-2xx status — still record it (and try to
        # extract from the error body, some devices return data with 4xx/5xx).
        entry["status_code"] = exc.code
        entry["response_ms"] = round((time.monotonic() - start) * 1000, 1)
        try:
            raw = exc.read(_MAX_BYTES)
        except Exception:
            raw = b""
        body = raw.decode("utf-8", errors="replace") if raw else ""
        if body:
            entry["fields"], missing = _extract_fields(logger, body, endpoint.get("Extract", {}))
            if missing:
                entry["fields_missing"] = missing
        entry["error"] = f"HTTP {exc.code}"
        entry["ok"] = False
        logger.warning("HTTP probe '{}' -> HTTP {}.", name, exc.code)
        return entry
    except (urllib.error.URLError, OSError, ValueError) as exc:
        entry["response_ms"] = round((time.monotonic() - start) * 1000, 1)
        entry["error"] = str(getattr(exc, "reason", exc))
        logger.warning("HTTP probe '{}' ({}) failed: {}", name, url, entry["error"])
        return entry

    entry["response_ms"] = round((time.monotonic() - start) * 1000, 1)
    body = raw.decode("utf-8", errors="replace") if raw else ""
    entry["fields"], missing = _extract_fields(logger, body, endpoint.get("Extract", {}))
    if missing:
        entry["fields_missing"] = missing
    # Healthy = 2xx response. Missing fields are reported but don't flip ok, so a
    # page layout change is visible without looking like the device is down.
    code = entry["status_code"] or 0
    entry["ok"] = 200 <= code < 300
    logger.debug("HTTP probe '{}': {}", name, entry)
    return entry


def collect_http(logger, endpoints, timeout=10):
    """Probe each configured HTTP endpoint and extract fields.

    Args:
        logger: Loguru logger.
        endpoints: list of dicts, each ``{"Name", "URL", "Extract": {field: regex}}``.
            ``URL`` may omit the scheme (``10.0.0.15:1337`` -> ``http://...``).
            ``Extract`` maps a result field name to a regex whose first capture
            group is the value (case-insensitive, multiline).
        timeout: per-request timeout in seconds.

    Returns:
        list of dicts, each with ``name``, ``url``, ``ok``, ``status_code``,
        ``response_ms``, extracted ``fields`` (dict) and, when some regex did not
        match, ``fields_missing``.
    """
    endpoints = endpoints or []
    logger.debug("Probing {} HTTP endpoint(s).", len(endpoints))

    results = [_probe_one(logger, ep, timeout) for ep in endpoints]

    down = [e["name"] for e in results if not e.get("ok")]
    if down:
        logger.warning("HTTP probe: {} endpoint(s) not OK: {}", len(down), down)
    else:
        logger.info("HTTP probe OK ({} endpoint(s)).", len(results))
    return results
