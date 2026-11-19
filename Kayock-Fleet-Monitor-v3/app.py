"""Kayock Fleet Monitor v3 — cross-platform (Windows/Linux) Flask dashboard
for system metrics, AI model servers and three-machine fleet monitoring.

* Binds to 127.0.0.1 by default; LAN binding is an explicit operator choice
  (monitor_config.json / --host 0.0.0.0 / start script).
* GET /          — the dashboard page (constants injected via Jinja).
* GET /metrics   — the thread-safe CACHED snapshots, merged: system metrics
  from the single background sampler thread, AI-server sections from the
  bounded AI discovery workers, fleet sections from the fleet poller. No
  handler ever samples counters or touches baselines.
* GET /healthz   — liveness for scripting/tests.
* POST/DELETE /api/ai/... — explicit user-driven management controls that
  operate ONLY on configured local servers (never guessed processes).
* GET/POST/DELETE /api/fleet/... — fleet configuration; /api/fleet/<id>/detail
  returns the last-known-good full snapshot of that machine; remote CONTROL
  actions are proxied via /api/fleet/<id>/ai/... and are refused unless the
  operator explicitly enabled allow_control for that machine.
* LAN discovery: UDP broadcast announcements (port 51015) let instances find
  each other; discovering a peer changes nothing on it.

This app intentionally has no AI model, no external service and no cloud
connection: after installation it runs fully offline.
"""
from __future__ import annotations

import atexit
import logging
import socket

from flask import Flask, jsonify, render_template, request

from ai import AIManager
from fleet import FleetManager
from sampler import HISTORY_LEN, REFRESH_SECONDS, SystemSampler
from utils import utc_now_iso

_werkzeug_log = logging.getLogger("werkzeug")
_werkzeug_log.setLevel(logging.WARNING)  # avoid a log line every 2 s poll

APP_NAME = "Kayock Fleet Monitor"
APP_VERSION = "3.0.0"


def _json_result(ok, message, data=None, status=200):
    payload = {"ok": bool(ok), "message": message}
    if data is not None:
        payload["data"] = data
    return jsonify(payload), (status if ok else (status if status != 200 else 400))


def create_app(sampler: SystemSampler | None = None,
               ai_manager: AIManager | None = None,
               fleet_manager: FleetManager | None = None,
               start_sampler: bool = True,
               start_ai: bool = True,
               start_fleet: bool = True,
               self_port: int | None = None,
               machine_label: str | None = None) -> Flask:
    """App factory. Managers may be injected (tests pass start_*=False)."""
    if sampler is None:
        sampler = SystemSampler()
    if ai_manager is None:
        ai_manager = AIManager()
    if fleet_manager is None:
        fleet_manager = FleetManager(self_port=self_port,
                                     machine_label=machine_label)
    if start_sampler and not sampler.alive:
        sampler.start()
    if start_ai and not ai_manager.alive:
        ai_manager.start()
    if start_fleet and not fleet_manager.alive:
        fleet_manager.start()
    atexit.register(sampler.stop)
    atexit.register(ai_manager.stop)
    atexit.register(fleet_manager.stop)

    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False

    # ---------------- dashboard + snapshots ----------------
    @app.route("/")
    def index():
        return render_template(
            "index.html",
            refresh_ms=int(round(sampler.refresh_seconds * 1000)),
            history_len=sampler.history_len,
            app_version=APP_VERSION,
            app_name=APP_NAME,
            machine_label=machine_label or socket.gethostname(),
            self_port=self_port or 0,
        )

    @app.route("/metrics")
    def metrics():
        # Cached snapshots only — no psutil calls, no baseline mutation.
        data = sampler.snapshot()
        try:
            data["ai_servers"] = ai_manager.snapshot()
        except Exception as exc:
            data["ai_servers"] = {"servers": [], "config": [], "ops": {},
                                  "discovery": {"last_error": f"{type(exc).__name__}: {exc}"},
                                  "error": True}
        data["fleet"] = fleet_manager.snapshot()
        data["instance"] = {"label": machine_label or socket.gethostname(),
                            "hostname": socket.gethostname(),
                            "port": self_port or 0,
                            "app_version": APP_VERSION,
                            "app_name": APP_NAME}
        resp = jsonify(data)
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

    @app.route("/healthz")
    def healthz():
        info = sampler.health()
        info["ts_utc"] = utc_now_iso()
        info["version"] = APP_VERSION
        info["app_name"] = APP_NAME
        info["ai_discovery_alive"] = ai_manager.alive
        info["fleet_alive"] = fleet_manager.alive
        status = 200 if info["ok"] else 503
        return jsonify(info), status

    # ---------------- AI server management API (local) ----------------
    @app.route("/api/ai/config", methods=["GET"])
    def ai_config_list():
        return jsonify({"ok": True, "data": ai_manager.list_config()})

    @app.route("/api/ai/servers", methods=["POST"])
    def ai_config_save():
        payload = request.get_json(silent=True) or {}
        ok, msg, warnings = ai_manager.save_server_config(payload)
        return _json_result(ok, msg, {"warnings": warnings})

    @app.route("/api/ai/servers/<server_id>", methods=["DELETE"])
    def ai_config_delete(server_id):
        ok, msg, _ = ai_manager.delete_server_config(server_id)
        return _json_result(ok, msg)

    @app.route("/api/ai/servers/<server_id>/logs", methods=["GET"])
    def ai_server_logs(server_id):
        return jsonify({"ok": True, "data": ai_manager.server_logs(server_id)})

    @app.route("/api/ai/servers/<server_id>/models", methods=["GET"])
    def ai_server_models(server_id):
        ok, models, msg = ai_manager.list_model_files(server_id)
        return _json_result(ok, msg or "ok", models)

    @app.route("/api/ai/servers/<server_id>/start", methods=["POST"])
    def ai_server_start(server_id):
        payload = request.get_json(silent=True) or {}
        ok, msg, tag = ai_manager.start_server(server_id, model=payload.get("model"))
        return _json_result(ok, msg, {"detail": tag} if tag else None)

    @app.route("/api/ai/servers/<server_id>/stop", methods=["POST"])
    def ai_server_stop(server_id):
        payload = request.get_json(silent=True) or {}
        ok, msg, tag = ai_manager.stop_server(server_id, force=bool(payload.get("force")))
        status = 409 if tag == "busy" else 200
        return _json_result(ok, msg, {"detail": tag} if tag else None, status=status)

    @app.route("/api/ai/servers/<server_id>/restart", methods=["POST"])
    def ai_server_restart(server_id):
        payload = request.get_json(silent=True) or {}
        ok, msg, tag = ai_manager.restart_server(server_id,
                                                 force=bool(payload.get("force")))
        status = 409 if tag == "busy" else 200
        return _json_result(ok, msg, {"detail": tag} if tag else None, status=status)

    @app.route("/api/ai/servers/<server_id>/load", methods=["POST"])
    def ai_server_load(server_id):
        payload = request.get_json(silent=True) or {}
        ok, msg, tag = ai_manager.load_model(server_id, payload.get("model"))
        status = 501 if tag == "unsupported" else 200
        return _json_result(ok, msg, {"detail": tag} if tag else None, status=status)

    @app.route("/api/ai/servers/<server_id>/unload", methods=["POST"])
    def ai_server_unload(server_id):
        payload = request.get_json(silent=True) or {}
        ok, msg, tag = ai_manager.unload_model(server_id, payload.get("model"))
        status = 501 if tag == "unsupported" else 200
        return _json_result(ok, msg, {"detail": tag} if tag else None, status=status)

    @app.route("/api/ai/servers/<server_id>/adopt", methods=["POST"])
    def ai_server_adopt(server_id):
        # server_id here is the discovery KEY (host:port / proc:pid)
        ok, msg, warnings = ai_manager.adopt_server(server_id)
        return _json_result(ok, msg, {"warnings": warnings})

    @app.route("/api/ai/discovery/refresh", methods=["POST"])
    def ai_discovery_refresh():
        ai_manager.request_refresh()
        return jsonify({"ok": True, "message": "discovery refresh requested"})

    # ---------------- fleet API ----------------
    @app.route("/api/fleet", methods=["GET"])
    def fleet_list():
        return jsonify({"ok": True, "data": fleet_manager.list_machines()})

    @app.route("/api/fleet", methods=["POST"])
    def fleet_save():
        payload = request.get_json(silent=True) or {}
        ok, msg = fleet_manager.save_machine(payload)
        return _json_result(ok, msg)

    @app.route("/api/fleet/<machine_id>", methods=["DELETE"])
    def fleet_delete(machine_id):
        ok, msg = fleet_manager.delete_machine(machine_id)
        return _json_result(ok, msg)

    @app.route("/api/fleet/<machine_id>/detail", methods=["GET"])
    def fleet_detail(machine_id):
        data = fleet_manager.detail(machine_id)
        if not data:
            return (jsonify({"ok": False,
                             "message": "no cached detail for this machine "
                                        "yet"}), 404)
        return jsonify({"ok": True, "data": data})

    @app.route("/api/fleet/<machine_id>/control", methods=["POST"])
    def fleet_control_toggle(machine_id):
        payload = request.get_json(silent=True) or {}
        ok, msg = fleet_manager.set_control(machine_id,
                                            bool(payload.get("enabled")))
        return _json_result(ok, msg)

    @app.route("/api/fleet/<machine_id>/ai/<path:action>", methods=["POST"])
    def fleet_remote_ai(machine_id, action):
        """Proxy an AI management action to a peer. Explicitly refused unless
        the operator enabled allow_control for that machine."""
        payload = request.get_json(silent=True) or {}
        status, data = fleet_manager.control_request(
            machine_id, f"/api/ai/{action}", payload)
        return jsonify({"ok": bool(data.get("ok")), "message": data.get("message"),
                        "data": data.get("data")}), status

    @app.route("/api/fleet/discovered", methods=["GET"])
    def fleet_discovered():
        return jsonify({"ok": True, "data": fleet_manager.discovered_peers(),
                        "discovery": fleet_manager.discovery_state()})

    @app.route("/api/fleet/discovered/<ip>/add", methods=["POST"])
    def fleet_discovered_add(ip):
        ok, msg = fleet_manager.add_discovered_peer(ip)
        return _json_result(ok, msg)

    return app


def main(host="127.0.0.1", port=5000, debug=False):
    app = create_app(self_port=port)
    app.run(host=host, port=port, debug=debug, threaded=True,
            use_reloader=False)


if __name__ == "__main__":
    from run_monitor import cli_main  # reuses port picking / browser handling
    cli_main()
