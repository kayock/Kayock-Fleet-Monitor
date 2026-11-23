"""FOXAI AI Lab Command Center — Station-local dashboard.

Aggregates Fleet Monitor discovery, provides direct chat/compare against
verified endpoints, and hosts the Kayock QR Toolkit (generate + scan).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from flask import (Flask, Response, jsonify, request, send_from_directory,
                   stream_with_context)

import chat_proxy
import discovery

ROOT = Path(__file__).resolve().parent
QR_SRC = Path("/home/kayock/Projects/Kayock-QR-Generator/src")
APP_NAME = "FOXAI AI Lab Command Center"
APP_VERSION = "1.3.1"


def _parse_max_tokens(body: dict):
    """None = model/client default. Reject invalid custom values."""
    if "max_tokens" not in body:
        return None, None
    return chat_proxy.normalize_max_tokens(body.get("max_tokens"))


def _parse_timeouts(body: dict) -> dict | None:
    raw = body.get("timeouts")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    return raw

app = Flask(__name__, static_folder=str(ROOT / "static"),
            template_folder=str(ROOT / "templates"))


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "app": APP_NAME, "version": APP_VERSION,
                    "ts": time.time(),
                    "timeouts": chat_proxy.resolve_timeouts()})


@app.get("/api/settings/timeouts")
def api_timeout_settings():
    """Read-only view of effective timeout defaults (env + built-in)."""
    return jsonify({"ok": True, "data": chat_proxy.resolve_timeouts(),
                    "notes": {
                        "connect": "Time to establish TCP/HTTP connection",
                        "first_token": "Time allowed before first stream chunk",
                        "read_inactivity": "Max idle time between stream chunks",
                        "total": "Optional overall wall-clock limit (null = none for streaming)",
                        "nonstream_total": "Total timeout for non-streaming requests (default 90s)",
                    }})


@app.get("/api/servers")
def api_servers():
    return jsonify({"ok": True, "data": discovery.discover_servers()})


@app.get("/api/homepage-links")
def api_homepage_links():
    return jsonify({"ok": True, "data": discovery.homepage_links()})


@app.get("/api/models")
def api_models():
    endpoint = request.args.get("endpoint", "").strip()
    engine = request.args.get("engine", "").strip()
    if not endpoint:
        return jsonify({"ok": False, "error": "endpoint required"}), 400
    return jsonify(chat_proxy.list_loaded_models(endpoint, engine))


@app.post("/api/chat")
def api_chat():
    body = request.get_json(silent=True) or {}
    endpoint = (body.get("endpoint") or "").strip()
    engine = (body.get("engine") or "").strip()
    model = (body.get("model") or "").strip()
    messages = body.get("messages") or []
    if not endpoint or not model or not messages:
        return jsonify({"ok": False, "error": "endpoint, model, messages required"}), 400
    max_tokens, mt_err = _parse_max_tokens(body)
    if mt_err:
        return jsonify({"ok": False, "error": mt_err}), 400
    timeouts = _parse_timeouts(body)
    # Streaming?
    if body.get("stream"):
        def generate():
            try:
                info = chat_proxy.list_loaded_models(endpoint, engine)
                loaded_ids = {m["id"] for m in info.get("loaded") or []}
                if info.get("engine") == "ollama":
                    if not loaded_ids:
                        yield (json.dumps({
                            "ok": False,
                            "error": "No models are currently loaded on this Ollama server."
                        }) + "\n").encode()
                        return
                    if model not in loaded_ids:
                        yield (json.dumps({
                            "ok": False,
                            "error": f"Model {model!r} is not currently loaded."
                        }) + "\n").encode()
                        return
                    for chunk in chat_proxy.stream_ollama_chat(
                            endpoint=endpoint, model=model, messages=messages,
                            think=body.get("think", "auto"),
                            max_tokens=max_tokens,
                            timeouts=timeouts):
                        yield chunk
                else:
                    if info.get("ok") is False:
                        yield (json.dumps({
                            "ok": False,
                            "error": info.get("error") or "endpoint unavailable"
                        }) + "\n").encode()
                        return
                    for chunk in chat_proxy.stream_openai_chat(
                            endpoint=endpoint, model=model, messages=messages,
                            max_tokens=max_tokens,
                            timeouts=timeouts):
                        yield chunk
            except Exception as exc:
                yield (json.dumps({"ok": False,
                                   "error": f"{type(exc).__name__}: {exc}"}) + "\n").encode()

        return Response(stream_with_context(generate()),
                        mimetype="application/x-ndjson")

    result = chat_proxy.chat_completion(
        endpoint=endpoint, engine=engine, model=model, messages=messages,
        think=body.get("think", "auto"),
        max_tokens=max_tokens,
        require_loaded=bool(body.get("require_loaded", True)),
        timeouts=timeouts,
    )
    return jsonify(result), (200 if result.get("ok") else 400)


@app.post("/api/compare")
def api_compare():
    """Run independent completions for up to 3 targets in parallel.

    Failures are per-target; a slow or dead endpoint does not cancel others.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    targets = body.get("targets") or []
    if not prompt or not targets:
        return jsonify({"ok": False, "error": "prompt and targets required"}), 400
    if len(targets) > 3:
        return jsonify({"ok": False, "error": "select at most 3 models"}), 400

    think = body.get("think", "auto")
    max_tokens, mt_err = _parse_max_tokens(body)
    if mt_err:
        return jsonify({"ok": False, "error": mt_err}), 400
    timeouts = _parse_timeouts(body)

    def run_one(index: int, t: dict) -> tuple[int, dict]:
        endpoint = (t.get("endpoint") or "").strip()
        engine = (t.get("engine") or "").strip()
        model = (t.get("model") or "").strip()
        one = chat_proxy.chat_completion(
            endpoint=endpoint, engine=engine, model=model,
            messages=[{"role": "user", "content": prompt}],
            think=think,
            max_tokens=max_tokens,
            require_loaded=True,
            timeouts=timeouts,
        )
        one["target"] = {
            "endpoint": endpoint, "engine": engine, "model": model,
            "machine": t.get("machine"), "label": t.get("label"),
        }
        one["slot"] = index
        return index, one

    results: list[dict | None] = [None] * len(targets)
    with ThreadPoolExecutor(max_workers=min(3, len(targets))) as pool:
        futures = [pool.submit(run_one, i, t) for i, t in enumerate(targets)]
        for fut in as_completed(futures):
            idx, one = fut.result()
            results[idx] = one

    # Same-model detection (intentional duplicate selection)
    keys = [
        ((t.get("endpoint") or "").strip(), (t.get("model") or "").strip())
        for t in targets
    ]
    duplicate_same_model = len(keys) >= 2 and len(set(keys)) < len([k for k in keys if k[1]])

    return jsonify({
        "ok": True,
        "results": results,
        "prompt": prompt,
        "duplicate_same_model": duplicate_same_model,
    })


# ---------- QR Toolkit (serve existing project; do not copy implementation) ----
@app.get("/qr/")
@app.get("/qr/<path:filename>")
def qr_static(filename: str = "index.html"):
    if not QR_SRC.is_dir():
        return jsonify({"ok": False, "error": "QR Generator source missing"}), 404
    # Allow scan.html and project assets only
    safe = Path(filename).name if "/" not in filename.strip("/") else filename
    target = (QR_SRC / filename).resolve()
    if not str(target).startswith(str(QR_SRC.resolve())):
        return jsonify({"ok": False, "error": "invalid path"}), 400
    if not target.exists():
        # scan page lives beside index in QR src after we add it
        return jsonify({"ok": False, "error": "not found"}), 404
    return send_from_directory(QR_SRC, filename)


def main():
    host = os.environ.get("AI_LAB_HOST", "0.0.0.0")
    port = int(os.environ.get("AI_LAB_PORT", "3010"))
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
