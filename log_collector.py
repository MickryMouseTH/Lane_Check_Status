"""Read and filter application log files incrementally (tail by offset).

For each configured program the collector remembers the byte offset it last
read from. On the next cycle it reads only the bytes that were appended, applies
include/exclude regex filters, and returns the matching lines. Offsets are
persisted to a small JSON state file so restarts do not re-send old log content.

Log rotation is detected by comparing the stored file size/inode with the
current file: if the file shrank (truncated) or the inode changed (rotated),
reading restarts from the beginning of the new file.
"""
import json
import os
import re
from datetime import datetime


# Date tokens supported in Log_Path, applied with the current local time each
# cycle. Longer tokens are listed first so they are substituted before any
# shorter token that is a prefix of them (e.g. yyyy before yy, MM/minute is
# uppercase while mm/month is lowercase).
_DATE_TOKENS = [
    ("yyyy", "%Y"),  # 4-digit year   -> 2026
    ("yy", "%y"),    # 2-digit year   -> 26
    ("mm", "%m"),    # 2-digit month  -> 06
    ("dd", "%d"),    # 2-digit day    -> 23
    ("HH", "%H"),    # 2-digit hour   -> 14
    ("MM", "%M"),    # 2-digit minute -> 05
    ("SS", "%S"),    # 2-digit second -> 09
]


def expand_date_tokens(path, now=None):
    """Replace date tokens in `path` with the current date/time.

    Supports patterns such as ``/tct/yyyy-mm/tct_app_ddmmyy.log`` which on
    2026-06-23 expands to ``/tct/2026-06/tct_app_230626.log``. Tokens are
    case-sensitive: ``mm`` is month, ``MM`` is minute. A literal ``strftime``
    directive (``%Y`` etc.) also passes through unchanged.
    """
    if now is None:
        now = datetime.now()
    result = path
    for token, directive in _DATE_TOKENS:
        result = result.replace(token, now.strftime(directive))
    return result


def load_state(logger, state_path):
    """Load the persisted per-file read offsets, tolerating a missing file."""
    if not os.path.exists(state_path):
        logger.debug("Log state file '{}' not found; starting fresh.", state_path)
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        logger.debug("Loaded log state: {}", state)
        return state if isinstance(state, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read log state '{}' ({}); starting fresh.", state_path, exc)
        return {}


def save_state(logger, state_path, state):
    """Persist read offsets atomically so a crash cannot corrupt the file."""
    tmp_path = f"{state_path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, state_path)
        logger.debug("Saved log state: {}", state)
    except OSError as exc:
        logger.error("Failed to persist log state '{}': {}", state_path, exc)


def _compile_patterns(patterns):
    """Compile a list of regex strings, skipping any that fail to compile."""
    compiled = []
    for pat in patterns or []:
        try:
            compiled.append(re.compile(pat))
        except re.error:
            # Fall back to treating an invalid regex as a literal substring.
            compiled.append(re.compile(re.escape(pat)))
    return compiled


def _line_matches(line, includes, excludes):
    """Return True if `line` passes the include/exclude filter rules.

    - If `includes` is empty, every line is considered included.
    - A line is dropped if it matches ANY exclude pattern.
    """
    if any(rx.search(line) for rx in excludes):
        return False
    if not includes:
        return True
    return any(rx.search(line) for rx in includes)


def _read_new_lines(logger, path, prev_offset, prev_inode, prev_size):
    """Read appended bytes from `path`, handling rotation/truncation.

    Returns (lines, new_offset, inode, size). On any read error returns an empty
    result that leaves the stored offset unchanged for a later retry.
    """
    try:
        stat = os.stat(path)
    except OSError as exc:
        logger.warning("Log file '{}' not accessible: {}", path, exc)
        return [], prev_offset, prev_inode, prev_size

    inode = stat.st_ino
    size = stat.st_size

    start = prev_offset
    rotated = (prev_inode is not None and inode != prev_inode)
    truncated = size < prev_offset
    if rotated or truncated:
        logger.info(
            "Log '{}' rotated/truncated (inode {}->{}, size {}->{}); reading from start.",
            path, prev_inode, inode, prev_offset, size,
        )
        start = 0

    if start >= size:
        logger.debug("No new bytes in '{}' (offset={}, size={}).", path, start, size)
        return [], size, inode, size

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(start)
            chunk = fh.read()
            new_offset = fh.tell()
    except OSError as exc:
        logger.error("Failed reading '{}': {}", path, exc)
        return [], prev_offset, inode, size

    lines = chunk.splitlines()
    logger.debug("Read {} new line(s) from '{}'.", len(lines), path)
    return lines, new_offset, inode, size


def collect_program_logs(logger, programs, state):
    """Collect filtered log lines for every configured program.

    Args:
        logger: Loguru logger.
        programs: List of program config dicts. Each entry supports:
            - Name (str): identifier included in the output.
            - Log_Path (str): path to the log file.
            - Include_Patterns (list[str]): regex; line kept if any match
              (empty list = keep all).
            - Exclude_Patterns (list[str]): regex; line dropped if any match.
            - Max_Lines (int): cap on matched lines returned per cycle.
        state: Mutable dict of persisted offsets, keyed by log path. Updated
            in place so the caller can persist it.

    Returns:
        list[dict]: One result per program with the matched lines.
    """
    logger.debug("Collecting logs for {} program(s).", len(programs or []))
    results = []

    for prog in programs or []:
        name = prog.get("Name", "unknown")
        path_pattern = prog.get("Log_Path", "")
        if not path_pattern:
            logger.warning("Program '{}' has no Log_Path; skipping.", name)
            results.append({"name": name, "error": "no Log_Path configured"})
            continue

        # Resolve any date tokens (yyyy/mm/dd ...) to the current date so logs
        # that rotate by day/month are followed automatically.
        path = expand_date_tokens(path_pattern)
        if path != path_pattern:
            logger.debug("Program '{}' log path '{}' resolved to '{}'.", name, path_pattern, path)

        includes = _compile_patterns(prog.get("Include_Patterns"))
        excludes = _compile_patterns(prog.get("Exclude_Patterns"))
        max_lines = int(prog.get("Max_Lines", 200))

        file_state = state.get(path, {})
        lines, new_offset, inode, size = _read_new_lines(
            logger,
            path,
            int(file_state.get("offset", 0)),
            file_state.get("inode"),
            int(file_state.get("size", 0)),
        )

        matched = [ln for ln in lines if _line_matches(ln, includes, excludes)]
        total_matched = len(matched)
        truncated_output = total_matched > max_lines
        if truncated_output:
            logger.info(
                "Program '{}' matched {} lines; capping at Max_Lines={}.",
                name, total_matched, max_lines,
            )
            matched = matched[-max_lines:]  # keep the most recent matches

        # Persist the new read position regardless of how many lines matched,
        # so filtered-out content is not re-scanned next cycle.
        state[path] = {"offset": new_offset, "inode": inode, "size": size}

        entry = {
            "name": name,
            "log_path_pattern": path_pattern,
            "log_path": path,
            "new_lines_total": len(lines),
            "matched_count": total_matched,
            "output_truncated": truncated_output,
            "lines": matched,
        }
        logger.debug("Program log result for '{}': {}", name, entry)
        results.append(entry)

    return results
