"""GPU collector: parsers, aggregation, AdapterRAM cap, and every failure
path (no nvidia-smi, counters unavailable, PowerShell missing, collector
exceptions). Runs on Linux — which exercises exactly the graceful-degradation
paths a Windows machine hits when counters/nvidia-smi are missing."""
import pytest

import gpu as gpu_mod
from gpu import (GPUCollector, aggregate_adapter_memory,
                 aggregate_engine_utilization, gpu_error, gpu_placeholder,
                 parse_adapter_memory, parse_engine_instance,
                 parse_identity_payload, parse_nvidia_smi_output)


# ---------------------------------------------------------------------------
# engine instance parsing / aggregation
# ---------------------------------------------------------------------------
def test_parse_engine_instance_typical():
    name = ("pid_4320_luid_0x00000000_0x0000C35D_phys_0"
            "_eng_0_engtype_3D")
    p = parse_engine_instance(name, 12.3)
    assert p["pid"] == 4320
    assert p["luid"] == "0x00000000_0x0000C35D"
    assert p["phys"] == 0
    assert p["eng"] == 0
    assert p["engtype"] == "3D"
    assert p["value"] == pytest.approx(12.3)


def test_parse_engine_instance_rejects_junk():
    assert parse_engine_instance("not-an-instance", 1.0) is None
    assert parse_engine_instance(None, 1.0) is None
    assert parse_engine_instance("pid_1_luid_0x0_0x0_phys_0_eng_0_engtype_3D",
                                 None) is None
    assert parse_engine_instance("pid_1_luid_0x0_0x0_phys_0_eng_0_engtype_3D",
                                 -5) is None  # negative = transient junk


def test_aggregation_dedups_pid_instances_per_engine():
    # same engine node reported for two processes -> MAX, not SUM
    parsed = [
        {"pid": 100, "luid": "0x0_0xA", "phys": 0, "eng": 0, "engtype": "3D", "value": 30.0},
        {"pid": 200, "luid": "0x0_0xA", "phys": 0, "eng": 0, "engtype": "3D", "value": 45.0},
        # a different engine of the same adapter runs in parallel
        {"pid": 100, "luid": "0x0_0xA", "phys": 0, "eng": 1, "engtype": "Copy", "value": 10.0},
    ]
    agg = aggregate_engine_utilization(parsed)
    assert agg["engines_by_type"] == {"3D": 45.0, "Copy": 10.0}
    assert agg["total_percent"] == pytest.approx(55.0)
    assert agg["adapters"] == {"LUID 0x0_0xA phys 0": 55.0}
    assert agg["engine_count"] == 2


def test_aggregation_empty():
    agg = aggregate_engine_utilization([])
    assert agg["total_percent"] == 0.0
    assert agg["adapters"] == {}


def test_adapter_memory_parse_and_aggregate():
    m = parse_adapter_memory("luid_0x00000000_0x0000C35D_phys_0", 268435456.0)
    assert m["bytes"] == 268435456
    agg = aggregate_adapter_memory([m, m])
    assert agg["total_bytes"] == 2 * 268435456
    assert aggregate_adapter_memory([])["total_bytes"] == 0


# ---------------------------------------------------------------------------
# nvidia-smi output parsing
# ---------------------------------------------------------------------------
NVIDIA_CSV = """NVIDIA GeForce RTX 4090, 37, 1024, 24564, 52.5, 546.33
Bad line, 12
Intel UHD, abc, 1, 2, 3, 4
"""

def test_parse_nvidia_smi_output():
    gpus = parse_nvidia_smi_output(NVIDIA_CSV)
    assert len(gpus) == 1  # malformed lines skipped
    g = gpus[0]
    assert g["name"] == "NVIDIA GeForce RTX 4090"
    assert g["utilization_percent"] == 37.0
    assert g["memory_used_bytes"] == 1024 * 1024 * 1024
    assert g["temperature_c"] == 52.5
    assert parse_nvidia_smi_output("") == []
    assert parse_nvidia_smi_output(None) == []


# ---------------------------------------------------------------------------
# WMI identity payload
# ---------------------------------------------------------------------------
IDENTITY_OBJ = {
    "gpus": {"name": "Intel(R) UHD Graphics 770", "driver_version": "31.0.101.5333",
             "driver_date": "2024-07-16", "adapter_ram": 1073741824,
             "status": "OK", "video_processor": "Intel(R) UHD Graphics Family",
             "pnp_id": "PCI\\VEN_8086&DEV_4680"},
    "cpus": {"name": "12th Gen Intel(R) Core(TM) i5-12500",
             "current_clock_mhz": 3700, "max_clock_mhz": 4600,
             "physical_cores": 6, "logical_cores": 12},
}

def test_parse_identity_payload_single_gpu_becomes_list():
    adapters, cpu = parse_identity_payload(IDENTITY_OBJ)
    assert len(adapters) == 1
    assert adapters[0]["name"] == "Intel(R) UHD Graphics 770"
    assert cpu["name"].endswith("i5-12500")
    assert cpu["max_clock_mhz"] == 4600


def test_adapter_ram_cap_flagged_not_hidden(monkeypatch):
    """A saturated AdapterRAM must surface as '≥ 4 GB (…cap…)', not as a lie."""
    monkeypatch.setattr(gpu_mod, "_IS_WINDOWS", True)
    capped_obj = {"gpus": [{"name": "Big GPU", "driver_version": "1.0",
                            "driver_date": None, "adapter_ram": 4294967295,
                            "status": "OK", "video_processor": None,
                            "pnp_id": None}],
                  "cpus": []}
    monkeypatch.setattr(gpu_mod, "run_powershell_json",
                        lambda *_a, **_k: capped_obj)
    c = GPUCollector()
    try:
        adapters = c._ensure_identity()
    finally:
        c.close()
    assert adapters[0]["adapter_ram_capped"] is True
    assert "cap" in adapters[0]["adapter_ram_note"].lower()


def test_parse_identity_bad_payload():
    assert parse_identity_payload(None) == ([], None)
    assert parse_identity_payload("junk") == ([], None)


# ---------------------------------------------------------------------------
# GPUCollector degradation (on Linux: PDH unavailable, powershell missing)
# ---------------------------------------------------------------------------
def test_collector_on_non_windows_degrades_cleanly():
    c = GPUCollector()
    try:
        result = c.collect()
    finally:
        c.close()
    assert set(result) >= {"status", "reason", "adapters", "utilization",
                           "memory", "temperature", "nvidia", "collected_at"}
    assert result["status"] in ("unavailable", "ok")
    if result["status"] == "unavailable":
        assert result["reason"]
        assert result["utilization"]["available"] is False
        assert result["utilization"]["percent"] is None  # never fabricated
        assert result["memory"]["dedicated_bytes"] is None
        assert result["temperature"]["available"] is False


def test_collector_never_raises_on_internal_error(monkeypatch):
    c = GPUCollector()
    monkeypatch.setattr(c, "_collect",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("kaboom")))
    result = c.collect()
    assert result["status"] == "error"
    assert "kaboom" in result["reason"]
    c.close()


def test_missing_nvidia_smi_is_reported(monkeypatch):
    monkeypatch.setattr(gpu_mod.shutil, "which", lambda _p: None)
    c = GPUCollector()
    try:
        result = c.collect()
    finally:
        c.close()
    assert result["nvidia"]["available"] is False
    assert "nvidia-smi not found" in result["nvidia"]["reason"]


def test_nvidia_smi_timeout_is_caught(monkeypatch):
    monkeypatch.setattr(gpu_mod.shutil, "which", lambda _p: "C:/fake/nvidia-smi.exe")

    class Boom(Exception):
        pass

    def raise_timeout(*_a, **_k):
        raise gpu_mod.subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=3.0)

    monkeypatch.setattr(gpu_mod.subprocess, "run", raise_timeout)
    c = GPUCollector()
    try:
        result = c.collect()
    finally:
        c.close()
    assert result["nvidia"]["available"] is False
    assert result["nvidia"]["reason"]


def test_powershell_missing_returns_none(monkeypatch):
    monkeypatch.setattr(gpu_mod.shutil, "which", lambda _p: None)
    assert gpu_mod.run_powershell_json("Get-Date") is None


def test_powershell_bad_json_returns_none(monkeypatch):
    monkeypatch.setattr(gpu_mod.shutil, "which",
                        lambda _p: "powershell-exe-stub")

    class FakeProc:
        returncode = 0
        stdout = "not json {"

    monkeypatch.setattr(gpu_mod.subprocess, "run",
                        lambda *_a, **_k: FakeProc())
    assert gpu_mod.run_powershell_json("x") is None


def test_placeholder_and_error_have_full_shape():
    ph = gpu_placeholder()
    er = gpu_error(RuntimeError("x"))
    for section in (ph, er):
        assert set(section) >= {"status", "reason", "adapters", "utilization",
                                "memory", "temperature", "nvidia",
                                "collected_at"}
        assert set(section["utilization"]) >= {
            "available", "percent", "source", "scope", "engines_by_type",
            "per_adapter", "note", "reason"}
        assert set(section["memory"]) >= {
            "available", "dedicated_bytes", "shared_bytes",
            "total_adapter_bytes", "per_adapter", "source", "note"}
        assert set(section["temperature"]) >= {
            "available", "celsius", "source", "reason"}
