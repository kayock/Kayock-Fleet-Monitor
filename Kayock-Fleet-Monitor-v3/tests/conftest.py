"""Pytest fixtures: a fully faked psutil module, a controllable clock and
fake GPU collectors, so the sampler can be exercised deterministically on any
OS (including this Linux CI sandbox)."""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sampler import SystemSampler  # noqa: E402


class FakePc:
    """Stand-in for psutil namedtuples / sdiskusage objects."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeProcess:
    def __init__(self, info):
        self.info = info


class FakePsutil:
    """Mimics exactly the psutil surface the sampler touches."""

    def __init__(self):
        self.boot_epoch = 1_737_200_000.0
        self.cpu_overall = 12.5
        self.cpu_cores = [10.0, 30.0]
        self.logical = 12
        self.physical = 6
        self.mem = FakePc(total=16 * 2**30, used=8 * 2**30,
                          available=8 * 2**30, percent=50.0)
        self.swap = FakePc(total=4 * 2**30, used=1 * 2**30, percent=25.0)
        self.volumes = [
            FakePc(device="C:\\", mountpoint="C:\\", fstype="NTFS", opts="rw fixed"),
            FakePc(device="D:\\", mountpoint="D:\\", fstype="NTFS", opts="rw fixed"),
        ]
        self.usages = {
            "C:\\": FakePc(total=100 * 2**30, used=40 * 2**30,
                           free=60 * 2**30, percent=40.0),
            "D:\\": FakePc(total=50 * 2**30, used=5 * 2**30,
                           free=45 * 2**30, percent=10.0),
        }
        self.raise_on_usage = {}          # mountpoint -> exception
        self.disks = {
            "PhysicalDrive0": FakePc(read_bytes=1_000, write_bytes=2_000,
                                     read_count=10, write_count=20),
        }
        self.nics = {
            "Ethernet": FakePc(bytes_sent=10_000, bytes_recv=20_000),
            "Loopback Pseudo-Interface 1": FakePc(bytes_sent=0, bytes_recv=0),
        }
        self.processes = [
            FakeProcess({"pid": 100, "name": "explorer.exe", "cpu_percent": 2.5,
                         "memory_percent": 1.2,
                         "memory_info": FakePc(rss=10 * 2**20)}),
            FakeProcess({"pid": 200, "name": "chrome.exe", "cpu_percent": 18.0,
                         "memory_percent": 8.4,
                         "memory_info": FakePc(rss=250 * 2**20)}),
        ]
        self.fail_process_iter = None     # exception to raise, or None
        self.fail_net_counters = None
        self.fail_disk_counters = None
        self.fail_cpu_percent = None
        self.fail_cpu_freq = False
        self.calls = {"net_io_counters": 0, "disk_io_counters": 0,
                      "cpu_percent": 0, "process_iter": 0}

    # ---- counters ----
    def advance_net(self, sent=0, recv=0):
        for name in self.nics:
            c = self.nics[name]
            c.bytes_sent += sent
            c.bytes_recv += recv

    def advance_disk(self, read_bytes=0, write_bytes=0, read_count=0, write_count=0):
        for name in self.disks:
            c = self.disks[name]
            c.read_bytes += read_bytes
            c.write_bytes += write_bytes
            c.read_count += read_count
            c.write_count += write_count

    # ---- psutil API ----
    def cpu_percent(self, interval=None, percpu=False):
        self.calls["cpu_percent"] += 1
        if self.fail_cpu_percent:
            raise self.fail_cpu_percent
        return list(self.cpu_cores) if percpu else self.cpu_overall

    def cpu_count(self, logical=True):
        return self.logical if logical else self.physical

    def cpu_freq(self):
        if self.fail_cpu_freq:
            raise RuntimeError("cpu_freq unavailable")
        return FakePc(current=3700.0, min=0.0, max=4600.0)

    def virtual_memory(self):
        return self.mem

    def swap_memory(self):
        return self.swap

    def boot_time(self):
        return self.boot_epoch

    def disk_partitions(self, all=False):
        return list(self.volumes)

    def disk_usage(self, mountpoint):
        if mountpoint in self.raise_on_usage:
            raise self.raise_on_usage[mountpoint]
        return self.usages[mountpoint]

    def disk_io_counters(self, perdisk=True):
        self.calls["disk_io_counters"] += 1
        if self.fail_disk_counters:
            raise self.fail_disk_counters
        return dict(self.disks)

    def net_io_counters(self, pernic=True):
        self.calls["net_io_counters"] += 1
        if self.fail_net_counters:
            raise self.fail_net_counters
        return dict(self.nics)

    def process_iter(self, attrs=None, ad_value=None):
        self.calls["process_iter"] += 1
        if self.fail_process_iter:
            raise self.fail_process_iter
        return list(self.processes)


class FakeClock:
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeGPU:
    """Configurable GPU collector stand-in."""

    def __init__(self, result=None, exc=None):
        self.result = result if result is not None else self.ok_result()
        self.exc = exc
        self.collect_calls = 0

    @staticmethod
    def ok_result(percent=42.5):
        return {
            "status": "ok", "reason": None, "platform": "win32",
            "adapters": [{"name": "Intel(R) UHD Graphics 770",
                          "driver_version": "31.0.101.5333", "driver_date": None,
                          "adapter_ram_bytes": 1073741824,
                          "adapter_ram_capped": False,
                          "adapter_ram_note": "WMI AdapterRAM", "status": "OK",
                          "video_processor": "Intel(R) UHD Graphics Family",
                          "pnp_id": "PCI\\VEN_8086&DEV_4680"}],
            "utilization": {"available": True, "percent": percent,
                            "source": "Windows GPU Engine performance counters (PDH)",
                            "scope": "adapter-specific", "engines_by_type": {"3D": percent},
                            "per_adapter": {"LUID 0x0_0xC35D phys 0": percent},
                            "note": None, "reason": None},
            "memory": {"available": True, "dedicated_bytes": 256 * 2**20,
                       "shared_bytes": 64 * 2**20, "total_adapter_bytes": None,
                       "per_adapter": {"dedicated": {}, "shared": {}},
                       "source": "Windows GPU Adapter Memory performance counters (PDH)",
                       "note": None},
            "temperature": {"available": False, "celsius": None, "source": None,
                            "reason": "Not exposed by Windows for this adapter"},
            "nvidia": {"available": False, "gpus": [], "reason": "no nvidia-smi"},
            "collected_at": 0.0,
        }

    def collect(self):
        self.collect_calls += 1
        if self.exc:
            raise self.exc
        return dict(self.result)

    def get_cpu_identity(self):
        return {"name": "12th Gen Intel(R) Core(TM) i5-12500",
                "current_clock_mhz": 3700, "max_clock_mhz": 4600,
                "physical_cores": 6, "logical_cores": 12}

    def close(self):
        pass


@pytest.fixture
def fake_psutil():
    return FakePsutil()


@pytest.fixture
def fake_clock():
    return FakeClock(start=1000.0)


@pytest.fixture
def sampler(fake_psutil, fake_clock):
    """A sampler fully driven by fakes; sample_once() is called manually."""
    s = SystemSampler(gpu_collector=FakeGPU(), psutil_module=fake_psutil,
                      clock=fake_clock, sleeper=lambda _s: None,
                      refresh_seconds=2.0, history_len=60)
    yield s
    s.stop()


@pytest.fixture
def seeded_sampler(sampler, fake_clock):
    """Sampler with primed baselines and two completed ticks, with a real-time
    pause between them so the GPU executor future is deterministically done."""
    import time
    sampler._prime()
    fake_clock.advance(2.0)
    sampler.sample_once()
    time.sleep(0.05)  # let the single-worker GPU future finish (FakeGPU is instant)
    fake_clock.advance(2.0)
    sampler.sample_once()
    return sampler
