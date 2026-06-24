"""RAID metadata collector via ``dmraid -n`` (ATARAID / fakeRAID / BIOS RAID).

``dmraid -n`` (``--native_log``) dumps the raw native metadata that the RAID
controller stores on disk. We shell out, capture it verbatim, and fold the
result into the status payload so operators can see the on-disk RAID config of
each host.

The service runs as root, so ``dmraid`` is invoked directly (same convention as
``smartctl``/``ionice`` elsewhere in this project — no interactive ``sudo``). On
hosts without any ATARAID arrays ``dmraid`` typically prints "no raid disks" and
exits non-zero; that is reported as ``raid_detected = false`` rather than an
error.
"""
import shutil
import subprocess


def _snippet(text, limit=2000):
    """Trim long command output for logging / payload safety."""
    if text is None:
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " ...[truncated]"


def collect_raid(logger, dmraid_path="dmraid", timeout=20):
    """Run ``dmraid -n`` and return a structured summary dict.

    Never raises: any failure (missing binary, timeout, OS error) is captured in
    the returned dict so one bad probe cannot abort the collection cycle.
    """
    logger.debug("Collecting RAID metadata via '{} -n' ...", dmraid_path)

    resolved = shutil.which(dmraid_path) or dmraid_path
    if shutil.which(dmraid_path) is None:
        logger.debug("dmraid not found on PATH ('{}'); skipping RAID metadata.", dmraid_path)
        return {
            "available": False,
            "raid_detected": False,
            "command": f"{dmraid_path} -n",
            "error": "dmraid not installed",
        }

    cmd = [resolved, "-n"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("dmraid execution failed: {}", exc)
        return {
            "available": True,
            "raid_detected": False,
            "command": " ".join(cmd),
            "error": str(exc),
        }

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    # dmraid exits non-zero and says "no raid disks" when there is nothing to
    # report; treat that as a clean "no RAID" rather than an error.
    combined = (stdout + "\n" + stderr).lower()
    no_raid = "no raid disks" in combined
    raid_detected = bool(stdout) and not no_raid

    data = {
        "available": True,
        "raid_detected": raid_detected,
        "command": " ".join(cmd),
        "returncode": proc.returncode,
        "output": stdout.splitlines() if stdout else [],
    }
    if stderr and not no_raid:
        data["stderr"] = _snippet(stderr)

    logger.debug("RAID metadata collected (detected={}): {}", raid_detected, _snippet(stdout))
    return data
