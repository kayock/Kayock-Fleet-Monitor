"""Formatting helpers."""
import pytest

from utils import (clamp_nonneg_rate, clamp_percent, format_bytes,
                   format_clock, format_epoch_local, format_rate, format_uptime)


def test_format_bytes():
    assert format_bytes(None) == "—"
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1024) == "1.00 KB"
    assert format_bytes(1536) == "1.50 KB"
    assert format_bytes(1024 * 1024) == "1.00 MB"
    assert format_bytes(3.5 * 1024**3) == "3.50 GB"
    assert format_bytes(2.25 * 1024**4) == "2.25 TB"
    assert format_bytes(-5) == "—"


def test_format_rate():
    assert format_rate(None) == "—"
    assert format_rate(0) == "0 B/s"
    assert format_rate(1048576) == "1.00 MB/s"


def test_format_clock():
    assert format_clock(None) == "—"
    assert format_clock(0) == "—"
    assert format_clock(850) == "850 MHz"
    assert format_clock(999) == "999 MHz"
    assert format_clock(1000) == "1.00 GHz"
    assert format_clock(4230) == "4.23 GHz"
    assert format_clock(3700.0) == "3.70 GHz"


def test_clamp_percent():
    assert clamp_percent(None) is None
    assert clamp_percent(-5) == 0.0
    assert clamp_percent(150) == 100.0
    assert clamp_percent(42.5) == pytest.approx(42.5)


def test_clamp_nonneg_rate():
    assert clamp_nonneg_rate(None) is None
    assert clamp_nonneg_rate(-0.5) == 0.0
    assert clamp_nonneg_rate(7.5) == pytest.approx(7.5)


def test_format_uptime():
    assert format_uptime(None) == "—"
    assert format_uptime(0) == "00:00:00"
    assert format_uptime(3661) == "01:01:01"
    assert format_uptime(90061) == "1d 01:01:01"
    assert format_uptime(2 * 86400 + 59) == "2d 00:00:59"


def test_format_epoch_local():
    assert format_epoch_local(None) == "—"
    assert format_epoch_local(0)  # some local rendering of epoch, never crashes
