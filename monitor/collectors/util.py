"""Small helpers for reading sysfs/procfs files and formatting numbers."""

import re


def read(path):
    """File contents stripped, or None if it can't be read."""
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, ValueError):
        return None


def read_num(path):
    raw = read(path)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def slug(text):
    """Make a string safe to use as a metric key."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "x"


def gb(value_bytes):
    return value_bytes / 1e9


def fmt_bytes(n):
    for unit in ("B", "kB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000 or unit == "PB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000.0
