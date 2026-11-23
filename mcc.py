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
        "context_tokens": model_meta.get("context_tokens") if model_meta.get("context_tokens") is not None else s.get("context_tokens"),
        "ram_bytes": s.get("ram_bytes"),
        "vram_bytes": s.get("vram_bytes") if s.get("vram_bytes") is not None else model_meta.get("vram_bytes"),
        "active_requests": s.get("active_requests"),
        "tokens_per_sec": s.get("tokens_per_sec"),
        "configured": kw["configured"],
        "capabilities": caps,
        "server_capabilities": server_caps,
        "actions": actions,
        "disabled_reasons": disabled_reasons,
        "resources": {
            "ram_used_bytes": kw["used_ram"],
            "ram_available_bytes": kw["avail_ram"],
            "ram_total_bytes": kw["total_ram"],
            "vram_used_bytes": kw["vram_used"],
            "vram_total_bytes": kw["vram_total"],
        },
        "ops": s.get("ops"),
        "configured_args": (s.get("config") or {}).get("args") if isinstance(s.get("config"), dict) else None,
    }


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
            json.dump({"version": 1, "profiles": self._profiles}, fh, indent=2)
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

    def save(self, entry: dict) -> tuple[bool, str, dict | None]:
        with self._lock:
            name = (entry.get("name") or "").strip()
            if not name:
                return False, "profile name is required", None
            pid = (entry.get("id") or "").strip() or uuid.uuid4().hex[:12]
            now = time.time()
            existing = next((p for p in self._profiles if p.get("id") == pid), None)
            profile = {
                "id": pid,
                "name": name,
                "description": entry.get("description") or "",
                "engine": entry.get("engine") or "",
                "model": entry.get("model") or "",
                "machine": entry.get("machine") or "",
                "server_id": entry.get("server_id") or "",
                "startup_settings": entry.get("startup_settings") or {},
                "request_settings": entry.get("request_settings") or {},
                "settings": entry.get("settings") or {},
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
        src["name"] = new_name or (src.get("name") + " copy")
        return self.save(src)

    def export_all(self) -> dict:
        with self._lock:
            return {"version": 1, "profiles": deepcopy(self._profiles),
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
    view["profile"] = {"id": profile.get("id"), "name": profile.get("name")}
    view["applied"] = False
    view["note"] = "Preview only — nothing was applied."
    return view
