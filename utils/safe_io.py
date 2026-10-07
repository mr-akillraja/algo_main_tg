"""Safe I/O helpers for non-blocking CSV and file operations.

Provides `safe_write_csv_row` that never raises to callers and rate-limits
warning logs to avoid spamming when disk is read-only or directory missing.
"""
import csv
import time
from pathlib import Path
from typing import Iterable, Dict, Any, Optional

_LAST_WARN: Dict[str, float] = {}
_WARN_INTERVAL = 60.0


def _should_warn(key: str) -> bool:
    now = time.time()
    last = _LAST_WARN.get(key, 0.0)
    if now - last >= _WARN_INTERVAL:
        _LAST_WARN[key] = now
        return True
    return False


def safe_write_csv_row(path: Path, fieldnames: Iterable[str], row: Dict[str, Any], *, write_header: bool = False) -> bool:
    """Attempt to write a CSV row. Returns True on success, False on failure.

    Exceptions are swallowed; callers can inspect return value. Warning is rate-limited.
    """
    key = str(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists()
        # Open in append+ mode for atomic append
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
            if write_header and not exists:
                writer.writeheader()
            writer.writerow({k: row.get(k, "") for k in fieldnames})
        return True
    except Exception as exc:
        # Rate-limited warning via logging to avoid circular import at module import time.
        if _should_warn(key):
            try:
                import logging

                logging.getLogger("safe_io").warning("[SafeIO] write_failed path=%s reason=%s", key, exc)
            except Exception:
                pass
        return False
