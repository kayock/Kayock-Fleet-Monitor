"""Fleet-aware AI server discovery for the FOXAI AI Lab Command Center.

Reuses each machine's Fleet Monitor /metrics (and fleet detail) rather than
running a competing scanner. Candidate endpoints are verified with short
health probes before being exposed for chat.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any

FLEET_MONITORS = [
    {"machine": "Ubuntu Station", "host": "10.0.0.202", "port": 5000,
     "local_urls": ["http://127.0.0.1:5000"]},
    {"machine": "Ubuntu Portable", "host": "10.0.0.45", "port": 5001,
     "local_urls": ["http://10.0.0.45:5001"]},
    {"machine": "Windows", "host": "10.0.0.104", "port": 5000,
     "local_urls": ["http://10.0.0.104:5000"]},
]

# Known lab candidates — verified before exposure; never force exposure.
CANDIDATE_ENDPOINTS = [
    {"machine": "Windows", "host": "10.0.0.104", "port": 12434, "engine": "llama-server"},
    {"machine": "Windows", "host": "10.0.0.104", "port": 8080, "engine": "llama-server",
     "note": "Often loopback-only on Windows"},
    {"machine": "Ubuntu Portable", "host": "10.0.0.45", "port": 11434, "engine": "ollama"},
    {"machine": "Ubuntu Portable", "host": "10.0.0.45", "port": 1337, "engine": "openai"},
    {"machine": "Ubuntu Portable", "host": "10.0.0.45", "port": 13305, "engine": "lemonade",
     "note": "May require auth / may be loopback-only"},
    {"machine": "Ubuntu Station", "host": "10.0.0.202", "port": 11434, "engine": "ollama"},
    {"machine": "Ubuntu Station", "host": "10.0.0.202", "port": 11435, "engine": "llama-server"},
]

CHAT_COMPAT = {"ollama", "openai", "openai-compatible", "llama-server", "lm-studio",
               "lemonade", "vllm", "sglang"}

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _fetch(url: str, timeout: float = 2.5) -> tuple[int | None, Any, str | None]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(512 * 1024)
            try:
                return resp.status, json.loads(raw.decode("utf-8", "replace")), None
            except Exception:
                return resp.status, raw.decode("utf-8", "replace")[:500], None
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096).decode("utf-8", "replace")
        except Exception:
            body = ""
        return exc.code, body, f"HTTPError: {exc.code}"
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def _tcp_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _lan_host_for(machine: str, host: str) -> str:
    if host not in LOOPBACK:
        return host
    mon = next((m for m in FLEET_MONITORS if m["machine"] == machine), None)
    return mon["host"] if mon else host


def _endpoint_key(machine: str, host: str, port: int | None) -> tuple:
    return (machine, _lan_host_for(machine, host or ""), int(port or 0))


def _normalize_server(machine: str, rec: dict, source: str) -> dict:
    host = rec.get("host") or "127.0.0.1"
    port = rec.get("port")
    engine = rec.get("api_type") or rec.get("type") or rec.get("type_hint") or "unknown"
    loaded = rec.get("loaded_models") or []
    model_name = rec.get("model_name")
    if not model_name and loaded:
        model_name = (loaded[0] or {}).get("name")
    lan_host = _lan_host_for(machine, host)
    endpoint = None
    if port:
        endpoint = f"http://{lan_host}:{int(port)}"
    chat_ok = bool(engine and any(engine.startswith(c) or c in str(engine)
                                  for c in CHAT_COMPAT))
    # Embeddings-only heuristic
    if model_name and "embed" in str(model_name).lower():
        chat_ok = False
    mon = next((m for m in FLEET_MONITORS if m["machine"] == machine), None)
    return {
        "id": f"{machine}|{lan_host}:{port}|{rec.get('key') or ''}",
        "machine": machine,
        "engine": engine,
        "host": host,
        "lan_host": lan_host,
        "port": port,
        "endpoint": endpoint,
        "status": rec.get("status"),
        "status_detail": rec.get("status_detail"),
        "ready": rec.get("ready"),
        "health_ok": rec.get("health_ok"),
        "model": model_name,
        "loaded_models": loaded,
        "quantization": _guess_quant(model_name or rec.get("model_path")),
        "context_tokens": rec.get("context_tokens"),
        "cpu_percent": rec.get("cpu_percent"),
        "ram_bytes": rec.get("ram_bytes"),
        "vram_bytes": rec.get("vram_bytes"),
        "active_requests": rec.get("active_requests"),
        "tokens_per_sec": rec.get("tokens_per_sec"),
        "last_check": rec.get("probed_at"),
        "latency_ms": rec.get("latency_ms"),
        "chat_compatible": chat_ok,
        "loopback_only_suspected": host in LOOPBACK,
        "source": source,
        "fleet_monitor_url": (f"http://{mon['host']}:{mon['port']}/"
                              if mon else None),
        "url": rec.get("url"),
    }


def _guess_quant(name: str | None) -> str | None:
    if not name:
        return None
    import re
    m = re.search(r"(Q[2-8]_[A-Z0-9_]+|q[2-8]_[a-z0-9_]+|IQ\d_[A-Z0-9_]+)", name)
    return m.group(1) if m else None


def _collect_monitor(mon: dict) -> tuple[dict | None, str | None]:
    urls = list(mon.get("local_urls") or []) + [f"http://{mon['host']}:{mon['port']}"]
    last_err = None
    for url in urls:
        status, body, err = _fetch(f"{url}/metrics", timeout=3.0)
        if status == 200 and isinstance(body, dict):
            return body, None
        last_err = err or f"HTTP {status}"
    return None, last_err


def _model_entries(loaded: list, installed: list) -> list[dict]:
    """Build unified model list with loaded vs installed distinction."""
    loaded_ids: list[str] = []
    for m in loaded or []:
        if isinstance(m, dict):
            mid = m.get("name") or m.get("model") or m.get("id")
        else:
            mid = m
        if mid:
            loaded_ids.append(str(mid))
    installed_ids: list[str] = []
    for m in installed or []:
        if isinstance(m, dict):
            mid = m.get("name") or m.get("model") or m.get("id")
        else:
            mid = m
        if mid:
            installed_ids.append(str(mid))
    seen = set()
    out = []
    for mid in loaded_ids:
        if mid in seen:
            continue
        seen.add(mid)
        out.append({
            "id": mid,
            "loaded": True,
            "chat": "embed" not in mid.lower(),
            "status": "loaded",
        })
    for mid in installed_ids:
        if mid in seen:
            continue
        seen.add(mid)
        out.append({
            "id": mid,
            "loaded": False,
            "chat": "embed" not in mid.lower(),
            "status": "installed_not_loaded",
        })
    return out


def _verify_endpoint(host: str, port: int, engine: str) -> dict:
    reachable = _tcp_open(host, port)
    result = {
        "reachable": reachable,
        "api_ok": False,
        "loaded_models": [],
        "installed_models": [],
        "engine": engine,
        "detail": None,
        "checked_at": time.time(),
    }
    if not reachable:
        result["detail"] = "TCP connect failed"
        return result
    base = f"http://{host}:{int(port)}"
    # Ollama
    st, body, err = _fetch(f"{base}/api/tags", timeout=2.0)
    if st == 200 and isinstance(body, dict) and "models" in body:
        # llama.cpp sometimes mirrors an Ollama-shaped /api/tags; prefer
        # OpenAI /v1/models when the engine is clearly not Ollama.
        engine_l = (engine or "").lower()
        looks_ollama = "ollama" in engine_l or not engine_l or engine_l == "unknown"
        if looks_ollama:
            result["api_ok"] = True
            result["engine"] = "ollama"
            result["installed_models"] = [
                m.get("name") or m.get("model") for m in (body.get("models") or [])
                if (m.get("name") or m.get("model"))
            ]
            st2, ps, _ = _fetch(f"{base}/api/ps", timeout=2.0)
            if st2 == 200 and isinstance(ps, dict):
                result["loaded_models"] = [
                    m.get("name") or m.get("model") for m in (ps.get("models") or [])
                    if m.get("name") or m.get("model")
                ]
            result["detail"] = "ollama API reachable"
            return result
    # OpenAI-compatible
    st, body, err = _fetch(f"{base}/v1/models", timeout=2.0)
    if st == 200 and isinstance(body, dict) and "data" in body:
        result["api_ok"] = True
        result["engine"] = engine if engine and engine != "ollama" else "openai-compatible"
        result["installed_models"] = [
            m.get("id") for m in (body.get("data") or []) if m.get("id")
        ]
        # For single-model servers (llama.cpp), listed models are loaded.
        result["loaded_models"] = list(result["installed_models"])
        result["detail"] = "OpenAI-compatible /v1/models ok"
        return result
    # llama.cpp props
    st, body, err = _fetch(f"{base}/props", timeout=2.0)
    if st == 200 and isinstance(body, dict):
        result["api_ok"] = True
        result["engine"] = "llama-server"
        model = ((body.get("model") or {}).get("path")
                 or (body.get("default_generation_settings") or {}).get("model"))
        if model:
            result["loaded_models"] = [str(model).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]]
            result["installed_models"] = list(result["loaded_models"])
        result["detail"] = "llama-server /props ok"
        return result
    result["detail"] = err or f"no compatible API (last HTTP {st})"
    return result


def _enrich_verified(s: dict) -> dict:
    """Probe LAN endpoint and attach loaded/installed model inventories."""
    if not (s.get("endpoint") and s.get("port")):
        return s
    v = _verify_endpoint(s["lan_host"], int(s["port"]), s.get("engine") or "")
    s["lan_reachable"] = v["reachable"]
    s["lan_api_ok"] = v["api_ok"]
    s["lan_detail"] = v["detail"]
    s["last_check"] = v["checked_at"]
    if v.get("engine"):
        # Prefer verified engine over fleet placeholder when probe succeeds.
        if v["api_ok"]:
            s["engine"] = v["engine"]
    if v["loaded_models"]:
        s["loaded_models_lan"] = v["loaded_models"]
        s["loaded_models"] = [{"name": m} for m in v["loaded_models"]]
        if not s.get("model"):
            s["model"] = v["loaded_models"][0]
    if v["installed_models"]:
        s["installed_models"] = v["installed_models"]
    # Only treat models as "loaded" when the LAN API probe succeeded.
    # Fleet may report loaded models on loopback-only peers that Station cannot reach.
    if v["api_ok"]:
        s["models"] = _model_entries(
            v["loaded_models"],
            v["installed_models"] or s.get("installed_models") or [],
        )
    else:
        reported = [
            (m.get("name") if isinstance(m, dict) else m)
            for m in (s.get("loaded_models") or [])
        ]
        reported = [m for m in reported if m]
        s["models"] = [
            {
                "id": mid,
                "loaded": False,
                "chat": "embed" not in str(mid).lower(),
                "status": "unreachable_from_station",
            }
            for mid in reported
        ]
        if not s.get("status_detail") and v.get("detail"):
            s["status_detail"] = v["detail"]

    if not v["api_ok"]:
        if s.get("loopback_only_suspected") and not v["reachable"]:
            s["chat_compatible"] = False
            detail = s.get("status_detail") or ""
            note = "not reachable from Station LAN (likely loopback-bound)"
            if note not in detail:
                s["status_detail"] = (detail + " · " + note).strip(" ·")
        elif s.get("loopback_only_suspected"):
            s["chat_compatible"] = False
    elif s.get("chat_compatible") is not False:
        engine = (s.get("engine") or "").lower()
        s["chat_compatible"] = any(
            engine.startswith(c) or c in engine for c in CHAT_COMPAT
        )
    return s


def _merge_server(existing: dict, incoming: dict) -> dict:
    """Prefer LAN-verified, non-loopback, richer records when deduplicating."""
    score = lambda s: (
        1 if s.get("lan_api_ok") else 0,
        1 if s.get("chat_compatible") else 0,
        len(s.get("models") or []),
        0 if (s.get("host") in LOOPBACK) else 1,
        1 if (s.get("source") or "").startswith("fleet") else 0,
    )
    keep, other = (existing, incoming) if score(existing) >= score(incoming) else (incoming, existing)
    # Preserve useful fields from the lesser record.
    for key in ("installed_models", "loaded_models_lan", "models", "note",
                "status_detail", "fleet_monitor_url", "quantization",
                "context_tokens"):
        if not keep.get(key) and other.get(key):
            keep[key] = other[key]
    if not keep.get("models"):
        keep["models"] = _model_entries(
            keep.get("loaded_models_lan") or [
                (m.get("name") if isinstance(m, dict) else m)
                for m in (keep.get("loaded_models") or [])
            ],
            keep.get("installed_models") or [],
        )
    return keep


def discover_servers() -> dict:
    machines = []
    servers = []
    errors = []
    for mon in FLEET_MONITORS:
        metrics, err = _collect_monitor(mon)
        entry = {
            "machine": mon["machine"],
            "monitor_host": mon["host"],
            "monitor_port": mon["port"],
            "monitor_ok": metrics is not None,
            "error": err,
            "gpu": (metrics or {}).get("gpu"),
            "cpu": (metrics or {}).get("cpu"),
            "memory": (metrics or {}).get("memory"),
            "sampler": (metrics or {}).get("sampler"),
            "instance": (metrics or {}).get("instance"),
        }
        machines.append(entry)
        if not metrics:
            errors.append(f"{mon['machine']}: {err}")
            continue
        for rec in ((metrics.get("ai_servers") or {}).get("servers") or []):
            if rec.get("status") == "error" and not rec.get("port"):
                continue  # skip malformed probe-error placeholders
            servers.append(_normalize_server(mon["machine"], rec, "fleet-monitor"))

    # Verify + dedupe fleet records by machine/LAN-host/port
    by_key: dict[tuple, dict] = {}
    for s in servers:
        key = _endpoint_key(s["machine"], s.get("lan_host") or s.get("host") or "", s.get("port"))
        enriched = _enrich_verified(s)
        if key in by_key:
            by_key[key] = _merge_server(by_key[key], enriched)
        else:
            by_key[key] = enriched

    # Verify known candidates and merge / enrich
    for cand in CANDIDATE_ENDPOINTS:
        key = _endpoint_key(cand["machine"], cand["host"], cand["port"])
        if key in by_key:
            continue
        v = _verify_endpoint(cand["host"], cand["port"], cand.get("engine") or "")
        if not v["reachable"] and not v["api_ok"]:
            by_key[key] = {
                "id": f"{cand['machine']}|{cand['host']}:{cand['port']}|candidate",
                "machine": cand["machine"],
                "engine": cand.get("engine"),
                "host": cand["host"],
                "lan_host": cand["host"],
                "port": cand["port"],
                "endpoint": f"http://{cand['host']}:{cand['port']}",
                "status": "offline",
                "status_detail": v["detail"] or cand.get("note"),
                "ready": False,
                "health_ok": False,
                "model": None,
                "loaded_models": [],
                "models": [],
                "chat_compatible": False,
                "lan_reachable": False,
                "lan_api_ok": False,
                "source": "candidate",
                "note": cand.get("note"),
                "last_check": v["checked_at"],
            }
            continue
        model = (v["loaded_models"][0] if v["loaded_models"] else None)
        by_key[key] = {
            "id": f"{cand['machine']}|{cand['host']}:{cand['port']}|candidate",
            "machine": cand["machine"],
            "engine": v.get("engine") or cand.get("engine"),
            "host": cand["host"],
            "lan_host": cand["host"],
            "port": cand["port"],
            "endpoint": f"http://{cand['host']}:{cand['port']}",
            "status": "online" if v["api_ok"] else ("listening" if v["reachable"] else "offline"),
            "status_detail": v["detail"] or cand.get("note"),
            "ready": bool(v["loaded_models"]),
            "health_ok": v["api_ok"],
            "model": model,
            "loaded_models": [{"name": m} for m in v["loaded_models"]],
            "loaded_models_lan": v["loaded_models"],
            "installed_models": v["installed_models"],
            "models": _model_entries(v["loaded_models"], v["installed_models"]),
            "chat_compatible": v["api_ok"] and (v.get("engine") in CHAT_COMPAT
                                                or cand.get("engine") in CHAT_COMPAT),
            "lan_reachable": v["reachable"],
            "lan_api_ok": v["api_ok"],
            "source": "candidate-verified",
            "note": cand.get("note"),
            "last_check": v["checked_at"],
            "fleet_monitor_url": next(
                (f"http://{m['host']}:{m['port']}/" for m in FLEET_MONITORS
                 if m["machine"] == cand["machine"]), None),
        }

    verified = list(by_key.values())
    # Stable sort: chat-ready first, then machine name, then port
    verified.sort(key=lambda s: (
        0 if s.get("lan_api_ok") and s.get("chat_compatible") else 1,
        s.get("machine") or "",
        int(s.get("port") or 0),
        s.get("endpoint") or "",
    ))

    return {
        "ts": time.time(),
        "machines": machines,
        "servers": verified,
        "errors": errors,
    }


def homepage_links() -> list[dict]:
    """Application links from the Family Hub services.yaml for QR convenience."""
    path = "/opt/foxai-services/homepage/config/services.yaml"
    links = []
    try:
        import re
        text = open(path, encoding="utf-8").read()
    except OSError:
        return links
    # Very small YAML scrape: "- Name:" blocks with href:
    current = None
    for line in text.splitlines():
        m = re.match(r"^\s{4}-\s+(.+):\s*$", line)
        if m:
            current = {"name": m.group(1).strip()}
            continue
        if current and "href:" in line:
            href = line.split("href:", 1)[1].strip()
            if href:
                current["href"] = href
                links.append(current)
            current = None
    return links
