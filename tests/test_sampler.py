"""Sampler behaviour: bounded history, cached-snapshot-only HTTP path,
transient psutil failures, first-sample semantics, and the single-sampler
guarantee (only one place samples, /metrics only reads)."""
import sys

import pytest

from conftest import FakeGPU
from sampler import SystemSampler


def test_history_bounded_and_equal_length(sampler, fake_clock):
    for _ in range(120):  # far more than history_len=60
        fake_clock.advance(2.0)
        sampler.sample_once()
    h = sampler.snapshot()["histories"]
    expected = {"t": 60, "cpu": 60, "mem": 60, "net_recv_bps": 60,
                "net_sent_bps": 60, "disk_read_bps": 60, "disk_write_bps": 60,
                "gpu_util": 60}
    for key, length in expected.items():
        assert len(h[key]) == length, key  # deque(maxlen) keeps it bounded
    assert h["t"] == sorted(h["t"])  # ascending time


def test_first_sample_rates_are_none_without_priming(sampler, fake_clock):
    fake_clock.advance(2.0)
    snap = sampler.sample_once()
    # baselines were never primed -> honest 'measuring', never fake 0
    assert snap["network"]["totals"]["recv_bps"] is None
    assert snap["storage"]["io"]["read_bps"] is None


def test_second_sample_yields_real_rates(sampler, fake_psutil, fake_clock):
    sampler._prime()
    fake_clock.advance(2.0)
    snap = sampler.sample_once()
    io = snap["storage"]["io"]
    net = snap["network"]["totals"]
    # First tick after priming is the CALIBRATION pass: baselines were just
    # set, so the startup delta window would produce a bogus spike. It must
    # publish 'measuring' instead of a rate (v2 spike fix).
    assert io["read_bps"] is None
    assert io["available"] is False
    assert "calibrating" in (io["reason"] or "")
    assert net["recv_bps"] is None
    # from the second tick on, rates are real and accurate
    fake_psutil.advance_disk(read_bytes=2000, write_bytes=1000,
                             read_count=2, write_count=1)
    fake_psutil.advance_net(sent=600, recv=1400)
    fake_clock.advance(2.0)
    snap2 = sampler.sample_once()
    io2 = snap2["storage"]["io"]
    assert io2["read_bps"] == 1000.0
    assert io2["write_bps"] == 500.0
    assert io2["read_iops"] == 1.0
    assert io2["write_iops"] == 0.5
    # loopback excluded from totals; Ethernet is the only real adapter
    assert snap2["network"]["totals"]["sent_bps"] == 300.0
    assert snap2["network"]["totals"]["recv_bps"] == 700.0


def test_calibration_suppresses_startup_spike(sampler, fake_psutil, fake_clock):
    """A huge counter delta in the prime->first-tick window (the reported
    'initial chart spike') must never reach the UI."""
    sampler._prime()
    # simulate a startup burst: a lot of traffic between prime and tick 1
    fake_psutil.advance_disk(read_bytes=900_000_000, write_bytes=800_000_000)
    fake_psutil.advance_net(sent=50_000_000, recv=80_000_000)
    fake_clock.advance(2.0)
    snap = sampler.sample_once()
    assert snap["storage"]["io"]["read_bps"] is None
    assert snap["network"]["totals"]["recv_bps"] is None
    hist = snap["histories"]
    assert hist["disk_read_bps"][-1] is None   # chart point is a gap, no spike
    assert hist["net_recv_bps"][-1] is None
    # the burst is not lost forever: the NEXT tick measures normally from the
    # (already updated) baselines
    fake_clock.advance(2.0)
    snap2 = sampler.sample_once()
    assert snap2["storage"]["io"]["read_bps"] == 0.0  # no further traffic


def test_snapshot_is_cached_and_never_resamples(seeded_sampler, fake_psutil,
                                                fake_clock):
    s = seeded_sampler
    before_calls = dict(fake_psutil.calls)
    before_baseline = tuple(s._prev_net["Ethernet"])

    first = s.snapshot()
    # underlying counters change underneath, but nobody is sampling
    fake_psutil.advance_net(sent=9_999_999, recv=9_999_999)
    fake_psutil.advance_disk(read_bytes=9_999_999, write_bytes=9_999_999)
    second = s.snapshot()

    assert first == second  # bit-for-bit identical cached payload
    assert fake_psutil.calls == before_calls  # GET-path read zero counters
    assert s._prev_net["Ethernet"] == before_baseline  # baselines untouched


def test_snapshot_before_first_tick_is_wellformed(sampler):
    snap = sampler.snapshot()
    for key in ("ts", "meta", "cpu", "memory", "storage", "network",
                "processes", "gpu", "histories", "refresh_seconds",
                "history_len", "sampler"):
        assert key in snap
    assert all(len(v) == 0 for v in snap["histories"].values())


def test_snapshot_never_mutates_published_lists(sampler, fake_clock):
    sampler._prime()
    fake_clock.advance(2.0)
    snap1 = sampler.sample_once()
    hist1 = [list(v) for v in snap1["histories"].values()]
    fake_clock.advance(2.0)
    sampler.sample_once()
    snap2 = sampler.snapshot()
    # snap1's lists were never mutated by the second tick
    for (key, values), new_values in zip(snap1["histories"].items(),
                                         [list(v) for v in snap2["histories"].values()]):
        assert key in snap2["histories"]
    assert [list(v) for v in snap1["histories"].values()] == hist1


def test_constants_exposed_to_browser(sampler):
    snap = sampler.snapshot()
    assert snap["refresh_seconds"] == 2.0
    assert snap["history_len"] == 60
    assert isinstance(snap["refresh_seconds"], float)
    assert isinstance(snap["history_len"], int)


# ---------------------------------------------------------------------------
# transient psutil failures must degrade, never crash
# ---------------------------------------------------------------------------
def test_process_iter_failure_is_reported_not_raised(sampler, fake_psutil,
                                                     fake_clock):
    fake_psutil.fail_process_iter = PermissionError("Access denied")
    sampler._prime()
    fake_clock.advance(2.0)
    snap = sampler.sample_once()  # must not raise
    assert snap["processes"]["error"] is not None
    assert snap["processes"]["list"] == []


def test_process_with_missing_fields_is_skipped_safely(sampler, fake_psutil,
                                                       fake_clock):
    class Weird:
        info = {"pid": 5}  # everything else missing (ad_value=None case)
    fake_psutil.processes = [Weird()]
    sampler._prime()
    fake_clock.advance(2.0)
    snap = sampler.sample_once()
    assert snap["processes"]["total"] == 1
    assert snap["processes"]["list"][0]["name"] == "<unknown>"
    assert snap["processes"]["list"][0]["rss"] is None


def test_net_counters_failure(sampler, fake_psutil, fake_clock):
    fake_psutil.fail_net_counters = OSError("counters unavailable")
    sampler._prime()
    fake_clock.advance(2.0)
    snap = sampler.sample_once()
    assert snap["network"]["reason"] is not None
    assert snap["network"]["available"] is False


def test_disk_counters_failure(sampler, fake_psutil, fake_clock):
    fake_psutil.fail_disk_counters = OSError("no io counters")
    sampler._prime()
    fake_clock.advance(2.0)
    snap = sampler.sample_once()
    assert snap["storage"]["io"]["available"] is False
    assert snap["storage"]["io"]["reason"] is not None


def test_disk_usage_failure_per_volume(sampler, fake_psutil, fake_clock):
    fake_psutil.raise_on_usage["D:\\"] = OSError("device not ready (empty DVD)")
    sampler._prime()
    fake_clock.advance(2.0)
    snap = sampler.sample_once()
    vols = {v["mountpoint"]: v for v in snap["storage"]["volumes"]}
    assert vols["C:\\"]["error"] is None
    assert vols["C:\\"]["total"] == 100 * 2**30
    assert vols["D:\\"]["error"] is not None
    assert vols["D:\\"]["total"] is None  # explicit unavailable, not 0


def test_cpu_percent_failure_degrades(sampler, fake_psutil, fake_clock):
    fake_psutil.fail_cpu_percent = RuntimeError("pdh hiccup")
    sampler._prime()
    fake_clock.advance(2.0)
    snap = sampler.sample_once()
    assert snap["cpu"]["percent"] is None
    assert snap["cpu"]["per_core"] == []


def test_cpu_freq_failure_falls_back(sampler, fake_psutil, fake_clock):
    fake_psutil.fail_cpu_freq = True
    sampler._prime()
    fake_clock.advance(2.0)
    snap = sampler.sample_once()
    # FakeGPU exposes an identity -> platform identity fallback
    # (WMI CurrentClockSpeed on Windows, /proc/cpuinfo MHz on Linux)
    assert snap["cpu"]["clock_mhz"] == 3700.0
    expected = ("wmi_current_clock_speed" if sys.platform == "win32"
                else "proc_cpuinfo_mhz")
    assert snap["cpu"]["clock_source"] == expected


def test_cpu_clock_unavailable_is_labelled(sampler, fake_psutil, fake_clock,
                                           monkeypatch):
    fake_psutil.fail_cpu_freq = True
    monkeypatch.setattr(sampler._gpu, "get_cpu_identity", lambda: None)
    sampler._prime()
    fake_clock.advance(2.0)
    snap = sampler.sample_once()
    assert snap["cpu"]["clock_mhz"] is None
    assert snap["cpu"]["clock_note"]


# ---------------------------------------------------------------------------
# GPU section: non-blocking, failure-isolated
# ---------------------------------------------------------------------------
def test_gpu_error_is_caught_and_labelled(fake_psutil, fake_clock):
    import time
    s = SystemSampler(gpu_collector=FakeGPU(exc=RuntimeError("Boom")),
                      psutil_module=fake_psutil, clock=fake_clock,
                      sleeper=lambda _s: None)
    try:
        s._prime()
        fake_clock.advance(2.0)
        s.sample_once()
        time.sleep(0.05)  # let the failing future settle
        fake_clock.advance(2.0)
        snap = s.sample_once()
        assert snap["gpu"]["status"] == "error"
        assert "Boom" in snap["gpu"]["reason"]
        assert snap["histories"]["gpu_util"][-1] is None  # never a fabricated %
    finally:
        s.stop()


def test_gpu_placeholder_before_first_result(sampler):
    snap = sampler.snapshot()
    assert snap["gpu"]["status"] == "initializing"


def test_gpu_data_flows_into_history(seeded_sampler):
    s = seeded_sampler
    assert s.snapshot()["histories"]["gpu_util"][-1] == 42.5


def test_gpu_futures_never_leak(sampler, fake_clock):
    import time
    gpu = sampler._gpu
    sampler._prime()
    for _ in range(5):
        fake_clock.advance(2.0)
        sampler.sample_once()
        time.sleep(0.02)
    # results were consumed and at most one submit per tick happened
    assert sampler._gpu_latest["status"] == "ok"
    assert 4 <= gpu.collect_calls <= 5  # exactly one submit per tick, none lost


def test_memory_and_meta_values(sampler, fake_clock):
    sampler._prime()
    fake_clock.advance(2.0)
    snap = sampler.sample_once()
    m = snap["memory"]
    assert m["total"] == 16 * 2**30
    assert m["percent"] == 50.0
    assert m["swap_percent"] == 25.0
    meta = snap["meta"]
    assert meta["hostname"]
    assert meta["os_system"]  # platform.uname() based, never os.uname()
    assert meta["python_version"]
    assert meta["architecture"]
    assert meta["boot_time"] == 1_737_200_000.0
    assert meta["uptime_seconds"] > 0
