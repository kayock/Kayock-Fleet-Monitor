"""GPU metrics collector — layered backends, read-only, never fabricates.

Backends (in preference order):

1. NVIDIA      — `nvidia-smi --query-gpu=...` (accurate util/VRAM/temp for
                 NVIDIA adapters only; subprocess is timeout-guarded and
                 results are cached).
2. Windows PDH — GPU Engine + GPU Adapter Memory performance counters via
                 `PdhAddEnglishCounterW` (ctypes). This works for Intel iGPUs
                 (e.g. Intel UHD 770) and AMD on Windows 10 1709+ / 11, is
                 locale-independent (English counter paths resolve on any
                 Windows language), costs microseconds per sample and spawns
                 NO subprocess. Values are *engine utilization summed across
                 engines* — clearly labelled, never called "sensor load".
3. Identity    — Win32_VideoController / Win32_Processor through a cached
                 PowerShell Get-CimInstance call (adapter name, driver,
                 AdapterRAM, CPU identity). AdapterRAM is a 32-bit field and
                 saturates at 4 GiB — the UI is told about the cap instead of
                 showing a wrong number.

Every failure path degrades to an explicit `unavailable` state with a reason;
nothing is invented. Windows does not expose GPU temperatures for Intel/AMD
adapters through WMI or PDH — we say so rather than guessing.

Threading: one GPUCollector instance is used exclusively from the sampler's
GPU executor thread (see sampler.py), except get_cpu_identity() which is a
locked read used by the sampler for the CPU clock fallback.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

_IS_WINDOWS = sys.platform == "win32"  # True for 64-bit Windows as well

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2
PDH_CSTATUS_VALID_DATA = 0x00000000

ENGINE_UTIL_PATH = r"\GPU Engine(*)\Utilization Percentage"
ADAPTER_DEDICATED_PATH = r"\GPU Adapter Memory(*)\Dedicated Usage"
ADAPTER_SHARED_PATH = r"\GPU Adapter Memory(*)\Shared Usage"

# Windows gives MiB in nvidia-smi --format=csv,noheader,nounits
_MIB = 1024 * 1024
# Win32_VideoController.AdapterRAM is a uint32 -> saturates near 4 GiB
_ADAPTER_RAM_U32_MAX = (2 ** 32) - 1
_ADAPTER_RAM_U32_MAX_TOLERANCE = 1024


class PdhError(RuntimeError):
    """Raised for any PDH API failure; callers degrade to 'unavailable'."""


# ---------------------------------------------------------------------------
# PDH (Performance Data Helper) ctypes wrapper — Windows only
# ---------------------------------------------------------------------------
if _IS_WINDOWS:  # pragma: no cover - exercised only on Windows
    _pdh = ctypes.WinDLL("pdh")
    _PDH_HQUERY = ctypes.c_void_p
    _PDH_HCOUNTER = ctypes.c_void_p

    _pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t,
                                   ctypes.POINTER(_PDH_HQUERY)]
    _pdh.PdhAddEnglishCounterW.argtypes = [_PDH_HQUERY, ctypes.c_wchar_p,
                                           ctypes.c_size_t,
                                           ctypes.POINTER(_PDH_HCOUNTER)]
    _pdh.PdhCollectQueryData.argtypes = [_PDH_HQUERY]
    _pdh.PdhCloseQuery.argtypes = [_PDH_HQUERY]
    _pdh.PdhGetFormattedCounterArrayW.argtypes = [_PDH_HCOUNTER, ctypes.c_uint32,
                                                  ctypes.c_uint32,
                                                  ctypes.POINTER(ctypes.c_uint32),
                                                  ctypes.c_void_p]

    class _PdhFmtCounterValueUnion(ctypes.Union):
        _fields_ = [("longValue", ctypes.c_long),
                    ("doubleValue", ctypes.c_double),
                    ("largeValue", ctypes.c_longlong),
                    ("wideValue", ctypes.c_wchar_p)]

    class _PdhFmtCounterValue(ctypes.Structure):
        _fields_ = [("CStatus", ctypes.c_uint32),
                    ("u", _PdhFmtCounterValueUnion)]

    class _PdhFmtCounterValueItemW(ctypes.Structure):
        _fields_ = [("szName", ctypes.c_wchar_p),
                    ("FmtValue", _PdhFmtCounterValue)]


def _format_counter_array(hcounter):
    """Return [(instance_name, double_value), ...] for a wildcard counter.

    Values whose CStatus is not PDH_CSTATUS_VALID_DATA are skipped.
    """
    if not _IS_WINDOWS:  # pragma: no cover
        raise PdhError("PDH is only available on Windows")

    size = ctypes.c_uint32(0)
    count = ctypes.c_uint32(0)
    rc = _pdh.PdhGetFormattedCounterArrayW(hcounter, PDH_FMT_DOUBLE, 0,
                                          ctypes.byref(size), None)
    if rc == 0:
        return []
    if rc != PDH_MORE_DATA:
        raise PdhError(f"PdhGetFormattedCounterArrayW (size probe) failed: "
                       f"0x{rc & 0xFFFFFFFF:08X}")

    buf = ctypes.create_string_buffer(size.value)
    rc = _pdh.PdhGetFormattedCounterArrayW(hcounter, PDH_FMT_DOUBLE,
                                          size.value, ctypes.byref(count), buf)
    if rc != 0:
        raise PdhError(f"PdhGetFormattedCounterArrayW failed: "
                       f"0x{rc & 0xFFFFFFFF:08X}")

    n = count.value
    if n <= 0:
        return []
    items = ctypes.cast(buf, ctypes.POINTER(_PdhFmtCounterValueItemW * n)).contents
    out = []
    for item in items:
        name = item.szName  # ctypes converts c_wchar_p -> str (NUL-terminated)
        if not name:
            continue
        if item.FmtValue.CStatus != PDH_CSTATUS_VALID_DATA:
            continue
        out.append((name, float(item.FmtValue.u.doubleValue)))
    return out


class PdhQuery:
    """A persistent PDH query over one or more English counter paths.

    Must be used from a single thread (the sampler's GPU executor thread).
    """

    def __init__(self, paths):
        if not _IS_WINDOWS:
            raise PdhError("PDH is only available on Windows")
        self._h = _PDH_HQUERY()
        rc = _pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._h))
        if rc != 0:
            raise PdhError(f"PdhOpenQueryW failed: 0x{rc & 0xFFFFFFFF:08X}")
        self._counters = {}
        try:
            for path in paths:
                hc = _PDH_HCOUNTER()
                rc = _pdh.PdhAddEnglishCounterW(self._h, path, 0, ctypes.byref(hc))
                if rc != 0:
                    raise PdhError(
                        f"Counter unavailable: {path!r} "
                        f"(PdhAddEnglishCounterW: 0x{rc & 0xFFFFFFFF:08X}; "
                        f"the counter set may be disabled on this system)")
                self._counters[path] = hc
        except Exception:
            self.close()
            raise

    def collect(self):
        rc = _pdh.PdhCollectQueryData(self._h)
        if rc != 0:
            raise PdhError(f"PdhCollectQueryData failed: 0x{rc & 0xFFFFFFFF:08X}")
        return {path: _format_counter_array(hc)
                for path, hc in self._counters.items()}

    def warm_up(self, delay=0.15):
        """Two collects so rate counters produce cooked values."""
        self.collect()
        time.sleep(delay)
        self.collect()

    def close(self):
        h = getattr(self, "_h", None)
        if h:
            try:
                _pdh.PdhCloseQuery(h)
            except Exception:  # pragma: no cover
                pass
        self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ProcessorClockProbe:
    """Effective CPU clock via '\\Processor Information(_Total)\\% Processor
    Performance' — the same technique tools like HWiNFO use. Returns None on
    any failure; the sampler falls back to psutil/WMI base clocks."""

    PATH = r"\Processor Information(_Total)\% Processor Performance"

    def __init__(self):
        self._query = None
        self._failed_reason = None

    def current_percent(self):
        if not _IS_WINDOWS:
            return None
        if self._query is None:
            if self._failed_reason:
                return None
            try:
                self._query = PdhQuery([self.PATH])
                self._query.warm_up()
            except PdhError as exc:
                self._failed_reason = str(exc)
                return None
        try:
            data = self._query.collect()
        except PdhError:
            self._query = None  # recreate on the next tick
            return None
        values = data.get(self.PATH) or []
        if not values:
            return None
        pct = values[0][1]
        return pct if pct > 0 else None

    def effective_mhz(self, base_mhz):
        pct = self.current_percent()
        if pct is None or not base_mhz:
            return None
        mhz = float(base_mhz) * pct / 100.0
        if 100.0 <= mhz <= 20000.0:
            return round(mhz, 1)
        return None


# ---------------------------------------------------------------------------
# Pure parsers / aggregation (unit-tested on any OS)
# ---------------------------------------------------------------------------
_ENGINE_RE = re.compile(
    r"pid_(?P<pid>0x[0-9A-Fa-f]+|\d+)_"
    r"luid_(?P<luid>0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+)_"
    r"phys_(?P<phys>\d+)_"
    r"eng_(?P<eng>\d+)_"
    r"engtype_(?P<engtype>[A-Za-z0-9_]+)"
)
_ADAPTER_MEM_RE = re.compile(
    r"luid_(?P<luid>0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+)_phys_(?P<phys>\d+)"
)


def parse_engine_instance(name, value):
    """Parse a GPU Engine counter instance name into a dict, or None."""
    m = _ENGINE_RE.search(name or "")
    if not m:
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if val < 0:  # transient junk — ignore rather than clamp silently
        return None
    return {
        "pid": int(m.group("pid"), 0),
        "luid": m.group("luid"),
        "phys": int(m.group("phys")),
        "eng": int(m.group("eng")),
        "engtype": m.group("engtype"),
        "value": val,
    }


def aggregate_engine_utilization(parsed):
    """Aggregate per-engine-instance samples into per-adapter utilization.

    Windows exposes one counter instance per (pid, adapter, engine). Several
    PIDs can carry instances of the SAME engine node; for each unique engine
    node we take the MAX across PIDs (an engine has one real utilization),
    then SUM the engines per adapter. The sum can exceed 100% when several
    engines (3D + Copy + VideoDecode...) are busy in parallel — that is the
    honest kernel view, and the UI labels it as such.
    """
    engines = {}
    for s in parsed or []:
        key = (s["luid"], s["phys"], s["eng"], s["engtype"])
        engines[key] = max(engines.get(key, 0.0), s["value"])

    per_adapter = {}
    by_type = {}
    for (luid, phys, _eng, engtype), val in engines.items():
        per_adapter[(luid, phys)] = per_adapter.get((luid, phys), 0.0) + val
        by_type[engtype] = by_type.get(engtype, 0.0) + val

    adapters = {f"LUID {luid} phys {phys}": round(v, 1)
                for (luid, phys), v in per_adapter.items()}
    return {
        "adapters": adapters,
        "engines_by_type": {k: round(v, 1) for k, v in by_type.items()},
        "total_percent": round(sum(per_adapter.values()), 1),
        "engine_count": len(engines),
    }


def parse_adapter_memory(name, value):
    m = _ADAPTER_MEM_RE.search(name or "")
    if not m:
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if val < 0:
        return None
    return {"luid": m.group("luid"), "phys": int(m.group("phys")),
            "bytes": int(round(val))}


def aggregate_adapter_memory(parsed):
    per = {}
    total = 0
    for s in parsed or []:
        key = f"LUID {s['luid']} phys {s['phys']}"
        per[key] = per.get(key, 0) + s["bytes"]
        total += s["bytes"]
    return {"per_adapter": per, "total_bytes": int(total)}


def parse_nvidia_smi_output(text):
    """Parse `nvidia-smi --query-gpu=name,utilization.gpu,memory.used,
    memory.total,temperature.gpu,driver_version[,power.draw,power.limit]
    --format=csv,noheader,nounits`. Power fields are optional (older drivers
    and 'N/A' values degrade to None). Malformed lines are skipped."""
    gpus = []
    for line in (text or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            util = float(parts[1])
            used = int(float(parts[2])) * _MIB
            total = int(float(parts[3])) * _MIB
            temp = float(parts[4])
        except ValueError:
            continue

        def _watts(raw):
            if raw is None:
                return None
            raw = str(raw).replace("W", "").replace("w", "").strip()
            if not raw or raw.lower().startswith("n/a"):
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        draw = _watts(parts[6]) if len(parts) > 6 else None
        limit = _watts(parts[7]) if len(parts) > 7 else None
        gpus.append({
            "name": parts[0],
            "utilization_percent": util,
            "memory_used_bytes": used,
            "memory_total_bytes": total,
            "temperature_c": temp,
            "driver_version": parts[5],
            "power_draw_watts": draw,
            "power_limit_watts": limit,
        })
    return gpus


# ---------------------------------------------------------------------------
# Windows identity via PowerShell Get-CimInstance (cached)
# ---------------------------------------------------------------------------
_IDENTITY_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$v = @(Get-CimInstance -ClassName Win32_VideoController | ForEach-Object {
  [pscustomobject]@{
    name           = $_.Name
    driver_version = $_.DriverVersion
    driver_date    = if ($_.DriverDate) { $_.DriverDate.ToString('yyyy-MM-dd') } else { $null }
    adapter_ram    = if ($null -ne $_.AdapterRAM) { [uint64]$_.AdapterRAM } else { $null }
    status         = $_.Status
    video_processor= $_.VideoProcessor
    pnp_id         = $_.PNPDeviceID
  }
})
$p = @(Get-CimInstance -ClassName Win32_Processor | ForEach-Object {
  [pscustomobject]@{
    name              = $_.Name
    current_clock_mhz = $_.CurrentClockSpeed
    max_clock_mhz     = $_.MaxClockSpeed
    physical_cores    = $_.NumberOfCores
    logical_cores     = $_.NumberOfLogicalProcessors
  }
})
@{ gpus = $v; cpus = $p } | ConvertTo-Json -Depth 3 -Compress
"""

# Fallback for the GPU Engine / Adapter Memory counters when the PDH path is
# unavailable (counter set disabled, PDH init failure). Slower: one PowerShell
# subprocess per call, therefore throttled and cached by the collector.
_GETCOUNTER_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$e = Get-Counter '\GPU Engine(*)\Utilization Percentage' -MaxSamples 1
$m = Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage' -MaxSamples 1
$s = Get-Counter '\GPU Adapter Memory(*)\Shared Usage' -MaxSamples 1
@{
  engine    = @($e.CounterSamples | ForEach-Object { @{ instance = $_.InstanceName; value = [double]$_.CookedValue } })
  dedicated = @($m.CounterSamples | ForEach-Object { @{ instance = $_.InstanceName; value = [double]$_.CookedValue } })
  shared    = @($s.CounterSamples | ForEach-Object { @{ instance = $_.InstanceName; value = [double]$_.CookedValue } })
} | ConvertTo-Json -Depth 3 -Compress
"""


def _as_list(obj):
    if obj is None:
        return []
    if isinstance(obj, dict):
        return [obj]
    if isinstance(obj, list):
        return obj
    return []


def _to_int(value):
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def run_powershell_json(script, timeout=8.0):
    """Run a PowerShell command and parse its JSON stdout. Returns None on any
    failure (missing PowerShell, timeout, non-zero exit, bad JSON)."""
    exe = shutil.which("powershell") or "powershell"
    cmd = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-Command", script]
    extra = {}
    if _IS_WINDOWS:  # pragma: no cover
        extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, **extra)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout.strip())
    except (ValueError, json.JSONDecodeError):
        return None


def parse_identity_payload(obj):
    """Normalize the identity JSON into (adapters, cpu_identity)."""
    if not isinstance(obj, dict):
        return [], None
    adapters = []
    for a in _as_list(obj.get("gpus")):
        ram = _to_int(a.get("adapter_ram"))
        capped = ram is not None and ram >= _ADAPTER_RAM_U32_MAX - _ADAPTER_RAM_U32_MAX_TOLERANCE
        adapters.append({
            "name": (a.get("name") or "").strip() or "Unnamed display adapter",
            "driver_version": (a.get("driver_version") or "").strip() or None,
            "driver_date": (a.get("driver_date") or "").strip() or None,
            "adapter_ram_bytes": ram,
            "adapter_ram_capped": capped,
            "status": (a.get("status") or "").strip() or None,
            "video_processor": (a.get("video_processor") or "").strip() or None,
            "pnp_id": (a.get("pnp_id") or "").strip() or None,
        })
    cpus = _as_list(obj.get("cpus"))
    cpu_identity = None
    if cpus:
        c = cpus[0]
        cpu_identity = {
            "name": (c.get("name") or "").strip() or None,
            "current_clock_mhz": _to_int(c.get("current_clock_mhz")),
            "max_clock_mhz": _to_int(c.get("max_clock_mhz")),
            "physical_cores": _to_int(c.get("physical_cores")),
            "logical_cores": _to_int(c.get("logical_cores")),
        }
    return adapters, cpu_identity


# ---------------------------------------------------------------------------
# The collector used by the sampler
# ---------------------------------------------------------------------------
class GPUCollector:
    """Collects one GPU 'section' per call. Runs ONLY on the sampler's GPU
    executor thread; all expensive work is cached and subprocess calls are
    timeout-guarded so the sampler never stalls."""

    def __init__(self, nvidia_poll_seconds=4.0, identity_ttl_seconds=60.0,
                 powershell_timeout=8.0):
        self._nvidia_poll_seconds = nvidia_poll_seconds
        self._identity_ttl = identity_ttl_seconds
        self._ps_timeout = powershell_timeout

        self._lock = threading.Lock()  # guards _identity/_nvidia caches
        self._counters = None          # PdhQuery or None
        self._counters_failed = None   # reason str
        self._identity = None          # (adapters, cpu_identity)
        self._identity_ts = 0.0
        self._identity_failed = None
        self._nvidia_path = None
        self._nvidia_checked = 0.0
        self._nvidia_gpus = None
        self._nvidia_failed = None
        self._getcounter_last = None     # cached fallback (util, memory) dict
        self._getcounter_ts = 0.0
        self._getcounter_min_interval = 5.0
        # Linux collectors (Intel sysfs / intel_gpu_top / /proc cpuinfo)
        self._intel_cards = None
        self._intel_cards_ts = 0.0
        self._lspci_map = None
        self._lspci_ts = 0.0
        self._intel_top_last = None
        self._intel_top_ts = 0.0
        self._cpu_identity_linux = None
        self._cpu_identity_linux_ts = 0.0

    # -- public ------------------------------------------------------------
    def collect(self):
        try:
            return self._collect()
        except Exception as exc:  # defensive: the sampler must never crash
            return self._compose(
                status="error",
                reason=f"Unexpected GPU collector error: "
                       f"{type(exc).__name__}: {exc}",
                collected_at=time.time(),
            )

    def get_cpu_identity(self):
        """Cached CPU identity (name/clock) or None. Windows uses the
        (TTL-limited, timeout-guarded) PowerShell query; Linux reads
        /proc/cpuinfo. Safe to call from the sampler thread."""
        if sys.platform == "win32":
            self._ensure_identity()
            with self._lock:
                if self._identity and self._identity[1]:
                    return dict(self._identity[1])
            return None
        if sys.platform.startswith("linux"):
            return self._linux_cpu_identity()
        return None

    def close(self):
        if self._counters is not None:
            self._counters.close()
            self._counters = None

    # -- internals ----------------------------------------------------------
    def _collect(self):
        collected_at = time.time()
        if not _IS_WINDOWS:
            return self._collect_linux(collected_at)

        # Windows path
        identity_adapters = self._ensure_identity() or []
        engine = self._engine_and_memory()
        nvidia = self._nvidia()

        util = engine["utilization"]
        mem = engine["memory"]
        temp = self._temp_from_nvidia(nvidia)

        if nvidia["available"]:
            # nvidia-smi is the most accurate for the NVIDIA adapter; engine
            # counters (if present) are still reported for other adapters.
            util = self._util_from_nvidia(nvidia, engine=engine)
            mem = self._memory_from_nvidia(nvidia, engine=engine)
        elif not engine["utilization"]["available"] and not engine["memory"]["available"]:
            if identity_adapters:
                # Counters unavailable but the adapter is identified: still a
                # useful 'ok' section — but reason must be set (this call used
                # to raise TypeError: missing 'reason').
                return self._compose(
                    status="ok", reason=None, collected_at=collected_at,
                    adapters=identity_adapters,
                    utilization=engine["utilization"], memory=engine["memory"],
                    temperature=temp, nvidia=nvidia)
            return self._compose(
                status="unavailable",
                reason=("Windows GPU performance counters unavailable and no "
                        "nvidia-smi found; adapter identity query also failed"),
                collected_at=collected_at, nvidia=nvidia)

        adapters = identity_adapters or self._generic_adapters(engine, nvidia)
        status = "ok"
        return self._compose(status=status, reason=None,
                             collected_at=collected_at, adapters=adapters,
                             utilization=util, memory=mem,
                             temperature=temp, nvidia=nvidia)

    def _compose(self, status, collected_at, reason=None, adapters=None,
                 utilization=None, memory=None, temperature=None, nvidia=None,
                 power=None):
        """Single composer for every GPU section. `reason` defaults to None so
        no call site can ever raise 'missing positional argument' again (this
        was a real production bug on Windows)."""
        return {
            "status": status,
            "reason": reason,
            "platform": sys.platform,
            "adapters": adapters or [],
            "utilization": utilization or self._unavail_util(),
            "memory": memory or self._unavail_mem(),
            "temperature": temperature or self._unavail_temp(),
            "nvidia": nvidia or {"available": False, "gpus": [], "reason": None},
            "power": power or self._unavail_power(),
            "collected_at": collected_at,
        }

    @staticmethod
    def _unavail_power():
        return {"available": False, "draw_watts": None, "limit_watts": None,
                "source": None, "reason": "Not exposed for this adapter"}

    # -- utilization / memory shapes ----------------------------------------
    @staticmethod
    def _unavail_util():
        return {"available": False, "percent": None, "source": None,
                "scope": None, "engines_by_type": {}, "per_adapter": {},
                "note": None, "reason": "unavailable"}

    @staticmethod
    def _unavail_mem():
        return {"available": False, "dedicated_bytes": None,
                "shared_bytes": None, "total_adapter_bytes": None,
                "per_adapter": {}, "source": None, "note": "unavailable"}

    @staticmethod
    def _unavail_temp():
        return {"available": False, "celsius": None, "source": None,
                "reason": "Not exposed by this system for this adapter"}

    def _engine_and_memory(self):
        if self._counters is None:
            if self._counters_failed:
                return self._engine_and_memory_fallback(self._counters_failed)
            try:
                q = PdhQuery([ENGINE_UTIL_PATH, ADAPTER_DEDICATED_PATH,
                              ADAPTER_SHARED_PATH])
                q.warm_up()
                self._counters = q
            except PdhError as exc:
                self._counters_failed = str(exc)
                return self._engine_and_memory_fallback(self._counters_failed)
        try:
            data = self._counters.collect()
        except PdhError as exc:
            # e.g. counter set disabled mid-run — recreate next tick
            self._counters.close()
            self._counters = None
            return self._engine_and_memory_fallback(str(exc))

        parsed = []
        for name, val in data.get(ENGINE_UTIL_PATH) or []:
            p = parse_engine_instance(name, val)
            if p is not None:
                parsed.append(p)
        agg = aggregate_engine_utilization(parsed)

        n_adapters = len(agg["adapters"])
        util = {
            "available": n_adapters > 0,
            "percent": agg["total_percent"] if n_adapters else None,
            "source": "Windows GPU Engine performance counters (PDH)",
            "scope": ("adapter-specific: sum of engine utilization for the "
                      "adapter" if n_adapters == 1 else
                      f"system-wide: sum of engine utilization across "
                      f"{n_adapters} adapters"),
            "engines_by_type": agg["engines_by_type"],
            "per_adapter": agg["adapters"],
            "engine_count": agg["engine_count"],
            "note": ("Engine utilization is summed across engine types and may "
                     "exceed 100% when several engines run in parallel — this "
                     "is the Windows kernel view, not a sensor reading."),
            "reason": None if n_adapters else
                      ("No GPU Engine counter instances found (counters may "
                       "be disabled on this system)"),
        }

        dedicated = aggregate_adapter_memory(
            [m for name, val in data.get(ADAPTER_DEDICATED_PATH) or []
             if (m := parse_adapter_memory(name, val)) is not None])
        shared = aggregate_adapter_memory(
            [m for name, val in data.get(ADAPTER_SHARED_PATH) or []
             if (m := parse_adapter_memory(name, val)) is not None])

        mem = {
            "available": dedicated["total_bytes"] > 0 or shared["total_bytes"] > 0,
            "dedicated_bytes": dedicated["total_bytes"] or None,
            "shared_bytes": shared["total_bytes"] or None,
            "total_adapter_bytes": None,   # filled from identity when known
            "per_adapter": {"dedicated": dedicated["per_adapter"],
                            "shared": shared["per_adapter"]},
            "source": "Windows GPU Adapter Memory performance counters (PDH)",
            "note": None,
        }
        return {"utilization": util, "memory": mem}

    def _engine_and_memory_fallback(self, pdh_reason):
        """PowerShell Get-Counter fallback used only when the PDH counter path
        is unavailable (e.g. counter set disabled or PDH init failed). Slower
        (a subprocess per call) so it is throttled and cached; still uses the
        same instance parsing / aggregation as the PDH path."""
        now = time.time()
        with self._lock:
            if (self._getcounter_last is not None
                    and now - self._getcounter_ts < self._getcounter_min_interval):
                return self._getcounter_last

        def fail(reason):
            result = {
                "utilization": self._unavail_util() | {"reason": reason},
                "memory": self._unavail_mem(),
            }
            with self._lock:
                self._getcounter_last = result
                self._getcounter_ts = now
            return result

        if not _IS_WINDOWS:
            return fail(f"PDH: {pdh_reason}; Get-Counter fallback requires Windows")

        obj = run_powershell_json(_GETCOUNTER_SCRIPT, timeout=self._ps_timeout)
        if obj is None:
            return fail(f"PDH: {pdh_reason}; PowerShell Get-Counter fallback "
                        f"also failed (timed out, or counters disabled/localized)")

        parsed = []
        for s in _as_list(obj.get("engine")):
            p = parse_engine_instance(s.get("instance"), s.get("value"))
            if p is not None:
                parsed.append(p)
        agg = aggregate_engine_utilization(parsed)
        n_adapters = len(agg["adapters"])

        util = {
            "available": n_adapters > 0,
            "percent": agg["total_percent"] if n_adapters else None,
            "source": ("Windows GPU Engine performance counters "
                       "(PowerShell Get-Counter fallback)"),
            "scope": ("adapter-specific: sum of engine utilization for the "
                      "adapter" if n_adapters == 1 else
                      f"system-wide: sum across {n_adapters} adapters"),
            "engines_by_type": agg["engines_by_type"],
            "per_adapter": agg["adapters"],
            "engine_count": agg["engine_count"],
            "note": ("Engine utilization is summed across engine types and may "
                     "exceed 100% when several engines run in parallel — this "
                     "is the Windows kernel view, not a sensor reading."),
            "reason": None if n_adapters else
                      ("Get-Counter returned no GPU Engine instances "
                       "(counters may be disabled on this system)"),
        }

        dedicated = aggregate_adapter_memory(
            [m for s in _as_list(obj.get("dedicated"))
             if (m := parse_adapter_memory(s.get("instance"), s.get("value"))) is not None])
        shared = aggregate_adapter_memory(
            [m for s in _as_list(obj.get("shared"))
             if (m := parse_adapter_memory(s.get("instance"), s.get("value"))) is not None])
        mem = {
            "available": dedicated["total_bytes"] > 0 or shared["total_bytes"] > 0,
            "dedicated_bytes": dedicated["total_bytes"] or None,
            "shared_bytes": shared["total_bytes"] or None,
            "total_adapter_bytes": None,
            "per_adapter": {"dedicated": dedicated["per_adapter"],
                            "shared": shared["per_adapter"]},
            "source": "Windows GPU Adapter Memory counters (Get-Counter fallback)",
            "note": None,
        }
        result = {"utilization": util, "memory": mem}
        with self._lock:
            self._getcounter_last = result
            self._getcounter_ts = now
        return result

    # -- identity -------------------------------------------------------------
    def _ensure_identity(self):
        """Return the cached adapter list, refreshing it when the TTL expired.
        Never raises; on query failure returns stale data or []."""
        if not _IS_WINDOWS:
            return []
        with self._lock:
            if self._identity and time.time() - self._identity_ts < self._identity_ttl:
                return self._identity[0]
        obj = run_powershell_json(_IDENTITY_SCRIPT, timeout=self._ps_timeout)
        if obj is None:
            with self._lock:
                if self._identity:
                    return self._identity[0]  # stale-but-real beats nothing
                self._identity_failed = ("PowerShell Get-CimInstance query "
                                         "failed (timed out or unavailable)")
            return []
        adapters, cpu_identity = parse_identity_payload(obj)
        for a in adapters:
            if a["adapter_ram_bytes"] is not None:
                a["adapter_ram_note"] = (
                    "≥ 4 GB (WMI AdapterRAM is a 32-bit field that caps at "
                    "4 GB — the true size may be larger)"
                    if a["adapter_ram_capped"] else
                    "WMI AdapterRAM — for integrated GPUs this often reflects "
                    "shared memory, not the maximum")
            else:
                a["adapter_ram_note"] = "AdapterRAM not reported by WMI"
        with self._lock:
            self._identity = (adapters, cpu_identity)
            self._identity_ts = time.time()
            self._identity_failed = None
            return adapters

    # -- Linux collectors -------------------------------------------------------
    def _collect_linux(self, collected_at):
        """v3 Linux path: nvidia-smi when an NVIDIA card exists (Quadro P2000
        case), Intel DRM/i915 sysfs + optional intel_gpu_top for integrated
        Intel GPUs (HD 630 case). Both may coexist; unavailable states stay
        explicit and honest."""
        nvidia = self._nvidia()
        intel = self._intel_linux()

        adapters = []
        if nvidia["available"]:
            adapters.extend(self._generic_adapters_from_nvidia(nvidia))
        adapters.extend(intel["adapters"])

        power = self._power_from_nvidia(nvidia)
        if nvidia["available"]:
            util = self._util_from_nvidia(nvidia)
            mem = self._memory_from_nvidia(nvidia)
            temp = self._temp_from_nvidia(nvidia)
            if intel["utilization"]["available"]:
                # merge the second vendor's per-adapter data without
                # overriding the headline NVIDIA numbers
                util = dict(util)
                util["per_adapter"] = dict(util.get("per_adapter") or {})
                util["per_adapter"].update(intel["utilization"].get("per_adapter") or {})
        elif intel["available_any"]:
            util = intel["utilization"]
            mem = intel["memory"]
            temp = self._unavail_temp()
            if not intel["utilization"]["available"]:
                temp = self._unavail_temp()
        else:
            util = self._unavail_util()
            mem = self._unavail_mem()
            temp = self._unavail_temp()

        if nvidia["available"] or intel["available_any"]:
            status, reason = "ok", None
        else:
            status = "unavailable"
            reason = (intel["reason"] or "no GPU backend on this system")
        return self._compose(status=status, reason=reason,
                             collected_at=collected_at,
                             adapters=adapters or None,
                             utilization=util, memory=mem, temperature=temp,
                             nvidia=nvidia, power=power)

    @staticmethod
    def _power_from_nvidia(nvidia):
        if not nvidia["available"]:
            return GPUCollector._unavail_power()
        g0 = nvidia["gpus"][0]
        draw = g0.get("power_draw_watts")
        limit = g0.get("power_limit_watts")
        if draw is None and limit is None:
            return GPUCollector._unavail_power() | {
                "reason": "nvidia-smi did not report power for this GPU"}
        return {"available": True, "draw_watts": draw, "limit_watts": limit,
                "source": "nvidia-smi", "reason": None}

    def _find_intel_cards(self):
        """[(card_path, vendor_hex, device_hex)] for Intel DRM cards.
        Cached for 60 s — the sysfs layout is stable while running."""
        now = time.time()
        with self._lock:
            if self._intel_cards is not None \
                    and now - self._intel_cards_ts < 60.0:
                return self._intel_cards
        cards = []
        try:
            names = sorted(os.listdir("/sys/class/drm"))
        except OSError:
            names = []
        for entry in names:
            m = re.fullmatch(r"card(\d+)", entry)
            if not m:
                continue
            dev = f"/sys/class/drm/{entry}/device"
            vendor = self._read_sysfs_small(f"{dev}/vendor")
            if vendor is None or vendor.lower() != "0x8086":
                continue  # only Intel (AMD/NVIDIA use other backends)
            device_id = self._read_sysfs_small(f"{dev}/device") or "0x0000"
            cards.append((dev, vendor.lower(), device_id.lower()))
        with self._lock:
            self._intel_cards = cards or None
            self._intel_cards_ts = now
        return cards

    @staticmethod
    def _read_sysfs_small(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.readline().strip()
        except (OSError, UnicodeError):
            return None

    def _intel_card_name(self, dev, vendor, device_id):
        """Best-effort readable name: `lspci -nn` when available, else the
        PCI id. Throttled (60 s) since it shells out."""
        now = time.time()
        key = f"{vendor}:{device_id}"
        with self._lock:
            if self._lspci_ts and now - self._lspci_ts < 60.0 \
                    and self._lspci_map is not None:
                return self._lspci_map.get(key)
        name_map = {}
        lspci = shutil.which("lspci")
        if lspci:
            try:
                proc = subprocess.run([lspci, "-nn"], capture_output=True,
                                      text=True, encoding="utf-8",
                                      errors="replace", timeout=3.0)
                for line in (proc.stdout or "").splitlines():
                    parts = line.split()
                    if len(parts) < 3 or not ("VGA" in line
                                              or "Display" in line):
                        continue
                    pid = parts[2].strip("[]")
                    label = line.split(": ", 1)[-1].strip()
                    label = re.sub(r"\s*\[8086:[0-9a-f]+\]", "", label).strip()
                    if pid.startswith("8086:") and label:
                        name_map[pid] = f"Intel {label}"
            except (OSError, subprocess.SubprocessError):
                pass
        with self._lock:
            self._lspci_map = name_map
            self._lspci_ts = now
        return name_map.get(key) or f"Intel GPU ({vendor}:{device_id})"

    def _intel_freq(self, dev):
        """(current_mhz, max_mhz) from i915 sysfs; (None, None) when absent."""
        cur = None
        for cand in (f"{dev}/gt_cur_freq_mhz",
                     f"{dev}/gt/gt0/gt_cur_freq_mhz"):
            raw = self._read_sysfs_small(cand)
            if raw:
                try:
                    cur = float(raw)
                    break
                except ValueError:
                    pass
        mx = None
        for cand in (f"{dev}/gt_max_freq_mhz",
                     f"{dev}/gt/gt0/gt_max_freq_mhz",
                     f"{dev}/gt_RP0_freq_mhz"):
            raw = self._read_sysfs_small(cand)
            if raw:
                try:
                    mx = float(raw)
                    break
                except ValueError:
                    pass
        return cur, mx

    def _intel_gpu_top(self):
        """intel_gpu_top utilization (perf-based), throttled to 5 s and fully
        failure-tolerant: most systems without intel-gpu-tools or without
        perf permissions simply get an explicit unavailable state."""
        now = time.time()
        with self._lock:
            if self._intel_top_last is not None \
                    and now - self._intel_top_ts < 5.0:
                return self._intel_top_last
        result = {"available": False, "percent": None, "engines": {},
                  "reason": "intel_gpu_top is not installed "
                            "(package intel-gpu-tools)"}
        top = shutil.which("intel_gpu_top")
        if top:
            result = {"available": False, "percent": None, "engines": {},
                      "reason": "intel_gpu_top could not read perf counters "
                                "(needs permissions, e.g. run as root or "
                                "grant perf_event_paranoid access)"}
            try:
                proc = subprocess.run(
                    [top, "-J", "-s", "1"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=4.0,
                    stdin=subprocess.DEVNULL)
                out = (proc.stdout or "").strip()
                if out and proc.returncode == 0:
                    line = out.splitlines()[0]
                    data = json.loads(line)
                    engines = {}
                    for ename, edata in (data.get("engines") or {}).items():
                        busy = (edata or {}).get("busy")
                        if isinstance(busy, (int, float)):
                            engines[ename] = round(float(busy), 1)
                    if engines:
                        result = {"available": True,
                                  "percent": round(sum(engines.values())
                                                   / len(engines), 1),
                                  "engines": engines, "reason": None}
            except (OSError, subprocess.SubprocessError, ValueError,
                    AttributeError):
                pass
        with self._lock:
            self._intel_top_last = result
            self._intel_top_ts = now
        return result

    def _intel_linux(self):
        cards = self._find_intel_cards()
        if not cards:
            return {"available_any": False, "adapters": [],
                    "utilization": self._unavail_util(),
                    "memory": self._unavail_mem(),
                    "reason": ("No Intel DRM card node found under "
                               "/sys/class/drm (no i915/xe device on this "
                               "machine)")}
        adapters = []
        for dev, vendor, device_id in cards:
            cur, mx = self._intel_freq(dev)
            adapters.append({
                "name": self._intel_card_name(dev, vendor, device_id),
                "driver_version": None,
                "driver_date": None,
                "adapter_ram_bytes": None,
                "adapter_ram_capped": False,
                "adapter_ram_note": ("integrated GPU — uses shared system "
                                     "memory; per-card VRAM is not exposed "
                                     "by i915"),
                "status": None,
                "video_processor": None,
                "pnp_id": f"PCI {vendor}:{device_id}",
                "current_clock_mhz": cur,
                "max_clock_mhz": mx,
            })
        igt = self._intel_gpu_top()
        if igt["available"]:
            util = {"available": True, "percent": igt["percent"],
                    "source": "intel_gpu_top (perf engine busy average)",
                    "scope": "Intel iGPU engines (Render/Copy/Video)",
                    "engines_by_type": igt["engines"], "per_adapter": {},
                    "note": None, "reason": None}
        else:
            freq_note = ""
            cur, mx = self._intel_freq(cards[0][0])
            if cur:
                freq_note = (f" GPU frequency {cur:.0f} MHz"
                             + (f" / max {mx:.0f} MHz" if mx else ""))
            util = self._unavail_util() | {
                "reason": igt["reason"] + freq_note}
        mem = self._unavail_mem() | {
            "reason": ("i915 does not expose per-card VRAM for integrated "
                       "Intel GPUs (shared system memory)")}
        return {"available_any": True, "adapters": adapters,
                "utilization": util, "memory": mem, "reason": None}

    def _linux_cpu_identity(self):
        """Cached CPU identity from /proc/cpuinfo (name + per-cpu MHz)."""
        now = time.time()
        with self._lock:
            if self._cpu_identity_linux is not None \
                    and now - self._cpu_identity_linux_ts < 60.0:
                return self._cpu_identity_linux
        name = None
        cur = None
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8",
                      errors="replace") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("MHz") and cur is None:
                        try:
                            cur = float(line.split(":", 1)[1].strip())
                        except ValueError:
                            pass
                    if name and cur is not None:
                        break
        except OSError:
            pass
        logical = None
        try:
            logical = os.cpu_count()
        except Exception:
            pass
        identity = {"name": name, "current_clock_mhz": cur,
                    "max_clock_mhz": None, "physical_cores": None,
                    "logical_cores": logical}
        with self._lock:
            self._cpu_identity_linux = identity
            self._cpu_identity_linux_ts = now
        return identity

    # -- nvidia ---------------------------------------------------------------
    def _nvidia(self):
        path = self._find_nvidia_smi()
        if not path:
            with self._lock:
                return {"available": False, "gpus": [],
                        "reason": ("nvidia-smi not found — no NVIDIA driver/"
                                   "hardware installed (expected on Intel-only "
                                   "systems)")}
        with self._lock:
            if (self._nvidia_gpus is not None
                    and time.time() - self._nvidia_ts < self._nvidia_poll_seconds):
                return {"available": True, "gpus": self._nvidia_gpus,
                        "reason": None}
        try:
            proc = subprocess.run(
                [path, "--query-gpu=name,utilization.gpu,memory.used,"
                        "memory.total,temperature.gpu,driver_version,"
                        "power.draw,power.limit",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=3.0,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                               if _IS_WINDOWS else 0))
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            with self._lock:
                self._nvidia_failed = f"nvidia-smi failed: {exc}"
                self._nvidia_gpus = None
            return {"available": False, "gpus": [],
                    "reason": self._nvidia_failed}
        if proc.returncode != 0 or not proc.stdout.strip():
            with self._lock:
                self._nvidia_failed = (f"nvidia-smi exited {proc.returncode}")
                self._nvidia_gpus = None
            return {"available": False, "gpus": [], "reason": self._nvidia_failed}
        gpus = parse_nvidia_smi_output(proc.stdout)
        with self._lock:
            self._nvidia_gpus = gpus or None
            self._nvidia_ts = time.time()
            self._nvidia_failed = None if gpus else "nvidia-smi output unparsable"
        if not gpus:
            return {"available": False, "gpus": [], "reason": self._nvidia_failed}
        return {"available": True, "gpus": gpus, "reason": None}

    def _find_nvidia_smi(self):
        now = time.time()
        with self._lock:
            if self._nvidia_path and now - self._nvidia_checked < 300.0:
                return self._nvidia_path
        path = shutil.which("nvidia-smi")
        with self._lock:
            self._nvidia_path = path
            self._nvidia_checked = now
        return path

    # -- adapters from partial data -------------------------------------------
    @staticmethod
    def _generic_adapters_from_nvidia(nvidia):
        return [{"name": g["name"],
                 "driver_version": g.get("driver_version"),
                 "driver_date": None,
                 "adapter_ram_bytes": g.get("memory_total_bytes"),
                 "adapter_ram_capped": False,
                 "adapter_ram_note": "VRAM reported by nvidia-smi",
                 "status": None,
                 "video_processor": None,
                 "pnp_id": None} for g in nvidia["gpus"]]

    def _generic_adapters(self, engine, nvidia):
        out = []
        for label in (engine["utilization"].get("per_adapter") or {}):
            out.append({"name": f"GPU adapter ({label})",
                        "driver_version": None, "driver_date": None,
                        "adapter_ram_bytes": None, "adapter_ram_capped": False,
                        "adapter_ram_note": ("WMI identity query failed — "
                                             "adapter name unavailable"),
                        "status": None, "video_processor": None, "pnp_id": None})
        return out

    @staticmethod
    def _util_from_nvidia(nvidia, engine=None):
        if not nvidia["available"]:
            return GPUCollector._unavail_util()
        g0 = nvidia["gpus"][0]
        return {
            "available": True,
            "percent": g0["utilization_percent"],
            "source": "nvidia-smi (adapter-specific sensor)",
            "scope": f"NVIDIA adapter: {g0['name']}",
            "engines_by_type": (engine or {}).get("utilization", {}).get(
                "engines_by_type", {}),
            "per_adapter": (engine or {}).get("utilization", {}).get(
                "per_adapter", {}),
            "note": None,
            "reason": None,
        }

    @staticmethod
    def _memory_from_nvidia(nvidia, engine=None):
        if not nvidia["available"]:
            return GPUCollector._unavail_mem()
        g0 = nvidia["gpus"][0]
        return {
            "available": True,
            "dedicated_bytes": g0["memory_used_bytes"],
            "shared_bytes": None,
            "total_adapter_bytes": g0["memory_total_bytes"],
            "per_adapter": (engine or {}).get("memory", {}).get("per_adapter", {}),
            "source": "nvidia-smi (adapter VRAM)",
            "note": None,
        }

    @staticmethod
    def _temp_from_nvidia(nvidia):
        if not nvidia["available"]:
            return GPUCollector._unavail_temp()
        return {"available": True, "celsius": nvidia["gpus"][0]["temperature_c"],
                "source": "nvidia-smi",
                "reason": None}


def gpu_placeholder(reason="First GPU sample is still being collected…"):
    """Full-shape 'initializing' section so the UI always has every key."""
    return GPUCollector()._compose(status="initializing", reason=reason,
                                   collected_at=time.time())


def gpu_error(exc):
    return GPUCollector()._compose(
        status="error",
        reason=f"GPU collector failed: {type(exc).__name__}: {exc}",
        collected_at=time.time())
