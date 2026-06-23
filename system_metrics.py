"""System metric collectors: CPU, RAM, and per-path disk usage.

Uses psutil so the same code works across Linux distributions and is easy to
package with PyInstaller. Every collector logs at DEBUG and dumps the values it
produces so operators can trace exactly what was read.
"""
import psutil


def _round(value, ndigits=2):
    """Round numeric values defensively (psutil may hand back ints/None)."""
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return value


def _to_kb(num_bytes):
    """Convert a byte count to kilobytes (KiB = bytes / 1024), as an integer."""
    try:
        return int(num_bytes) // 1024
    except (TypeError, ValueError):
        return num_bytes


def collect_cpu(logger):
    """Return CPU utilisation details.

    `psutil.cpu_percent(interval=1)` blocks for one second to produce a real
    sample (a non-blocking call returns 0.0 on the first invocation).
    """
    logger.debug("Collecting CPU metrics ...")

    per_core = psutil.cpu_percent(interval=1, percpu=True)
    overall = _round(sum(per_core) / len(per_core)) if per_core else 0.0

    load1, load5, load15 = (None, None, None)
    try:
        load1, load5, load15 = psutil.getloadavg()
    except (OSError, AttributeError):
        # getloadavg is unavailable on some platforms; not fatal.
        logger.debug("Load average not available on this platform.")

    data = {
        "percent": overall,
        "per_core_percent": [_round(c) for c in per_core],
        "core_count": len(per_core),
        "load_avg_1m": _round(load1) if load1 is not None else None,
        "load_avg_5m": _round(load5) if load5 is not None else None,
        "load_avg_15m": _round(load15) if load15 is not None else None,
    }
    logger.debug("CPU metrics collected: {}", data)
    return data


def collect_memory(logger):
    """Return virtual and swap memory usage in bytes plus percentages."""
    logger.debug("Collecting memory metrics ...")

    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()

    data = {
        "ram": {
            "total_kb": _to_kb(vm.total),
            "available_kb": _to_kb(vm.available),
            "used_kb": _to_kb(vm.used),
            "percent": _round(vm.percent),
        },
        "swap": {
            "total_kb": _to_kb(sm.total),
            "used_kb": _to_kb(sm.used),
            "percent": _round(sm.percent),
        },
    }
    logger.debug("Memory metrics collected: {}", data)
    return data


def collect_disk_usage(logger, paths):
    """Return usage for each configured mount path.

    Paths that cannot be read (unmounted, permission denied) are recorded with
    an `error` field instead of aborting the whole collection cycle.
    """
    logger.debug("Collecting disk usage for paths: {}", paths)

    results = []
    for path in paths:
        try:
            usage = psutil.disk_usage(path)
            entry = {
                "path": path,
                "total_kb": _to_kb(usage.total),
                "used_kb": _to_kb(usage.used),
                "free_kb": _to_kb(usage.free),
                "percent": _round(usage.percent),
            }
            logger.debug("Disk usage for '{}': {}", path, entry)
        except (OSError, FileNotFoundError) as exc:
            entry = {"path": path, "error": str(exc)}
            logger.warning("Could not read disk usage for '{}': {}", path, exc)
        results.append(entry)

    return results
