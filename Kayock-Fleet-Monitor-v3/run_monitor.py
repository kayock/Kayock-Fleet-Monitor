#!/usr/bin/env python3
"""Launcher for Kayock Fleet Monitor (Windows + Linux).

Picks the first free port starting at 5000, optionally opens the browser and
runs the Flask server. Press Ctrl+C to stop.

Configuration (monitor_config.json, created on first run):
  { "host": "127.0.0.1",       <- change to "0.0.0.0" to expose on the LAN
    "port": 5000,              <- 0 = auto-pick first free port
    "open_browser": true,
    "machine_label": "" }      <- friendly name shown in fleet tabs
CLI flags override the config file.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import webbrowser

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
PORT_TRIES = 20
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "monitor_config.json")


def load_config():
    cfg = {"host": DEFAULT_HOST, "port": 0, "open_browser": True,
           "machine_label": ""}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in cfg})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError:
        pass


def pick_free_port(host: str, start: int, tries: int = PORT_TRIES) -> int:
    """First bindable port in [start, start+tries). Raises SystemExit if none."""
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(
        f"[ERROR] No free port found in range {start}-{start + tries - 1}. "
        f"Free a port or pass --port explicitly.")


def cli_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Kayock Fleet Monitor (local system + AI servers + fleet)")
    parser.add_argument("--host", default=None,
                        help=f"bind address; default from monitor_config.json "
                             f"({DEFAULT_HOST} — use 0.0.0.0 for LAN access)")
    parser.add_argument("--port", type=int, default=None,
                        help=f"default: {DEFAULT_PORT}, auto-increments when "
                             f"occupied (0 = auto in config)")
    parser.add_argument("--name", default=None,
                        help="friendly machine label shown in fleet tabs")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not auto-open the dashboard in a browser")
    parser.add_argument("--no-discovery", action="store_true",
                        help="disable UDP LAN discovery")
    parser.add_argument("--debug", action="store_true",
                        help="verbose Flask debugging (development only)")
    args = parser.parse_args(argv)

    cfg = load_config()
    host = args.host or cfg.get("host") or DEFAULT_HOST
    machine_label = args.name or cfg.get("machine_label") or None
    open_browser = (not args.no_browser) and bool(cfg.get("open_browser", True))
    configured_port = args.port if args.port is not None \
        else int(cfg.get("port") or 0)

    port = configured_port if configured_port > 0 \
        else pick_free_port(host, DEFAULT_PORT)

    # Import here so --help works without the heavy modules and so that
    # app.py's __main__ block does not recurse.
    from app import create_app
    app = create_app(self_port=port, machine_label=machine_label)

    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    print("=" * 64)
    print("  Kayock Fleet Monitor v3 (system + AI servers + fleet)")
    print(f"  Dashboard : {url}" + ("   (LAN: also reachable via this "
                                        "machine's IP)" if host == "0.0.0.0" else ""))
    print(f"  Machine   : {machine_label or socket.gethostname()}")
    print(f"  Refresh   : every 2 s   |  Fleet AI poll: every 10 s")
    print("  Stop      : press Ctrl+C in this window")
    print("=" * 64)

    if open_browser:
        threading.Timer(1.5, webbrowser.open, [url]).start()

    try:
        app.run(host=host, port=port, debug=args.debug, threaded=True,
                use_reloader=False)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    except OSError as exc:
        print(f"\n[ERROR] Could not bind {host}:{port}: {exc}\n"
              f"        Try: python run_monitor.py --port 5050")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli_main())
