"""Model Control Center helpers for Kayock Fleet Monitor v3.2.

Engine capability matrix, launch-arg parsing (active vs configured vs pending),
persistent user profiles, inventory card shaping, and memory-warning estimates.

Does not start/stop/load/unload by itself — callers use AIManager / fleet proxy.
Never invents metrics the backends did not report.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from typing import Any

# ---------------------------------------------------------------------------
# Engine capability matrix
# support: runtime | per_request | load_time | startup | unsupported | unknown
# apply:   immediate | per_request | reload | restart | unsupported | unknown
# ---------------------------------------------------------------------------

ENGINE_CAPABILITIES: dict[str, dict[str, Any]] = {
    "ollama": {
        "label": "Ollama",
        "can_load": True,
        "can_unload": True,
        "can_start": True,
        "can_stop": True,
        "can_restart": True,
        "model_switch": "runtime_load",
        "notes": "Load/unload via keep_alive. Modelfile defaults are not rewritten.",
        "parameters": {
            "context": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider_number", "label": "Context window",
                "help": "How much information the model can consider during a request.",
                "note": "Per-request / Modelfile; not a global live server setting.",
            },
            "max_output": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider_number", "label": "Maximum output tokens",
                "help": "Maximum length of the generated answer.",
            },
            "temperature": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider_number", "label": "Temperature",
                "help": "Controls randomness in generated responses.",
                "min": 0, "max": 2, "step": 0.05,
            },
            "top_p": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider", "label": "Top-P", "min": 0, "max": 1, "step": 0.01,
            },
            "top_k": {
                "support": "per_request", "apply": "per_request",
                "ui": "number", "label": "Top-K",
            },
            "repeat_penalty": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider", "label": "Repeat penalty", "min": 0.5, "max": 2, "step": 0.05,
            },
            "thinking": {
                "support": "per_request", "apply": "per_request",
                "ui": "toggle", "label": "Thinking mode",
                "help": "Controls reasoning behavior on models/APIs that support it.",
            },
            "keep_alive": {
                "support": "load_time", "apply": "reload",
                "ui": "dropdown_custom", "label": "Keep alive",
                "help": "How long the model stays resident after a request.",
            },
            "gpu_layers": {
                "support": "unsupported", "apply": "unsupported",
                "ui": "none", "label": "GPU layers",
                "help": "Not exposed as a runtime control for Ollama in this app.",
            },
            "launch_args": {
                "support": "startup", "apply": "restart",
                "ui": "text", "label": "Launch arguments",
            },
        },
    },
    "llama-server": {
        "label": "llama.cpp / llama-server",
        "can_load": False,
        "can_unload": False,
        "can_start": True,
        "can_stop": True,
        "can_restart": True,
        "model_switch": "restart_with_model",
        "notes": ("Single resident model. Changing model/context/GPU layers requires "
                  "a managed restart — Unload is not available while leaving the "
                  "server running."),
        "parameters": {
            "context": {
                "support": "startup", "apply": "restart",
                "ui": "slider_number", "label": "Context window",
                "help": "How much information the model can consider during a request.",
                "arg_keys": ["-c", "--ctx-size", "--context-size"],
            },
            "gpu_layers": {
                "support": "startup", "apply": "restart",
                "ui": "slider_number", "label": "GPU layers",
                "help": "How much of the model is offloaded to the GPU when supported.",
                "arg_keys": ["-ngl", "--gpu-layers", "--n-gpu-layers"],
            },
            "threads": {
                "support": "startup", "apply": "restart",
                "ui": "auto_manual", "label": "CPU threads",
                "arg_keys": ["-t", "--threads"],
            },
            "parallel": {
                "support": "startup", "apply": "restart",
                "ui": "dropdown_custom", "label": "Parallel slots",
                "arg_keys": ["-np", "--parallel"],
            },
            "batch": {
                "support": "startup", "apply": "restart",
                "ui": "number", "label": "Batch size",
                "arg_keys": ["-b", "--batch-size"],
            },
            "temperature": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider_number", "label": "Temperature",
                "help": "Controls randomness in generated responses.",
                "min": 0, "max": 2, "step": 0.05,
            },
            "max_output": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider_number", "label": "Maximum output tokens",
                "help": "Maximum length of the generated answer.",
            },
            "top_p": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider", "label": "Top-P", "min": 0, "max": 1, "step": 0.01,
            },
            "top_k": {
                "support": "per_request", "apply": "per_request",
                "ui": "number", "label": "Top-K",
            },
            "repeat_penalty": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider", "label": "Repeat penalty", "min": 0.5, "max": 2, "step": 0.05,
            },
            "thinking": {
                "support": "unsupported", "apply": "unsupported",
                "ui": "none", "label": "Thinking mode",
            },
            "launch_args": {
                "support": "startup", "apply": "restart",
                "ui": "text", "label": "Launch arguments",
                "help": "Expert Mode edits the raw args string; never rewritten silently.",
            },
        },
    },
    "lemonade": {
        "label": "Lemonade",
        "can_load": True,
        "can_unload": True,
        "can_start": True,
        "can_stop": True,
        "can_restart": True,
        "model_switch": "runtime_load",
        "notes": "Load/unload via Lemonade HTTP API when reachable.",
        "parameters": {
            "context": {
                "support": "unknown", "apply": "unknown",
                "ui": "number", "label": "Context window",
                "note": "Unknown — not reported by backend unless Lemonade exposes it.",
            },
            "temperature": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider_number", "label": "Temperature", "min": 0, "max": 2, "step": 0.05,
            },
            "max_output": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider_number", "label": "Maximum output tokens",
            },
            "launch_args": {
                "support": "startup", "apply": "restart",
                "ui": "text", "label": "Launch arguments",
            },
        },
    },
    "lm-studio": {
        "label": "LM Studio",
        "can_load": True,
        "can_unload": True,
        "can_start": True,
        "can_stop": True,
        "can_restart": True,
        "model_switch": "runtime_load",
        "notes": "Uses LM Studio /api/v0/models/load and unload.",
        "parameters": {
            "context": {
                "support": "unknown", "apply": "unknown",
                "ui": "number", "label": "Context window",
            },
            "gpu_layers": {
                "support": "unknown", "apply": "unknown",
                "ui": "number", "label": "GPU layers",
            },
            "temperature": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider_number", "label": "Temperature", "min": 0, "max": 2, "step": 0.05,
            },
            "max_output": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider_number", "label": "Maximum output tokens",
            },
            "launch_args": {
                "support": "startup", "apply": "restart",
                "ui": "text", "label": "Launch arguments",
            },
        },
    },
    "vllm": {
        "label": "vLLM",
        "can_load": False, "can_unload": False,
        "can_start": True, "can_stop": True, "can_restart": True,
        "model_switch": "unsupported",
        "notes": "Monitoring only for model management — no fake load/unload.",
        "parameters": {},
    },
    "sglang": {
        "label": "SGLang",
        "can_load": False, "can_unload": False,
        "can_start": True, "can_stop": True, "can_restart": True,
        "model_switch": "unsupported",
        "notes": "Monitoring only for model management — no fake load/unload.",
        "parameters": {},
    },
    "openai": {
        "label": "OpenAI-compatible",
        "can_load": False, "can_unload": False,
        "can_start": True, "can_stop": True, "can_restart": True,
        "model_switch": "unsupported",
        "notes": "Generic OpenAI-compatible endpoints are monitored only.",
        "parameters": {
            "temperature": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider_number", "label": "Temperature", "min": 0, "max": 2, "step": 0.05,
            },
            "max_output": {
                "support": "per_request", "apply": "per_request",
                "ui": "slider_number", "label": "Maximum output tokens",
            },
        },
    },
}

PROFILE_TEMPLATES: list[dict[str, Any]] = [
    {"name": "Fast", "description": "Lower context / quicker responses (template only).",
     "settings": {"context": 8192, "temperature": 0.4, "max_output": 1024}},
    {"name": "Balanced", "description": "General-purpose defaults (template only).",
     "settings": {"context": 32768, "temperature": 0.7, "max_output": 2048}},
    {"name": "Deep Thinking", "description": "Thinking-friendly request prefs (template only).",
     "settings": {"temperature": 0.6, "thinking": True, "max_output": 4096}},
    {"name": "Coding", "description": "Lower temperature coding prefs (template only).",
     "settings": {"temperature": 0.2, "max_output": 4096}},
    {"name": "Creative", "description": "Higher temperature creative prefs (template only).",
     "settings": {"temperature": 1.0, "top_p": 0.95, "max_output": 2048}},
    {"name": "Benchmark", "description": "Stable benchmark request prefs (template only).",
     "settings": {"temperature": 0.0, "max_output": 512}},
    {"name": "Custom", "description": "Empty custom profile.", "settings": {}},
]


def capabilities_for(engine: str | None) -> dict[str, Any]:
    key = (engine or "openai").lower().strip()
    if key in ("llama.cpp", "llamacpp", "llama"):
        key = "llama-server"
    if key in ("lmstudio", "lm_studio"):
        key = "lm-studio"
    base = ENGINE_CAPABILITIES.get(key) or ENGINE_CAPABILITIES["openai"]
    out = deepcopy(base)
    out["engine"] = key
    return out


def split_args(value: str | list | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    # lightweight shell-ish split respecting double quotes
    out: list[str] = []
    buf = ""
    in_q = False
    for ch in str(value):
        if ch == '"':
            in_q = not in_q
            buf += ch
            continue
        if ch.isspace() and not in_q:
            if buf:
                out.append(buf.strip('"'))
                buf = ""
            continue
        buf += ch
    if buf:
        out.append(buf.strip('"'))
    return out


def parse_startup_settings(engine: str | None, args: str | list | None) -> dict[str, Any]:
    """Extract known startup settings from configured launch args (configured, not live)."""
    caps = capabilities_for(engine)
    tokens = split_args(args)
    configured: dict[str, Any] = {"launch_args": " ".join(tokens) if tokens else ""}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        matched = False
        for pname, meta in (caps.get("parameters") or {}).items():
            keys = meta.get("arg_keys") or []
            if tok in keys:
                val = tokens[i + 1] if i + 1 < len(tokens) else True
                if isinstance(val, str) and val.startswith("-"):
                    configured[pname] = True
                else:
                    try:
                        if isinstance(val, str) and re.fullmatch(r"-?\d+", val):
                            configured[pname] = int(val)
                        elif isinstance(val, str) and re.fullmatch(r"-?\d+\.\d+", val):
                            configured[pname] = float(val)
                        else:
                            configured[pname] = val
                    except Exception:
                        configured[pname] = val
                    i += 1
                matched = True
                break
        if not matched and tok in ("-m", "--model", "--model-path"):
            if i + 1 < len(tokens):
                configured["model_path"] = tokens[i + 1]
                i += 1
        i += 1
    return configured


def settings_view(*, engine: str | None, configured_args: str | None,
                  live_context: Any = None, live_model: str | None = None,
                  pending: dict | None = None) -> dict[str, Any]:
    """Distinguish active (verified live) vs configured vs pending."""
    caps = capabilities_for(engine)
    configured = parse_startup_settings(engine, configured_args)
    active: dict[str, Any] = {}
    if live_context is not None:
        active["context"] = live_context
    else:
        active["context"] = None  # Unknown — not reported
    active["model"] = live_model
    # Startup params are not "active" unless we verified them from the process.
    for key in ("gpu_layers", "threads", "parallel", "batch"):
        active[key] = None
    pending = dict(pending or {})
    changes = []
    restart_required = False
    for key, new_val in pending.items():
        meta = (caps.get("parameters") or {}).get(key) or {}
        cur = active.get(key)
        if cur is None:
            cur = configured.get(key)
        if cur != new_val:
            apply = meta.get("apply") or "unknown"
            changes.append({
                "parameter": key,
                "label": meta.get("label") or key,
                "current": cur if cur is not None else "Unknown — not reported by backend",
                "requested": new_val,
                "apply": apply,
                "support": meta.get("support") or "unknown",
            })
            if apply == "restart":
                restart_required = True
    return {
        "engine": caps.get("engine"),
        "capabilities": caps,
        "active": active,
        "configured": configured,
        "pending": pending,
        "changes": changes,
        "restart_required": restart_required,
    }


def estimate_memory_warning(*, model_size_bytes: int | None,
                            available_ram_bytes: int | None,
                            available_vram_bytes: int | None = None,
                            context_tokens: int | None = None) -> dict[str, Any]:
    """Heuristic warning only — clearly labeled as estimate, never guaranteed."""
    if model_size_bytes is None and available_ram_bytes is None:
        return {
            "warn": False,
            "reason": "insufficient measurements",
            "estimate_label": "estimate",
        }
    # Rough working-set estimate: model weights + optional KV fudge.
    estimate = model_size_bytes or 0
    if context_tokens:
        # Very rough 2 bytes * layers-unknown; keep a conservative additive fudge.
        estimate = int(estimate + min(context_tokens, 200_000) * 16)
    avail = available_ram_bytes
    pressure = False
    remaining = None
    if avail is not None and estimate:
        remaining = avail - estimate
        pressure = remaining < max(1_073_741_824, int(avail * 0.15))  # <1GiB or <15%
    return {
        "warn": bool(pressure),
        "estimate_bytes": estimate or None,
        "available_ram_bytes": available_ram_bytes,
        "available_vram_bytes": available_vram_bytes,
        "estimated_remaining_bytes": remaining,
        "estimate_label": "estimate — not guaranteed",
        "message": (
            "Selected model may require substantial additional memory."
            if pressure else None
        ),
    }


def stable_model_identity(*, machine: str, machine_id: str | None,
                          server: dict, model: str | None) -> dict:
    """Stable identity — never display-name alone."""
    host = server.get("host") or server.get("lan_host") or ""
    port = server.get("port")
    sid = server.get("id") or ""
    skey = server.get("key") or f"{host}:{port}"
    mid = (machine_id or machine or "").strip()
    model_id = (model or "").strip() or "_"
    # Deterministic key without relying on friendly names.
    key = f"{mid}|{skey}|{model_id}"
    return {
        "key": key,
        "machine": machine,
        "machine_id": machine_id,
        "server_id": sid or None,
        "server_key": skey,
        "host": host,
        "port": port,
        "model": model if model else None,
        "endpoint": server.get("url") or (
            f"http://{host}:{port}" if host and port else None),
    }


def guess_association(model: str | None) -> dict | None:
    """Evidence-light association hint — always unverified unless confirmed."""
    if not model:
        return None
    m = model.lower()
    if m.startswith("saturday-gm-") or "gamemaster" in m or "gemmasutra" in m:
        return {
            "name": "Saturday Multiverse",
            "status": "possible_unverified",
            "label": "Possible association — unverified",
            "evidence": "model identifier prefix/pattern only",
        }
    return None


def default_friendly_name(model: str | None, machine: str) -> str:
    if not model:
        return f"Server on {machine}"
    # Keep readable; do not invent marketing names.
    return model


def management_status(*, configured: bool, is_self: bool, allow_control: bool,
                      caps: dict, server_caps: dict,
                      server_status: str | None) -> dict:
    """Clear why a model can/cannot be managed.

    Registration (configured) is checked before remote-control authorization so
    unregistered peers correctly show Setup required rather than only
    remote-control messaging.
    """
    if not configured:
        detail = "Server is discovered but not registered for management."
        if not is_self and not allow_control:
            detail += " Remote registration also requires Allow remote control."
        return {
            "code": "setup_required",
            "label": "Setup required",
            "detail": detail,
            "needs_setup": True,
            "manageable": False,
        }
    if not is_self and not allow_control:
        return {
            "code": "remote_control_disabled",
            "label": "Remote control disabled",
            "detail": "Server is registered, but Allow remote control is off for this machine.",
            "needs_setup": False,
            "manageable": False,
        }
    if server_status in ("offline",):
        return {
            "code": "server_unavailable",
            "label": "Server unavailable",
            "detail": "Configured server is offline.",
            "needs_setup": False,
            "manageable": False,
        }
    # Model-management vs process-management are separate.
    model_mgmt = any([
        server_caps.get("can_load") and caps.get("can_load"),
        server_caps.get("can_unload") and caps.get("can_unload"),
    ])
    process_mgmt = any([
        server_caps.get("can_start"),
        server_caps.get("can_stop"),
        server_caps.get("can_restart"),
    ])
    switch_via_restart = (
        caps.get("model_switch") == "restart_with_model"
        and server_caps.get("can_restart"))
    can_manage = bool(model_mgmt or process_mgmt or switch_via_restart)
    if not can_manage and not (caps.get("can_load") or caps.get("can_unload")
                               or caps.get("model_switch") == "restart_with_model"):
        return {
            "code": "backend_unsupported",
            "label": "Backend unsupported",
            "detail": caps.get("notes") or "This engine does not support model management here.",
            "needs_setup": False,
            "manageable": False,
        }
    if can_manage:
        return {
            "code": "manageable",
            "label": "Manageable",
            "detail": "Authorized management actions are available.",
            "needs_setup": False,
            "manageable": True,
            "model_management": bool(model_mgmt or switch_via_restart),
            "process_management": bool(process_mgmt),
        }
    return {
        "code": "monitoring_only",
        "label": "Monitoring only",
        "detail": "Server is registered but no model-management actions are available "
                  "(process control may still require an executable path).",
        "needs_setup": False,
        "manageable": False,
    }


def stable_server_identity(*, machine: str, machine_id: str | None,
                           server: dict) -> dict:
    """Stable per-server identity — not display-name based."""
    host = server.get("host") or server.get("lan_host") or ""
    port = server.get("port")
    sid = server.get("id") or ""
    skey = server.get("key") or (f"{host}:{port}" if host and port else "")
    mid = (machine_id or machine or "").strip()
    engine = (server.get("type") or server.get("api_type") or server.get("engine")
              or "unknown")
    key = f"{mid}|{skey}|{engine}"
    return {
        "key": key,
        "machine": machine,
        "machine_id": machine_id,
        "server_id": sid or None,
        "server_key": skey or None,
        "engine": engine,
        "host": host,
        "port": port,
        "endpoint": server.get("url") or (
            f"http://{host}:{port}" if host and port else None),
    }


def build_server_inventory(cards: list[dict]) -> list[dict]:
    """Collapse model cards into one row per inference server."""
    by: dict[str, dict] = {}
    for c in cards or []:
        skey = c.get("server_key") or c.get("server_id") or c.get("endpoint") or "_"
        mid = c.get("machine_id") or c.get("machine") or ""
        engine = c.get("engine") or "unknown"
        ident_key = f"{mid}|{skey}|{engine}"
        row = by.get(ident_key)
        if not row:
            scaps = c.get("server_capabilities") or {}
            caps = c.get("capabilities") or {}
            row = {
                "id": ident_key,
                "identity": {
                    "key": ident_key,
                    "machine": c.get("machine"),
                    "machine_id": c.get("machine_id"),
                    "server_key": c.get("server_key"),
                    "server_id": c.get("server_id"),
                    "engine": engine,
                    "endpoint": c.get("endpoint"),
                },
                "machine": c.get("machine"),
                "machine_id": c.get("machine_id"),
                "is_self": bool(c.get("is_self")),
                "allow_control": bool(c.get("allow_control")),
                "engine": engine,
                "engine_label": c.get("engine_label") or engine,
                "endpoint": c.get("endpoint"),
                "server_key": c.get("server_key"),
                "server_id": c.get("server_id"),
                "server_name": c.get("server_name"),
                "status": c.get("status"),
                "configured": bool(c.get("configured")),
                "management_status": c.get("management_status"),
                "model_count": 0,
                "models": [],
                "loaded_count": 0,
                "supported_actions": {
                    "load": bool(caps.get("can_load")),
                    "unload": bool(caps.get("can_unload")),
                    "start": bool(scaps.get("can_start")),
                    "stop": bool(scaps.get("can_stop")),
                    "restart": bool(scaps.get("can_restart")),
                    "switch_via_restart": caps.get("model_switch") == "restart_with_model",
                },
                "model_management": bool(caps.get("can_load") or caps.get("can_unload")),
                "process_management": bool(
                    scaps.get("can_start") or scaps.get("can_stop") or scaps.get("can_restart")),
                "eligible_for_registration": (
                    not bool(c.get("configured"))
                    and bool(c.get("server_key"))
                    and (bool(c.get("is_self")) or bool(c.get("allow_control")))
                ),
                "registration_blocked_reason": None,
            }
            if not c.get("server_key"):
                row["registration_blocked_reason"] = "Missing discovery server key."
            elif not c.get("is_self") and not c.get("allow_control"):
                row["registration_blocked_reason"] = (
                    "Remote control disabled — enable Allow remote control first.")
                row["eligible_for_registration"] = False
            elif c.get("configured"):
                row["registration_blocked_reason"] = "Already registered."
            by[ident_key] = row
        if c.get("model"):
            row["model_count"] += 1
            row["models"].append(c.get("model"))
            if c.get("model_status") == "loaded":
                row["loaded_count"] += 1
        # Prefer online-ish status if mixed
        if c.get("status") in ("online", "ready", "busy") and row.get("status") not in (
                "online", "ready", "busy"):
            row["status"] = c.get("status")
        if c.get("configured"):
            row["configured"] = True
            row["eligible_for_registration"] = False
            row["server_id"] = c.get("server_id") or row.get("server_id")
            row["management_status"] = c.get("management_status")
    # Deduplicate model lists
    for row in by.values():
        seen = set()
        uniq = []
        for m in row["models"]:
            if m not in seen:
                seen.add(m)
                uniq.append(m)
        row["models"] = uniq
        row["model_count"] = len(uniq)
        ms = row.get("management_status") or {}
        row["setup_required"] = bool(ms.get("needs_setup") or not row.get("configured"))
        row["setup_explanation"] = ms.get("detail") or (
            "Already registered." if row.get("configured")
            else "Register this server once to enable management for its models.")
    return sorted(by.values(), key=lambda r: (
        0 if r.get("is_self") else 1,
        r.get("machine") or "",
        r.get("engine") or "",
        r.get("endpoint") or "",
    ))


def fleet_setup_preview(cards: list[dict]) -> dict:
    """Fleet-wide server registration preview — never mutates."""
    servers = build_server_inventory(cards)
    eligible = [s for s in servers if s.get("eligible_for_registration")]
    registered = [s for s in servers if s.get("configured")]
    needs = [s for s in servers if s.get("setup_required")]
    attention = []
    for s in servers:
        if not s.get("is_self") and not s.get("allow_control"):
            attention.append({
                "machine": s.get("machine"),
                "machine_id": s.get("machine_id"),
                "reason": "Remote control disabled",
            })
    # de-dupe attention by machine
    seen_m = set()
    attn_uniq = []
    for a in attention:
        k = a.get("machine_id") or a.get("machine")
        if k in seen_m:
            continue
        seen_m.add(k)
        attn_uniq.append(a)
    model_cards = [c for c in cards if c.get("model")]
    manageable_models = [
        c for c in model_cards
        if (c.get("management_status") or {}).get("manageable")
    ]
    needs_models = [
        c for c in model_cards
        if (c.get("management_status") or {}).get("needs_setup")
    ]
    proposed = []
    for s in eligible:
        proposed.append({
            "action": "register_server",
            "server_id": s.get("id"),
            "machine": s.get("machine"),
            "machine_id": s.get("machine_id"),
            "is_self": s.get("is_self"),
            "allow_control": s.get("allow_control"),
            "server_key": s.get("server_key"),
            "engine": s.get("engine"),
            "endpoint": s.get("endpoint"),
            "model_count": s.get("model_count"),
            "writes": (
                "local ai_servers.json" if s.get("is_self")
                else f"peer {s.get('machine')} ai_servers.json via allow_control proxy"
            ),
            "does_not": [
                "start/stop/restart inference",
                "load/unload models",
                "rewrite launch arguments",
                "enable allow_control",
            ],
            "selected_default": True,
        })
    return {
        "summary": {
            "servers_discovered": len(servers),
            "servers_registered": len(registered),
            "servers_requiring_setup": len(needs),
            "servers_eligible": len(eligible),
            "models_discovered": len(model_cards),
            "models_manageable": len(manageable_models),
            "models_needing_setup": len(needs_models),
            "machines_needing_attention": attn_uniq,
        },
        "servers": servers,
        "proposed_registrations": proposed,
        "applies_changes": False,
        "note": (
            "Preview only — nothing was written. Confirm Configure Selected Servers "
            "to register eligible inference servers once each. Model cards inherit "
            "capabilities from their server after registration."
        ),
    }


def management_setup_preview(card: dict) -> dict:
    """Guided setup preview for one card — never mutates. Prefers server-level register."""
    ms = card.get("management_status") or {}
    steps = []
    proposed = []
    if not card.get("configured"):
        steps.append("Server is discovered but not registered for management.")
        if card.get("is_self") or card.get("allow_control"):
            proposed.append({
                "action": "register_server",
                "label": "Register this inference server (once for all its models)",
                "automatic": True,
                "requires": "server_key",
                "server_key": card.get("server_key"),
                "machine_id": card.get("machine_id"),
                "is_self": bool(card.get("is_self")),
                "note": (
                    "Writes Fleet Monitor management config only. Does not start, stop, "
                    "load, or unload anything. Process control still requires an exe path."
                ),
            })
        if not card.get("is_self") and not card.get("allow_control"):
            steps.append("Remote control is disabled for this machine.")
            proposed.append({
                "action": "enable_allow_control",
                "label": "Enable Allow remote control on the fleet machine entry",
                "automatic": False,
                "where": "Fleet machine Configure → Allow remote control",
            })
    elif not card.get("is_self") and not card.get("allow_control"):
        steps.append("Server is registered, but remote control is disabled.")
        proposed.append({
            "action": "enable_allow_control",
            "label": "Enable Allow remote control on the fleet machine entry",
            "automatic": False,
            "where": "Fleet machine Configure → Allow remote control",
        })
    caps = card.get("capabilities") or {}
    if caps.get("model_switch") == "restart_with_model":
        steps.append(
            "llama-server: model switch uses managed restart when process control "
            "is available; Unload while leaving the server running is unsupported.")
    if not proposed and ms.get("manageable"):
        steps.append("Already manageable — no setup required.")
    return {
        "model": card.get("model"),
        "friendly_name": card.get("friendly_name") or card.get("model"),
        "machine": card.get("machine"),
        "engine": card.get("engine"),
        "endpoint": card.get("endpoint"),
        "server_key": card.get("server_key"),
        "current_status": ms,
        "explanation": steps,
        "proposed_actions": proposed,
        "applies_changes": False,
        "note": "Preview only — nothing was changed. Prefer Configure Fleet Management for multi-server setup.",
    }


def merge_startup_args(existing: str | None, updates: dict,
                       engine: str | None) -> tuple[str, list[str]]:
    """Replace values for known arg keys only; preserve unknown flags/order.

    Returns (new_args_string, list_of_change_notes).
    """
    caps = capabilities_for(engine)
    tokens = split_args(existing)
    notes: list[str] = []
    # Map parameter -> arg key preference
    param_to_keys = {}
    for pname, meta in (caps.get("parameters") or {}).items():
        keys = meta.get("arg_keys") or []
        if keys:
            param_to_keys[pname] = keys

    # Index existing known flags
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        handled = False
        for pname, keys in param_to_keys.items():
            if tok in keys and pname in updates:
                new_val = updates[pname]
                if i + 1 < len(tokens) and not str(tokens[i + 1]).startswith("-"):
                    old = tokens[i + 1]
                    tokens[i + 1] = str(new_val)
                    notes.append(f"{pname}: {old} → {new_val} (via {tok})")
                    i += 2
                else:
                    tokens.insert(i + 1, str(new_val))
                    notes.append(f"{pname}: set {new_val} (via {tok})")
                    i += 2
                handled = True
                # consume update
                updates = {k: v for k, v in updates.items() if k != pname}
                break
        if not handled:
            i += 1

    # Append missing known params
    for pname, new_val in list(updates.items()):
        keys = param_to_keys.get(pname)
        if not keys:
            notes.append(f"{pname}: skipped — no known startup flag for this engine")
            continue
        tokens.extend([keys[0], str(new_val)])
        notes.append(f"{pname}: appended {keys[0]} {new_val}")

    # Re-quote tokens with spaces
    out = []
    for t in tokens:
        if any(ch.isspace() for ch in t) and not (t.startswith('"') and t.endswith('"')):
            out.append(f'"{t}"')
        else:
            out.append(t)
    return " ".join(out), notes


def classify_pending_for_apply(engine: str | None, pending: dict) -> dict:
    """Split pending into request prefs vs restart-required startup vs unsupported."""
    caps = capabilities_for(engine)
    request_prefs = {}
    startup = {}
    unsupported = {}
    unknown = {}
    for key, val in (pending or {}).items():
        meta = (caps.get("parameters") or {}).get(key) or {}
        apply = meta.get("apply") or "unknown"
        support = meta.get("support") or "unknown"
        if support == "unsupported" or apply == "unsupported":
            unsupported[key] = {"value": val, "reason": "unsupported by engine"}
        elif apply == "per_request":
            request_prefs[key] = val
        elif apply in ("restart", "reload") or support == "startup":
            startup[key] = val
        elif apply == "immediate":
            # No true immediate global settings implemented yet — treat as request pref save
            request_prefs[key] = val
        else:
            unknown[key] = {"value": val, "reason": f"apply={apply}, support={support}"}
    return {
        "request_preferences": request_prefs,
        "startup_settings": startup,
        "unsupported": unsupported,
        "unknown": unknown,
        "restart_required": bool(startup),
    }


def build_model_cards(*, machine: str, machine_id: str | None,
                      allow_control: bool, is_self: bool,
                      servers: list[dict],
                      memory: dict | None = None,
                      gpu: dict | None = None) -> list[dict]:
    """Shape per-model cards for the Model Control Center UI."""
    cards = []
    mem = memory or {}
    avail_ram = mem.get("available")
    used_ram = mem.get("used")
    total_ram = mem.get("total")
    vram_total = None
    vram_used = None
    try:
        gmem = (gpu or {}).get("memory") or {}
        vram_total = gmem.get("total_adapter_bytes") or gmem.get("dedicated_bytes")
        # Prefer nvidia aggregate if present
        ng = ((gpu or {}).get("nvidia") or {}).get("gpus") or []
        if ng:
            vram_total = sum(g.get("memory_total_bytes") or 0 for g in ng) or vram_total
            vram_used = sum(g.get("memory_used_bytes") or 0 for g in ng) or None
    except Exception:
        pass

    for s in servers or []:
        engine = s.get("type") or s.get("api_type") or "unknown"
        caps = capabilities_for(engine)
        server_caps = s.get("capabilities") or {}
        configured = bool(s.get("configured"))
        can_control = bool(is_self or allow_control) and configured
        loaded = s.get("loaded_models") or []
        installed = s.get("installed_models") or []
        # Normalize installed to {name,...}
        inst_names = []
        for m in installed:
            if isinstance(m, dict):
                n = m.get("name") or m.get("id") or m.get("model")
            else:
                n = m
            if n:
                inst_names.append(str(n))
        loaded_names = set()
        for m in loaded:
            if isinstance(m, dict):
                name = m.get("name") or m.get("id") or m.get("model")
                if not name:
                    continue
                loaded_names.add(str(name))
                cards.append(_card(
                    machine=machine, machine_id=machine_id, allow_control=allow_control,
                    is_self=is_self, server=s, engine=engine, caps=caps,
                    server_caps=server_caps, configured=configured,
                    can_control=can_control, model_name=str(name),
                    loaded=True, model_meta=m,
                    avail_ram=avail_ram, used_ram=used_ram, total_ram=total_ram,
                    vram_total=vram_total, vram_used=vram_used,
                ))
        # Primary model_name if loaded list empty but name present
        if not loaded and s.get("model_name"):
            name = str(s.get("model_name"))
            loaded_names.add(name)
            cards.append(_card(
                machine=machine, machine_id=machine_id, allow_control=allow_control,
                is_self=is_self, server=s, engine=engine, caps=caps,
                server_caps=server_caps, configured=configured,
                can_control=can_control, model_name=name,
                loaded=bool(s.get("ready") or s.get("status") in ("online", "ready", "busy")),
                model_meta={"name": name, "path": s.get("model_path"),
                            "context_tokens": s.get("context_tokens")},
                avail_ram=avail_ram, used_ram=used_ram, total_ram=total_ram,
                vram_total=vram_total, vram_used=vram_used,
            ))
        for name in inst_names:
            if name in loaded_names:
                continue
            if "embed" in name.lower():
                continue
            cards.append(_card(
                machine=machine, machine_id=machine_id, allow_control=allow_control,
                is_self=is_self, server=s, engine=engine, caps=caps,
                server_caps=server_caps, configured=configured,
                can_control=can_control, model_name=name,
                loaded=False, model_meta={"name": name},
                avail_ram=avail_ram, used_ram=used_ram, total_ram=total_ram,
                vram_total=vram_total, vram_used=vram_used,
            ))
        # Server-only card when no models known
        if not loaded and not inst_names and not s.get("model_name"):
            cards.append(_card(
                machine=machine, machine_id=machine_id, allow_control=allow_control,
                is_self=is_self, server=s, engine=engine, caps=caps,
                server_caps=server_caps, configured=configured,
                can_control=can_control, model_name=None,
                loaded=False, model_meta={},
                avail_ram=avail_ram, used_ram=used_ram, total_ram=total_ram,
                vram_total=vram_total, vram_used=vram_used,
            ))
    return cards


def _card(**kw) -> dict:
    s = kw["server"]
    caps = kw["caps"]
    server_caps = kw["server_caps"] or {}
    model_meta = kw["model_meta"] or {}
    loaded = kw["loaded"]
    can_control = kw["can_control"]
    engine = kw["engine"]
    actions = {
        "load": bool(can_control and caps.get("can_load") and server_caps.get("can_load", caps.get("can_load")) and not loaded and kw["model_name"]),
        "unload": bool(can_control and caps.get("can_unload") and server_caps.get("can_unload", caps.get("can_unload")) and loaded and kw["model_name"]),
        "start": bool(can_control and server_caps.get("can_start")),
        "stop": bool(can_control and server_caps.get("can_stop")),
        "restart": bool(can_control and server_caps.get("can_restart")),
        "switch_via_restart": bool(
            can_control and caps.get("model_switch") == "restart_with_model"
            and server_caps.get("can_restart") and kw["model_name"]),
        "settings": True,
        "details": True,
    }
    disabled_reasons = {}
    if not caps.get("can_unload"):
        disabled_reasons["unload"] = (
            "This engine serves one resident model; use Stop / Restart instead of Unload."
            if caps.get("model_switch") == "restart_with_model"
            else "Unload is not supported for this engine.")
    if not caps.get("can_load"):
        disabled_reasons["load"] = (
            "Runtime load is unsupported — use Restart with a selected model."
            if caps.get("model_switch") == "restart_with_model"
            else "Load is not supported for this engine.")
    if not kw["configured"]:
        disabled_reasons["manage"] = "Adopt/configure this server before managing it."
    if not (kw["is_self"] or kw["allow_control"]):
        disabled_reasons["remote"] = "Remote control disabled for this machine (enable allow_control)."

    return {
        "id": f"{kw['machine']}|{s.get('id') or s.get('key')}|{kw['model_name'] or '_'}",
        "machine": kw["machine"],
        "machine_id": kw["machine_id"],
        "is_self": kw["is_self"],
        "allow_control": kw["allow_control"],
        "server_id": s.get("id"),
        "server_key": s.get("key"),
        "server_name": s.get("name"),
        "engine": engine,
        "engine_label": caps.get("label") or engine,
        "endpoint": s.get("url") or (f"http://{s.get('host')}:{s.get('port')}" if s.get("port") else None),
        "status": s.get("status"),
        "model_status": "loaded" if loaded else "unloaded",
        "model": kw["model_name"],
        "model_path": model_meta.get("path") or s.get("model_path"),
        "quantization": model_meta.get("quantization"),
        "parameter_size": model_meta.get("parameter_size"),
        "size_bytes": model_meta.get("size_bytes"),
        "context_tokens": model_meta.get("context_tokens") if model_meta.get("context_tokens") is not None else (
            s.get("context_tokens") if loaded else None),
        # Never copy server-process RSS onto unloaded model cards.
        "model_ram_bytes": (model_meta.get("ram_bytes") if loaded else None),
        "model_vram_bytes": (
            model_meta.get("vram_bytes") if loaded
            else None),
        "server_ram_bytes": s.get("ram_bytes"),
        "server_vram_bytes": s.get("vram_bytes"),
        # Backward-compatible aliases used carefully by UI:
        "ram_bytes": (model_meta.get("ram_bytes") if loaded else None),
        "vram_bytes": (model_meta.get("vram_bytes") if loaded else (
            s.get("vram_bytes") if loaded else None)),
        "active_requests": s.get("active_requests") if loaded else None,
        "tokens_per_sec": s.get("tokens_per_sec") if loaded else None,
        "configured": kw["configured"],
        "capabilities": caps,
        "server_capabilities": server_caps,
        "actions": actions,
        "disabled_reasons": disabled_reasons,
        "management_status": management_status(
            configured=kw["configured"], is_self=kw["is_self"],
            allow_control=kw["allow_control"], caps=caps,
            server_caps=server_caps, server_status=s.get("status")),
        "resources": {
            "machine_ram_used_bytes": kw["used_ram"],
            "machine_ram_available_bytes": kw["avail_ram"],
            "machine_ram_total_bytes": kw["total_ram"],
            "machine_vram_used_bytes": kw["vram_used"],
            "machine_vram_total_bytes": kw["vram_total"],
            # legacy keys kept for older callers
            "ram_used_bytes": kw["used_ram"],
            "ram_available_bytes": kw["avail_ram"],
            "ram_total_bytes": kw["total_ram"],
            "vram_used_bytes": kw["vram_used"],
            "vram_total_bytes": kw["vram_total"],
        },
        "ops": s.get("ops"),
        "configured_args": (s.get("config") or {}).get("args") if isinstance(s.get("config"), dict) else None,
        "identity": stable_model_identity(
            machine=kw["machine"], machine_id=kw["machine_id"],
            server=s, model=kw["model_name"]),
    }


# Discovery metadata keys that sync may refresh without touching user prefs.
_SYNC_DISCOVERY_KEYS = (
    "model", "engine", "machine", "machine_id", "server_id", "server_key",
    "endpoint", "model_path", "quantization", "parameter_size", "size_bytes",
    "detected_context", "configured_context", "configured_args",
    "model_status", "management_status", "identity", "last_seen_at",
    "available",
)


class ProfileStore:
    """Persistent user-editable model profiles (never auto-applied)."""

    def __init__(self, path: str | None = None):
        base = os.path.dirname(os.path.abspath(__file__))
        self.path = path or os.path.join(base, "model_profiles.json")
        self._lock = threading.RLock()
        self._profiles: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                self._profiles = data
            elif isinstance(data, dict) and isinstance(data.get("profiles"), list):
                self._profiles = data["profiles"]
            else:
                self._profiles = []
        except FileNotFoundError:
            self._profiles = []
        except Exception:
            self._profiles = []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 2, "profiles": self._profiles}, fh, indent=2)
        os.replace(tmp, self.path)

    def list_profiles(self) -> list[dict]:
        with self._lock:
            return deepcopy(self._profiles)

    def templates(self) -> list[dict]:
        return deepcopy(PROFILE_TEMPLATES)

    def get(self, profile_id: str) -> dict | None:
        with self._lock:
            for p in self._profiles:
                if p.get("id") == profile_id:
                    return deepcopy(p)
        return None

    def get_by_identity(self, identity_key: str) -> dict | None:
        with self._lock:
            for p in self._profiles:
                if (p.get("identity") or {}).get("key") == identity_key:
                    return deepcopy(p)
        return None

    def save(self, entry: dict) -> tuple[bool, str, dict | None]:
        with self._lock:
            name = (entry.get("name") or entry.get("friendly_name") or "").strip()
            if not name:
                return False, "profile name is required", None
            pid = (entry.get("id") or "").strip() or uuid.uuid4().hex[:12]
            now = time.time()
            existing = next((p for p in self._profiles if p.get("id") == pid), None)
            # If identity provided, also find by identity to avoid duplicates
            ident = entry.get("identity") or {}
            if not existing and ident.get("key"):
                existing = next(
                    (p for p in self._profiles
                     if (p.get("identity") or {}).get("key") == ident["key"]),
                    None)
                if existing:
                    pid = existing["id"]

            profile = {
                "id": pid,
                "name": name,
                "friendly_name": entry.get("friendly_name") or name,
                "description": entry.get("description") if entry.get("description") is not None
                    else (existing or {}).get("description") or "",
                "engine": entry.get("engine") or (existing or {}).get("engine") or "",
                "model": entry.get("model") or (existing or {}).get("model") or "",
                "machine": entry.get("machine") or (existing or {}).get("machine") or "",
                "machine_id": entry.get("machine_id") if "machine_id" in entry
                    else (existing or {}).get("machine_id"),
                "server_id": entry.get("server_id") if "server_id" in entry
                    else (existing or {}).get("server_id") or "",
                "server_key": entry.get("server_key") if "server_key" in entry
                    else (existing or {}).get("server_key"),
                "endpoint": entry.get("endpoint") if "endpoint" in entry
                    else (existing or {}).get("endpoint"),
                "identity": entry.get("identity") or (existing or {}).get("identity") or {},
                "model_path": entry.get("model_path") if "model_path" in entry
                    else (existing or {}).get("model_path"),
                "quantization": entry.get("quantization") if "quantization" in entry
                    else (existing or {}).get("quantization"),
                "parameter_size": entry.get("parameter_size") if "parameter_size" in entry
                    else (existing or {}).get("parameter_size"),
                "size_bytes": entry.get("size_bytes") if "size_bytes" in entry
                    else (existing or {}).get("size_bytes"),
                "detected_context": entry.get("detected_context") if "detected_context" in entry
                    else (existing or {}).get("detected_context"),
                "configured_context": entry.get("configured_context") if "configured_context" in entry
                    else (existing or {}).get("configured_context"),
                "configured_args": entry.get("configured_args") if "configured_args" in entry
                    else (existing or {}).get("configured_args"),
                "model_status": entry.get("model_status") if "model_status" in entry
                    else (existing or {}).get("model_status"),
                "management_status": entry.get("management_status") if "management_status" in entry
                    else (existing or {}).get("management_status"),
                "startup_settings": entry.get("startup_settings") if "startup_settings" in entry
                    else (existing or {}).get("startup_settings") or {},
                "request_settings": entry.get("request_settings") if "request_settings" in entry
                    else (existing or {}).get("request_settings") or {},
                "settings": entry.get("settings") if "settings" in entry
                    else (existing or {}).get("settings") or {},
                "associations": entry.get("associations") if "associations" in entry
                    else (existing or {}).get("associations") or [],
                "protected": bool(entry["protected"]) if "protected" in entry
                    else bool((existing or {}).get("protected")),
                "tags": entry.get("tags") if "tags" in entry
                    else (existing or {}).get("tags") or [],
                "notes": entry.get("notes") if "notes" in entry
                    else (existing or {}).get("notes") or "",
                "user_customized": bool((existing or {}).get("user_customized"))
                    or bool(entry.get("user_customized")),
                "available": entry.get("available") if "available" in entry
                    else (existing or {}).get("available", True),
                "last_seen_at": entry.get("last_seen_at") if "last_seen_at" in entry
                    else (existing or {}).get("last_seen_at"),
                "updated_at": now,
                "created_at": (existing or {}).get("created_at", now),
            }
            if existing:
                self._profiles = [profile if p.get("id") == pid else p
                                  for p in self._profiles]
            else:
                self._profiles.append(profile)
            try:
                self._save()
            except OSError as exc:
                return False, f"save failed: {exc}", None
            return True, "saved", deepcopy(profile)

    def patch_user_fields(self, profile_id: str, fields: dict) -> tuple[bool, str, dict | None]:
        """Update user-facing fields without clobbering discovery metadata."""
        allowed = {
            "friendly_name", "name", "description", "notes", "tags",
            "protected", "associations", "startup_settings",
            "request_settings", "settings",
        }
        with self._lock:
            existing = next((p for p in self._profiles if p.get("id") == profile_id), None)
            if not existing:
                return False, "unknown profile id", None
            merged = deepcopy(existing)
            for k, v in (fields or {}).items():
                if k in allowed:
                    merged[k] = v
            if "friendly_name" in fields and fields["friendly_name"]:
                merged["name"] = fields["friendly_name"]
            merged["user_customized"] = True
            return self.save(merged)

    def sync_from_cards(self, cards: list[dict]) -> dict:
        """Non-destructive inventory sync — create/update discovery metadata only."""
        summary = {
            "created": 0, "updated": 0, "unchanged": 0,
            "unavailable": 0, "conflicts": [], "total_cards": 0,
        }
        seen_keys = set()
        now = time.time()
        with self._lock:
            by_ident = {
                (p.get("identity") or {}).get("key"): p
                for p in self._profiles
                if (p.get("identity") or {}).get("key")
            }
            for card in cards or []:
                if not card.get("model"):
                    continue  # skip server-only placeholders
                summary["total_cards"] += 1
                ident = card.get("identity") or stable_model_identity(
                    machine=card.get("machine") or "",
                    machine_id=card.get("machine_id"),
                    server={
                        "id": card.get("server_id"),
                        "key": card.get("server_key"),
                        "host": (card.get("endpoint") or "").replace("http://", "").split(":")[0]
                        if card.get("endpoint") else "",
                        "port": card.get("port"),
                        "url": card.get("endpoint"),
                    },
                    model=card.get("model"),
                )
                key = ident.get("key")
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                configured = parse_startup_settings(
                    card.get("engine"), card.get("configured_args"))
                assoc = guess_association(card.get("model"))
                associations = []
                if assoc:
                    associations.append(assoc)

                existing = by_ident.get(key)
                if not existing:
                    # Adopt a legacy profile (pre-identity) that uniquely matches
                    # model + machine so user customizations are preserved.
                    orphans = [
                        p for p in self._profiles
                        if not (p.get("identity") or {}).get("key")
                        and (p.get("model") or "") == (card.get("model") or "")
                        and (p.get("machine") or "") == (card.get("machine") or "")
                    ]
                    if len(orphans) == 1:
                        existing = orphans[0]
                        existing["identity"] = ident
                        existing["friendly_name"] = (
                            existing.get("friendly_name") or existing.get("name")
                            or default_friendly_name(card.get("model"), card.get("machine") or ""))
                        existing["user_customized"] = True
                        by_ident[key] = existing
                        # Fall through to discovery update below
                    else:
                        if len(orphans) > 1:
                            summary["conflicts"].append(
                                f"multiple legacy profiles for {card.get('model')} on "
                                f"{card.get('machine')} — left untouched")
                        profile = {
                            "id": uuid.uuid4().hex[:12],
                            "name": default_friendly_name(card.get("model"), card.get("machine") or ""),
                            "friendly_name": default_friendly_name(
                                card.get("model"), card.get("machine") or ""),
                            "description": "",
                            "engine": card.get("engine"),
                            "model": card.get("model"),
                            "machine": card.get("machine"),
                            "machine_id": card.get("machine_id"),
                            "server_id": card.get("server_id"),
                            "server_key": card.get("server_key"),
                            "endpoint": card.get("endpoint"),
                            "identity": ident,
                            "model_path": card.get("model_path"),
                            "quantization": card.get("quantization"),
                            "parameter_size": card.get("parameter_size"),
                            "size_bytes": card.get("size_bytes"),
                            "detected_context": card.get("context_tokens"),
                            "configured_context": configured.get("context"),
                            "configured_args": card.get("configured_args"),
                            "model_status": card.get("model_status"),
                            "management_status": card.get("management_status"),
                            "startup_settings": {},
                            "request_settings": {},
                            "settings": {},
                            "associations": associations,
                            "protected": False,
                            "tags": [],
                            "notes": "",
                            "user_customized": False,
                            "available": True,
                            "last_seen_at": now,
                            "created_at": now,
                            "updated_at": now,
                        }
                        self._profiles.append(profile)
                        by_ident[key] = profile
                        summary["created"] += 1
                        continue

                # Update discovery metadata only; preserve user fields.
                # last_seen_at always refreshes and does not count as "updated".
                changed = False
                discovery = {
                    "model": card.get("model"),
                    "engine": card.get("engine"),
                    "machine": card.get("machine"),
                    "machine_id": card.get("machine_id"),
                    "server_id": card.get("server_id"),
                    "server_key": card.get("server_key"),
                    "endpoint": card.get("endpoint"),
                    "identity": ident,
                    "model_path": card.get("model_path"),
                    "quantization": card.get("quantization"),
                    "parameter_size": card.get("parameter_size"),
                    "size_bytes": card.get("size_bytes"),
                    "detected_context": card.get("context_tokens"),
                    "configured_context": configured.get("context"),
                    "configured_args": card.get("configured_args"),
                    "model_status": card.get("model_status"),
                    "management_status": card.get("management_status"),
                    "available": True,
                }
                for k, v in discovery.items():
                    if existing.get(k) != v:
                        existing[k] = v
                        changed = True
                existing["last_seen_at"] = now
                # Merge unverified associations if user has none
                if associations and not existing.get("associations"):
                    existing["associations"] = associations
                    changed = True
                if not existing.get("friendly_name"):
                    existing["friendly_name"] = existing.get("name") or default_friendly_name(
                        card.get("model"), card.get("machine") or "")
                    changed = True
                if changed:
                    existing["updated_at"] = now
                    summary["updated"] += 1
                else:
                    summary["unchanged"] += 1

            # Mark missing as unavailable — never delete
            for p in self._profiles:
                key = (p.get("identity") or {}).get("key")
                if key and key not in seen_keys and p.get("model"):
                    if p.get("available") is not False:
                        p["available"] = False
                        p["updated_at"] = now
                        summary["unavailable"] += 1
                    else:
                        summary["unchanged"] += 1

            # Merge customizations from leftover legacy orphans into matching
            # identity profiles (never delete the orphan). Idempotent.
            for orphan in list(self._profiles):
                if (orphan.get("identity") or {}).get("key"):
                    continue
                if not orphan.get("model"):
                    continue
                if orphan.get("superseded_by") or "Superseded by inventory profile" in (orphan.get("notes") or ""):
                    continue
                matches = [
                    p for p in self._profiles
                    if (p.get("identity") or {}).get("key")
                    and (p.get("model") or "") == (orphan.get("model") or "")
                    and (p.get("machine") or "") == (orphan.get("machine") or "")
                ]
                if len(matches) != 1:
                    continue
                target = matches[0]
                merged_any = False
                for field in ("request_settings", "startup_settings", "settings"):
                    src = orphan.get(field) or {}
                    dst = dict(target.get(field) or {})
                    if src and not dst:
                        target[field] = dict(src)
                        merged_any = True
                    elif src:
                        for sk, sv in src.items():
                            if sk not in dst:
                                dst[sk] = sv
                                merged_any = True
                        target[field] = dst
                # Prefer customized friendly name from orphan when target still
                # uses the raw model id.
                o_name = orphan.get("friendly_name") or orphan.get("name")
                t_name = target.get("friendly_name") or target.get("name")
                if o_name and (not t_name or t_name == target.get("model")):
                    target["friendly_name"] = o_name
                    target["name"] = o_name
                    merged_any = True
                if orphan.get("protected") and not target.get("protected"):
                    target["protected"] = True
                    merged_any = True
                if orphan.get("tags") and not target.get("tags"):
                    target["tags"] = list(orphan.get("tags") or [])
                    merged_any = True
                if orphan.get("notes") and not (target.get("notes") or "").strip():
                    # Do not copy the superseded boilerplate note
                    note = orphan.get("notes") or ""
                    if "Superseded by inventory profile" not in note:
                        target["notes"] = note
                        merged_any = True
                if orphan.get("associations") and not target.get("associations"):
                    target["associations"] = list(orphan.get("associations") or [])
                    merged_any = True
                orphan["superseded_by"] = target.get("id")
                orphan["available"] = False
                if merged_any:
                    target["user_customized"] = True
                    target["updated_at"] = now
                    summary["updated"] += 1
                orphan["notes"] = (
                    (orphan.get("notes") or "")
                    + ("\n" if orphan.get("notes") else "")
                    + f"Superseded by inventory profile {target.get('id')} "
                      "(customizations merged; legacy entry kept)."
                ).strip()
                orphan["updated_at"] = now

            try:
                self._save()
            except OSError as exc:
                summary["conflicts"].append(f"save failed: {exc}")
        return summary

    def delete(self, profile_id: str) -> tuple[bool, str]:
        with self._lock:
            before = len(self._profiles)
            self._profiles = [p for p in self._profiles if p.get("id") != profile_id]
            if len(self._profiles) == before:
                return False, "unknown profile id"
            try:
                self._save()
            except OSError as exc:
                return False, f"delete failed: {exc}"
            return True, "deleted"

    def duplicate(self, profile_id: str, new_name: str | None = None) -> tuple[bool, str, dict | None]:
        src = self.get(profile_id)
        if not src:
            return False, "unknown profile id", None
        src.pop("id", None)
        # Duplicates get a new identity suffix so they don't collide on sync
        ident = dict(src.get("identity") or {})
        if ident.get("key"):
            ident["key"] = ident["key"] + "|copy|" + uuid.uuid4().hex[:6]
        src["identity"] = ident
        src["name"] = new_name or ((src.get("friendly_name") or src.get("name") or "profile") + " copy")
        src["friendly_name"] = src["name"]
        src["user_customized"] = True
        return self.save(src)

    def export_all(self) -> dict:
        with self._lock:
            return {"version": 2, "profiles": deepcopy(self._profiles),
                    "exported_at": time.time()}

    def import_all(self, payload: dict | list, *, merge: bool = True) -> tuple[bool, str, int]:
        if isinstance(payload, list):
            incoming = payload
        elif isinstance(payload, dict):
            incoming = payload.get("profiles") or []
        else:
            return False, "invalid import payload", 0
        if not isinstance(incoming, list):
            return False, "profiles must be a list", 0
        count = 0
        with self._lock:
            if not merge:
                self._profiles = []
            for raw in incoming:
                if not isinstance(raw, dict):
                    continue
                ok, _, _ = self.save(raw)
                if ok:
                    count += 1
        return True, f"imported {count} profile(s)", count


def attach_profiles_to_cards(cards: list[dict], profiles: list[dict]) -> list[dict]:
    by_key = {(p.get("identity") or {}).get("key"): p for p in profiles
              if (p.get("identity") or {}).get("key")}
    out = []
    for c in cards:
        c2 = dict(c)
        ident = c.get("identity") or stable_model_identity(
            machine=c.get("machine") or "",
            machine_id=c.get("machine_id"),
            server={
                "id": c.get("server_id"),
                "key": c.get("server_key"),
                "host": "",
                "port": None,
                "url": c.get("endpoint"),
            },
            model=c.get("model"),
        )
        c2["identity"] = ident
        key = ident.get("key")
        prof = by_key.get(key)
        if prof:
            c2["profile_id"] = prof.get("id")
            c2["friendly_name"] = prof.get("friendly_name") or prof.get("name") or c.get("model")
            c2["protected"] = bool(prof.get("protected"))
            c2["tags"] = prof.get("tags") or []
            c2["associations"] = prof.get("associations") or []
            c2["notes"] = prof.get("notes") or ""
            c2["request_settings"] = prof.get("request_settings") or {}
            c2["startup_settings"] = prof.get("startup_settings") or {}
        else:
            c2["friendly_name"] = c.get("model")
            c2["protected"] = False
            c2["tags"] = []
            c2["associations"] = []
            if guess_association(c.get("model")):
                c2["associations"] = [guess_association(c.get("model"))]
        out.append(c2)
    return out


def preview_apply_profile(profile: dict, *, engine: str | None,
                          configured_args: str | None,
                          live_context: Any = None,
                          live_model: str | None = None) -> dict:
    """Show what would change — does not apply."""
    pending = {}
    pending.update(profile.get("startup_settings") or {})
    pending.update(profile.get("request_settings") or {})
    pending.update(profile.get("settings") or {})
    view = settings_view(engine=engine or profile.get("engine"),
                         configured_args=configured_args,
                         live_context=live_context,
                         live_model=live_model or profile.get("model"),
                         pending=pending)
    view["profile"] = {"id": profile.get("id"), "name": profile.get("friendly_name") or profile.get("name")}
    view["applied"] = False
    view["classification"] = classify_pending_for_apply(
        engine or profile.get("engine"), pending)
    view["note"] = "Preview only — nothing was applied."
    return view
