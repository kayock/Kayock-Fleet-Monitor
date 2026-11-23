"""Local chat / compare proxy for Ollama and OpenAI-compatible endpoints.

Does not start, load, switch, or unload models. Requests go only to endpoints
the caller selected. Thinking parameters are sent for Ollama only.

Completed requests (streaming and non-streaming) emit metadata-only diagnostics
to Fleet Monitor when configured (no prompts/responses/secrets).
"""
from __future__ import annotations

import json
import os
import socket
import time
import uuid
import urllib.error
import urllib.request
from typing import Any, Iterator
from urllib.parse import urlparse

# Default: Station-local Fleet Monitor diagnostics ingest (no auth).
_DEFAULT_DIAG_URL = "http://127.0.0.1:5000/api/diagnostics/ingest"

# Timeout defaults (seconds). Non-stream total keeps the historical 90s default;
# streaming uses inactivity between chunks instead of a short hard total.
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_FIRST_TOKEN_TIMEOUT = 90.0
DEFAULT_READ_INACTIVITY_TIMEOUT = 120.0
DEFAULT_NONSTREAM_TOTAL_TIMEOUT = 90.0  # historical Direct Chat / compare value


def _env_float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    if str(raw).strip().lower() in ("none", "null", "off", "0"):
        return None if name.endswith("TOTAL_TIMEOUT") else default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if val > 0 else default


def resolve_timeouts(overrides: dict | None = None) -> dict:
    """Merge env defaults with optional per-request overrides.

    Keys: connect, first_token, read_inactivity, total
    total=None means no overall wall-clock limit (streaming).
    """
    overrides = overrides or {}
    cfg = {
        "connect": _env_float("AI_LAB_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT),
        "first_token": _env_float(
            "AI_LAB_FIRST_TOKEN_TIMEOUT", DEFAULT_FIRST_TOKEN_TIMEOUT),
        "read_inactivity": _env_float(
            "AI_LAB_READ_INACTIVITY_TIMEOUT", DEFAULT_READ_INACTIVITY_TIMEOUT),
        "total": _env_float("AI_LAB_TOTAL_TIMEOUT", None),
        "nonstream_total": _env_float(
            "AI_LAB_NONSTREAM_TOTAL_TIMEOUT", DEFAULT_NONSTREAM_TOTAL_TIMEOUT),
    }
    for key in ("connect", "first_token", "read_inactivity", "total",
                "nonstream_total"):
        if key in overrides and overrides[key] is not None:
            try:
                val = float(overrides[key])
            except (TypeError, ValueError):
                continue
            if val > 0:
                cfg[key] = val
        elif key in overrides and overrides[key] is None and key == "total":
            cfg[key] = None
    return cfg


def _request(method: str, url: str, payload: dict | None = None,
             timeout: float = 60.0, stream: bool = False):
    data = None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def _urlopen_request(req: urllib.request.Request, *, connect_timeout: float):
    """Open HTTP request; connect_timeout also seeds the initial socket timeout."""
    return urllib.request.urlopen(req, timeout=connect_timeout)


def _set_socket_timeout(resp, seconds: float | None) -> None:
    if seconds is None:
        return
    try:
        resp.fp.raw._sock.settimeout(float(seconds))  # type: ignore[attr-defined]
    except Exception:
        try:
            resp.fp._sock.settimeout(float(seconds))  # type: ignore[attr-defined]
        except Exception:
            pass


def _classify_transport_timeout(*, saw_data: bool, timed_out: bool,
                                connection_error: bool) -> str | None:
    if connection_error and not saw_data:
        return "connection"
    if timed_out and not saw_data:
        return "first_token"
    if timed_out and saw_data:
        return "stream_inactivity"
    if timed_out:
        return "total"
    return None


def _engine_family(engine: str) -> str:
    e = (engine or "").lower()
    if "ollama" in e:
        return "ollama"
    if any(x in e for x in ("openai", "llama-server", "llama.cpp", "llamacpp",
                            "vllm", "sglang", "lm-studio", "lemonade")):
        return "openai"
    return "auto"


def normalize_max_tokens(value: Any) -> tuple[int | None, str | None]:
    """Return (max_tokens_or_None, error).

    None / '' / 'default' / 'auto' → model/client default (do not send an
    explicit output cap). Positive ints are custom caps. Does not invent
    backend-specific upper bounds.
    """
    if value is None or value == "" or value is False:
        return None, None
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("default", "auto", "model", "none", "null"):
            return None, None
        try:
            value = int(s)
        except ValueError:
            return None, f"invalid max_tokens: {value!r}"
    if isinstance(value, bool):
        return None, "invalid max_tokens: boolean"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None, f"invalid max_tokens: {value!r}"
    if n < 1:
        return None, "max_tokens must be a positive integer"
    return n, None


def _request_meta(*, max_tokens: int | None, think: str | None,
                  stream: bool, family: str,
                  timeouts: dict | None = None) -> dict:
    mode = "custom" if max_tokens is not None else "model_default"
    meta: dict[str, Any] = {
        "max_tokens": max_tokens,
        "max_tokens_mode": mode,
        "think": think,
        "stream": stream,
    }
    if family == "ollama" and max_tokens is not None:
        meta["num_predict"] = int(max_tokens)
    if timeouts:
        meta["timeouts"] = {
            k: timeouts.get(k) for k in (
                "connect", "first_token", "read_inactivity", "total",
                "nonstream_total")
        }
    return meta


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
        "request_id": result.get("request_id"),
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
        "timeout_kind": result.get("timeout_kind"),
        "connection_error": bool(result.get("connection_error")),
        "cancelled": bool(result.get("cancelled")),
        "stream_complete": result.get("stream_complete"),
        "partial": bool(result.get("partial")),
        "prompt_eval_duration_ns": result.get("prompt_eval_duration_ns"),
        "eval_duration_ns": result.get("eval_duration_ns"),
        "request": req_meta,
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
            if try_ollama():
                return out
        else:
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
                    max_tokens: int | None = None,
                    require_loaded: bool = True,
                    timeouts: dict | None = None) -> dict:
    """Non-streaming convenience wrapper used by compare + simple chat.

    max_tokens=None means model/client default — do not send an explicit
    num_predict / max_tokens cap.

    Non-stream uses an explicit total timeout (default 90s, configurable).
    Prefer streaming for slow models so inactivity timeouts apply between chunks.
    """
    base = endpoint.rstrip("/")
    family = _engine_family(engine)
    started = time.time()
    request_id = uuid.uuid4().hex
    tcfg = resolve_timeouts(timeouts)
    total_timeout = tcfg.get("nonstream_total") or tcfg.get("total") or DEFAULT_NONSTREAM_TOTAL_TIMEOUT
    connect_timeout = tcfg.get("connect") or DEFAULT_CONNECT_TIMEOUT
    max_tokens, mt_err = normalize_max_tokens(max_tokens)
    if mt_err:
        return {"ok": False, "error": mt_err, "request_id": request_id,
                "elapsed_ms": 0, "request": _request_meta(
                    max_tokens=None, think=think, stream=False, family=family,
                    timeouts=tcfg)}

    def fail_early(error: str) -> dict:
        result = {
            "ok": False,
            "error": error,
            "elapsed_ms": round((time.time() - started) * 1000, 1),
            "model": model, "engine": engine, "endpoint": base,
            "request_id": request_id,
            "request": _request_meta(
                max_tokens=max_tokens, think=think, stream=False, family=family,
                timeouts=tcfg),
        }
        _emit_diagnostic(result, client="foxai-ai-lab")
        return result

    if require_loaded:
        info = list_loaded_models(endpoint, engine)
        loaded_ids = {m["id"] for m in info.get("loaded") or []}
        detected = _engine_family(info.get("engine") or engine)
        if detected == "ollama" and loaded_ids and model not in loaded_ids:
            return fail_early(
                f"Model {model!r} is not currently loaded. "
                "AI Lab will not load models automatically.")
        if detected == "ollama" and not loaded_ids:
            return fail_early(
                "No models are currently loaded on this Ollama server. "
                "Load one manually first; AI Lab will not auto-load.")
        if detected == "openai" and loaded_ids and model not in loaded_ids:
            return fail_early(
                f"Model {model!r} is not available on this endpoint. "
                "AI Lab will not substitute another model.")

    # urllib uses one socket timeout for connect+read; for non-stream that is
    # the configured total wall-clock budget (historical default: 90s).
    open_timeout = float(total_timeout)

    try:
        if family == "ollama" or (family == "auto" and "ollama" in (engine or "").lower()):
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
            }
            if max_tokens is not None:
                payload["options"] = {"num_predict": int(max_tokens)}
            think_v = _ollama_think_value(think)
            if think_v is not None:
                payload["think"] = think_v
            with _request("POST", f"{base}/api/chat", payload=payload,
                          timeout=open_timeout) as resp:
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
                "request_id": request_id,
                "request": _request_meta(
                    max_tokens=max_tokens, think=think, stream=False,
                    family="ollama", timeouts=tcfg),
                "endpoint": base,
                "stream_complete": True,
            }
            _emit_diagnostic(result, client="foxai-ai-lab")
            return result

        # OpenAI-compatible — never send think; omit max_tokens when default
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        with _request("POST", f"{base}/v1/chat/completions", payload=payload,
                      timeout=open_timeout) as resp:
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
            "request_id": request_id,
            "request": _request_meta(
                max_tokens=max_tokens, think=None, stream=False,
                family="openai", timeouts=tcfg),
            "endpoint": base,
            "stream_complete": True,
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
                  "request_id": request_id,
                  "request": _request_meta(
                      max_tokens=max_tokens, think=think, stream=False,
                      family=family, timeouts=tcfg)}
        _emit_diagnostic(result, client="foxai-ai-lab")
        return result
    except Exception as exc:
        timed_out = "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()
        conn = "connection" in str(exc).lower() or "refused" in str(exc).lower()
        kind = None
        if timed_out:
            # Non-stream: socket timeout covers connect+full body read → total
            kind = "connection" if (conn and (time.time() - started) < (connect_timeout + 1)) else "total"
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                  "elapsed_ms": round((time.time() - started) * 1000, 1),
                  "model": model, "engine": engine, "endpoint": base,
                  "timed_out": timed_out, "timeout_kind": kind,
                  "connection_error": conn,
                  "request_id": request_id,
                  "request": _request_meta(
                      max_tokens=max_tokens, think=think, stream=False,
                      family=family, timeouts=tcfg)}
        _emit_diagnostic(result, client="foxai-ai-lab")
        return result


def stream_ollama_chat(*, endpoint: str, model: str, messages: list[dict],
                       think: str | None = "auto",
                       max_tokens: int | None = None,
                       engine: str = "ollama",
                       timeouts: dict | None = None) -> Iterator[bytes]:
    """Stream Ollama NDJSON; emit exactly one diagnostic record when finished.

    Socket timeout starts as first_token, then switches to read_inactivity after
    the first chunk so slow-but-active generation is not killed by a short total.
    """
    base = endpoint.rstrip("/")
    max_tokens, mt_err = normalize_max_tokens(max_tokens)
    tcfg = resolve_timeouts(timeouts)
    request_id = uuid.uuid4().hex
    started = time.time()
    emitted = False
    last: dict[str, Any] = {}
    stream_complete = False
    cancelled = False
    timed_out = False
    connection_error = False
    timeout_kind: str | None = None
    saw_data = False
    error: str | None = mt_err
    partial_content = ""

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if max_tokens is not None:
        payload["options"] = {"num_predict": int(max_tokens)}
    think_v = _ollama_think_value(think)
    if think_v is not None:
        payload["think"] = think_v

    def finalize() -> None:
        nonlocal emitted
        if emitted:
            return
        emitted = True
        eval_count = last.get("eval_count")
        eval_duration = last.get("eval_duration")
        tps = None
        if eval_count and eval_duration:
            tps = round(eval_count / (eval_duration / 1e9), 2)
        result = {
            "ok": error is None and not cancelled,
            "error": error,
            "engine": "ollama",
            "model": model,
            "endpoint": base,
            "request_id": request_id,
            "prompt_tokens": last.get("prompt_eval_count"),
            "completion_tokens": eval_count,
            "tokens_per_sec": tps,
            "elapsed_ms": round((time.time() - started) * 1000, 1),
            "done_reason": last.get("done_reason"),
            "prompt_eval_duration_ns": last.get("prompt_eval_duration"),
            "eval_duration_ns": eval_duration,
            "stream_complete": stream_complete,
            "cancelled": cancelled,
            "timed_out": timed_out,
            "timeout_kind": timeout_kind,
            "connection_error": connection_error,
            "partial": bool(partial_content) and not stream_complete,
            "request": _request_meta(
                max_tokens=max_tokens, think=think, stream=True,
                family="ollama", timeouts=tcfg),
        }
        _emit_diagnostic(result, client="foxai-ai-lab")
        # Expose diagnostic ids to stream consumers via trailing meta line
        if result.get("diagnostic_id"):
            meta = {
                "ok": result["ok"],
                "type": "diagnostic_meta",
                "diagnostic_id": result.get("diagnostic_id"),
                "diagnostic_url": result.get("diagnostic_url"),
                "stop_reason": result.get("stop_reason"),
                "timeout_kind": timeout_kind,
                "request_id": request_id,
                "elapsed_ms": result.get("elapsed_ms"),
                "completion_tokens": eval_count,
                "done_reason": last.get("done_reason"),
                "partial": result.get("partial"),
                "error": error,
            }
            # Store for optional yield after finalize from caller — attached on result
            stream_ollama_chat._last_meta = meta  # type: ignore[attr-defined]

    if mt_err:
        finalize()
        yield (json.dumps({"ok": False, "error": mt_err}) + "\n").encode()
        return

    try:
        req = urllib.request.Request(
            f"{base}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        first_to = float(tcfg.get("first_token") or DEFAULT_FIRST_TOKEN_TIMEOUT)
        inactivity = float(tcfg.get("read_inactivity") or DEFAULT_READ_INACTIVITY_TIMEOUT)
        connect_to = float(tcfg.get("connect") or DEFAULT_CONNECT_TIMEOUT)
        total_to = tcfg.get("total")
        with _urlopen_request(req, connect_timeout=min(connect_to, first_to)) as resp:
            _set_socket_timeout(resp, first_to)
            for line in resp:
                if total_to and (time.time() - started) > float(total_to):
                    timed_out = True
                    timeout_kind = "total"
                    error = f"TimeoutError: total request timeout ({total_to}s)"
                    break
                if not saw_data:
                    _set_socket_timeout(resp, inactivity)
                # Parse before yield so cancel/timeout mid-stream still sees partials
                try:
                    text = line.decode("utf-8", "replace").strip()
                    if text:
                        chunk = json.loads(text)
                        if isinstance(chunk, dict):
                            saw_data = True
                            last = chunk
                            msg = chunk.get("message") or {}
                            if msg.get("content"):
                                partial_content += str(msg.get("content") or "")
                            if chunk.get("done") is True:
                                stream_complete = True
                except (ValueError, UnicodeError):
                    pass
                yield line
    except GeneratorExit:
        cancelled = True
        finalize()
        raise
    except Exception as exc:
        timed_out = "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()
        connection_error = "connection" in str(exc).lower() or "refused" in str(exc).lower()
        if timed_out and not timeout_kind:
            timeout_kind = _classify_transport_timeout(
                saw_data=saw_data, timed_out=True,
                connection_error=connection_error)
        error = f"{type(exc).__name__}: {exc}"
        yield (json.dumps({
            "ok": False, "error": error, "timeout_kind": timeout_kind,
            "timed_out": timed_out, "partial": bool(partial_content),
            "message": partial_content or None,
        }) + "\n").encode()
    finally:
        finalize()
        meta = None if cancelled else getattr(stream_ollama_chat, "_last_meta", None)
        stream_ollama_chat._last_meta = None  # type: ignore[attr-defined]
    if meta:
        yield (json.dumps(meta) + "\n").encode()


def stream_openai_chat(*, endpoint: str, model: str, messages: list[dict],
                       max_tokens: int | None = None,
                       engine: str = "openai-compatible",
                       timeouts: dict | None = None) -> Iterator[bytes]:
    """Stream OpenAI SSE; emit exactly one diagnostic record when finished."""
    base = endpoint.rstrip("/")
    max_tokens, mt_err = normalize_max_tokens(max_tokens)
    tcfg = resolve_timeouts(timeouts)
    request_id = uuid.uuid4().hex
    started = time.time()
    emitted = False
    finish_reason = None
    usage: dict[str, Any] = {}
    stream_complete = False
    cancelled = False
    timed_out = False
    connection_error = False
    timeout_kind: str | None = None
    error: str | None = mt_err
    saw_chunk = False
    partial_content = ""

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)

    def finalize() -> None:
        nonlocal emitted
        if emitted:
            return
        emitted = True
        comp = usage.get("completion_tokens")
        elapsed = time.time() - started
        tps = round(comp / elapsed, 2) if comp and elapsed > 0 else None
        result = {
            "ok": error is None and not cancelled,
            "error": error,
            "engine": "openai-compatible",
            "model": model,
            "endpoint": base,
            "request_id": request_id,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": comp,
            "tokens_per_sec": tps,
            "elapsed_ms": round(elapsed * 1000, 1),
            "finish_reason": finish_reason,
            "stream_complete": stream_complete or bool(finish_reason),
            "cancelled": cancelled,
            "timed_out": timed_out,
            "timeout_kind": timeout_kind,
            "connection_error": connection_error,
            "partial": bool(partial_content) and not (stream_complete or finish_reason),
            "request": _request_meta(
                max_tokens=max_tokens, think=None, stream=True,
                family="openai", timeouts=tcfg),
        }
        if saw_chunk and finish_reason is None and error is None and not cancelled:
            result["stream_complete"] = False
        _emit_diagnostic(result, client="foxai-ai-lab")
        if result.get("diagnostic_id"):
            stream_openai_chat._last_meta = {  # type: ignore[attr-defined]
                "ok": result["ok"],
                "type": "diagnostic_meta",
                "diagnostic_id": result.get("diagnostic_id"),
                "diagnostic_url": result.get("diagnostic_url"),
                "stop_reason": result.get("stop_reason"),
                "timeout_kind": timeout_kind,
                "request_id": request_id,
                "elapsed_ms": result.get("elapsed_ms"),
                "completion_tokens": comp,
                "finish_reason": finish_reason,
                "partial": result.get("partial"),
                "error": error,
            }

    if mt_err:
        finalize()
        yield (json.dumps({"ok": False, "error": mt_err}) + "\n").encode()
        return

    try:
        req = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            method="POST",
        )
        first_to = float(tcfg.get("first_token") or DEFAULT_FIRST_TOKEN_TIMEOUT)
        inactivity = float(tcfg.get("read_inactivity") or DEFAULT_READ_INACTIVITY_TIMEOUT)
        connect_to = float(tcfg.get("connect") or DEFAULT_CONNECT_TIMEOUT)
        total_to = tcfg.get("total")
        with _urlopen_request(req, connect_timeout=min(connect_to, first_to)) as resp:
            _set_socket_timeout(resp, first_to)
            for line in resp:
                if total_to and (time.time() - started) > float(total_to):
                    timed_out = True
                    timeout_kind = "total"
                    error = f"TimeoutError: total request timeout ({total_to}s)"
                    break
                if not saw_chunk:
                    _set_socket_timeout(resp, inactivity)
                try:
                    text = line.decode("utf-8", "replace").strip()
                    if text.startswith("data:"):
                        data = text[5:].strip()
                        if data == "[DONE]":
                            stream_complete = True
                        else:
                            chunk = json.loads(data)
                            saw_chunk = True
                            if isinstance(chunk.get("usage"), dict):
                                usage = chunk["usage"]
                            choices = chunk.get("choices") or []
                            if choices:
                                delta = choices[0].get("delta") or {}
                                if delta.get("content"):
                                    partial_content += str(delta.get("content") or "")
                                fr = choices[0].get("finish_reason")
                                if fr:
                                    finish_reason = fr
                                    stream_complete = True
                except (ValueError, UnicodeError, IndexError, TypeError):
                    pass
                yield line
    except GeneratorExit:
        cancelled = True
        finalize()
        raise
    except Exception as exc:
        timed_out = "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()
        connection_error = "connection" in str(exc).lower() or "refused" in str(exc).lower()
        if timed_out and not timeout_kind:
            timeout_kind = _classify_transport_timeout(
                saw_data=saw_chunk, timed_out=True,
                connection_error=connection_error)
        error = f"{type(exc).__name__}: {exc}"
        yield (json.dumps({
            "ok": False, "error": error, "timeout_kind": timeout_kind,
            "timed_out": timed_out, "partial": bool(partial_content),
            "message": partial_content or None,
        }) + "\n").encode()
    finally:
        finalize()
        meta = None if cancelled else getattr(stream_openai_chat, "_last_meta", None)
        stream_openai_chat._last_meta = None  # type: ignore[attr-defined]
    if meta:
        yield (json.dumps(meta) + "\n").encode()
