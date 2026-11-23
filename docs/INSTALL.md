# Kayock Fleet Monitor — Installation Guide

Cross-platform system + AI-server monitor for Windows 11 and Ubuntu Linux.
No AI model, cloud service, or external telemetry is required.

## System requirements

| Item | Requirement |
| --- | --- |
| OS | Windows 11, or Ubuntu 22.04+ (other modern Linux likely works) |
| Python | 3.10+ recommended (3.11–3.14 tested in this lab) |
| Disk | ~50 MB application + virtualenv |
| Network | Optional LAN binding for fleet peers on a **trusted** network |

Optional tools (monitoring degrades honestly without them):

- **NVIDIA:** `nvidia-smi` on PATH
- **Intel (Linux):** `intel-gpu-tools` (`intel_gpu_top`) plus perf permissions — see [INTEL_GPU.md](INTEL_GPU.md)
- **Linux PCI names:** `pciutils` (`lspci`)

## Install from GitHub

```bash
git clone https://github.com/kayock/Kayock-Fleet-Monitor.git
cd Kayock-Fleet-Monitor
git checkout fix/ai-lab-reliability   # or master once merged
```

### Linux

```bash
./setup_linux.sh
./start_linux.sh                          # http://127.0.0.1:5000
./start_linux.sh --host 0.0.0.0 --port 5000 --no-browser
```

Optional systemd user or system unit (example — adjust paths/user):

```ini
[Unit]
Description=Kayock Fleet Monitor
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/Kayock-Fleet-Monitor
ExecStart=/path/to/Kayock-Fleet-Monitor/start_linux.sh --host 0.0.0.0 --port 5000 --no-browser
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Do **not** copy another lab’s `fleet.json` / `ai_servers.json` with private addresses into a new install. Configure peers in the UI, or start from `fleet.example.json`.

### Windows

1. Install Python and enable **Add python.exe to PATH**.
2. Extract or clone the repository.
3. Double-click `setup_windows.bat` once.
4. Double-click `start_monitor.bat` → `http://127.0.0.1:5000`.
5. Optional LAN: set `"host": "0.0.0.0"` in `monitor_config.json`, or start with `--host 0.0.0.0`. Allow inbound TCP to the monitor port **from your trusted LAN** only if you intentionally want remote dashboards.

## Configuration files (local, not in Git)

Created next to the app at runtime:

| File | Purpose |
| --- | --- |
| `monitor_config.json` | host, port, open_browser, machine_label |
| `fleet.json` | peer monitors + `allow_control` |
| `ai_servers.json` | optional configured AI servers |

Templates: `fleet.example.json`. All three paths are gitignored.

## Fleet peers

On each machine, open **All Machines → + Add machine** (name, host/IP, monitor port) or use **⟳ Scan LAN** (UDP 51015). Discovery is monitor-only; remote control stays off until you opt in per peer.

## AI server discovery

Discovery observes listening ports and AI-looking processes. It never starts, stops, loads, or unloads models unless you use an explicit control on a **configured** local server (and, for remote peers, only when `allow_control` is enabled).

Supported probe families include Ollama, llama.cpp server, LM Studio, Lemonade, and generic OpenAI-compatible endpoints. Metrics that an API does not expose are shown as unavailable — never invented.

## Updating an existing install

1. Stop the running monitor (Ctrl+C or stop the service).
2. Backup `fleet.json`, `ai_servers.json`, `monitor_config.json`.
3. `git pull` (or copy updated source files over the install, preserving those JSON files and `.venv`).
4. Re-run `setup_*.` only if dependencies changed.
5. Start again and open `/healthz` — confirm `version` and rising `samples`.

## Uninstall

1. Stop/disable any systemd unit or scheduled task.
2. Delete the application directory (and optional `.venv`).
3. Remove any firewall rules you added for the monitor port.

## Troubleshooting

See the main [README.md](../README.md#troubleshooting). Common items:

- First seconds show `measuring…` / `sampler: 0 samples` until the first ticks complete.
- Peer offline: peer must be running and LAN-reachable on its **monitor** port.
- Intel GPU util missing: permissions — [INTEL_GPU.md](INTEL_GPU.md).
