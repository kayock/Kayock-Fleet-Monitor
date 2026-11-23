"""AI Diagnostics & Troubleshooting Center (Fleet Monitor v3.3).

Read-only configuration inspection, inference request history, stop-reason
classification, configuration conflict detection, and rules-based findings.

Never invents values. Never mutates inference configs or loads models.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
import urllib.error
import urllib.request
from copy import deepcopy
from typing import Any

UNKNOWN = "Unknown — backend does not report this setting."
HISTORY_MAX = 200
HISTORY_FILENAME = "diagnostic_history.json"

_lock = threading.RLock()


def _num(v):
    try:
        if v is None or v == "":
            return None
        return float(v) if isinstance(v, str) and "." in v else int(v)
    except (TypeError, ValueError):
        return None


def _fetch_json(url: str, timeout: float = 4.0) -> tuple[int | None, Any, str | None]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(2_000_000)
            try:
                return resp.status, json.loads(raw.decode("utf-8", "replace")), None
            except ValueError:
                return resp.status, None, "unparsable JSON"
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(512).decode("utf-8", "replace")
        except Exception:
            detail = str(exc)
        return exc.code, None, detail[:200]
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def _post_json(url: str, payload: dict, timeout: float = 4.0) -> tuple[int | None, Any, str | None]:
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(2_000_000)
            try:
                return resp.status, json.loads(raw.decode("utf-8", "replace")), None
            except ValueError:
                return resp.status, None, "unparsable JSON"
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(512).decode("utf-8", "replace")
        except Exception:
            detail = str(exc)
        return exc.code, None, detail[:200]
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def _field(value, *, source: str, layer: str) -> dict:
    if value is None or value == "":
        return {"value": None, "display": UNKNOWN, "source": source, "layer": layer,
                "known": False}
    return {"value": value, "display": value, "source": source, "layer": layer,
            "known": True}


def _engine_family(engine: str | None) -> str:
    e = (engine or "").lower()
    if "ollama" in e:
        return "ollama"
    if "llama" in e or "llamacpp" in e:
        return "llama-server"
    if "lemonade" in e:
        return "lemonade"
    if "lm-studio" in e or "lmstudio" in e:
        return "lm-studio"
    return "openai"


def parse_startup_args_lite(args: str | None) -> dict[str, Any]:
    """Best-effort known flag extraction (read-only)."""
    if not args:
        return {}
    tokens = []
    buf, in_q = "", False
    for ch in str(args):
        if ch == '"':
            in_q = not in_q
            continue
        if ch.isspace() and not in_q:
            if buf:
                tokens.append(buf)
                buf = ""
            continue
        buf += ch
    if buf:
        tokens.append(buf)
    out: dict[str, Any] = {}
    pairs = {
        "-c": "context", "--ctx-size": "context", "--context-size": "context",
        "-ngl": "gpu_layers", "--gpu-layers": "gpu_layers", "--n-gpu-layers": "gpu_layers",
        "-t": "threads", "--threads": "threads",
        "-np": "parallel", "--parallel": "parallel",
        "-b": "batch", "--batch-size": "batch",
        "-m": "model_path", "--model": "model_path", "--model-path": "model_path",
    }
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in pairs and i + 1 < len(tokens):
            key = pairs[tok]
            val = tokens[i + 1]
            n = _num(val)
            out[key] = n if n is not None else val
            i += 2
            continue
        i += 1
    return out


# ---------------------------------------------------------------------------
# Model configuration inspector
# ---------------------------------------------------------------------------

def inspect_ollama_model(endpoint: str, model: str) -> dict:
    """Read-only Ollama metadata. Does not load models."""
    base = endpoint.rstrip("/")
    result = {
        "engine": "ollama",
        "endpoint": base,
        "model": model,
        "fetched_at": time.time(),
        "fields": {},
        "raw": {},
        "errors": [],
    }
    st, body, err = _post_json(f"{base}/api/show", {"name": model}, timeout=6.0)
    if st == 200 and isinstance(body, dict):
        result["raw"]["api_show"] = {
            k: body.get(k) for k in (
                "modelfile", "parameters", "template", "details", "model_info",
                "capabilities") if k in body
        }
        details = body.get("details") or {}
        result["fields"]["quantization"] = _field(
            details.get("quantization_level"), source="Ollama /api/show details",
            layer="model_default")
        result["fields"]["parameter_size"] = _field(
            details.get("parameter_size"), source="Ollama /api/show details",
            layer="model_default")
        result["fields"]["family"] = _field(
            details.get("family"), source="Ollama /api/show details",
            layer="model_default")
        # Parse parameters block (Modelfile-style key value lines)
        params_text = body.get("parameters") or ""
        parsed = {}
        for line in str(params_text).splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                parsed[parts[0].lower()] = parts[1]
        result["raw"]["parameters_text"] = params_text
        result["fields"]["temperature"] = _field(
            _num(parsed.get("temperature")), source="Ollama Modelfile parameters",
            layer="model_default")
        result["fields"]["top_p"] = _field(
            _num(parsed.get("top_p")), source="Ollama Modelfile parameters",
            layer="model_default")
        result["fields"]["top_k"] = _field(
            _num(parsed.get("top_k")), source="Ollama Modelfile parameters",
            layer="model_default")
        result["fields"]["repeat_penalty"] = _field(
            _num(parsed.get("repeat_penalty")), source="Ollama Modelfile parameters",
            layer="model_default")
        # Context: model_info num_ctx or parameter
        minfo = body.get("model_info") or {}
        ctx = None
        for k, v in minfo.items():
            if str(k).endswith(".context_length") or str(k).endswith(".context_length"):
                ctx = _num(v)
                break
        if ctx is None:
            ctx = _num(parsed.get("num_ctx"))
        result["fields"]["context_window"] = _field(
            ctx, source="Ollama /api/show model_info/parameters",
            layer="model_default")
        result["fields"]["max_output"] = _field(
            _num(parsed.get("num_predict")), source="Ollama Modelfile parameters",
            layer="model_default")
        caps = body.get("capabilities") or []
        result["fields"]["thinking_support"] = _field(
            ("thinking" in caps) or ("tools" in caps and False) or None
            if caps else None,
            source="Ollama /api/show capabilities",
            layer="model_default")
        if caps:
            result["fields"]["capabilities"] = _field(
                caps, source="Ollama /api/show capabilities", layer="model_default")
    else:
        result["errors"].append(f"/api/show: {err or st}")

    st, body, err = _fetch_json(f"{base}/api/ps", timeout=4.0)
    if st == 200 and isinstance(body, dict):
        loaded = None
        for m in body.get("models") or []:
            if (m.get("name") or m.get("model")) == model:
                loaded = m
                break
        if loaded:
            result["fields"]["model_status"] = _field(
                "loaded", source="Ollama /api/ps", layer="reported_effective")
            result["fields"]["ram_bytes"] = _field(
                _num(loaded.get("size")), source="Ollama /api/ps size",
                layer="reported_effective")
            result["fields"]["vram_bytes"] = _field(
                _num(loaded.get("size_vram")), source="Ollama /api/ps size_vram",
                layer="reported_effective")
            result["fields"]["keep_alive"] = _field(
                loaded.get("expires_at"), source="Ollama /api/ps expires_at",
                layer="reported_effective")
            det = loaded.get("details") or {}
            if det.get("quantization_level") and not result["fields"].get("quantization", {}).get("known"):
                result["fields"]["quantization"] = _field(
                    det.get("quantization_level"), source="Ollama /api/ps",
                    layer="reported_effective")
        else:
            result["fields"]["model_status"] = _field(
                "unloaded", source="Ollama /api/ps", layer="reported_effective")
    else:
        result["errors"].append(f"/api/ps: {err or st}")

    st, body, err = _fetch_json(f"{base}/api/version", timeout=3.0)
    if st == 200 and isinstance(body, dict):
        result["fields"]["server_version"] = _field(
            body.get("version"), source="Ollama /api/version",
            layer="server_configuration")
    return result


def inspect_llama_server(endpoint: str, model: str | None,
                         configured_args: str | None = None) -> dict:
    base = endpoint.rstrip("/")
    result = {
        "engine": "llama-server",
        "endpoint": base,
        "model": model,
        "fetched_at": time.time(),
        "fields": {},
        "raw": {},
        "errors": [],
    }
    startup = parse_startup_args_lite(configured_args)
    if startup:
        result["raw"]["startup_parsed"] = startup
        for key, layer_src in (
            ("context", "startup args"),
            ("gpu_layers", "startup args"),
            ("threads", "startup args"),
            ("parallel", "startup args"),
            ("batch", "startup args"),
            ("model_path", "startup args"),
        ):
            if key in startup:
                result["fields"][key if key != "context" else "context_window"] = _field(
                    startup[key], source=f"Server {layer_src}",
                    layer="server_configuration")
        result["fields"]["startup_arguments"] = _field(
            configured_args, source="ai_servers.json / configured args",
            layer="server_configuration")

    st, body, err = _fetch_json(f"{base}/props", timeout=4.0)
    if st == 200 and isinstance(body, dict):
        result["raw"]["props"] = body
        # Tolerant shapes
        def dig(*paths):
            for p in paths:
                cur = body
                ok = True
                for part in p.split("."):
                    if isinstance(cur, dict) and part in cur:
                        cur = cur[part]
                    else:
                        ok = False
                        break
                if ok and cur not in (None, ""):
                    return cur
            return None

        result["fields"]["context_window_reported"] = _field(
            _num(dig("default_generation_settings.n_ctx", "n_ctx",
                     "default_generation_settings.params.n_ctx")),
            source="llama.cpp /props", layer="reported_effective")
        result["fields"]["model_path"] = _field(
            dig("model_path", "default_generation_settings.model", "model"),
            source="llama.cpp /props", layer="reported_effective")
        result["fields"]["parallel"] = result["fields"].get("parallel") or _field(
            _num(dig("total_slots", "params.n_parallel")),
            source="llama.cpp /props", layer="reported_effective")
    elif st == 404:
        result["errors"].append("/props not available on this llama.cpp build")
    else:
        result["errors"].append(f"/props: {err or st}")

    st, body, err = _fetch_json(f"{base}/health", timeout=3.0)
    if st == 200:
        result["fields"]["model_status"] = _field(
            "loaded" if model else "online",
            source="llama.cpp /health", layer="reported_effective")
    elif st is not None and st >= 500:
        result["fields"]["model_status"] = _field(
            "loading_or_error", source="llama.cpp /health",
            layer="reported_effective")
        result["errors"].append(f"/health HTTP {st}")
    else:
        result["errors"].append(f"/health: {err or st}")

    st, body, err = _fetch_json(f"{base}/v1/models", timeout=3.0)
    if st == 200 and isinstance(body, dict):
        models = body.get("data") or []
        if models:
            mid = models[0].get("id")
            result["fields"]["model"] = _field(
                mid, source="llama.cpp /v1/models", layer="reported_effective")
            if not model:
                result["model"] = mid
    return result


def inspect_lm_studio(endpoint: str, model: str | None) -> dict:
    base = endpoint.rstrip("/")
    result = {
        "engine": "lm-studio", "endpoint": base, "model": model,
        "fetched_at": time.time(), "fields": {}, "raw": {}, "errors": [],
    }
    st, body, err = _fetch_json(f"{base}/api/v0/models", timeout=4.0)
    if st == 200 and isinstance(body, dict):
        match = None
        for m in body.get("data") or []:
            if not model or m.get("id") == model:
                match = m
                if model:
                    break
        if match:
            result["fields"]["model"] = _field(
                match.get("id"), source="LM Studio /api/v0/models",
                layer="reported_effective")
            result["fields"]["quantization"] = _field(
                match.get("quantization"), source="LM Studio /api/v0/models",
                layer="reported_effective")
            result["fields"]["context_window"] = _field(
                _num(match.get("max_context_length")),
                source="LM Studio /api/v0/models", layer="reported_effective")
            result["fields"]["model_status"] = _field(
                match.get("state") or UNKNOWN, source="LM Studio /api/v0/models",
                layer="reported_effective")
        else:
            result["errors"].append("model not listed by LM Studio")
    else:
        result["errors"].append(f"/api/v0/models: {err or st}")
    return result


def inspect_lemonade(endpoint: str, model: str | None) -> dict:
    base = endpoint.rstrip("/")
    result = {
        "engine": "lemonade", "endpoint": base, "model": model,
        "fetched_at": time.time(), "fields": {}, "raw": {}, "errors": [],
    }
    # Lemonade exposes OpenAI-compatible + proprietary routes; be honest.
    st, body, err = _fetch_json(f"{base}/v1/models", timeout=4.0)
    if st == 200 and isinstance(body, dict):
        models = body.get("data") or []
        match = None
        for m in models:
            if not model or m.get("id") == model:
                match = m
                if model:
                    break
        if match:
            result["fields"]["model"] = _field(
                match.get("id"), source="Lemonade /v1/models",
                layer="reported_effective")
            result["fields"]["model_status"] = _field(
                "listed", source="Lemonade /v1/models",
                layer="reported_effective")
        result["fields"]["server_version"] = _field(
            None, source="Lemonade", layer="server_configuration")
        result["errors"].append(
            "Detailed Lemonade parameter inspection is limited — many settings "
            "are Unknown unless the API exposes them.")
    else:
        result["errors"].append(f"/v1/models: {err or st}")
    return result


def inspect_model_configuration(*, engine: str | None, endpoint: str | None,
                                model: str | None,
                                configured_args: str | None = None,
                                profile: dict | None = None,
                                card: dict | None = None) -> dict:
    """Unified read-only inspector. Never loads models."""
    card = card or {}
    profile = profile or {}
    family = _engine_family(engine or card.get("engine"))
    endpoint = (endpoint or card.get("endpoint") or "").rstrip("/")
    model = model or card.get("model")

    overview = {
        "model": model,
        "friendly_name": profile.get("friendly_name") or card.get("friendly_name") or model,
        "machine": card.get("machine"),
        "engine": engine or card.get("engine"),
        "engine_family": family,
        "endpoint": endpoint or None,
        "model_status": card.get("model_status"),
        "quantization": card.get("quantization"),
        "size_bytes": card.get("size_bytes"),
        "model_path": card.get("model_path"),
    }

    backend = {"engine": family, "fields": {}, "errors": ["no endpoint"], "raw": {}}
    if endpoint and model:
        if family == "ollama":
            backend = inspect_ollama_model(endpoint, model)
        elif family == "llama-server":
            backend = inspect_llama_server(endpoint, model, configured_args)
        elif family == "lm-studio":
            backend = inspect_lm_studio(endpoint, model)
        elif family == "lemonade":
            backend = inspect_lemonade(endpoint, model)
        else:
            backend = {
                "engine": family, "fields": {}, "raw": {},
                "errors": [f"No dedicated inspector for engine family {family!r}"],
            }
    elif endpoint and family == "llama-server":
        backend = inspect_llama_server(endpoint, model, configured_args)
    elif not endpoint:
        backend = {"engine": family, "fields": {}, "raw": {},
                   "errors": ["No endpoint available for live inspection"]}

    # Saved profile layers (never presented as live)
    profile_fields = {}
    for key in ("temperature", "max_output", "context", "thinking", "top_p",
                "top_k", "repeat_penalty", "keep_alive"):
        req = (profile.get("request_settings") or {}).get(key)
        start = (profile.get("startup_settings") or {}).get(key)
        if req is not None:
            profile_fields[key] = _field(
                req, source="Fleet Monitor saved profile (request preference)",
                layer="saved_profile")
        elif start is not None:
            profile_fields[key] = _field(
                start, source="Fleet Monitor saved profile (startup preference)",
                layer="saved_profile")

    # Card-reported context
    if card.get("context_tokens") is not None:
        backend["fields"].setdefault("context_window_card", _field(
            card.get("context_tokens"),
            source="Fleet Monitor last discovery snapshot",
            layer="reported_effective"))

    return {
        "overview": overview,
        "backend": backend,
        "profile_fields": profile_fields,
        "layers_legend": {
            "model_default": "Defaults from the model / Modelfile / weights metadata",
            "server_configuration": "Stored launch/server configuration",
            "saved_profile": "Fleet Monitor profile preferences (not necessarily live)",
            "client_request_override": "Values sent by a client for one request",
            "reported_effective": "Values verified from a live backend response",
        },
        "read_only": True,
        "mutated": False,
    }


# ---------------------------------------------------------------------------
# Stop reasons, conflicts, rules engine
# ---------------------------------------------------------------------------

STOP_REASONS = {
    "completed_normally": "Completed Normally",
    "output_limit_reached": "Output Limit Reached",
    "context_limit_exceeded": "Context Limit Exceeded",
    "request_timed_out": "Request Timed Out",
    "connection_lost": "Connection Lost",
    "model_unavailable": "Model Unavailable",
    "streaming_interrupted": "Streaming Interrupted",
    "unknown": "Unknown Completion Reason",
}


def classify_stop_reason(*, ok: bool | None = None,
                         done_reason: str | None = None,
                         finish_reason: str | None = None,
                         error: str | None = None,
                         timed_out: bool = False,
                         connection_error: bool = False,
                         completion_tokens: int | None = None,
                         max_output: int | None = None,
                         stream_complete: bool | None = None) -> dict:
    evidence = []
    code = "unknown"
    if timed_out:
        code = "request_timed_out"
        evidence.append("Client or transport timeout flagged")
    elif connection_error:
        code = "connection_lost"
        evidence.append("Connection error before completion")
    elif error:
        el = error.lower()
        if "timeout" in el or "timed out" in el:
            code = "request_timed_out"
        elif "connection" in el or "refused" in el or "reset" in el:
            code = "connection_lost"
        elif "not currently loaded" in el or "not available" in el or "model" in el and "load" in el:
            code = "model_unavailable"
        elif "context" in el or "n_ctx" in el or "too long" in el:
            code = "context_limit_exceeded"
        else:
            code = "unknown"
        evidence.append(f"error: {error[:240]}")
    else:
        reason = (done_reason or finish_reason or "").lower().strip()
        if reason in ("stop", "end", "eos", "completed", "done"):
            code = "completed_normally"
            evidence.append(f"backend reason={done_reason or finish_reason}")
        elif reason in ("length", "max_tokens", "max_token", "num_predict"):
            code = "output_limit_reached"
            evidence.append(f"backend reason={done_reason or finish_reason}")
        elif reason in ("context", "context_length", "length_context"):
            code = "context_limit_exceeded"
            evidence.append(f"backend reason={done_reason or finish_reason}")
        elif ok is True and not reason:
            # Heuristic only when max_output known and counts match
            if (completion_tokens is not None and max_output is not None
                    and completion_tokens >= max_output):
                code = "output_limit_reached"
                evidence.append(
                    f"completion_tokens ({completion_tokens}) >= max_output ({max_output})")
                evidence.append("Backend did not provide an explicit stop reason")
            else:
                code = "completed_normally"
                evidence.append("Request succeeded without an explicit stop reason")
        elif stream_complete is False:
            code = "streaming_interrupted"
            evidence.append("Stream ended without completion marker")

    confidence = "confirmed"
    if code == "unknown":
        confidence = "insufficient_evidence"
    elif code == "output_limit_reached" and not (done_reason or finish_reason):
        confidence = "probable"
    elif code == "completed_normally" and not (done_reason or finish_reason):
        confidence = "probable"

    return {
        "code": code,
        "label": STOP_REASONS.get(code, STOP_REASONS["unknown"]),
        "confidence": confidence,
        "evidence": evidence,
        "raw_done_reason": done_reason,
        "raw_finish_reason": finish_reason,
    }


def detect_config_conflicts(*, inspection: dict,
                            request: dict | None = None) -> list[dict]:
    """Compare layers; do not treat intentional overrides as automatic errors."""
    request = request or {}
    findings = []
    fields = (inspection.get("backend") or {}).get("fields") or {}
    profile_fields = inspection.get("profile_fields") or {}

    def val(block):
        return block.get("value") if isinstance(block, dict) else None

    model_ctx = val(fields.get("context_window")) or val(fields.get("context_window_reported"))
    req_ctx = request.get("context") or request.get("num_ctx")
    if model_ctx and req_ctx and int(model_ctx) != int(req_ctx):
        findings.append({
            "code": "context_override",
            "severity": "intentional_override",
            "title": "Request context differs from model default",
            "detail": f"Model/default context {model_ctx}; request used {req_ctx}.",
            "confidence": "confirmed",
        })

    model_temp = val(fields.get("temperature"))
    req_temp = request.get("temperature")
    if model_temp is not None and req_temp is not None and float(model_temp) != float(req_temp):
        findings.append({
            "code": "temperature_override",
            "severity": "intentional_override",
            "title": "Request temperature differs from model default",
            "detail": f"Default {model_temp}; request {req_temp}.",
            "confidence": "confirmed",
        })

    saved_think = val(profile_fields.get("thinking"))
    req_think = request.get("think") or request.get("thinking")
    if saved_think is False and req_think in (True, "enabled", "on"):
        findings.append({
            "code": "thinking_conflict",
            "severity": "potential_conflict",
            "title": "Thinking enabled despite saved disabled preference",
            "detail": "Saved profile prefers thinking off; this request enabled it.",
            "confidence": "probable",
        })

    expected_model = (inspection.get("overview") or {}).get("model")
    actual_model = request.get("model")
    if expected_model and actual_model and expected_model != actual_model:
        findings.append({
            "code": "model_mismatch",
            "severity": "confirmed_mismatch",
            "title": "Request model differs from inspected model card",
            "detail": f"Card/profile model {expected_model!r}; request used {actual_model!r}.",
            "confidence": "confirmed",
        })

    if request.get("unsupported_params"):
        findings.append({
            "code": "unsupported_params",
            "severity": "potential_conflict",
            "title": "Unsupported parameters were passed",
            "detail": f"Params: {request.get('unsupported_params')}",
            "confidence": "probable",
        })

    if not findings:
        findings.append({
            "code": "none",
            "severity": "unknown",
            "title": "No configuration conflicts detected from available evidence",
            "detail": "Absence of findings is not proof of perfect configuration.",
            "confidence": "insufficient_evidence",
        })
    return findings


def troubleshoot_record(record: dict) -> list[dict]:
    """Deterministic rules-based findings — never invents root causes."""
    findings = []
    stop = record.get("stop_reason") or {}
    code = stop.get("code") or "unknown"
    err = record.get("error")
    max_out = record.get("request", {}).get("max_output") or record.get("request", {}).get("max_tokens")
    comp = record.get("completion_tokens")
    prompt = record.get("prompt_tokens")
    tps = record.get("tokens_per_sec")
    elapsed = record.get("elapsed_ms")

    def add(what, evidence, cause, next_step, confidence, severity="info"):
        findings.append({
            "what_happened": what,
            "evidence": evidence,
            "likely_cause": cause,
            "suggested_next_step": next_step,
            "confidence": confidence,
            "severity": severity,
        })

    if code == "output_limit_reached":
        add("Generation stopped because an output-token limit was reached.",
            stop.get("evidence") or [f"completion_tokens={comp}", f"max_output={max_out}"],
            "The client or model configuration capped generated tokens "
            "(for example AI Lab Direct Chat uses a small max_tokens for speed).",
            "Inspect the request max_tokens/num_predict in Diagnostics → Recent Requests. "
            "Raise it only if you intentionally want longer answers.",
            stop.get("confidence") or "probable", "warn")
    elif code == "context_limit_exceeded":
        add("The backend reported a context-size problem.",
            stop.get("evidence") or [err],
            "Prompt + history exceeded the effective context window, or the "
            "backend rejected the request for context reasons.",
            "Compare requested context vs model/server context in the Configuration tab.",
            "confirmed", "error")
    elif code == "request_timed_out":
        add("The request timed out before completion.",
            stop.get("evidence") or [err],
            "Client timeout, overloaded host, or extremely slow generation.",
            "Check machine load and tokens/sec history. Avoid long runs while diagnosing.",
            "confirmed", "error")
    elif code == "connection_lost":
        add("The connection failed before generation completed.",
            stop.get("evidence") or [err],
            "Backend restarted, network interruption, or refused connection.",
            "Verify the endpoint is online in Model Control Center.",
            "confirmed", "error")
    elif code == "model_unavailable":
        add("The requested model could not serve inference.",
            stop.get("evidence") or [err],
            "Model not loaded / not installed / wrong endpoint.",
            "Confirm loaded state without auto-loading; load manually if needed.",
            "confirmed", "error")
    elif code == "unknown" and err:
        add("The request failed without a classified stop reason.",
            [err],
            None,
            "Export a diagnostic report and inspect the raw error text.",
            "insufficient_evidence", "warn")
    elif code == "completed_normally":
        if (comp is not None and max_out is not None and comp >= int(max_out)
                and not stop.get("raw_done_reason") and not stop.get("raw_finish_reason")):
            add("Response may have hit an output cap even though completion looked successful.",
                [f"completion_tokens={comp}", f"max_output={max_out}",
                 "No explicit backend stop reason"],
                "Probable output limit — not confirmed truncation-from-model-failure.",
                "Re-check with a higher max_tokens only if longer output is desired; "
                "do not assume the model weights are defective.",
                "probable", "warn")

    if prompt is not None and comp is not None and prompt < 0:
        add("Suspicious token accounting detected.",
            [f"prompt_tokens={prompt}", f"completion_tokens={comp}"],
            None,
            "Treat token counts as unreliable for this request.",
            "insufficient_evidence", "warn")

    if tps is not None and tps < 1 and elapsed and elapsed > 5000:
        add("Generation was unusually slow based on measured tokens/sec.",
            [f"tokens_per_sec={tps}", f"elapsed_ms={elapsed}"],
            "Possible CPU offload, memory pressure, or cold start — not confirmed.",
            "Compare with Performance History for this model.",
            "probable", "info")

    if not findings:
        add("No automated troubleshooting findings from available evidence.",
            ["Record inspected"],
            None,
            "Review Configuration and Recent Requests manually.",
            "insufficient_evidence", "info")
    return findings


# ---------------------------------------------------------------------------
# History store + reports
# ---------------------------------------------------------------------------

def default_history_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), HISTORY_FILENAME)


class DiagnosticStore:
    def __init__(self, path: str | None = None, maxlen: int = HISTORY_MAX):
        self.path = path or default_history_path()
        self.maxlen = maxlen
        self._records: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("records"), list):
                self._records = data["records"][-self.maxlen:]
            elif isinstance(data, list):
                self._records = data[-self.maxlen:]
        except FileNotFoundError:
            self._records = []
        except Exception:
            self._records = []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "records": self._records}, fh, indent=2)
        os.replace(tmp, self.path)

    def add(self, record: dict) -> dict:
        with _lock:
            rec = deepcopy(record)
            rec.setdefault("id", uuid.uuid4().hex[:12])
            rec.setdefault("ts", time.time())
            # Privacy: never persist prompts/responses/secrets
            for banned in ("prompt", "messages", "content", "response",
                           "thinking_text", "api_key", "authorization",
                           "headers"):
                rec.pop(banned, None)
            if "request" in rec and isinstance(rec["request"], dict):
                for banned in ("messages", "prompt", "content"):
                    rec["request"].pop(banned, None)
            stop = classify_stop_reason(
                ok=rec.get("ok"),
                done_reason=rec.get("done_reason"),
                finish_reason=rec.get("finish_reason"),
                error=rec.get("error"),
                timed_out=bool(rec.get("timed_out")),
                connection_error=bool(rec.get("connection_error")),
                completion_tokens=rec.get("completion_tokens"),
                max_output=(rec.get("request") or {}).get("max_tokens")
                or (rec.get("request") or {}).get("max_output"),
                stream_complete=rec.get("stream_complete"),
            )
            rec["stop_reason"] = stop
            rec["findings"] = troubleshoot_record(rec)
            self._records.append(rec)
            self._records = self._records[-self.maxlen:]
            try:
                self._save()
            except OSError:
                pass
            return deepcopy(rec)

    def list(self, *, model: str | None = None, machine: str | None = None,
             engine: str | None = None, client: str | None = None,
             since: float | None = None, limit: int = 50) -> list[dict]:
        with _lock:
            rows = list(self._records)
        out = []
        for r in reversed(rows):
            if model and r.get("model") != model:
                continue
            if machine and r.get("machine") != machine:
                continue
            if engine and (r.get("engine") or "").lower() != engine.lower():
                continue
            if client and r.get("client") != client:
                continue
            if since and (r.get("ts") or 0) < since:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def performance_summary(self, *, model: str | None = None,
                            machine: str | None = None) -> dict:
        rows = self.list(model=model, machine=machine, limit=self.maxlen)
        completed = [r for r in rows if r.get("ok")]
        failed = [r for r in rows if not r.get("ok")]
        speeds = [r.get("tokens_per_sec") for r in completed
                  if isinstance(r.get("tokens_per_sec"), (int, float))]
        durations = [r.get("elapsed_ms") for r in completed
                     if isinstance(r.get("elapsed_ms"), (int, float))]
        timeouts = sum(1 for r in rows
                       if (r.get("stop_reason") or {}).get("code") == "request_timed_out")
        out_limits = sum(1 for r in rows
                         if (r.get("stop_reason") or {}).get("code") == "output_limit_reached")
        return {
            "sample_size": len(rows),
            "requests_completed": len(completed),
            "requests_failed": len(failed),
            "average_tokens_per_sec": (
                round(sum(speeds) / len(speeds), 2) if speeds else None),
            "recent_tokens_per_sec": speeds[:10],
            "average_elapsed_ms": (
                round(sum(durations) / len(durations), 1) if durations else None),
            "timeout_count": timeouts,
            "output_limit_occurrences": out_limits,
            "note": "Derived from captured request metadata only — not a synthetic benchmark.",
        }


def sanitize_report(payload: dict) -> dict:
    """Strip secrets and conversation content from exportable reports."""
    banned_keys = {
        "api_key", "authorization", "password", "token", "secret",
        "messages", "prompt", "content", "response", "thinking_text",
        "headers", "cookie", "set-cookie",
    }
    path_re = re.compile(r"(/home/[^/\s]+|/Users/[^/\s]+|[A-Za-z]:\\\\Users\\\\[^\\]+)")

    def scrub(obj):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if k.lower() in banned_keys or k.lower().endswith("_key"):
                    continue
                out[k] = scrub(v)
            return out
        if isinstance(obj, list):
            return [scrub(x) for x in obj]
        if isinstance(obj, str):
            # Redact home directories in paths when not essential
            return path_re.sub("[redacted-path]", obj)
        return obj

    return scrub(deepcopy(payload))


def format_report_text(report: dict) -> str:
    lines = ["Kayock Fleet Monitor — Diagnostic Report", "=" * 48]
    ov = report.get("overview") or {}
    lines += [
        f"Timestamp: {report.get('generated_at_iso') or report.get('generated_at')}",
        f"Model: {ov.get('model')}",
        f"Friendly name: {ov.get('friendly_name')}",
        f"Machine: {ov.get('machine')}",
        f"Engine: {ov.get('engine')}",
        f"Endpoint: {ov.get('endpoint')}",
        f"Status: {ov.get('model_status')}",
        "",
        "Configuration fields:",
    ]
    fields = ((report.get("inspection") or {}).get("backend") or {}).get("fields") or {}
    for k, meta in fields.items():
        if not isinstance(meta, dict):
            continue
        lines.append(
            f"  - {k}: {meta.get('display')} "
            f"[{meta.get('layer')}; {meta.get('source')}]")
    if report.get("recent_requests"):
        lines.append("")
        lines.append("Recent requests:")
        for r in report["recent_requests"][:10]:
            stop = (r.get("stop_reason") or {}).get("label")
            lines.append(
                f"  - {r.get('id')} client={r.get('client')} ok={r.get('ok')} "
                f"stop={stop} completion_tokens={r.get('completion_tokens')} "
                f"elapsed_ms={r.get('elapsed_ms')}")
    if report.get("findings"):
        lines.append("")
        lines.append("Troubleshooting:")
        for f in report["findings"]:
            lines.append(f"  * {f.get('what_happened')}")
            lines.append(f"    Evidence: {f.get('evidence')}")
            if f.get("likely_cause"):
                lines.append(f"    Likely cause: {f.get('likely_cause')}")
            lines.append(f"    Next: {f.get('suggested_next_step')}")
            lines.append(f"    Confidence: {f.get('confidence')}")
    lines.append("")
    lines.append("Privacy: prompts, responses, and secrets are excluded by default.")
    return "\n".join(lines)


def build_diagnostic_report(*, inspection: dict, recent: list[dict],
                            performance: dict | None = None,
                            include_raw: bool = False) -> dict:
    findings = []
    for r in recent[:5]:
        findings.extend(r.get("findings") or [])
    conflicts = detect_config_conflicts(inspection=inspection)
    report = {
        "generated_at": time.time(),
        "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overview": inspection.get("overview"),
        "inspection": {
            "backend": {
                "engine": (inspection.get("backend") or {}).get("engine"),
                "fields": (inspection.get("backend") or {}).get("fields"),
                "errors": (inspection.get("backend") or {}).get("errors"),
            },
            "profile_fields": inspection.get("profile_fields"),
            "layers_legend": inspection.get("layers_legend"),
        },
        "recent_requests": recent[:20],
        "performance": performance,
        "conflicts": conflicts,
        "findings": findings[:20],
        "privacy": {
            "prompts_excluded": True,
            "responses_excluded": True,
            "secrets_excluded": True,
        },
    }
    if include_raw:
        report["inspection"]["backend"]["raw"] = (
            (inspection.get("backend") or {}).get("raw"))
    return sanitize_report(report)
