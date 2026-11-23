"""Local chat / compare proxy for Ollama and OpenAI-compatible endpoints.

Does not start, load, switch, or unload models. Requests go only to endpoints
the caller selected. Thinking parameters are sent for Ollama only.

Completed requests emit metadata-only diagnostics to Fleet Monitor when
configured (no prompts/responses/secrets).
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Iterator
from urllib.parse import urlparse

# Default: Station-local Fleet Monitor diagnostics ingest (no auth).
_DEFAULT_DIAG_URL = "http://127.0.0.1:5000/api/diagnostics/ingest"


def _request(method: str, url: str, payload: dict | None = None,
             timeout: float = 60.0, stream: bool = False):
    data = None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def _engine_family(engine: str) -> str:
    e = (engine or "").lower()
    if "ollama" in e:
        return "ollama"
    if any(x in e for x in ("openai", "llama-server", "llama.cpp", "llamacpp",
                            "vllm", "sglang", "lm-studio", "lemonade")):
        return "openai"
    return "auto"


def _machine_guess(endpoint: str) -> str | None:
    try:
        host = urlparse(endpoint).hostname or ""
    except Exception:
        host = ""
    if host in ("127.0.0.1", "localhost", "::1"):
        return socket.gethostname()
    mapping = {
        "10.0.0.202": "Ubuntu Station",
        "10.0.0.45": "Ubuntu Portable",
        "10.0.0.104": "Windows",
    }
    return mapping.get(host)


def _emit_diagnostic(result: dict, *, client: str = "foxai-ai-lab") -> None:
    """Best-effort metadata post to Fleet Monitor. Never blocks chat on failure."""
    url = (os.environ.get("FLEET_DIAGNOSTICS_URL") or _DEFAULT_DIAG_URL).strip()
    if not url or os.environ.get("FLEET_DIAGNOSTICS_DISABLE") == "1":
        return
    req_meta = dict(result.get("request") or {})
    for banned in ("messages", "prompt", "content"):
        req_meta.pop(banned, None)
    payload = {
        "client": client,
        "ok": result.get("ok"),
        "model": result.get("model"),
        "engine": result.get("engine"),
        "endpoint": result.get("endpoint"),
        "machine": result.get("machine") or _machine_guess(result.get("endpoint") or ""),
        "prompt_tokens": result.get("prompt_tokens"),
        "completion_tokens": result.get("completion_tokens"),
        "tokens_per_sec": result.get("tokens_per_sec"),
        "elapsed_ms": result.get("elapsed_ms"),
        "done_reason": result.get("done_reason"),
        "finish_reason": result.get("finish_reason"),
        "error": result.get("error"),
        "timed_out": bool(result.get("timed_out")),
        "connection_error": bool(result.get("connection_error")),
        "stream_complete": result.get("stream_complete"),
        "prompt_eval_duration_ns": result.get("prompt_eval_duration_ns"),
        "eval_duration_ns": result.get("eval_duration_ns"),
        "request": req_meta,
        # Privacy: never include prompts/responses
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            raw = resp.read(8192)
            try:
                body = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                body = {}
            data_out = body.get("data") if isinstance(body, dict) else None
            if isinstance(data_out, dict) and data_out.get("id"):
                result["diagnostic_id"] = data_out["id"]
                result["stop_reason"] = data_out.get("stop_reason")
                result["findings"] = data_out.get("findings")
                fm = os.environ.get("FLEET_MONITOR_URL") or "http://10.0.0.202:5000"
                result["diagnostic_url"] = (
                    f"{fm.rstrip('/')}/#mcc?diag={data_out['id']}")
    except Exception:
        pass


def list_loaded_models(endpoint: str, engine: str) -> dict:
    """Return currently loaded / immediately usable models without loading new ones."""
    base = endpoint.rstrip("/")
    family = _engine_family(engine)
    out = {"ok": True, "engine": (engine or "").lower() or "unknown",
           "loaded": [], "available": [],
           "chat_supported": True, "note": None}

    def _mark_chat(bucket: str) -> None:
        for m in out[bucket]:
            mid = (m.get("id") or "").lower()
            m["chat"] = "embed" not in mid

    def try_ollama() -> bool:
        try:
            with _request("GET", f"{base}/api/ps", timeout=4) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            out["loaded"] = [
                {"id": m.get("name") or m.get("model"), "raw": m}
                for m in (body.get("models") or [])
                if m.get("name") or m.get("model")
            ]
            out["engine"] = "ollama"
        except Exception:
            pass
        try:
            with _request("GET", f"{base}/api/tags", timeout=4) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            out["available"] = [
                {"id": m.get("name") or m.get("model"), "raw": m}
                for m in (body.get("models") or [])
                if m.get("name") or m.get("model")
            ]
            out["engine"] = "ollama"
        except Exception:
            pass
        if out["loaded"] or out["available"]:
            _mark_chat("loaded")
            _mark_chat("available")
            return True
        return False

    def try_openai() -> bool:
        try:
            with _request("GET", f"{base}/v1/models", timeout=4) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            models = [
                {"id": m.get("id"), "chat": "embed" not in str(m.get("id") or "").lower(),
                 "raw": m}
                for m in (body.get("data") or []) if m.get("id")
            ]
            if not models:
                return False
            out["engine"] = "openai-compatible" if family != "openai" else (
                engine or "openai-compatible"
            )
            # Single-process OpenAI servers expose currently loaded model(s).
            out["loaded"] = models
            out["available"] = models
            return True
        except Exception:
            return False

    try:
        if family == "ollama":
            if try_ollama():
                return out
            if try_openai():
                return out
        elif family == "openai":
            if try_openai():
                return out
            # Some llama.cpp builds expose Ollama-shaped routes; only fall back
            # when OpenAI listing failed.
            if try_ollama():
                return out
        else:
            # Auto: prefer Ollama markers, else OpenAI.
            if try_ollama():
                return out
            if try_openai():
                return out
        return {"ok": False, "error": "no compatible model listing API",
                "loaded": [], "available": [], "chat_supported": False,
                "engine": out["engine"]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "loaded": [], "available": [], "chat_supported": False}


def _ollama_think_value(mode: str | None):
    if mode in (None, "", "auto"):
        return None  # omit parameter
    if mode in ("enabled", "true", "on", "1"):
        return True
    if mode in ("disabled", "false", "off", "0"):
        return False
    return None


def chat_completion(*, endpoint: str, engine: str, model: str,
                    messages: list[dict], stream: bool = True,
                    think: str | None = "auto",
                    max_tokens: int = 128,
                    require_loaded: bool = True) -> dict:
    """Non-streaming convenience wrapper used by compare + simple chat."""
    base = endpoint.rstrip("/")
    family = _engine_family(engine)
    started = time.time()

    if require_loaded:
        info = list_loaded_models(endpoint, engine)
        loaded_ids = {m["id"] for m in info.get("loaded") or []}
        detected = _engine_family(info.get("engine") or engine)
        if detected == "ollama" and loaded_ids and model not in loaded_ids:
            result = {
                "ok": False,
                "error": (f"Model {model!r} is not currently loaded. "
                          "AI Lab will not load models automatically."),
                "elapsed_ms": round((time.time() - started) * 1000, 1),
                "model": model, "engine": engine, "endpoint": base,
                "request": {"max_tokens": int(max_tokens), "think": think},
            }
            _emit_diagnostic(result, client="foxai-ai-lab")
            return result
        if detected == "ollama" and not loaded_ids:
            result = {
                "ok": False,
                "error": ("No models are currently loaded on this Ollama server. "
                          "Load one manually first; AI Lab will not auto-load."),
                "elapsed_ms": round((time.time() - started) * 1000, 1),
                "model": model, "engine": engine, "endpoint": base,
                "request": {"max_tokens": int(max_tokens), "think": think},
            }
            _emit_diagnostic(result, client="foxai-ai-lab")
            return result
        if detected == "openai" and loaded_ids and model not in loaded_ids:
            result = {
                "ok": False,
                "error": (f"Model {model!r} is not available on this endpoint. "
                          "AI Lab will not substitute another model."),
                "elapsed_ms": round((time.time() - started) * 1000, 1),
                "model": model, "engine": engine, "endpoint": base,
                "request": {"max_tokens": int(max_tokens), "think": think},
            }
            _emit_diagnostic(result, client="foxai-ai-lab")
            return result

    try:
        if family == "ollama" or (family == "auto" and "ollama" in (engine or "").lower()):
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"num_predict": int(max_tokens)},
            }
            think_v = _ollama_think_value(think)
            if think_v is not None:
                payload["think"] = think_v
            with _request("POST", f"{base}/api/chat", payload=payload,
                          timeout=90) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            msg = body.get("message") or {}
            content = msg.get("content") or ""
            thinking = msg.get("thinking")
            eval_count = body.get("eval_count")
            eval_duration = body.get("eval_duration")  # ns
            prompt_eval_duration = body.get("prompt_eval_duration")
            tps = None
            if eval_count and eval_duration:
                tps = round(eval_count / (eval_duration / 1e9), 2)
            result = {
                "ok": True,
                "engine": "ollama",
                "model": model,
                "content": content,
                "thinking": thinking,
                "prompt_tokens": body.get("prompt_eval_count"),
                "completion_tokens": eval_count,
                "tokens_per_sec": tps,
                "elapsed_ms": round((time.time() - started) * 1000, 1),
                "done_reason": body.get("done_reason"),
                "prompt_eval_duration_ns": prompt_eval_duration,
                "eval_duration_ns": eval_duration,
                "raw_keys": sorted(body.keys()),
                "request": {
                    "max_tokens": int(max_tokens),
                    "think": think,
                    "stream": False,
                    "num_predict": int(max_tokens),
                },
                "endpoint": base,
            }
            _emit_diagnostic(result, client="foxai-ai-lab")
            return result

        # OpenAI-compatible (llama.cpp, vLLM, etc.) — never send think
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "max_tokens": int(max_tokens),
        }
        with _request("POST", f"{base}/v1/chat/completions", payload=payload,
                      timeout=90) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        usage = body.get("usage") or {}
        elapsed = time.time() - started
        comp = usage.get("completion_tokens")
        tps = round(comp / elapsed, 2) if comp and elapsed > 0 else None
        result = {
            "ok": True,
            "engine": "openai-compatible",
            "model": model,
            "content": content,
            "thinking": message.get("reasoning_content") or message.get("reasoning"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": comp,
            "tokens_per_sec": tps,
            "elapsed_ms": round(elapsed * 1000, 1),
            "finish_reason": choice.get("finish_reason"),
            "request": {
                "max_tokens": int(max_tokens),
                "think": None,
                "stream": False,
            },
            "endpoint": base,
        }
        _emit_diagnostic(result, client="foxai-ai-lab")
        return result
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(2048).decode("utf-8", "replace")
        except Exception:
            detail = str(exc)
        result = {"ok": False, "error": f"HTTP {exc.code}: {detail[:400]}",
                  "elapsed_ms": round((time.time() - started) * 1000, 1),
                  "model": model, "engine": engine, "endpoint": base,
                  "request": {"max_tokens": int(max_tokens), "think": think}}
        _emit_diagnostic(result, client="foxai-ai-lab")
        return result
    except Exception as exc:
        timed_out = "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()
        conn = "connection" in str(exc).lower() or "refused" in str(exc).lower()
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                  "elapsed_ms": round((time.time() - started) * 1000, 1),
                  "model": model, "engine": engine, "endpoint": base,
                  "timed_out": timed_out, "connection_error": conn,
                  "request": {"max_tokens": int(max_tokens), "think": think}}
        _emit_diagnostic(result, client="foxai-ai-lab")
        return result


def stream_ollama_chat(*, endpoint: str, model: str, messages: list[dict],
                       think: str | None = "auto",
                       max_tokens: int = 256) -> Iterator[bytes]:
    base = endpoint.rstrip("/")
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"num_predict": int(max_tokens)},
    }
    think_v = _ollama_think_value(think)
    if think_v is not None:
        payload["think"] = think_v
    req = urllib.request.Request(
        f"{base}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        for line in resp:
            yield line


def stream_openai_chat(*, endpoint: str, model: str, messages: list[dict],
                       max_tokens: int = 256) -> Iterator[bytes]:
    base = endpoint.rstrip("/")
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": int(max_tokens),
    }
    req = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        for line in resp:
            yield line
