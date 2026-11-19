#!/usr/bin/env bash
# Kayock Fleet Monitor — one-time Linux setup (Ubuntu Station / Ubuntu Portable)
# Creates a local virtual environment and installs Flask + psutil.
# No services, no autostart, no sudo beyond what pip needs inside .venv.
set -e
cd "$(dirname "$0")"

PY=python3
if ! command -v $PY >/dev/null 2>&1; then
  echo "[ERROR] python3 not found. Install it with:"
  echo "        sudo apt install python3 python3-venv"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "[setup] creating virtual environment (.venv)…"
  $PY -m venv .venv
fi

echo "[setup] installing dependencies (Flask, psutil)…"
./.venv/bin/pip install --upgrade pip >/dev/null
./.venv/bin/pip install -r requirements.txt

echo
echo "[setup] done. Start the monitor with:  ./start_linux.sh"
echo "        Optional NVIDIA tools (Ubuntu Portable, for GPU metrics):"
echo "          sudo apt install nvidia-utils-550   # provides nvidia-smi"
echo "        Optional Intel tools (Ubuntu Station):"
echo "          sudo apt install intel-gpu-tools pciutils"
echo "        (both are optional — the dashboard shows honest 'unavailable'"
echo "         states when they are missing)"
