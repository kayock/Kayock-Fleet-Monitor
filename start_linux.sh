#!/usr/bin/env bash
# Kayock Fleet Monitor — start on Linux (Ubuntu Station / Ubuntu Portable)
# Opens the dashboard in your default browser at http://127.0.0.1:5000.
#
# LAN access (so the other machines can open THIS monitor):
#   ./start_linux.sh --host 0.0.0.0
# Friendly fleet-tab name:
#   ./start_linux.sh --name "Ubuntu Station"
# Extra flags pass through to run_monitor.py (--port, --no-browser, …).
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "[start] no .venv yet — running setup first…"
  ./setup_linux.sh
fi

exec ./.venv/bin/python run_monitor.py "$@"
