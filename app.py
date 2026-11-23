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
from mcc import (ProfileStore, attach_profiles_to_cards, build_model_cards,
                 build_server_inventory, capabilities_for,
                 classify_pending_for_apply, estimate_memory_warning,
                 fleet_setup_preview, management_setup_preview,
                 merge_startup_args, preview_apply_profile, settings_view)
from sampler import HISTORY_LEN, REFRESH_SECONDS, SystemSampler
from utils import utc_now_iso

_werkzeug_log = logging.getLogger("werkzeug")
_werkzeug_log.setLevel(logging.WARNING)  # avoid a log line every 2 s poll

APP_NAME = "Kayock Fleet Monitor"
APP_VERSION = "3.2.2"


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
        resp = app.make_response(render_template(
            "index.html",
            refresh_ms=int(round(sampler.refresh_seconds * 1000)),
            history_len=sampler.history_len,
            app_version=APP_VERSION,
            app_name=APP_NAME,
            machine_label=machine_label or socket.gethostname(),
            self_port=self_port or 0,
        ))
        # Prevent stale MCC UI after upgrades
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

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
        ok, msg, tag = ai_manager.restart_server(
            server_id,
            force=bool(payload.get("force")),
            model=payload.get("model"))
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

    @app.route("/api/fleet/<machine_id>/ai/<path:action>", methods=["POST", "GET"])
    def fleet_remote_ai(machine_id, action):
        """Proxy an AI management action to a peer. Explicitly refused unless
        the operator enabled allow_control for that machine."""
        payload = request.get_json(silent=True) or {}
        status, data = fleet_manager.control_request(
            machine_id, f"/api/ai/{action}", payload,
            method=request.method)
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

    # ---------------- Model Control Center API ----------------
    profile_store = ProfileStore()

    def _mcc_gather_cards():
        """Build fleet-wide cards and attach saved profiles (read-only)."""
        self_label = machine_label or socket.gethostname()
        local_ai = ai_manager.snapshot()
        local_sys = sampler.snapshot()
        cards = build_model_cards(
            machine=self_label, machine_id=None, allow_control=True,
            is_self=True, servers=local_ai.get("servers") or [],
            memory=local_sys.get("memory"), gpu=local_sys.get("gpu"),
        )
        ops = local_ai.get("ops") or {}
        for c in cards:
            if c.get("server_id") and c["server_id"] in ops:
                c["ops"] = ops[c["server_id"]]

        for m in fleet_manager.list_machines():
            mid = m.get("id")
            wrap = fleet_manager.detail(mid) or {}
            metrics = wrap.get("detail") if isinstance(wrap.get("detail"), dict) else {}
            ai = metrics.get("ai_servers") or {}
            peer_cfg = {c.get("id"): c for c in (ai.get("config") or [])
                        if isinstance(c, dict) and c.get("id")}
            servers = []
            for s in (ai.get("servers") or []):
                s2 = dict(s)
                sid = s2.get("id")
                if sid and sid in peer_cfg and not s2.get("config"):
                    s2["config"] = peer_cfg[sid]
                servers.append(s2)
            cards.extend(build_model_cards(
                machine=m.get("name") or mid, machine_id=mid,
                allow_control=bool(m.get("allow_control")),
                is_self=False, servers=servers,
                memory=metrics.get("memory"), gpu=metrics.get("gpu"),
            ))

        seen = set()
        uniq = []
        for c in cards:
            cid = c.get("id")
            if cid in seen:
                continue
            seen.add(cid)
            uniq.append(c)
        uniq = attach_profiles_to_cards(uniq, profile_store.list_profiles())
        return uniq, self_label, local_sys

    @app.route("/api/mcc/capabilities", methods=["GET"])
    def mcc_capabilities_all():
        engines = request.args.get("engine")
        if engines:
            return jsonify({"ok": True, "data": capabilities_for(engines)})
        from mcc import ENGINE_CAPABILITIES
        return jsonify({"ok": True, "data": {
            k: capabilities_for(k) for k in ENGINE_CAPABILITIES
        }})

    @app.route("/api/mcc/inventory", methods=["GET"])
    def mcc_inventory():
        """Fleet-wide model cards built from local + peer cached snapshots."""
        uniq, self_label, local_sys = _mcc_gather_cards()
        setup = fleet_setup_preview(uniq)
        return jsonify({
            "ok": True,
            "data": {
                "cards": uniq,
                "servers": setup.get("servers") or [],
                "management_summary": setup.get("summary") or {},
                "self_label": self_label,
                "profiles_count": len(profile_store.list_profiles()),
                "ts": local_sys.get("ts") or local_sys.get("collected_at"),
            },
        })

    @app.route("/api/mcc/profiles/sync", methods=["POST"])
    def mcc_profiles_sync():
        """Non-destructive inventory → profile sync. Never mutates engines."""
        cards, _, _ = _mcc_gather_cards()
        summary = profile_store.sync_from_cards(cards)
        cards2, self_label, _ = _mcc_gather_cards()
        return jsonify({
            "ok": True,
            "message": "profiles synchronized",
            "data": {
                "summary": summary,
                "cards": cards2,
                "profiles": profile_store.list_profiles(),
                "self_label": self_label,
            },
        })

    @app.route("/api/mcc/settings/preview", methods=["POST"])
    def mcc_settings_preview():
        body = request.get_json(silent=True) or {}
        pending = body.get("pending") or {}
        view = settings_view(
            engine=body.get("engine"),
            configured_args=body.get("configured_args"),
            live_context=body.get("live_context"),
            live_model=body.get("live_model"),
            pending=pending,
        )
        view["classification"] = classify_pending_for_apply(
            body.get("engine"), pending)
        return jsonify({"ok": True, "data": view})

    @app.route("/api/mcc/settings/apply", methods=["POST"])
    def mcc_settings_apply():
        """Apply pending settings through supported pathways only.

        Modes:
          - save_profile: persist prefs only (no engine mutation)
          - supported: save request-level prefs to the model profile
          - with_restart: merge known startup args into local ai_servers.json
            and optionally restart (requires confirm_restart=true)
        """
        body = request.get_json(silent=True) or {}
        mode = (body.get("mode") or "supported").strip()
        pending = dict(body.get("pending") or {})
        engine = body.get("engine")
        configured_args = body.get("configured_args")
        server_id = body.get("server_id")
        is_self = bool(body.get("is_self", True))
        confirm_restart = bool(body.get("confirm_restart"))
        confirm_disruptive = bool(body.get("confirm_disruptive"))
        protected = bool(body.get("protected"))
        profile_id = body.get("profile_id")
        identity = body.get("identity") or {}

        if protected and mode == "with_restart" and not confirm_disruptive:
            return _json_result(False,
                "Protected model — confirm disruptive restart explicitly "
                "(confirm_disruptive=true).",
                {"needs_confirm": True, "protected": True}, status=409)

        classification = classify_pending_for_apply(engine, pending)
        view = settings_view(
            engine=engine, configured_args=configured_args,
            live_context=body.get("live_context"),
            live_model=body.get("live_model") or body.get("model"),
            pending=pending,
        )
        results = {
            "mode": mode,
            "applied": [],
            "saved": [],
            "pending": [],
            "skipped": [],
            "restart_performed": False,
            "new_args": None,
            "classification": classification,
            "preview": view,
        }

        # Resolve / create profile for this identity
        profile = profile_store.get(profile_id) if profile_id else None
        if not profile and identity.get("key"):
            profile = profile_store.get_by_identity(identity["key"])

        def _persist_prefs(req_prefs, startup_prefs):
            nonlocal profile
            entry = {
                "id": (profile or {}).get("id"),
                "name": (body.get("friendly_name")
                         or (profile or {}).get("friendly_name")
                         or body.get("model") or "model"),
                "friendly_name": (body.get("friendly_name")
                                  or (profile or {}).get("friendly_name")
                                  or body.get("model")),
                "engine": engine or (profile or {}).get("engine"),
                "model": body.get("model") or (profile or {}).get("model"),
                "machine": body.get("machine") or (profile or {}).get("machine"),
                "machine_id": body.get("machine_id") if "machine_id" in body
                    else (profile or {}).get("machine_id"),
                "server_id": server_id or (profile or {}).get("server_id"),
                "identity": identity or (profile or {}).get("identity") or {},
                "request_settings": {
                    **((profile or {}).get("request_settings") or {}),
                    **req_prefs,
                },
                "startup_settings": {
                    **((profile or {}).get("startup_settings") or {}),
                    **startup_prefs,
                },
                "settings": {
                    **((profile or {}).get("settings") or {}),
                    **req_prefs, **startup_prefs,
                },
                "user_customized": True,
                "protected": protected if "protected" in body
                    else bool((profile or {}).get("protected")),
            }
            ok, msg, saved = profile_store.save(entry)
            if ok:
                profile = saved
                for k in req_prefs:
                    results["saved"].append({
                        "parameter": k, "value": req_prefs[k],
                        "as": "request_preference",
                        "note": "Saved to profile — clients/apps must send this per request.",
                    })
                for k in startup_prefs:
                    results["saved"].append({
                        "parameter": k, "value": startup_prefs[k],
                        "as": "startup_preference",
                        "note": "Saved to profile — not live until applied with restart.",
                    })
            else:
                results["skipped"].append({"reason": f"profile save failed: {msg}"})
            return ok

        if mode == "save_profile":
            _persist_prefs(
                classification["request_preferences"],
                classification["startup_settings"])
            for k, meta in classification["unsupported"].items():
                results["skipped"].append({
                    "parameter": k, "reason": meta.get("reason")})
            return jsonify({"ok": True, "message": "profile saved", "data": results})

        if mode == "supported":
            # Persist request prefs; report startup as needing Apply With Restart
            _persist_prefs(classification["request_preferences"], {})
            for k, v in classification["startup_settings"].items():
                results["pending"].append({
                    "parameter": k, "value": v,
                    "reason": "Requires Apply With Restart (startup setting).",
                })
            for k, meta in {**classification["unsupported"],
                            **classification["unknown"]}.items():
                results["skipped"].append({
                    "parameter": k,
                    "reason": meta.get("reason") if isinstance(meta, dict)
                    else "unsupported",
                })
            msg = ("Request preferences saved to profile."
                   if results["saved"]
                   else "No immediately supported changes to apply.")
            if results["pending"]:
                msg += " Some settings need Apply With Restart."
            return jsonify({"ok": True, "message": msg, "data": results})

        if mode == "with_restart":
            if not classification["startup_settings"] and not classification["request_preferences"]:
                return _json_result(False, "nothing to apply", results)
            _persist_prefs(
                classification["request_preferences"],
                classification["startup_settings"])

            if not classification["startup_settings"]:
                return jsonify({
                    "ok": True,
                    "message": "Only request preferences were present — saved to profile; no restart.",
                    "data": results,
                })

            if not is_self:
                results["skipped"].append({
                    "reason": (
                        "Startup argument edits apply only on the machine that "
                        "owns ai_servers.json. Open Model Control on that peer "
                        "(or edit Configure there), then restart."
                    ),
                })
                return jsonify({
                    "ok": False,
                    "message": "Remote startup-arg rewrite is not proxied — do it on the peer.",
                    "data": results,
                }), 400

            if not server_id:
                return _json_result(False,
                    "No configured server_id — adopt/configure the server first.",
                    results)

            cfg = next((c for c in ai_manager.list_config()
                        if c.get("id") == server_id), None)
            if not cfg:
                return _json_result(False, "unknown local server_id", results)

            existing_args = cfg.get("args") or configured_args or ""
            new_args, notes = merge_startup_args(
                existing_args, dict(classification["startup_settings"]), engine)
            results["new_args"] = new_args
            results["arg_notes"] = notes

            if not confirm_restart:
                results["needs_confirm"] = True
                return jsonify({
                    "ok": True,
                    "message": "Preview ready — re-submit with confirm_restart=true to save args"
                               + (" and restart" if body.get("do_restart") else ""),
                    "data": results,
                })

            # Save merged args into existing config (preserve unrelated fields)
            updated = dict(cfg)
            updated["args"] = new_args
            ok, msg, warnings = ai_manager.save_server_config(updated)
            if not ok:
                return _json_result(False, f"failed to save args: {msg}", results)
            for n in notes:
                results["applied"].append({"change": n, "as": "configured_args"})
            results["warnings"] = warnings

            do_restart = bool(body.get("do_restart", True))
            if do_restart:
                rok, rmsg, tag = ai_manager.restart_server(
                    server_id, force=bool(body.get("force")),
                    model=body.get("model"))
                results["restart_performed"] = bool(rok)
                results["restart_message"] = rmsg
                results["restart_detail"] = tag
                if not rok:
                    return jsonify({
                        "ok": False,
                        "message": f"Args saved but restart failed: {rmsg}",
                        "data": results,
                    }), (409 if tag == "busy" else 400)
                return jsonify({
                    "ok": True,
                    "message": "Startup settings saved and server restarted.",
                    "data": results,
                })
            return jsonify({
                "ok": True,
                "message": "Startup settings saved to ai_servers.json (restart not requested).",
                "data": results,
            })

        return _json_result(False, f"unknown mode {mode!r}", results)

    @app.route("/api/mcc/memory-warning", methods=["POST"])
    def mcc_memory_warning():
        body = request.get_json(silent=True) or {}
        warn = estimate_memory_warning(
            model_size_bytes=body.get("model_size_bytes"),
            available_ram_bytes=body.get("available_ram_bytes"),
            available_vram_bytes=body.get("available_vram_bytes"),
            context_tokens=body.get("context_tokens"),
        )
        return jsonify({"ok": True, "data": warn})

    def _mcc_register_server(*, server_key: str, is_self: bool,
                             machine_id: str | None = None) -> tuple[bool, str, dict]:
        """Register (adopt) one discovered server locally or via peer proxy.

        Never starts/stops/loads/unloads. Preserves existing peer/local config.
        """
        if not server_key:
            return False, "server_key is required", {}
        if is_self or not machine_id:
            ok, msg, warnings = ai_manager.adopt_server(server_key)
            # Immediate local verify via snapshot key match
            snap = ai_manager.snapshot()
            verified = False
            server_id = None
            for s in snap.get("servers") or []:
                if s.get("key") == server_key and s.get("configured"):
                    verified = True
                    server_id = s.get("id")
                    break
            for c in snap.get("config") or []:
                if f"{c.get('host')}:{c.get('port')}" == server_key or c.get("id") == server_id:
                    server_id = c.get("id")
                    verified = True
            return ok and verified, msg if ok else msg, {
                "warnings": warnings, "verified": verified,
                "server_id": server_id, "scope": "local",
                "inference_changed": False,
            }
        # Remote — requires existing allow_control; peer writes its own config
        status, data = fleet_manager.control_request(
            machine_id,
            f"/api/ai/servers/{server_key}/adopt",
            {},
            method="POST",
        )
        if status == 403:
            return False, (data or {}).get("message") or "remote control denied", {
                "scope": "remote", "http_status": status, "inference_changed": False,
            }
        ok = bool((data or {}).get("ok"))
        msg = (data or {}).get("message") or f"peer HTTP {status}"
        # Ask peer to refresh discovery, then refresh our cache and verify
        fleet_manager.control_request(
            machine_id, "/api/ai/discovery/refresh", {}, method="POST")
        try:
            fleet_manager.poll_now()
        except Exception:
            fleet_manager.request_poll()
        verified = False
        server_id = None
        wrap = fleet_manager.detail(machine_id) or {}
        metrics = wrap.get("detail") if isinstance(wrap.get("detail"), dict) else {}
        ai = metrics.get("ai_servers") or {}
        for s in ai.get("servers") or []:
            if s.get("key") == server_key and s.get("configured"):
                verified = True
                server_id = s.get("id")
                break
        for c in ai.get("config") or []:
            if isinstance(c, dict) and (
                    f"{c.get('host')}:{c.get('port')}" == server_key
                    or c.get("id") == server_id):
                verified = True
                server_id = c.get("id") or server_id
        if ok and not verified:
            # Peer accepted but cache may lag — still report partial success
            return True, msg + " (accepted; verification pending next fleet poll)", {
                "warnings": ((data or {}).get("data") or {}).get("warnings"),
                "verified": False, "server_id": server_id, "scope": "remote",
                "http_status": status, "inference_changed": False,
            }
        return ok and verified, msg, {
            "warnings": ((data or {}).get("data") or {}).get("warnings"),
            "verified": verified, "server_id": server_id, "scope": "remote",
            "http_status": status, "inference_changed": False,
        }

    @app.route("/api/mcc/fleet-setup/preview", methods=["GET", "POST"])
    def mcc_fleet_setup_preview():
        cards, self_label, _ = _mcc_gather_cards()
        data = fleet_setup_preview(cards)
        data["self_label"] = self_label
        return jsonify({"ok": True, "data": data})

    @app.route("/api/mcc/fleet-setup/apply", methods=["POST"])
    def mcc_fleet_setup_apply():
        """Register explicitly selected servers. No inference process changes."""
        body = request.get_json(silent=True) or {}
        if not bool(body.get("confirm")):
            return _json_result(False, "confirm=true is required", status=400)
        selections = body.get("servers") or body.get("selected") or []
        if not isinstance(selections, list) or not selections:
            return _json_result(False, "servers list is required")
        cards, _, _ = _mcc_gather_cards()
        preview = fleet_setup_preview(cards)
        by_id = {s["id"]: s for s in preview["servers"]}
        results = []
        for raw in selections:
            if isinstance(raw, str):
                match = by_id.get(raw)
            else:
                sid = raw.get("server_id") or raw.get("id")
                match = by_id.get(sid) if sid else None
                if match is None and raw.get("server_key"):
                    match = next(
                        (s for s in preview["servers"]
                         if s.get("server_key") == raw.get("server_key")
                         and bool(s.get("is_self")) == bool(raw.get("is_self", s.get("is_self")))
                         and (raw.get("machine_id") in (None, s.get("machine_id"))
                              or s.get("is_self"))),
                        None)
            if not match:
                results.append({"ok": False, "message": "unknown server selection",
                                "inference_changed": False})
                continue
            if not match.get("eligible_for_registration"):
                results.append({
                    "server_id": match.get("id"),
                    "server_key": match.get("server_key"),
                    "machine": match.get("machine"),
                    "engine": match.get("engine"),
                    "ok": False,
                    "message": match.get("registration_blocked_reason")
                               or "not eligible for registration",
                    "inference_changed": False,
                })
                continue
            ok, msg, detail = _mcc_register_server(
                server_key=match.get("server_key"),
                is_self=bool(match.get("is_self")),
                machine_id=match.get("machine_id"),
            )
            results.append({
                "server_id": match.get("id"),
                "server_key": match.get("server_key"),
                "machine": match.get("machine"),
                "engine": match.get("engine"),
                "ok": ok, "message": msg, **detail,
            })

        # Refresh inventory after registrations
        ai_manager.request_refresh()
        fleet_manager.request_poll()
        try:
            fleet_manager.poll_now()
        except Exception:
            pass
        cards2, _, _ = _mcc_gather_cards()
        after = fleet_setup_preview(cards2)
        succeeded = sum(1 for r in results if r.get("ok"))
        return jsonify({
            "ok": succeeded == len(results) and len(results) > 0,
            "message": f"registered {succeeded}/{len(results)} server(s)",
            "data": {
                "results": results,
                "summary_after": after.get("summary"),
                "servers_after": after.get("servers"),
                "inference_changed": False,
            },
        })

    @app.route("/api/mcc/setup/preview", methods=["POST"])
    def mcc_setup_preview():
        body = request.get_json(silent=True) or {}
        card = body.get("card") or body
        return jsonify({"ok": True, "data": management_setup_preview(card)})

    @app.route("/api/mcc/setup/apply", methods=["POST"])
    def mcc_setup_apply():
        """Execute an explicitly approved setup action (register/adopt)."""
        body = request.get_json(silent=True) or {}
        action = (body.get("action") or "").strip()
        confirm = bool(body.get("confirm"))
        if not confirm:
            return _json_result(False, "confirm=true is required", status=400)
        if action in ("adopt_server", "register_server"):
            key = body.get("server_key")
            is_self = bool(body.get("is_self", True))
            mid = body.get("machine_id")
            ok, msg, detail = _mcc_register_server(
                server_key=key, is_self=is_self, machine_id=mid)
            ai_manager.request_refresh()
            fleet_manager.request_poll()
            return _json_result(ok, msg, detail)
        if action == "enable_allow_control":
            mid = body.get("machine_id")
            if not mid:
                return _json_result(False, "machine_id is required")
            # Never auto-enable — require explicit enable_allow_control flag
            if not bool(body.get("enable_allow_control")):
                return _json_result(
                    False,
                    "Refusing to enable remote control implicitly. "
                    "Pass enable_allow_control=true after operator confirmation, "
                    "or use Fleet machine Configure.",
                    status=400)
            ok, msg = fleet_manager.set_control(mid, True)
            return _json_result(ok, msg)
        return _json_result(False, f"unsupported setup action {action!r}")

    @app.route("/api/mcc/profiles", methods=["GET"])
    def mcc_profiles_list():
        return jsonify({
            "ok": True,
            "data": {
                "profiles": profile_store.list_profiles(),
                "templates": profile_store.templates(),
            },
        })

    @app.route("/api/mcc/profiles", methods=["POST"])
    def mcc_profiles_save():
        body = request.get_json(silent=True) or {}
        ok, msg, profile = profile_store.save(body)
        return _json_result(ok, msg, profile)

    @app.route("/api/mcc/profiles/<profile_id>", methods=["PATCH"])
    def mcc_profiles_patch(profile_id):
        """Patch user-facing fields (friendly name, tags, protected, notes…)."""
        body = request.get_json(silent=True) or {}
        ok, msg, profile = profile_store.patch_user_fields(profile_id, body)
        return _json_result(ok, msg, profile)

    @app.route("/api/mcc/profiles/<profile_id>", methods=["DELETE"])
    def mcc_profiles_delete(profile_id):
        ok, msg = profile_store.delete(profile_id)
        return _json_result(ok, msg)

    @app.route("/api/mcc/profiles/<profile_id>/duplicate", methods=["POST"])
    def mcc_profiles_duplicate(profile_id):
        body = request.get_json(silent=True) or {}
        ok, msg, profile = profile_store.duplicate(profile_id, body.get("name"))
        return _json_result(ok, msg, profile)

    @app.route("/api/mcc/profiles/<profile_id>/preview", methods=["POST"])
    def mcc_profiles_preview(profile_id):
        profile = profile_store.get(profile_id)
        if not profile:
            return _json_result(False, "unknown profile id")
        body = request.get_json(silent=True) or {}
        view = preview_apply_profile(
            profile,
            engine=body.get("engine") or profile.get("engine"),
            configured_args=body.get("configured_args"),
            live_context=body.get("live_context"),
            live_model=body.get("live_model"),
        )
        return jsonify({"ok": True, "data": view})

    @app.route("/api/mcc/profiles/export", methods=["GET"])
    def mcc_profiles_export():
        return jsonify({"ok": True, "data": profile_store.export_all()})

    @app.route("/api/mcc/profiles/import", methods=["POST"])
    def mcc_profiles_import():
        body = request.get_json(silent=True) or {}
        ok, msg, count = profile_store.import_all(
            body, merge=bool(body.get("merge", True)))
        return _json_result(ok, msg, {"imported": count})

    return app


def main(host="127.0.0.1", port=5000, debug=False):
    app = create_app(self_port=port)
    app.run(host=host, port=port, debug=debug, threaded=True,
            use_reloader=False)


if __name__ == "__main__":
    from run_monitor import cli_main  # reuses port picking / browser handling
    cli_main()
