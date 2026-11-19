"""Formatting / small helpers shared by the sampler and the launcher.

Pure functions only — no psutil, no Flask, no platform-specific imports —
so they are trivially unit-testable on any OS.
"""
from __future__ import annotations

from datetime import datetime, timezone

_KIB = 1024.0
_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def format_bytes(value) -> str:
    """Human-readable byte size, e.g. 1536 -> '1.50 KB'."""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v < 0:
        return "—"
    if v < _KIB:
        return f"{int(v)} B"
    u = 0
    x = v
    while x >= _KIB and u < len(_UNITS) - 1:
        x /= _KIB
        u += 1
    return f"{x:.2f} {_UNITS[u]}"


def format_rate(bps) -> str:
    """Human-readable bytes-per-second, e.g. 1048576.0 -> '1.00 MB/s'."""
    if bps is None:
        return "—"
    return format_bytes(bps) + "/s"


def format_clock(mhz) -> str:
    """Format a frequency given in MHz with proper MHz/GHz units.

    4230.0 -> '4.23 GHz';  850.0 -> '850 MHz'
    """
    if mhz is None:
        return "—"
    try:
        v = float(mhz)
    except (TypeError, ValueError):
        return "—"
    if v <= 0:
        return "—"
    if v >= 1000.0:
        return f"{v / 1000.0:.2f} GHz"
    return f"{v:.0f} MHz"


def clamp_percent(value) -> float | None:
    """Clamp a percentage to [0, 100]; None passes through as None."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, v))


def clamp_nonneg_rate(value) -> float | None:
    """Counter deltas that went negative (device reset) are clamped to 0.0."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, v)


def format_uptime(seconds) -> str:
    """'1d 03:24:56' style uptime string."""
    if seconds is None or seconds < 0:
        return "—"
    s = int(seconds)
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_epoch_local(epoch) -> str:
    """Local-time string for a UNIX epoch, or '—'."""
    if epoch is None:
        return "—"
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError, TypeError):
        return "—"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
