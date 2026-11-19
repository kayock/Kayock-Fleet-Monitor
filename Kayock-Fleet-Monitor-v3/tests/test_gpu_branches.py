"""GPU collector branch regression — this suite exists because of a real
production crash on Windows:
    GPUCollector._compose() missing 1 required positional argument: 'reason'
The Windows 'identity-only' branch omitted `reason`. Every compose branch is
now forced through its paces so a missing-argument bug can never return.
"""
import pytest

import gpu as gpu_mod
from gpu import GPUCollector


@pytest.fixture
def win_collector(monkeypatch):
    monkeypatch.setattr(gpu_mod, "_IS_WINDOWS", True)
    c = GPUCollector()
    yield c
    c.close()


def _identity(adapters):
    return adapters or []


@pytest.fixture
def identity_adapters():
    return [{"name": "Intel(R) UHD Graphics 770",
             "driver_version": "31.0.101.5333", "driver_date": "2024-07-16",
             "adapter_ram_bytes": 1073741824, "adapter_ram_capped": False,
             "adapter_ram_note": "WMI AdapterRAM", "status": "OK",
             "video_processor": "Intel(R) UHD Graphics Family",
             "pnp_id": "PCI\\VEN_8086&DEV_4680"}]


def _engine(unavailable=False):
    if unavailable:
        return {"utilization": gpu_mod.GPUCollector._unavail_util()
                | {"reason": "PDH open failed: 0xC0000BD0"},
                "memory": gpu_mod.GPUCollector._unavail_mem()}
    return {
        "utilization": {"available": True, "percent": 13.5,
                        "source": "Windows GPU Engine performance counters (PDH)",
                        "scope": "adapter-specific", "engines_by_type": {"3D": 13.5},
                        "per_adapter": {"LUID 0x0_0xC35D phys 0": 13.5},
                        "engine_count": 2, "note": None, "reason": None},
        "memory": {"available": True, "dedicated_bytes": 1234567,
                   "shared_bytes": 654321, "total_adapter_bytes": None,
                   "per_adapter": {"dedicated": {}, "shared": {}},
                   "source": "PDH", "note": None},
    }


def test_compose_signature_is_defensive(win_collector):
    """`reason` and every other optional argument may be omitted."""
    section = win_collector._compose(status="ok", collected_at=123.0)
    assert section["status"] == "ok"
    assert section["reason"] is None
    assert set(section) >= {"status", "reason", "platform", "adapters",
                            "utilization", "memory", "temperature", "nvidia",
                            "collected_at"}


def test_the_reported_windows_branch_no_longer_crashes(win_collector,
                                                       identity_adapters,
                                                       monkeypatch):
    """Counters unavailable + adapter identified -> 'ok' section, no TypeError.
    This is the exact path that raised
    'GPUCollector._compose() missing 1 required positional argument: reason'
    on the user's Intel UHD 770 machine."""
    monkeypatch.setattr(win_collector, "_ensure_identity",
                        lambda: identity_adapters)
    monkeypatch.setattr(win_collector, "_engine_and_memory",
                        lambda: _engine(unavailable=True))
    monkeypatch.setattr(win_collector, "_nvidia",
                        lambda: {"available": False, "gpus": [], "reason": None})
    section = win_collector._collect()  # must not raise
    assert section["status"] == "ok"
    assert section["reason"] is None
    assert section["adapters"][0]["name"].startswith("Intel(R) UHD Graphics 770")
    assert section["utilization"]["available"] is False  # honest, not faked


def test_branch_all_unavailable(win_collector, monkeypatch):
    monkeypatch.setattr(win_collector, "_ensure_identity", lambda: [])
    monkeypatch.setattr(win_collector, "_engine_and_memory",
                        lambda: _engine(unavailable=True))
    monkeypatch.setattr(win_collector, "_nvidia",
                        lambda: {"available": False, "gpus": [], "reason": None})
    section = win_collector._collect()
    assert section["status"] == "unavailable"
    assert section["reason"]


def test_branch_counters_available(win_collector, identity_adapters, monkeypatch):
    monkeypatch.setattr(win_collector, "_ensure_identity",
                        lambda: identity_adapters)
    monkeypatch.setattr(win_collector, "_engine_and_memory",
                        lambda: _engine())
    monkeypatch.setattr(win_collector, "_nvidia",
                        lambda: {"available": False, "gpus": [], "reason": None})
    section = win_collector._collect()
    assert section["status"] == "ok"
    assert section["utilization"]["percent"] == pytest.approx(13.5)
    assert section["memory"]["dedicated_bytes"] == 1234567


def test_branch_nvidia_available(win_collector, monkeypatch):
    nvidia = {"available": True, "reason": None, "gpus": [{
        "name": "NVIDIA GeForce RTX 4090", "utilization_percent": 37.0,
        "memory_used_bytes": 1024**3, "memory_total_bytes": 24 * 1024**3,
        "temperature_c": 52.5, "driver_version": "546.33"}]}
    monkeypatch.setattr(win_collector, "_ensure_identity", lambda: [])
    monkeypatch.setattr(win_collector, "_engine_and_memory",
                        lambda: _engine(unavailable=True))
    monkeypatch.setattr(win_collector, "_nvidia", lambda: nvidia)
    section = win_collector._collect()
    assert section["status"] == "ok"
    assert section["utilization"]["percent"] == pytest.approx(37.0)
    assert section["temperature"]["available"] is True


def test_branch_internal_crash_becomes_error_section(win_collector, monkeypatch):
    def boom():
        raise RuntimeError("identity exploded")
    monkeypatch.setattr(win_collector, "_ensure_identity", boom)
    section = win_collector.collect()  # collect() must swallow the crash
    assert section["status"] == "error"
    assert "identity exploded" in section["reason"]


# ---------------------------------------------------------------------------
# PowerShell Get-Counter fallback (used when PDH counters are unavailable —
# the suspected situation on the machine that hit the _compose crash)
# ---------------------------------------------------------------------------
GETCOUNTER_PAYLOAD = {
    "engine": [
        {"instance": "pid_100_luid_0x00000000_0x0000C35D_phys_0_eng_0_engtype_3D",
         "value": 21.0},
        {"instance": "pid_200_luid_0x00000000_0x0000C35D_phys_0_eng_0_engtype_3D",
         "value": 30.0},  # same engine node, second pid -> max, not sum
        {"instance": "pid_100_luid_0x00000000_0x0000C35D_phys_0_eng_1_engtype_Copy",
         "value": 5.0},
    ],
    "dedicated": [{"instance": "luid_0x00000000_0x0000C35D_phys_0",
                   "value": 234567890.0}],
    "shared": [{"instance": "luid_0x00000000_0x0000C35D_phys_0",
                "value": 12345678.0}],
}


def test_getcounter_fallback_parses_and_aggregates(win_collector, monkeypatch):
    monkeypatch.setattr(gpu_mod, "run_powershell_json",
                        lambda script, timeout: GETCOUNTER_PAYLOAD)
    result = win_collector._engine_and_memory_fallback("PDH broken: 0xC0000BD0")
    util = result["utilization"]
    assert util["available"] is True
    assert util["percent"] == pytest.approx(35.0)  # max(21,30) + 5
    assert util["engines_by_type"] == {"3D": 30.0, "Copy": 5.0}
    assert "Get-Counter" in util["source"]
    assert result["memory"]["dedicated_bytes"] == 234567890


def test_getcounter_fallback_throttled_and_cached(win_collector, monkeypatch):
    calls = []

    def fake_ps(script, timeout):
        calls.append(1)
        return GETCOUNTER_PAYLOAD

    monkeypatch.setattr(gpu_mod, "run_powershell_json", fake_ps)
    first = win_collector._engine_and_memory_fallback("pdh down")
    second = win_collector._engine_and_memory_fallback("pdh down")
    assert len(calls) == 1  # second call served from cache
    assert first is second


def test_getcounter_fallback_failure_is_explicit(win_collector, monkeypatch):
    monkeypatch.setattr(gpu_mod, "run_powershell_json",
                        lambda script, timeout: None)
    result = win_collector._engine_and_memory_fallback("pdh down")
    assert result["utilization"]["available"] is False
    assert "PDH: pdh down" in result["utilization"]["reason"]
    assert "Get-Counter" in result["utilization"]["reason"]
