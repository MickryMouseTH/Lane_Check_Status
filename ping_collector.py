"""Ping / reachability collector.

Pings each configured host once per due cycle and records both reachability and
round-trip time (latency in milliseconds). Useful to spot a lane/host that is
still up but has degraded network to an upstream service.

Like the SMART / RAID / service collectors this module never raises: any failure
(binary missing, DNS failure, timeout) is captured in the returned structure so
one bad probe cannot abort a collection cycle. The result is cheap but a cache
cadence is supported via the caller (see ``Ping.Interval_Cycles``).
"""
import platform
import re
import shutil
import subprocess


# "time=12.3 ms" / "time=12 ms" / "time<1 ms" on a reply line.
_TIME_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)
# Summary: "rtt min/avg/max/mdev = 0.1/0.2/0.3/0.4 ms" (Linux) or
#          "round-trip min/avg/max/stddev = ..." (macOS/BSD).
_RTT_SUMMARY_RE = re.compile(
    r"(?:rtt|round-trip)[^=]*=\s*[\d.]+/([\d.]+)/", re.IGNORECASE
)
# "0% packet loss" / "100.0% packet loss".
_LOSS_RE = re.compile(r"([\d.]+)%\s*packet loss", re.IGNORECASE)


def _ping_command(address, count, timeout):
    """Build the platform-appropriate ping command.

    Target platform is Linux (the whole agent is Ubuntu-oriented), but keep the
    macOS/Windows variants so the collector is testable off-target.
    """
    system = platform.system().lower()
    if system == "windows":
        # -n count, -w timeout in milliseconds (per-reply).
        return ["ping", "-n", str(count), "-w", str(int(timeout * 1000)), address]
    if system == "darwin":
        # macOS: -c count, -t total deadline seconds, -W per-reply ms.
        return ["ping", "-c", str(count), "-t", str(int(timeout * count) + 1),
                "-W", str(int(timeout * 1000)), address]
    # Linux: -c count, -w overall deadline (s), -W per-reply timeout (s).
    return ["ping", "-c", str(count), "-w", str(int(timeout * count) + 1),
            "-W", str(int(timeout)), address]


def _ping_once(logger, address, count, timeout):
    """Ping one address and return (reachable, rtt_ms, loss_percent, error)."""
    cmd = _ping_command(address, count, timeout)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            # Give the process a little headroom over ping's own deadline.
            timeout=(timeout * count) + 5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ping failed for '{}': {}", address, exc)
        return False, None, None, str(exc)

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")

    loss = None
    m = _LOSS_RE.search(out)
    if m:
        try:
            loss = float(m.group(1))
        except ValueError:
            loss = None

    rtt = None
    m = _RTT_SUMMARY_RE.search(out)
    if m:
        try:
            rtt = float(m.group(1))
        except ValueError:
            rtt = None
    if rtt is None:
        # No summary (e.g. single packet, or Windows): take the first reply time.
        m = _TIME_RE.search(out)
        if m:
            try:
                rtt = float(m.group(1))
            except ValueError:
                rtt = None

    # returncode 0 = at least one reply received.
    reachable = proc.returncode == 0
    return reachable, rtt, loss, None


def collect_ping(logger, hosts, count=1, timeout=2):
    """Ping each configured host and report reachability + latency.

    Args:
        logger: Loguru logger.
        hosts: list of dicts ``{"Hostname": ..., "Address": ...}``. ``Hostname``
            is a friendly label; ``Address`` is what is actually pinged.
        count: ICMP echoes to send per host (averaged into ``rtt_ms``).
        timeout: per-reply timeout in seconds.

    Returns:
        list of dicts, each with ``hostname``, ``address``, ``reachable``,
        ``rtt_ms`` (round-trip time, ms; None if unreachable),
        ``packet_loss_percent`` and an ``ok`` boolean for quick alerting.
    """
    hosts = hosts or []
    count = max(1, int(count))
    logger.debug("Pinging {} host(s) (count={}, timeout={}s).", len(hosts), count, timeout)

    if hosts and shutil.which("ping") is None:
        logger.warning("ping binary not found; skipping ping checks.")
        return [
            {"hostname": h.get("Hostname") or h.get("Address") or "unknown",
             "address": (h.get("Address") or "").strip(),
             "reachable": False, "rtt_ms": None, "packet_loss_percent": None,
             "error": "ping binary not found", "ok": False}
            for h in hosts
        ]

    results = []
    for host in hosts:
        name = host.get("Hostname") or host.get("Address") or "unknown"
        address = (host.get("Address") or "").strip()
        entry = {
            "hostname": name,
            "address": address,
            "reachable": False,
            "rtt_ms": None,
            "packet_loss_percent": None,
            "ok": False,
        }
        if not address:
            entry["error"] = "no address configured"
            logger.warning("Ping host '{}' has no Address; skipping.", name)
            results.append(entry)
            continue

        reachable, rtt, loss, error = _ping_once(logger, address, count, timeout)
        entry["reachable"] = reachable
        entry["rtt_ms"] = rtt
        entry["packet_loss_percent"] = loss
        if error:
            entry["error"] = error
        # Healthy = we got at least one reply back.
        entry["ok"] = reachable
        logger.debug("ping '{}' ({}): {}", name, address, entry)
        results.append(entry)

    down = [e["hostname"] for e in results if not e.get("ok")]
    if down:
        logger.warning("Ping check: {} host(s) unreachable: {}", len(down), down)
    else:
        logger.info("Ping check OK ({} host(s)).", len(results))
    return results
