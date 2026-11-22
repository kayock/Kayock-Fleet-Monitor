"""The ONE background sampler + thread-safe cached snapshot.

Design rules (this fixes the duplicated-sampling bug of the earlier rewrite):

* Exactly ONE thread — the daemon thread started by :meth:`SystemSampler.start`
  — ever calls psutil counters. It owns the network and disk I/O baseline
  books and the rolling histories.
* Flask request handlers only ever call :meth:`SystemSampler.snapshot`, which
  returns the pre-built, JSON-ready snapshot. An HTTP request NEVER samples
  and NEVER touches the baselines.
* GPU collection runs on a single-worker executor fed by the sampler: the
  sampler polls the finished future (non-blocking) and keeps the last known
  GPU section while a subprocess (nvidia-smi) is still in flight, so a slow
  GPU query can never stall metric updates or Flask responses.
* All rates are computed from counter deltas over a monotonic elapsed time.
  The first observation of any counter is reported as None ("measuring…"),
  never as a fake zero. Negative deltas (device/counter reset) clamp to 0.0.
"""
from __future__ import annotations

import platform
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import psutil

from gpu import GPUCollector, ProcessorClockProbe, gpu_error, gpu_placeholder
from utils import clamp_percent

REFRESH_SECONDS = 2.0
HISTORY_LEN = 60          # 60 samples x 2 s = ~2 minutes of history
TOP_PROCESSES = 12
PRIME_LABEL = "measuring"  # UI hint: not-yet-known rate

_HISTORY_KEYS = ("t", "cpu", "mem", "net_recv_bps", "net_sent_bps",
                 "disk_read_bps", "disk_write_bps", "gpu_util")

_BUILD_RE = re.compile(r"Build\s+([\d.]+)")


# ---------------------------------------------------------------------------
# Pure delta math (unit-testable without psutil)
# ---------------------------------------------------------------------------
def _safe_div(delta, elapsed):
    if delta is None or elapsed is None or elapsed <= 0:
        return None
    return max(0.0, delta) / elapsed


def valid_elapsed(elapsed) -> float | None:
    """None (first sample) or non-positive/too-large gaps invalidate rates."""
    if elapsed is None or elapsed <= 0:
        return None
    return float(elapsed)


def compute_interface_rates(prev, current, elapsed, skip_names=frozenset()):
    """Bytes/sec per network adapter + totals from two counter snapshots.

    prev/current: {nic_name: (bytes_sent, bytes_recv)}
    First observation of a NIC -> None rates ('measuring'), never a fake 0.
    Counters that reset (negative delta) clamp to 0.0.
    Totals exclude adapters named in skip_names (loopback).
    """
    per_nic = {}
    tot_sent = tot_recv = 0.0
    have_tot = False
    e = valid_elapsed(elapsed)
    for name, cur in (current or {}).items():
        sent_b = recv_b = None
        p = (prev or {}).get(name)
        if p is not None and e is not None:
            sent_b = _safe_div(cur[0] - p[0], e)
            recv_b = _safe_div(cur[1] - p[1], e)
        per_nic[name] = {"sent_bps": sent_b, "recv_bps": recv_b}
        if name in skip_names:
            continue
        if sent_b is not None:
            tot_sent += sent_b
            have_tot = True
        if recv_b is not None:
            tot_recv += recv_b
            have_tot = True
    return {
        "per_nic": per_nic,
        "totals": {"sent_bps": tot_sent if have_tot else None,
                   "recv_bps": tot_recv if have_tot else None},
    }


def compute_disk_io_rates(prev, current, elapsed):
    """Aggregate + per-disk disk I/O rates from two perdisk counter snapshots.

    prev/current: {disk_name: (read_bytes, write_bytes, read_count, write_count)}
    Aggregate covers only disks present in BOTH snapshots, so a drive that
    appears mid-run cannot produce a bogus giant delta.
    """
    e = valid_elapsed(elapsed)
    agg = {"read_bps": None, "write_bps": None,
           "read_iops": None, "write_iops": None}
    per_disk = {}
    common = set(prev or {}) & set(current or {})
    if e is not None and common:
        rb = wb = rc = wc = 0
        for d in common:
            p, c = prev[d], current[d]
            rb += c[0] - p[0]
            wb += c[1] - p[1]
            rc += c[2] - p[2]
            wc += c[3] - p[3]
        agg = {"read_bps": _safe_div(rb, e), "write_bps": _safe_div(wb, e),
               "read_iops": _safe_div(rc, e), "write_iops": _safe_div(wc, e)}
    for name, cur in (current or {}).items():
        p = (prev or {}).get(name)
        if p is None or e is None:
            per_disk[name] = {k: None for k in
                              ("read_bps", "write_bps", "read_iops", "write_iops")}
        else:
            per_disk[name] = {
                "read_bps": _safe_div(cur[0] - p[0], e),
                "write_bps": _safe_div(cur[1] - p[1], e),
                "read_iops": _safe_div(cur[2] - p[2], e),
                "write_iops": _safe_div(cur[3] - p[3], e),
            }
    return {"aggregate": agg, "per_disk": per_disk}


# ---------------------------------------------------------------------------
# psutil adapters -> plain tuples (keeps the pure functions psutil-free)
# ---------------------------------------------------------------------------
def normalize_net_counters(nic_counters):
    try:
        return {name: (int(c.bytes_sent), int(c.bytes_recv))
                for name, c in (nic_counters or {}).items()}
    except AttributeError:
        return {}


def normalize_disk_counters(perdisk_counters):
    try:
        return {name: (int(c.read_bytes), int(c.write_bytes),
                       int(c.read_count), int(c.write_count))
                for name, c in (perdisk_counters or {}).items()}
    except AttributeError:
        return {}


_LOOPBACK_HINTS = ("loopback",)


def _suppress_network_rates(net):
    """Replace computed rates with the 'measuring' state (calibration pass)."""
    out = dict(net)
    out["totals"] = {"sent_bps": None, "recv_bps": None}
    out["adapters"] = [dict(a, sent_bps=None, recv_bps=None)
                       for a in net.get("adapters") or []]
    out["available"] = False
    out["reason"] = "calibrating counter baselines (first sample)"
    return out


def _suppress_disk_rates(io):
    out = dict(io)
    for k in ("read_bps", "write_bps", "read_iops", "write_iops"):
        out[k] = None
    out["per_disk"] = [dict(d, **{k: None for k in
                                  ("read_bps", "write_bps", "read_iops",
                                   "write_iops")})
                       for d in io.get("per_disk") or []]
    out["available"] = False
    out["reason"] = "calibrating counter baselines (first sample)"
    return out


def is_loopback_name(name: str) -> bool:
    n = (name or "").lower()
    return n == "lo" or any(h in n for h in _LOOPBACK_HINTS)


_VIRTUAL_FS_TYPES = {
    "tmpfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2", "nsfs", "bpf",
    "tracefs", "debugfs", "securityfs", "pstore", "efivarfs", "autofs",
    "fusectl", "configfs", "mqueue", "hugetlbfs", "rpc_pipefs", "devpts",
    "overlay", "aufs",
}
_SNAP_FS_TYPES = {"squashfs"}
_IDLE_PROCESS_NAMES = {
    "system idle process",  # Windows
    "idle",                 # some Windows tools
    "swapper/0",            # Linux kernel idle (rarely visible via psutil)
}


def classify_volume(device: str | None, mountpoint: str | None,
                    fstype: str | None) -> str:
    """Classify a mount as physical, snap, or virtual for dashboard filtering."""
    fs = (fstype or "").lower()
    mount = mountpoint or ""
    dev = device or ""
    if fs in _SNAP_FS_TYPES or mount.startswith("/snap/") or \
            (dev.startswith("/dev/loop") and "/snap/" in mount):
        return "snap"
    if fs in _VIRTUAL_FS_TYPES or mount.startswith("/var/lib/docker/") or \
            mount.startswith("/run/docker/"):
        return "virtual"
    if dev.startswith("/dev/loop") and fs and fs not in ("ext4", "xfs", "btrfs",
                                                          "ntfs", "vfat", "exfat",
                                                          "fat32", "fuseblk"):
        return "virtual"
    return "physical"


def classify_adapter(name: str) -> str:
    """Classify a NIC as physical, virtual, overlay, or loopback."""
    if is_loopback_name(name):
        return "loopback"
    n = (name or "").lower()
    if n.startswith(("br-", "veth", "virbr", "cni", "flannel", "weave",
                     "tun", "tap", "docker", "lxc", "podman", "nerdctl")) \
            or n in {"docker0", "lxcbr0", "cni0"} \
            or "vethernet" in n or "hyper-v" in n:
        return "virtual"
    if "tailscale" in n or n.startswith(("wg", "zt", "tailscale")):
        return "overlay"
    return "physical"


def is_idle_process_name(name: str | None) -> bool:
    return (name or "").strip().lower() in _IDLE_PROCESS_NAMES


# ---------------------------------------------------------------------------
# The sampler
# ---------------------------------------------------------------------------
class SystemSampler:
    def __init__(self, gpu_collector=None, refresh_seconds=REFRESH_SECONDS,
                 history_len=HISTORY_LEN, top_processes=TOP_PROCESSES,
                 clock=time.monotonic, sleeper=time.sleep, psutil_module=None):
        self.refresh_seconds = float(refresh_seconds)
        self.history_len = int(history_len)
        self.top_processes = int(top_processes)
        self._clock = clock
        self._sleeper = sleeper
        self.psutil = psutil_module if psutil_module is not None else psutil

        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread = None
        self._samples = 0
        self._last_error = None
        self._started_at = time.time()

        self._hist = {k: deque(maxlen=self.history_len) for k in _HISTORY_KEYS}

        self._prev_net = {}
        self._prev_net_ts = None
        self._prev_disk = {}
        self._prev_disk_ts = None
        self._last_mono = None
        # The window between priming the counter baselines and the first tick
        # is a calibration pass: its delta is not a trustworthy rate (startup
        # bursts, lazy counter init), so the first tick publishes 'measuring'
        # instead of a rate. This removes the initial chart spikes.
        self._calibrate = True

        self._gpu = gpu_collector if gpu_collector is not None else GPUCollector()
        self._gpu_pool = ThreadPoolExecutor(max_workers=1,
                                            thread_name_prefix="gpu-collect")
        self._gpu_future = None
        self._gpu_latest = None

        self._clock_probe = (ProcessorClockProbe()
                             if platform.system() == "Windows" else None)

        self._static_meta = self._collect_static_meta()
        self._snapshot = None  # published JSON-ready snapshot (immutable by rule)

    # -- lifecycle ------------------------------------------------------------
    def start(self):
        if self._thread is not None:
            return
        self._prime()
        self._thread = threading.Thread(target=self._loop,
                                        name="system-sampler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_evt.set()
        self._gpu_pool.shutdown(wait=False, cancel_futures=True)

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def last_sample_age(self) -> float | None:
        if self._last_mono is None:
            return None
        return self._clock() - self._last_mono

    def _prime(self):
        """Set psutil internal baselines so the FIRST tick already yields real
        values (cpu_percent(interval=None) returns 0 on its first call)."""
        try:
            self.psutil.cpu_percent(interval=None)
        except Exception:
            pass
        try:
            self.psutil.cpu_percent(percpu=True, interval=None)
        except Exception:
            pass
        try:
            # Full pass primes each Process object's cpu_percent baseline
            for _ in self.psutil.process_iter(["pid", "name", "cpu_percent",
                                               "memory_percent"]):
                pass
        except Exception:
            pass
        try:
            self._prev_net = normalize_net_counters(
                self.psutil.net_io_counters(pernic=True))
            self._prev_net_ts = self._clock()
        except Exception:
            self._prev_net, self._prev_net_ts = {}, None
        try:
            self._prev_disk = normalize_disk_counters(
                self.psutil.disk_io_counters(perdisk=True))
            self._prev_disk_ts = self._clock()
        except Exception:
            self._prev_disk, self._prev_disk_ts = {}, None
        self._last_mono = self._clock()

    def _loop(self):
        while not self._stop_evt.is_set():
            start = self._clock()
            try:
                self.sample_once()
                self._last_error = None
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
            delay = self.refresh_seconds - (self._clock() - start)
            self._sleeper(max(0.05, delay))

    # -- static meta ------------------------------------------------------------
    def _collect_static_meta(self):
        u = platform.uname()  # cross-platform; the os-module variant does not exist on Windows
        win_ver = ("", "", "", "")
        try:
            win_ver = platform.win32_ver()
        except Exception:
            pass
        release = win_ver[0] or u.release or ""
        build = None
        m = _BUILD_RE.search(u.version or "")
        if m:
            build = m.group(1)
        boot = None
        try:
            boot = float(self.psutil.boot_time())
        except Exception:
            pass
        os_name = " ".join(x for x in (u.system, release) if x)
        return {
            "hostname": u.node or "",
            "os_system": u.system or "",
            "os_release": release,
            "os_name": os_name,
            "os_version": u.version or "",
            "os_build": build,
            "architecture": u.machine or "",
            "processor": u.processor or u.machine or "",
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "boot_time": boot,
            "app_started_epoch": self._started_at,
        }

    # -- per-tick collectors ------------------------------------------------------
    def _collect_cpu(self):
        out = {"percent": None, "per_core": [], "count_logical": None,
               "count_physical": None, "clock_mhz": None, "clock_source": None,
               "clock_note": None}
        try:
            out["percent"] = clamp_percent(self.psutil.cpu_percent(interval=None))
        except Exception:
            pass
        try:
            cores = self.psutil.cpu_percent(percpu=True, interval=None)
            if isinstance(cores, list):
                out["per_core"] = [clamp_percent(c) for c in cores]
        except Exception:
            pass
        try:
            out["count_logical"] = self.psutil.cpu_count()
        except Exception:
            pass
        try:
            out["count_physical"] = self.psutil.cpu_count(logical=False)
        except Exception:
            pass
        out["clock_mhz"], out["clock_source"], out["clock_note"] = \
            self._resolve_clock()
        return out

    def _resolve_clock(self):
        """Layered clock resolution; labels the source honestly.

        1. PDH '% Processor Performance' x base clock -> real effective clock
        2. psutil.cpu_freq().current   (often reports the base clock on Windows)
        3. WMI CurrentClockSpeed (cached PowerShell identity probe)
        4. unavailable
        """
        base_mhz = None
        freq = None
        try:
            freq = self.psutil.cpu_freq()
        except Exception:
            freq = None
        if freq is not None:
            try:
                base_mhz = float(freq.max) if freq.max else None
            except (TypeError, ValueError):
                base_mhz = None
        cpu_identity = None
        if base_mhz is None or self._clock_probe is not None:
            try:
                cpu_identity = self._gpu.get_cpu_identity()
            except Exception:
                cpu_identity = None
            if base_mhz is None and cpu_identity:
                base_mhz = cpu_identity.get("max_clock_mhz")

        if self._clock_probe is not None and base_mhz:
            eff = self._clock_probe.effective_mhz(base_mhz)
            if eff:
                return (eff, "pdh_effective",
                        f"Effective clock ({base_mhz:.0f} MHz base x "
                        f"% Processor Performance).")

        if freq is not None and freq.current:
            try:
                cur = float(freq.current)
                if 50.0 <= cur <= 20000.0:
                    return (cur, "psutil_cpu_freq",
                            "psutil CPU frequency — on Windows this often "
                            "reports the base clock, not the current boost clock.")
            except (TypeError, ValueError):
                pass

        if cpu_identity and cpu_identity.get("current_clock_mhz"):
            if sys.platform.startswith("linux"):
                return (float(cpu_identity["current_clock_mhz"]),
                        "proc_cpuinfo_mhz",
                        "Per-CPU frequency from /proc/cpuinfo (kernel "
                        "reported; may be the requested frequency).")
            return (float(cpu_identity["current_clock_mhz"]),
                    "wmi_current_clock_speed",
                    "Win32_Processor.CurrentClockSpeed — a low-resolution "
                    "value that may not track rapid frequency changes.")

        return None, None, "Clock speed is not exposed on this system."

    def _collect_memory(self):
        out = {"total": None, "used": None, "available": None, "percent": None,
               "swap_total": None, "swap_used": None, "swap_percent": None}
        try:
            vm = self.psutil.virtual_memory()
            out.update(total=vm.total, used=vm.used,
                       available=vm.available, percent=vm.percent)
        except Exception:
            pass
        try:
            sm = self.psutil.swap_memory()
            out.update(swap_total=sm.total, swap_used=sm.used,
                       swap_percent=sm.percent)
        except Exception:
            pass
        return out

    def _collect_volumes(self):
        try:
            parts = self.psutil.disk_partitions(all=False)
        except Exception as exc:
            return {"volumes": [], "primary_volumes": [],
                    "other_volumes": [], "error": f"disk_partitions failed: {exc}"}
        volumes = []
        for p in parts:
            if not p.fstype:  # pseudo mounts
                continue
            kind = classify_volume(p.device, p.mountpoint, p.fstype)
            entry = {"device": p.device, "mountpoint": p.mountpoint,
                     "fstype": p.fstype, "opts": p.opts, "kind": kind,
                     "total": None, "used": None, "free": None,
                     "percent": None, "error": None}
            try:
                u = self.psutil.disk_usage(p.mountpoint)
                entry.update(total=u.total, used=u.used, free=u.free,
                             percent=u.percent)
            except (OSError, PermissionError, ValueError) as exc:
                # empty DVD drive, disconnected network share, ...
                entry["error"] = f"{type(exc).__name__}: {exc}"
            volumes.append(entry)
        primary = [v for v in volumes if v.get("kind") == "physical"]
        other = [v for v in volumes if v.get("kind") != "physical"]
        return {"volumes": volumes, "primary_volumes": primary,
                "other_volumes": other, "error": None}

    def _collect_disk_io(self, elapsed):
        out = {"read_bps": None, "write_bps": None, "read_iops": None,
               "write_iops": None, "total_read_bytes": None,
               "total_write_bytes": None, "per_disk": [], "available": False,
               "elapsed_seconds": valid_elapsed(elapsed),
               "reason": None}
        try:
            counters = self.psutil.disk_io_counters(perdisk=True)
        except Exception as exc:
            out["reason"] = f"disk_io_counters failed: {exc}"
            return out
        if not counters:
            out["reason"] = "No physical disk counters reported by the OS"
            return out
        current = normalize_disk_counters(counters)
        rates = compute_disk_io_rates(self._prev_disk, current, elapsed)
        self._prev_disk = current
        out.update(rates["aggregate"])
        out["available"] = rates["aggregate"]["read_bps"] is not None
        out["per_disk"] = [
            {"device": name, **vals} for name, vals in
            sorted(rates["per_disk"].items())]
        out["total_read_bytes"] = sum(c[0] for c in current.values())
        out["total_write_bytes"] = sum(c[1] for c in current.values())
        return out

    def _collect_network(self, elapsed):
        out = {"adapters": [], "totals": {"sent_bps": None, "recv_bps": None},
               "physical_totals": {"sent_bps": None, "recv_bps": None},
               "total_bytes_sent": None, "total_bytes_recv": None,
               "available": False, "reason": None}
        try:
            counters = self.psutil.net_io_counters(pernic=True)
        except Exception as exc:
            out["reason"] = f"net_io_counters failed: {exc}"
            return out
        if counters is None:
            out["reason"] = "Network counters unavailable"
            return out
        current = normalize_net_counters(counters)
        kinds = {n: classify_adapter(n) for n in current}
        # Exclude loopback + virtual bridges from the headline totals so the
        # dashboard prioritizes physical LAN traffic.
        skip_names = {n for n, kind in kinds.items()
                      if kind in ("loopback", "virtual")}
        rates = compute_interface_rates(self._prev_net, current, elapsed,
                                        skip_names=skip_names)
        self._prev_net = current
        out["totals"] = rates["totals"]
        out["physical_totals"] = rates["totals"]
        # Sort: physical first, then overlay, virtual, loopback
        kind_order = {"physical": 0, "overlay": 1, "virtual": 2, "loopback": 3}
        for name, cur in sorted(current.items(),
                                key=lambda kv: (kind_order.get(kinds[kv[0]], 9),
                                                kv[0])):
            r = rates["per_nic"].get(name, {})
            kind = kinds[name]
            out["adapters"].append({
                "name": name,
                "bytes_sent": cur[0],
                "bytes_recv": cur[1],
                "sent_bps": r.get("sent_bps"),
                "recv_bps": r.get("recv_bps"),
                "is_loopback": kind == "loopback",
                "kind": kind,
            })
        out["available"] = out["totals"]["sent_bps"] is not None \
            or out["totals"]["recv_bps"] is not None
        physical = [a for a in out["adapters"] if a["kind"] == "physical"]
        out["total_bytes_sent"] = sum(a["bytes_sent"] for a in physical)
        out["total_bytes_recv"] = sum(a["bytes_recv"] for a in physical)
        return out

    def _collect_processes(self):
        note = ("Process CPU% is per-core relative (can exceed 100% for "
                "multi-threaded processes). Aggregate CPU% in the header is "
                "system-wide 0–100. Idle/system-idle processes are excluded "
                "from rankings. Memory% is of total RAM.")
        rows = []
        idle_skipped = 0
        try:
            procs = list(self.psutil.process_iter(
                ["pid", "name", "cpu_percent", "memory_percent", "memory_info"]))
        except Exception as exc:
            return {"list": [], "total": None, "error": str(exc), "note": note,
                    "idle_excluded": 0}
        skipped_inaccessible = 0
        for p in procs:
            try:
                info = p.info
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                skipped_inaccessible += 1
                continue
            except Exception:
                skipped_inaccessible += 1
                continue
            name = info.get("name") or "<unknown>"
            if is_idle_process_name(name):
                idle_skipped += 1
                continue
            mi = info.get("memory_info")
            try:
                cpu = info.get("cpu_percent")
                mem = info.get("memory_percent")
                rss = getattr(mi, "rss", None) if mi is not None else None
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                skipped_inaccessible += 1
                continue
            rows.append({
                "pid": info.get("pid"),
                "name": name,
                "cpu_percent": cpu,
                "memory_percent": mem,
                "rss": rss,
            })
        total = len(rows) + idle_skipped + skipped_inaccessible
        rows.sort(key=lambda r: ((r["cpu_percent"] is None,
                                  -(r["cpu_percent"] or 0.0)),
                                 -(r["memory_percent"] or 0.0)))
        if skipped_inaccessible:
            note += (f" {skipped_inaccessible} inaccessible process(es) "
                     "skipped during sampling.")
        return {"list": rows[:self.top_processes], "total": total,
                "error": None, "note": note, "idle_excluded": idle_skipped,
                "inaccessible_skipped": skipped_inaccessible}

    # -- GPU section (non-blocking executor pattern) ----------------------------
    def _collect_gpu_section(self):
        fut = self._gpu_future
        if fut is not None and fut.done():
            self._gpu_future = None
            try:
                self._gpu_latest = fut.result()
            except Exception as exc:
                self._gpu_latest = gpu_error(exc)
        if self._gpu_future is None:
            self._gpu_future = self._gpu_pool.submit(self._gpu.collect)

        latest = self._gpu_latest
        if latest is None:
            section = gpu_placeholder()
        else:
            section = dict(latest)
            collected_at = section.get("collected_at")
            if collected_at:
                section["age_seconds"] = round(time.time() - collected_at, 1)
            # GPU utilization history: honest only when genuinely available
            section["history_available"] = bool(
                section.get("utilization", {}).get("available"))
        return section

    # -- one full tick ------------------------------------------------------------
    def sample_once(self):
        now_mono = self._clock()
        ts = time.time()
        elapsed = None if self._last_mono is None else now_mono - self._last_mono

        cpu = self._collect_cpu()
        memory = self._collect_memory()
        volumes = self._collect_volumes()
        disk_io = self._collect_disk_io(elapsed)
        network = self._collect_network(elapsed)
        if self._calibrate:
            disk_io = _suppress_disk_rates(disk_io)
            network = _suppress_network_rates(network)
        self._calibrate = False
        processes = self._collect_processes()
        gpu = self._collect_gpu_section()

        meta = dict(self._static_meta)
        if meta["boot_time"]:
            meta["uptime_seconds"] = max(0.0, ts - meta["boot_time"])

        # Publish under the lock: append to the rolling deques FIRST, then
        # copy, so the published histories include THIS tick (otherwise the
        # chart's newest point would always lag one sample behind the cards).
        with self._lock:
            self._hist["t"].append(ts)
            self._hist["cpu"].append(cpu["percent"])
            self._hist["mem"].append(memory["percent"])
            self._hist["net_recv_bps"].append(network["totals"]["recv_bps"])
            self._hist["net_sent_bps"].append(network["totals"]["sent_bps"])
            self._hist["disk_read_bps"].append(disk_io["read_bps"])
            self._hist["disk_write_bps"].append(disk_io["write_bps"])
            self._hist["gpu_util"].append(
                gpu["utilization"]["percent"]
                if gpu.get("utilization", {}).get("available") else None)
            histories = {k: list(v) for k, v in self._hist.items()}

        snap = {
            "ts": ts,
            "meta": meta,
            "cpu": cpu,
            "memory": memory,
            "storage": {"volumes": volumes["volumes"],
                        "primary_volumes": volumes.get("primary_volumes") or [],
                        "other_volumes": volumes.get("other_volumes") or [],
                        "volumes_error": volumes["error"],
                        "io": disk_io},
            "network": network,
            "processes": processes,
            "gpu": gpu,
            "histories": histories,
            "refresh_seconds": self.refresh_seconds,
            "history_len": self.history_len,
            "sampler": {
                "samples": self._samples,
                "last_error": self._last_error,
                "started_epoch": self._started_at,
            },
        }

        with self._lock:
            # Publish: the dict handed to snapshot() is replaced wholesale and
            # never mutated afterwards (see snapshot()).
            self._snapshot = snap

        self._last_mono = now_mono
        self._samples += 1
        return snap

    # -- public read path -----------------------------------------------------------
    def snapshot(self):
        """Return the latest cached, JSON-ready snapshot.

        NEVER samples. If called before the first tick, returns a minimal
        well-formed stub so the UI can show its loading state.
        """
        with self._lock:
            snap = self._snapshot
        if snap is not None:
            return snap  # published dicts are immutable by rule — no copy needed
        return self._empty_snapshot()

    def _empty_snapshot(self):
        meta = dict(self._static_meta)
        return {
            "ts": time.time(),
            "meta": meta,
            "cpu": {"percent": None, "per_core": [], "count_logical": None,
                    "count_physical": None, "clock_mhz": None,
                    "clock_source": None, "clock_note": None},
            "memory": {"total": None, "used": None, "available": None,
                       "percent": None, "swap_total": None, "swap_used": None,
                       "swap_percent": None},
            "storage": {"volumes": [], "primary_volumes": [], "other_volumes": [],
                        "volumes_error": None, "io": None},
            "network": {"adapters": [],
                        "totals": {"sent_bps": None, "recv_bps": None},
                        "physical_totals": {"sent_bps": None, "recv_bps": None},
                        "total_bytes_sent": None, "total_bytes_recv": None,
                        "available": False, "reason": "waiting for first sample"},
            "processes": {"list": [], "total": None, "error": None, "note": "",
                          "idle_excluded": 0, "inaccessible_skipped": 0},
            "gpu": gpu_placeholder(),
            "histories": {k: [] for k in _HISTORY_KEYS},
            "refresh_seconds": self.refresh_seconds,
            "history_len": self.history_len,
            "sampler": {"samples": 0, "last_error": None,
                        "started_epoch": self._started_at},
        }

    def health(self):
        return {
            "ok": self.alive,
            "samples": self._samples,
            "last_error": self._last_error,
            "last_sample_age_seconds": self.last_sample_age(),
            "refresh_seconds": self.refresh_seconds,
        }
