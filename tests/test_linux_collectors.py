"""v3 Linux collector tests (pure parsing + monkeypatched sysfs) and
nvidia-smi power-field parsing. No GPU hardware is required or assumed."""
import json
import sys

import pytest

import gpu as gpu_mod
from gpu import GPUCollector, parse_nvidia_smi_output


# ---------------------------------------------------------------------------
# nvidia-smi parsing incl. optional power fields
# ---------------------------------------------------------------------------
def test_parse_nvidia_6_fields_still_works():
    text = "NVIDIA Quadro P2000, 42, 1900, 4096, 61, 425.31\n"
    gpus = parse_nvidia_smi_output(text)
    assert len(gpus) == 1
    g = gpus[0]
    assert g["name"] == "NVIDIA Quadro P2000"
    assert g["utilization_percent"] == 42.0
    assert g["memory_used_bytes"] == 1900 * gpu_mod._MIB
    assert g["memory_total_bytes"] == 4096 * gpu_mod._MIB
    assert g["temperature_c"] == 61.0
    assert g["power_draw_watts"] is None      # absent -> None, never 0
    assert g["power_limit_watts"] is None


def test_parse_nvidia_8_fields_with_power():
    text = ("NVIDIA Quadro P2000, 42, 1900, 4096, 61, 425.31, 34.50, 60.00\n"
            "NVIDIA Quadro P2000, 7, 900, 4096, 55, 425.31, N/A, 60.00\n")
    gpus = parse_nvidia_smi_output(text)
    assert gpus[0]["power_draw_watts"] == 34.5
    assert gpus[0]["power_limit_watts"] == 60.0
    assert gpus[1]["power_draw_watts"] is None   # N/A -> honest None
    assert gpus[1]["power_limit_watts"] == 60.0


def test_parse_nvidia_units_in_power():
    text = "GPU, 0, 0, 4096, 40, 1, 27.35 W, 60 W\n"
    gpus = parse_nvidia_smi_output(text)
    assert gpus[0]["power_draw_watts"] == 27.35
    assert gpus[0]["power_limit_watts"] == 60.0


def test_power_from_nvidia():
    c = GPUCollector()
    nvidia = {"available": True, "gpus": [{"name": "P2000", "power_draw_watts": 34.5,
                                           "power_limit_watts": 60.0}]}
    p = c._power_from_nvidia(nvidia)
    assert p["available"] is True and p["draw_watts"] == 34.5
    p2 = c._power_from_nvidia({"available": True, "gpus": [{"power_draw_watts": None,
                                                            "power_limit_watts": None}]})
    assert p2["available"] is False
    p3 = c._power_from_nvidia({"available": False, "gpus": []})
    assert p3["available"] is False


# ---------------------------------------------------------------------------
# Intel Linux path: sysfs discovery + explicit unavailable states
# ---------------------------------------------------------------------------
def _fake_intel_sysfs(monkeypatch, freq=True, engines_json=None):
    monkeypatch.setattr(gpu_mod.os, "listdir",
                        lambda p: ["card0", "card0-DP-1"]
                        if p == "/sys/class/drm" else [])

    def read(path):
        if path.endswith("/vendor"):
            return "0x8086"
        if path.endswith("/device"):
            return "0x591b"   # HD Graphics 630
        if path.endswith("gt_cur_freq_mhz"):
            return "350" if freq else None
        if path.endswith("gt_max_freq_mhz"):
            return "1150" if freq else None
        return None

    monkeypatch.setattr(GPUCollector, "_read_sysfs_small",
                        staticmethod(lambda path: read(path)))
    # no lspci on the test machine -> name falls back to PCI id
    monkeypatch.setattr(gpu_mod.shutil, "which", lambda tool: None)
    # no intel_gpu_top binary
    monkeypatch.setattr(gpu_mod.shutil, "which",
                        lambda tool: None if tool == "intel_gpu_top" else None)


def test_intel_linux_finds_card_and_reports_honest_unavailability(monkeypatch):
    _fake_intel_sysfs(monkeypatch)
    c = GPUCollector()
    intel = c._intel_linux()
    assert intel["available_any"] is True
    assert len(intel["adapters"]) == 1
    a = intel["adapters"][0]
    assert "Intel" in a["name"]
    assert a["pnp_id"] == "pci:0x8086:0x591b" or a["pnp_id"].startswith("PCI 0x8086")
    assert a["current_clock_mhz"] == 350.0
    assert a["max_clock_mhz"] == 1150.0
    # utilization requires intel_gpu_top (perf) — explicitly unavailable
    assert intel["utilization"]["available"] is False
    assert "intel_gpu_top" in intel["utilization"]["reason"]
    # frequency stays visible in the reason — real data, not a dead end
    assert "350 MHz" in intel["utilization"]["reason"]
    # i915 does not expose per-card VRAM for iGPUs — explicit, not faked
    assert intel["memory"]["available"] is False
    assert "shared" in intel["memory"]["reason"].lower()


def test_intel_linux_no_cards_is_unavailable(monkeypatch):
    monkeypatch.setattr(gpu_mod.os, "listdir",
                        lambda p: ["renderD128"] if p == "/sys/class/drm" else [])
    c = GPUCollector()
    intel = c._intel_linux()
    assert intel["available_any"] is False
    assert "No Intel DRM card" in intel["reason"]


def test_intel_gpu_top_json_parsing(monkeypatch):
    c = GPUCollector()
    monkeypatch.setattr(gpu_mod.shutil, "which", lambda tool: "/usr/bin/intel_gpu_top")

    class FakeProc:
        returncode = 0
        # Real intel_gpu_top -J output is a multi-line JSON array.
        stdout = ('[\n{\n  "period": {"duration": 1000, "unit": "ms"},\n'
                  '  "engines": {"Render/3D": {"busy": 12.4}, '
                  '"Copy": {"busy": 3.0}, "Video": {"busy": 0.2}}\n}\n]\n')

    def fake_run(args, **kwargs):
        assert args == ["/usr/bin/intel_gpu_top", "-J", "-s", "1000",
                        "-n", "1", "-o", "-"]
        assert kwargs["timeout"] == 5.0
        return FakeProc()

    monkeypatch.setattr(gpu_mod.subprocess, "run", fake_run)
    c._intel_top_last = None
    res = c._intel_gpu_top()
    assert res["available"] is True
    assert res["percent"] == pytest.approx((12.4 + 3.0 + 0.2) / 3, abs=0.01)
    assert res["engines"]["Render/3D"] == 12.4


def test_intel_gpu_top_permission_failure_is_explicit(monkeypatch):
    c = GPUCollector()
    monkeypatch.setattr(gpu_mod.shutil, "which", lambda tool: "/usr/bin/intel_gpu_top")

    class FakeProc:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(gpu_mod.subprocess, "run",
                        lambda *a, **k: FakeProc())
    c._intel_top_last = None
    res = c._intel_gpu_top()
    assert res["available"] is False
    assert "perf" in res["reason"]


# ---------------------------------------------------------------------------
# Linux collect integration: NVIDIA machine + Intel machine + bare VM
# ---------------------------------------------------------------------------
def test_collect_linux_nvidia_machine(monkeypatch):
    monkeypatch.setattr(gpu_mod, "_IS_WINDOWS", False)
    c = GPUCollector()
    fake_nvidia = {"available": True, "gpus": [{
        "name": "NVIDIA Quadro P2000", "utilization_percent": 37.5,
        "memory_used_bytes": 1900 * gpu_mod._MIB,
        "memory_total_bytes": 4096 * gpu_mod._MIB,
        "temperature_c": 61.0, "driver_version": "425.31",
        "power_draw_watts": 34.5, "power_limit_watts": 60.0}]}
    monkeypatch.setattr(c, "_nvidia", lambda: fake_nvidia)
    monkeypatch.setattr(c, "_intel_linux", lambda: {
        "available_any": False, "adapters": [],
        "utilization": c._unavail_util(),
        "memory": c._unavail_mem(),
        "reason": "No Intel DRM card node found under /sys/class/drm "
                  "(no i915/xe device on this machine)"})
    section = c.collect()
    assert section["status"] == "ok"
    assert section["utilization"]["percent"] == 37.5
    assert section["memory"]["total_adapter_bytes"] == 4096 * gpu_mod._MIB
    assert section["temperature"]["celsius"] == 61.0
    assert section["power"]["draw_watts"] == 34.5
    assert section["power"]["limit_watts"] == 60.0
    assert section["adapters"][0]["name"] == "NVIDIA Quadro P2000"


def test_collect_linux_intel_machine(monkeypatch):
    monkeypatch.setattr(gpu_mod, "_IS_WINDOWS", False)
    c = GPUCollector()
    monkeypatch.setattr(c, "_nvidia", lambda: {
        "available": False, "gpus": [],
        "reason": "nvidia-smi not found"})
    monkeypatch.setattr(c, "_intel_linux", lambda: {
        "available_any": True,
        "adapters": [{"name": "Intel HD Graphics 630", "driver_version": None,
                      "driver_date": None, "adapter_ram_bytes": None,
                      "adapter_ram_capped": False, "adapter_ram_note": "shared",
                      "status": None, "video_processor": None,
                      "pnp_id": "PCI 0x8086:0x591b",
                      "current_clock_mhz": 350.0, "max_clock_mhz": 1150.0}],
        "utilization": c._unavail_util() | {
            "reason": "intel_gpu_top is not installed (package intel-gpu-tools)"
                      " GPU frequency 350 MHz / max 1150 MHz"},
        "memory": c._unavail_mem() | {"reason": "shared"},
        "reason": None})
    section = c.collect()
    assert section["status"] == "ok"
    assert section["adapters"][0]["name"] == "Intel HD Graphics 630"
    assert section["utilization"]["available"] is False    # explicit
    assert "350 MHz" in section["utilization"]["reason"]   # but informative
    assert section["power"]["available"] is False
    assert "nvidia-smi" in section["power"]["reason"] or "exposed" in section["power"]["reason"]


def test_collect_linux_bare_machine_unavailable(monkeypatch):
    monkeypatch.setattr(gpu_mod, "_IS_WINDOWS", False)
    c = GPUCollector()
    monkeypatch.setattr(c, "_nvidia", lambda: {
        "available": False, "gpus": [], "reason": "nvidia-smi not found"})
    monkeypatch.setattr(c, "_intel_linux", lambda: {
        "available_any": False, "adapters": [],
        "utilization": c._unavail_util(), "memory": c._unavail_mem(),
        "reason": "No Intel DRM card node found under /sys/class/dram"})
    section = c.collect()
    assert section["status"] == "unavailable"
    assert section["reason"]
    assert section["power"]["available"] is False


def test_linux_cpu_identity_from_proc():
    c = GPUCollector()
    ident = c.get_cpu_identity()   # real /proc/cpuinfo on this Linux box
    if not __import__("sys").platform.startswith("linux"):
        return  # Windows CI would return the WMI shape
    assert ident["name"] and "Intel" in ident["name"] or ident["name"]
    assert ident["logical_cores"] >= 1
