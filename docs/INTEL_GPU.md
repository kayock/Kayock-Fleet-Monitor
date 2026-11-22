# Intel GPU utilization — permissions note

On Linux hosts with i915/xe integrated GPUs, Kayock Fleet Monitor reads:

| Metric | Source | Privileges |
| --- | --- | --- |
| Adapter identity | sysfs / `lspci` | Normal user |
| GT frequency | `/sys/class/drm/card*/gt*_freq_mhz` | Normal user |
| Engine busy / utilization | `intel_gpu_top` (perf PMU) | Elevated |
| Dedicated VRAM | — | **Not exposed** for typical iGPU shared memory |

## Why utilization shows unavailable

`intel_gpu_top -J` needs performance-monitoring access. When
`kernel.perf_event_paranoid` is `4` (common on Ubuntu) and the service runs
as a normal user without `CAP_PERFMON`, the tool fails with
`Permission denied`. The dashboard then reports **unavailable** with an
explicit reason — it does **not** invent a percentage.

This host’s DRM `engine/*/busy` sysfs nodes were also absent, so there is no
unprivileged busy counter fallback.

## Narrowly scoped options (approval required before applying)

Do **not** apply these without operator approval. They change host security.

### Option A — capability on `intel_gpu_top` only

```bash
sudo apt install intel-gpu-tools
sudo setcap cap_perfmon+ep "$(command -v intel_gpu_top)"
```

Verify: `intel_gpu_top -J -s 1000 -n 1 -o -` as the monitor user.

### Option B — ambient capability on the Fleet Monitor service

Add to the systemd unit (example):

```ini
AmbientCapabilities=CAP_PERFMON
CapabilityBoundingSet=CAP_PERFMON
```

Then `daemon-reload` and restart the unit. Prefer Option A when it works,
because it limits elevation to the GPU tool.

### Option C — lower `perf_event_paranoid` (broader)

```bash
# temporary
sudo sysctl -w kernel.perf_event_paranoid=2
```

Persistent sysctl changes affect the whole machine and need explicit approval.

## What we will not do automatically

- Grant broad root to the monitor process
- Change firewall rules
- Install unverified third-party GPU tools
- Fabricate utilization numbers when counters are inaccessible
