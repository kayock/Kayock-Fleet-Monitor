"""AI Model Server discovery, probing and management.

Everything here runs on its own bounded worker threads, never on Flask
request paths and never inside the system sampler. Slow or unreachable AI
servers can therefore never freeze the dashboard.

Discovery layers (read-only):
  1. Process inspection  — psutil process_iter matched against known AI
     server names (llama.cpp/llama-server, Ollama, LM Studio, Lemonade,
     vLLM, SGLang, generic "llama").
  2. Listening ports     — psutil.net_connections maps listening TCP ports
     to owning PIDs (our own monitor port is excluded by PID).
  3. Known default ports — 11434 (Ollama), 1234 (LM Studio), 8080
     (llama-server), 8000 (Lemonade/vLLM) probed when something is listening
     on loopback.
  4. Configured servers  — the user's registry (ai_servers.json), which is
     also the ONLY thing management controls may act on.

Status semantics (a listening port is NOT health):
  online    API answered 200 with a recognized schema
  ready     subset of online: a model is actually loaded for inference
  busy      online and the server reports active requests (llama.cpp /slots)
  loading   process running but port not open yet, or the server reports a
            model load in progress
  listening port open but the API did not answer convincingly
  error     API answered with an HTTP 5xx error
  offline   configured server whose process/port is gone

Never-invented-data rule: any metric a server API does not expose is None
and the UI renders it as 'unavailable'.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import psutil

DEFAULT_PROBE_TIMEOUT = 2.5
DISCOVERY_INTERVAL = 10.0
FLEET_INDEPENDENT = True  # documentation marker: local monitoring never waits on AI probing

MAX_MANAGED_SERVERS = 8
MAX_LOG_LINES = 300
MAX_BODY_BYTES = 512 * 1024

# Well-known AI server ports probed when something listens (loopback or
# wildcard). Includes common lab endpoints; presence of a port here is a
# discovery candidate only — connectivity is still verified by probing.
KNOWN_AI_PORTS = {
    11434: "ollama",
    1234: "lm-studio",
    8080: "llama-server",
    8000: "lemonade",
    13305: "lemonade",
    1337: "openai",
    12434: "llama-server",
}

# Process-name / cmdline hints -> server type. Matched case-insensitively on
# the executable name and, as a fallback, on the command line.
PROCESS_HINTS = (
    ("llama-server", "llama-server"),
    ("llama.cpp", "llama-server"),
    ("llama-server.exe", "llama-server"),
    ("ollama", "ollama"),
    ("lm-studio", "lm-studio"),
    ("lmstudio", "lm-studio"),
    ("lm studio", "lm-studio"),
    ("lemonade", "lemonade"),
    ("vllm", "vllm"),
    ("sglang", "sglang"),
    ("llama", "llama-server"),  # last: llama-cli / llama-batched etc.
)

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def split_launch_args(value):
    """Split a launch-arguments string, honouring double quotes (Windows
    paths often contain spaces: -m "C:\\models\\my model.gguf")."""
    tokens, cur, in_q = [], [], False
    for ch in (value or ""):
        if ch == '"':
            in_q = not in_q
        elif ch in (" ", "\t") and not in_q:
            if cur:
                tokens.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        tokens.append("".join(cur))
    return tokens


def default_fetcher(url, timeout):
    """Stdlib HTTP GET. Returns (status_code|None, body_text|None, error|None).
    HTTP error statuses still return the body (useful for error display)."""
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_BODY_BYTES).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_BODY_BYTES).decode("utf-8", "replace")
        except Exception:
            body = None
        return exc.code, body, f"HTTP {exc.code}"
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError,
            ValueError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def default_poster(url, payload, timeout):
    """Stdlib HTTP POST JSON. Same tuple contract as default_fetcher."""
    try:
        import urllib.error
        import urllib.request

        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_BODY_BYTES).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_BODY_BYTES).decode("utf-8", "replace")
        except Exception:
            body = None
        return exc.code, body, f"HTTP {exc.code}"
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError,
            ValueError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def classify_process(name, exe, cmdline):
    """Return a server-type hint from process attributes, or None."""
    cmd0 = ""
    if isinstance(cmdline, (list, tuple)) and cmdline:
        cmd0 = str(cmdline[0])
    elif isinstance(cmdline, str):
        cmd0 = cmdline
    for text in (name or "", os.path.basename(exe or ""), cmd0):
        low = (text or "").lower()
        for needle, stype in PROCESS_HINTS:
            if needle in low:
                return stype
    return None


def _num(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_prometheus_metrics(text):
    """Parse simple Prometheus text lines into {metric: value}. Only simple
    gauges/counters (name value) are extracted — enough for llama.cpp's
    /metrics output; robust to metric-name variations via substring matching
    later. Returns {} on any problem."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            continue
        try:
            out[parts[0]] = float(parts[1])
        except ValueError:
            continue
    return out


def _find_metric(metrics, *needles):
    """Substring-tolerant metric lookup (llama.cpp metric names vary across
    builds; we match known fragments rather than exact names)."""
    for key in metrics:
        low = key.lower()
        if all(n in low for n in needles):
            return metrics[key]
    return None


# ---------------------------------------------------------------------------
# Per-type API probes. Each returns (info_dict, error_str|None) and never
# raises. Every unavailable field stays None.
# ---------------------------------------------------------------------------
def probe_ollama(base, fetch, post, timeout):
    info, err = {}, None
    st, body, e1 = fetch(base + "/api/tags", timeout)
    if st == 200 and body:
        try:
            data = json.loads(body)
            models = []
            for m in data.get("models") or []:
                details = m.get("details") or {}
                models.append({
                    "name": m.get("name") or m.get("model"),
                    "size_bytes": _num(m.get("size")),
                    "quantization": details.get("quantization_level"),
                    "parameter_size": details.get("parameter_size"),
                    "family": details.get("family"),
                    "digest": (m.get("digest") or "")[:12] or None,
                })
            info["installed_models"] = models
            info["api"] = "ollama"
        except ValueError:
            err = "Ollama /api/tags returned unparsable JSON"
    elif e1:
        err = e1

    st, body, e2 = fetch(base + "/api/ps", timeout)
    loaded = []
    if st == 200 and body:
        try:
            data = json.loads(body)
            for m in data.get("models") or []:
                loaded.append({
                    "name": m.get("name"),
                    "size_bytes": _num(m.get("size")),
                    "vram_bytes": _num(m.get("size_vram")),
                })
            info["loaded_models"] = loaded
        except ValueError:
            pass
    elif e2 and not err:
        err = e2

    st, body, e3 = fetch(base + "/api/version", timeout)
    if st == 200 and body:
        try:
            info["server_version"] = (json.loads(body) or {}).get("version")
        except ValueError:
            pass
    info["active_requests"] = None   # not exposed by polling APIs — honest
    info["tokens_per_sec"] = None
    return info, err


def probe_lm_studio(base, fetch, post, timeout):
    info, err = {}, None
    st, body, e1 = fetch(base + "/api/v0/models", timeout)
    if st == 200 and body:
        try:
            data = json.loads(body)
            installed, loaded = [], []
            for m in (data.get("data") or []):
                entry = {
                    "name": m.get("id"),
                    "publisher": m.get("publisher"),
                    "arch": m.get("arch"),
                    "quantization": m.get("quantization"),
                    "state": m.get("state"),
                    "context_tokens": _num(m.get("max_context_length")),
                }
                installed.append(entry)
                if m.get("state") == "loaded":
                    loaded.append(entry)
            info["installed_models"] = installed
            info["loaded_models"] = loaded
            info["api"] = "lm-studio"
        except ValueError:
            err = "LM Studio /api/v0/models returned unparsable JSON"
    elif e1:
        err = e1
    info["active_requests"] = None
    info["tokens_per_sec"] = None
    return info, err


def probe_llama_server(base, fetch, post, timeout):
    """llama.cpp server probe. Model identification is deliberately layered:
    /health → /props (tolerant across llama.cpp build shapes) → /v1/models
    fallback. A llama-server serves exactly ONE resident model, so when the
    API is healthy and any endpoint identifies the model, that model is
    loaded — this fixes the v2 'no model loaded' misreport."""
    info, err = {}, None

    # 1. /health — 200 ok; 503 typically means a model is loading
    health_ok = False
    st, body, e0 = fetch(base + "/health", timeout)
    if st == 200:
        health_ok = True
        info["health_detail"] = "ok"
    elif st is not None and st >= 500:
        msg = ""
        try:
            data = json.loads(body) if body else {}
            msg = str((data.get("error") or {}).get("message") or "")
        except ValueError:
            pass
        info["health_detail"] = msg or f"health returned HTTP {st}"
        if "load" in (msg or "").lower():
            info["loading"] = True
    elif e0:
        # older llama.cpp builds may not expose /health — not fatal
        err = e0

    # 2. /props — tolerant parsing (shapes differ between builds)
    st, body, e1 = fetch(base + "/props", timeout)
    model_path = model_name = n_ctx = None
    if st == 200 and body:
        try:
            data = json.loads(body)
            mdl = data.get("model") or {}
            model_path = (mdl.get("path") or data.get("model_path")
                          or mdl.get("model_path") or data.get("model_meta"))
            model_name = (mdl.get("name") or mdl.get("alias")
                          or data.get("model_alias"))
            n_ctx = _num(data.get("n_ctx")
                         or (data.get("default_generation_settings") or {})
                         .get("n_ctx") or mdl.get("n_ctx"))
        except ValueError:
            pass
    elif e1 and not err and st != 404:
        # 404 on /props is normal build variance — other endpoints decide
        err = e1

    # 3. /v1/models — always present on llama-server; id is the served model
    st, body, e2 = fetch(base + "/v1/models", timeout)
    v1_id = None
    if st == 200 and body:
        try:
            data = json.loads(body)
            items = data.get("data") or []
            if items:
                v1_id = items[0].get("id") or None
        except ValueError:
            pass
    if not model_name and v1_id:
        model_name = v1_id

    if model_path or model_name or health_ok:
        info["api"] = "llama-server"
    if model_path or model_name:
        info["model_path"] = model_path
        info["model_name"] = (model_name
                              or os.path.basename(model_path or "") or None)
        info["context_tokens"] = n_ctx
        info["loaded_models"] = [{
            "name": model_name or os.path.basename(model_path or "") or "model",
            "path": model_path,
            "context_tokens": n_ctx,
        }]

    # 4. /slots — active requests + context utilization
    st, body, e3 = fetch(base + "/slots", timeout)
    active = ctx_used = None
    if st == 200 and body:
        try:
            slots = json.loads(body)
            if isinstance(slots, dict):
                slots = slots.get("slots") or []
            if isinstance(slots, list):
                active = sum(1 for s in slots if s.get("is_processing"))
                used = 0
                for s in slots:
                    prompt = s.get("prompt") or {}
                    used += _num(prompt.get("n_ctx_tokens")) or 0
                if n_ctx:
                    ctx_used = round(used / n_ctx, 3)
                info["slots"] = len(slots)
        except ValueError:
            pass
    info["active_requests"] = active
    info["context_utilization"] = ctx_used

    # 5. /metrics — tolerant token counters for tokens/sec
    st, body, e4 = fetch(base + "/metrics", timeout)
    if st == 200 and body:
        metrics = parse_prometheus_metrics(body)
        predicted = (_find_metric(metrics, "predicted", "total")
                     or _find_metric(metrics, "generation", "total"))
        if predicted is not None:
            info["tokens_generated_total"] = predicted
        prompts = _find_metric(metrics, "prompt", "total")
        if prompts is not None:
            info["prompt_tokens_total"] = prompts
        processing = (_find_metric(metrics, "requests_processing")
                      or _find_metric(metrics, "processing"))
        if processing is not None and active is None:
            info["active_requests"] = _num(processing)
    elif e4 and not err and st != 404:
        err = e4
    return info, err


def probe_lemonade(base, fetch, post, timeout):
    info, err = {}, None
    st, body, e1 = fetch(base + "/api/v1/health", timeout)
    if st == 200:
        info["api"] = "lemonade"
        try:
            data = json.loads(body) if body else {}
            if data.get("status") and data.get("status") != "ok":
                info["health_detail"] = data.get("status")
        except ValueError:
            pass
    elif e1:
        err = e1

    st, body, e2 = fetch(base + "/api/v1/models", timeout)
    if st == 200 and body:
        try:
            data = json.loads(body)
            models = []
            items = data.get("data") or data.get("models") or []
            for m in items:
                models.append({"name": m.get("id") or m.get("name"),
                               "size_bytes": _num(m.get("size")),
                               "quantization": m.get("quantization")})
            info["installed_models"] = models
        except ValueError:
            pass
    elif e2 and not err:
        err = e2
    info["active_requests"] = None
    info["tokens_per_sec"] = None
    return info, err


def probe_openai_generic(base, fetch, post, timeout):
    info, err = {}, None
    st, body, e1 = fetch(base + "/v1/models", timeout)
    if st == 200 and body:
        try:
            data = json.loads(body)
            models = []
            for m in (data.get("data") or []):
                models.append({"name": m.get("id"),
                               "owned_by": m.get("owned_by")})
            info["installed_models"] = models
            info["loaded_models"] = [{"name": models[0]["name"]}] if models else []
            info["api"] = "openai-compatible"
        except ValueError:
            err = "/v1/models returned unparsable JSON"
    elif e1:
        err = e1
    info["active_requests"] = None
    info["tokens_per_sec"] = None
    return info, err


PROBES = {
    "ollama": probe_ollama,
    "lm-studio": probe_lm_studio,
    "llama-server": probe_llama_server,
    "lemonade": probe_lemonade,
    "vllm": probe_openai_generic,
    "sglang": probe_openai_generic,
    "openai": probe_openai_generic,
}


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------
class AIManager:
    """Owns AI-server discovery, probing, config and management controls.

    Threading contract: probing and scanning run on a bounded executor and a
    dedicated loop thread; Flask handlers only call snapshot()/control methods
    that submit work and return immediately. psutil usage here is limited to
    process inspection (its own Process objects — it never touches the system
    sampler's network/disk baselines).
    """

    def __init__(self, config_path=None, self_pid=None,
                 fetch=default_fetcher, post=default_poster,
                 psutil_module=None, popen=None,
                 discovery_interval=DISCOVERY_INTERVAL,
                 probe_timeout=DEFAULT_PROBE_TIMEOUT,
                 clock=time.monotonic, sleeper=time.sleep):
        self._config_path = config_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ai_servers.json")
        self._self_pid = self_pid if self_pid is not None else os.getpid()
        self._fetch = fetch
        self._post = post
        self.psutil = psutil_module if psutil_module is not None else psutil
        self._popen_fn = popen or subprocess.Popen
        self._discovery_interval = float(discovery_interval)
        self._probe_timeout = float(probe_timeout)
        self._clock = clock
        self._sleeper = sleeper

        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread = None
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-probe")
        self._ops_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-ops")
        self._scan_in_flight = False
        self._refresh_requested = False
        self._next_scan = 0.0

        self._records = []          # last probe results (list of dicts)
        self._last_scan_ts = None
        self._scan_error = None
        self._config = []           # configured servers (dicts)
        self._managed = {}          # server_id -> {"popen", "started_at", "logs"}
        self._ops = {}              # server_id -> op descriptor dict
        self._proc_cache = {}       # pid -> psutil.Process (cpu baselines)
        self._token_counters = {}   # (host,port) -> (last_total, last_ts)
        self._vram_by_pid = {}      # pid -> vram bytes (throttled nvidia-smi)
        self._vram_checked = 0.0
        self._nvidia_smi_path = None

        self._load_config()

    # -- lifecycle ------------------------------------------------------------
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop,
                                        name="ai-discovery", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_evt.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._ops_pool.shutdown(wait=False, cancel_futures=True)

    @property
    def alive(self):
        return bool(self._thread and self._thread.is_alive())

    def _loop(self):
        while not self._stop_evt.is_set():
            now = self._clock()
            due = now >= self._next_scan
            if (due or self._refresh_requested) and not self._scan_in_flight:
                self._refresh_requested = False
                self._next_scan = now + self._discovery_interval
                self._scan_in_flight = True
                self._pool.submit(self._scan_task)
            self._sleeper(0.5)

    def request_refresh(self):
        self._refresh_requested = True

    def _scan_task(self):
        try:
            self._scan_cycle()
            with self._lock:
                self._last_scan_ts = time.time()
                self._scan_error = None
        except Exception as exc:
            with self._lock:
                self._scan_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._scan_in_flight = False

    # -- discovery ------------------------------------------------------------
    def _find_ai_processes(self):
        """[(pid, name, exe, cmdline, type_hint)] for AI-looking processes."""
        found = []
        try:
            procs = list(self.psutil.process_iter(
                ["pid", "name", "exe", "cmdline", "create_time"]))
        except Exception:
            return found
        for p in procs:
            info = p.info
            pid = info.get("pid")
            if not pid or pid == self._self_pid:
                continue
            hint = classify_process(info.get("name"), info.get("exe"),
                                    info.get("cmdline"))
            if hint:
                found.append({"pid": pid, "name": info.get("name"),
                              "exe": info.get("exe"),
                              "cmdline": info.get("cmdline"),
                              "create_time": info.get("create_time"),
                              "type": hint})
        return found

    def _find_listening_ports(self):
        """Return (pid_map, orphan_ports).

        pid_map: {pid: [port, ...]} for listening TCP ports whose owning PID
        is known. orphan_ports: set of listening ports with no PID (common for
        Docker-published ports on Linux). Access denial returns empty results.
        """
        mapping = {}
        orphan_ports = set()
        try:
            conns = self.psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            return mapping, orphan_ports
        except Exception:
            return mapping, orphan_ports
        for c in conns or []:
            if c.status != self.psutil.CONN_LISTEN:
                continue
            pid = getattr(c, "pid", None)
            laddr = getattr(c, "laddr", None)
            port = getattr(laddr, "port", None) if laddr else None
            if not port:
                continue
            port = int(port)
            if not pid:
                orphan_ports.add(port)
                continue
            mapping.setdefault(pid, []).append(port)
        return mapping, orphan_ports

    @staticmethod
    def _valid_probe_port(port):
        """True when port is a usable TCP port number for URL construction."""
        try:
            p = int(port)
        except (TypeError, ValueError):
            return False
        return 1 <= p <= 65535

    def _probe_targets(self):
        """Combine configured servers + AI processes + known ports into probe
        targets (a LIST — one entry PER CONFIG, so several configs on the same
        host:port stay distinct and controls remain unambiguous)."""
        targets = []           # ordered, unique entries
        by_key = {}            # endpoint key -> primary target

        def new_target(key, host, port, **kw):
            t = {
                "key": key, "host": host, "port": port, "type_hint": None,
                "pid": None, "name": None, "exe": None, "cmdline": None,
                "create_time": None, "configured": False, "config": None,
                "port_listening": False, "process_found": False,
            }
            for k, v in kw.items():
                if v is not None:
                    t[k] = v
            targets.append(t)
            by_key.setdefault(key, t)
            return t

        port_map, orphan_ports = self._find_listening_ports()
        all_listening = set(orphan_ports)
        for pid, ports in port_map.items():
            if pid == self._self_pid:
                continue
            all_listening.update(ports)
        ai_procs = self._find_ai_processes()

        # configured servers — authoritative, one target per config entry
        for cfg in list(self._config):
            host = cfg.get("host") or "127.0.0.1"
            try:
                port = int(cfg.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
            if port and not self._valid_probe_port(port):
                continue
            key = f"{host}:{port}" if port else f"cfg:{cfg.get('id')}"
            new_target(key, host, port or None, type_hint=cfg.get("type"),
                       name=cfg.get("name"), exe=cfg.get("exe") or None,
                       configured=True, config=cfg)

        # AI-looking processes (attach their listening port when known)
        for pr in ai_procs:
            ports = [p for p in port_map.get(pr["pid"], []) if p != self._self_pid]
            port = ports[0] if ports else None
            # Processes without a known listening port are still visible as
            # "loading", but must never be HTTP-probed with port=None.
            key = f"127.0.0.1:{port}" if port else f"proc:{pr['pid']}"
            if key in by_key:
                # a configured target already covers this endpoint — enrich it
                t = by_key[key]
                if t["pid"] is None:
                    t["pid"] = pr["pid"]
                    t["process_found"] = True
                continue
            t = new_target(key, "127.0.0.1", port,
                           type_hint=pr["type"], pid=pr["pid"], name=pr["name"],
                           exe=pr["exe"], cmdline=pr["cmdline"],
                           create_time=pr.get("create_time"), process_found=True)
            t["port_listening"] = bool(port)

        # listening state + pid enrichment for every target
        for t in targets:
            if not t["port"]:
                continue
            if t["port"] in all_listening:
                t["port_listening"] = True
            owner_pids = [pid for pid, ports in port_map.items()
                          if t["port"] in ports and pid != self._self_pid]
            if owner_pids:
                t["port_listening"] = True
                if t["pid"] is None:
                    t["pid"] = owner_pids[0]
                    t["process_found"] = True

        # known AI ports listening without any target yet — includes
        # Docker-published ports that report no owning PID.
        for port in sorted(all_listening):
            if port not in KNOWN_AI_PORTS:
                continue
            key = f"127.0.0.1:{port}"
            if key in by_key:
                continue
            owner = next((pid for pid, ports in port_map.items()
                          if port in ports and pid != self._self_pid), None)
            new_target(key, "127.0.0.1", port,
                       type_hint=KNOWN_AI_PORTS[port], pid=owner,
                       port_listening=True, process_found=bool(owner))

        return self._dedupe_portless_processes(targets)

    @staticmethod
    def _dedupe_portless_processes(targets):
        """Attach Docker-orphaned AI processes to same-type listening ports.

        When Ollama (or similar) runs as a host process but Docker publishes
        the TCP port with pid=None, discovery previously emitted both
        ``proc:<pid>`` (loading, no port) and ``127.0.0.1:11434`` (online).
        Merge them into one listening/online target.
        """
        listening_by_type = {}
        for t in targets:
            if t.get("port") and t.get("type_hint"):
                listening_by_type.setdefault(t["type_hint"], []).append(t)

        merged = []
        for t in targets:
            if t.get("port"):
                merged.append(t)
                continue
            hint = t.get("type_hint")
            attached = False
            for cand in listening_by_type.get(hint, []):
                if cand.get("pid") not in (None, t.get("pid")):
                    continue
                if cand.get("pid") is None:
                    cand["pid"] = t.get("pid")
                    cand["process_found"] = True
                    for field in ("name", "exe", "cmdline", "create_time"):
                        if t.get(field) and not cand.get(field):
                            cand[field] = t.get(field)
                attached = True
                break
            if not attached:
                merged.append(t)
        return merged

    # -- probe one target ------------------------------------------------------
    def _probe_one(self, target):
        host = target.get("host") or "127.0.0.1"
        port = target.get("port")
        stype = target.get("type_hint") or "openai"

        # Malformed discovery entries must never build InvalidURL payloads.
        if not self._valid_probe_port(port):
            record = {
                "key": target.get("key") or f"proc:{target.get('pid') or 'unknown'}",
                "name": target.get("name") or stype,
                "type": stype,
                "api_type": None,
                "host": host,
                "port": port,
                "url": None,
                "pid": target.get("pid"),
                "exe": target.get("exe"),
                "configured": target.get("configured", False),
                "port_listening": False,
                "process_found": bool(target.get("process_found")),
                "http_status": None,
                "latency_ms": None,
                "api_error": "no listening port yet — skipped HTTP probe",
                "probed_at": time.time(),
            }
            cfg = target.get("config")
            if cfg:
                record["id"] = cfg.get("id")
            self._apply_status(record, target)
            self._apply_process_metrics(record, target)
            return record

        port = int(port)
        base = f"http://{host}:{port}"
        probe_fn = PROBES.get(stype, probe_openai_generic)

        info = {}
        api_error = None
        http_status = None
        latency_ms = None

        # health pre-probe: confirms the API answers and measures latency
        t0 = self._clock()
        try:
            st, body, err = self._fetch(base + "/", self._probe_timeout)
        except Exception as exc:
            # urllib.InvalidURL and friends must not abort the scan cycle
            st, body, err = None, None, f"{type(exc).__name__}: {exc}"
        latency_ms = round((self._clock() - t0) * 1000, 1)
        http_status = st
        if st is None and err:
            api_error = err

        if st == 200 or (st and st < 500):
            try:
                info, perr = probe_fn(base, self._fetch, self._post,
                                      self._probe_timeout)
                if perr:
                    api_error = api_error or perr
            except Exception as exc:  # a probe must never break the scan
                api_error = api_error or f"probe failed: {type(exc).__name__}: {exc}"
        elif st is not None:
            api_error = api_error or f"HTTP {st}"

        record = {
            "key": target["key"],
            "name": target.get("name") or info.get("api") or stype,
            "type": stype if info.get("api") else (info.get("api") or stype),
            "api_type": info.get("api"),
            "host": host,
            "port": port,
            "url": base,
            "pid": target.get("pid"),
            "exe": target.get("exe"),
            "configured": target.get("configured", False),
            "port_listening": bool(target.get("port_listening")),
            "process_found": bool(target.get("process_found")),
            "http_status": http_status,
            "latency_ms": latency_ms,
            "api_error": api_error,
            "probed_at": time.time(),
        }
        cfg = target.get("config")
        if cfg:
            # the record belongs to THIS config entry even when several
            # configs share a host:port — controls stay unambiguous
            record["id"] = cfg.get("id")
        record.update(info)
        self._apply_status(record, target)
        self._apply_process_metrics(record, target)
        self._apply_token_rate(record)
        return record

    def _apply_status(self, rec, target):
        """Status machine. A listening port alone is NOT health."""
        loading_states = {"loading"}
        loaded = rec.get("loaded_models") or []
        active = rec.get("active_requests")

        lm_loading = bool(rec.get("loading")) or \
            any((m or {}).get("state") in loading_states
                for m in (rec.get("installed_models") or []))

        if rec["http_status"] is None and rec.get("api_error"):
            # HTTP never answered
            rec["ready"] = None
            if rec.get("port_listening"):
                rec["status"] = "listening"
                rec["status_detail"] = ("port is listening but the API did "
                                        "not answer: " + str(rec["api_error"]))
            elif rec.get("process_found"):
                rec["status"] = "loading"
                rec["status_detail"] = ("process is running but no listening "
                                        "port yet")
            else:
                rec["status"] = "offline"
                rec["status_detail"] = "nothing listening and no process found"
            rec["health_ok"] = False
            return

        if rec["http_status"] is not None and rec["http_status"] >= 500:
            rec["status"] = "error"
            rec["status_detail"] = f"API returned HTTP {rec['http_status']}"
            rec["health_ok"] = False
            rec["ready"] = None
            return

        if rec.get("api_type"):
            rec["health_ok"] = True
            if lm_loading:
                rec["status"] = "loading"
                rec["status_detail"] = "model is loading (reported by server)"
            elif active:
                rec["status"] = "busy"
                rec["status_detail"] = f"{active} active request(s)"
            elif loaded:
                rec["status"] = "online"
                rec["status_detail"] = "model loaded and ready for inference"
                rec["ready"] = True
            elif rec.get("model_path"):  # llama-server always serves one model
                rec["status"] = "online"
                rec["status_detail"] = "model loaded and ready for inference"
                rec["ready"] = True
            elif rec.get("loaded_models") is not None:
                rec["status"] = "online"
                rec["status_detail"] = "API up — no model loaded yet"
                rec["ready"] = False
            else:
                rec["status"] = "online"
                rec["status_detail"] = "API up — load state not exposed"
                rec["ready"] = None
            return

        # API answered but not a recognized schema
        rec["status"] = "listening"
        rec["status_detail"] = "port is open but no known AI API responded"
        rec["health_ok"] = False
        rec["ready"] = None
    def _apply_process_metrics(self, rec, target):
        pid = rec.get("pid")
        if not pid:
            return
        try:
            proc = self._proc_cache.get(pid)
            if proc is None:
                proc = self.psutil.Process(pid)
                self._proc_cache[pid] = proc
                try:
                    proc.cpu_percent(None)  # prime baseline
                except Exception:
                    pass
                rec["cpu_percent"] = None   # real value next scan
            else:
                rec["cpu_percent"] = _num(proc.cpu_percent(None))
            mi = proc.memory_info()
            rec["ram_bytes"] = getattr(mi, "rss", None)
            create = rec.get("create_time") or proc.create_time()
            rec["uptime_seconds"] = max(0.0, time.time() - create)
            try:
                rec["exe"] = rec.get("exe") or proc.exe()
            except Exception:
                pass
        except (self.psutil.NoSuchProcess, self.psutil.AccessDenied,
                self.psutil.ZombieProcess):
            rec["cpu_percent"] = None
            rec["ram_bytes"] = None
        except Exception:
            rec["cpu_percent"] = None
            rec["ram_bytes"] = None

        if rec.get("vram_bytes") is None:
            rec["vram_bytes"] = self._vram_by_pid.get(pid)

    def _vram_map(self):
        """pid -> VRAM bytes via nvidia-smi compute apps; throttled to 15 s.
        Empty when no NVIDIA tooling exists (Intel iGPU path — honest None)."""
        now = time.time()
        if now - self._vram_checked < 15.0:
            return self._vram_by_pid
        self._vram_checked = now
        if self._nvidia_smi_path is None:
            import shutil
            self._nvidia_smi_path = shutil.which("nvidia-smi")
        if not self._nvidia_smi_path:
            self._vram_by_pid = {}
            return self._vram_by_pid
        extra = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} \
            if sys.platform == "win32" else {}
        try:
            proc = subprocess.run(
                [self._nvidia_smi_path,
                 "--query-compute-apps=pid,used_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=3.0, **extra)
            mapping = {}
            for line in (proc.stdout or "").strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) == 2:
                    try:
                        mapping[int(parts[0])] = int(float(parts[1])) * 1024 * 1024
                    except ValueError:
                        continue
            self._vram_by_pid = mapping
        except Exception:
            self._vram_by_pid = {}
        return self._vram_by_pid

    def _apply_token_rate(self, rec):
        key = rec.get("key")
        total = rec.get("tokens_generated_total")
        if total is None:
            return
        last = self._token_counters.get(key)
        if last and total >= last[0] and rec.get("probed_at") and last[1]:
            dt = rec["probed_at"] - last[1]
            if dt > 0:
                rec["tokens_per_sec"] = round((total - last[0]) / dt, 2)
        self._token_counters[key] = (total, rec.get("probed_at"))

    # -- scan ------------------------------------------------------------------
    def _scan_cycle(self):
        self._vram_map()  # refresh the (throttled) VRAM map
        targets = self._probe_targets()
        records = []
        if targets:
            futures = [self._pool.submit(self._probe_one, t) for t in targets]
            for fut in futures:
                try:
                    records.append(fut.result(timeout=30.0))
                except Exception as exc:
                    records.append({
                        "key": "unknown", "name": "probe error", "status": "error",
                        "status_detail": f"{type(exc).__name__}: {exc}",
                        "health_ok": False, "probed_at": time.time(),
                    })
        # managed servers that are not currently listening/probed
        records.extend(self._managed_offline_records(records))
        with self._lock:
            self._records = records

    def _managed_offline_records(self, existing):
        """For each managed server that produced no record this scan, emit an
        'offline' record (process exited or port gone)."""
        keys = {r.get("key") for r in existing}
        out = []
        with self._lock:
            managed = list(self._managed.items())
        for sid, m in managed:
            cfg = self._get_config(sid)
            if not cfg:
                continue
            port = int(cfg.get("port") or 0)
            key = f"{cfg.get('host') or '127.0.0.1'}:{port}" if port else sid
            if key in keys:
                continue
            popen = m.get("popen")
            running = popen is not None and popen.poll() is None
            out.append({
                "key": key, "id": sid, "name": cfg.get("name"),
                "type": cfg.get("type") or "openai", "host": cfg.get("host"),
                "port": port, "url": f"http://{cfg.get('host')}:{port}",
                "configured": True, "port_listening": False,
                "process_found": running,
                "status": "loading" if running else "offline",
                "status_detail": ("process running, waiting for port"
                                  if running else "process exited / not started"),
                "health_ok": False, "probed_at": time.time(),
            })
        return out

    # -- configuration ----------------------------------------------------------
    def _load_config(self):
        try:
            with open(self._config_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                self._config = [c for c in data if isinstance(c, dict) and c.get("id")]
        except (OSError, ValueError):
            self._config = []

    def _save_config(self):
        tmp = self._config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._config, fh, indent=2)
        os.replace(tmp, self._config_path)

    def _get_config(self, server_id):
        for c in self._config:
            if c.get("id") == server_id:
                return c
        return None

    def list_config(self):
        with self._lock:
            return [dict(c) for c in self._config]

    def save_server_config(self, entry):
        """Add or update a configured server. Returns (ok, message, warnings).
        Validation is strict about ports; missing executables are only a
        warning (monitor-only configs are allowed)."""
        warnings = []
        entry = dict(entry or {})
        name = (entry.get("name") or "").strip()
        try:
            port = int(entry.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not name:
            return False, "name is required", warnings
        if not 1 <= port <= 65535:
            return False, "port must be between 1 and 65535", warnings
        host = (entry.get("host") or "127.0.0.1").strip() or "127.0.0.1"
        stype = (entry.get("type") or "openai").strip()
        if stype not in PROBES:
            return False, (f"unknown server type {stype!r} — supported: "
                           + ", ".join(sorted(PROBES))), warnings
        exe = (entry.get("exe") or "").strip()
        if exe and not os.path.isfile(exe) and host in LOCAL_HOSTS:
            warnings.append(f"executable not found: {exe} — start will fail "
                            f"until the path is corrected")
        sid = entry.get("id") or uuid.uuid4().hex[:12]
        record = {
            "id": sid, "name": name, "type": stype, "host": host,
            "port": port, "exe": exe, "args": entry.get("args") or "",
            "model_dir": (entry.get("model_dir") or "").strip(),
        }
        with self._lock:
            replaced = False
            for i, c in enumerate(self._config):
                if c.get("id") == sid:
                    self._config[i] = record
                    replaced = True
                    break
            if not replaced:
                for c in self._config:
                    if int(c.get("port") or 0) == port and \
                            (c.get("host") or "127.0.0.1") == host:
                        warnings.append(
                            f"port {port} on {host} is already configured for "
                            f"'{c.get('name')}' — both will probe the same "
                            f"endpoint")
                self._config.append(record)
            try:
                self._save_config()
            except OSError as exc:
                return False, f"failed to save config: {exc}", warnings
        self.request_refresh()
        return True, ("updated" if replaced else "added"), warnings

    def delete_server_config(self, server_id):
        with self._lock:
            before = len(self._config)
            self._config = [c for c in self._config if c.get("id") != server_id]
            removed = len(self._config) < before
            if removed:
                self._managed.pop(server_id, None)
                try:
                    self._save_config()
                except OSError as exc:
                    return False, f"config removed in memory but save failed: {exc}"
        return removed, ("deleted" if removed else "unknown server id"), []

    def adopt_server(self, key):
        """Turn a discovered-but-unconfigured server into a configured one
        (pre-filled from the discovery record). This is the only way a
        discovered server gains management controls.

        Idempotent: if host:port is already registered, existing config is
        preserved (no overwrite of exe/args). Registration alone does not
        start/stop/load/unload anything.
        """
        with self._lock:
            rec = next((r for r in self._records if r.get("key") == key), None)
        if not rec:
            return False, f"no discovered server with key {key!r}", []
        host = rec.get("host") or "127.0.0.1"
        if host not in LOCAL_HOSTS:
            return False, "only local servers can be adopted for management", []
        try:
            port = int(rec.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65535:
            return False, f"invalid discovery port for key {key!r}", []
        # Preserve existing registration for this endpoint
        with self._lock:
            for c in self._config:
                if int(c.get("port") or 0) == port and \
                        (c.get("host") or "127.0.0.1") == host:
                    self.request_refresh()
                    return True, "already registered", []
        return self.save_server_config({
            "id": uuid.uuid4().hex[:12],
            "name": rec.get("name") or f"AI server @{port}",
            "type": rec.get("type") or "openai",
            "host": host, "port": port,
            # exe left empty unless discovery already knows it — avoids
            # implying process-control rights we do not have.
            "exe": rec.get("exe") or "",
            "args": "", "model_dir": "",
        })

    # -- controls -----------------------------------------------------------------
    def _capabilities(self, cfg, rec=None):
        host = (cfg.get("host") or "127.0.0.1")
        local = host in LOCAL_HOSTS
        stype = cfg.get("type") or "openai"
        exe_ok = bool(cfg.get("exe")) and local
        return {
            "can_start": exe_ok and local,
            "can_stop": exe_ok and local,
            "can_restart": exe_ok and local,
            "can_select_model": stype in ("llama-server", "lemonade",
                                          "lm-studio", "ollama"),
            "can_load": stype in ("ollama", "lm-studio", "lemonade"),
            "can_unload": stype in ("ollama", "lm-studio", "lemonade"),
            "local": local,
        }

    def _find_process_pid(self, cfg):
        """Find the live PID of a configured server WITHOUT guessing: the
        process must listen on the configured port AND its executable must
        match the configured path (case-insensitive on Windows)."""
        port = int(cfg.get("port") or 0)
        exe_cfg = os.path.normcase(os.path.abspath(cfg.get("exe") or ""))
        port_map, _orphan_ports = self._find_listening_ports()
        for pid, ports in port_map.items():
            if port and port not in ports:
                continue
            try:
                proc = self.psutil.Process(pid)
                exe = proc.exe()
            except Exception:
                continue
            if not exe:
                continue
            if exe_cfg and os.path.normcase(os.path.abspath(exe)) == exe_cfg:
                return pid
        return None

    def _server_busy(self, server_id):
        with self._lock:
            for r in self._records:
                if r.get("id") == server_id or r.get("key") == server_id:
                    return bool(r.get("active_requests"))
        return False

    def start_server(self, server_id, model=None):
        cfg = self._get_config(server_id)
        if not cfg:
            return False, "unknown server id — configure it first", None
        caps = self._capabilities(cfg)
        if not caps["can_start"]:
            if not cfg.get("exe"):
                return False, ("no executable configured for this server — "
                               "edit the configuration first"), None
            return False, (f"refusing to start a server on non-local host "
                           f"{cfg.get('host')}"), None
        exe = cfg["exe"]
        if not os.path.isfile(exe):
            return False, f"executable not found: {exe}", None
        with self._lock:
            if len(self._managed) >= MAX_MANAGED_SERVERS:
                return False, (f"managed-server limit reached "
                               f"({MAX_MANAGED_SERVERS}) — stop one first"), None
            if server_id in self._managed and \
                    self._managed[server_id]["popen"].poll() is None:
                return False, "server is already running (managed)", None
        cmd = [exe] + split_launch_args(cfg.get("args"))
        stype = cfg.get("type") or "openai"
        if model and stype == "llama-server" and \
                "--model" not in cmd and "-m" not in cmd:
            cmd += ["--model", model]
        env = dict(os.environ)
        if stype == "ollama" and cfg.get("port"):
            env["OLLAMA_HOST"] = f"{cfg.get('host') or '127.0.0.1'}:{cfg['port']}"
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                  "text": True, "env": env, "cwd": os.path.dirname(exe) or None}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            popen = self._popen_fn(cmd, **kwargs)
        except OSError as exc:
            return False, f"failed to launch {exe}: {exc}", None
        entry = {"popen": popen, "started_at": time.time(),
                 "logs": deque(maxlen=MAX_LOG_LINES),
                 "model": model}
        with self._lock:
            self._managed[server_id] = entry
            self._ops[server_id] = {"kind": "start", "status": "running",
                                    "message": f"launched pid {popen.pid}",
                                    "ts": time.time()}
        for stream, tag in ((popen.stdout, "out"), (popen.stderr, "err")):
            if stream is not None:
                self._pool.submit(self._drain_logs, server_id, stream, tag)
        self.request_refresh()
        return True, f"started (pid {popen.pid})", model

    def _drain_logs(self, server_id, stream, tag):
        try:
            for line in iter(stream.readline, ""):
                with self._lock:
                    m = self._managed.get(server_id)
                    if m is None:
                        break
                    m["logs"].append(
                        f"{time.strftime('%H:%M:%S')} [{tag}] {line.rstrip()}")
            stream.close()
        except Exception:
            pass

    def server_logs(self, server_id, max_lines=80):
        with self._lock:
            m = self._managed.get(server_id)
            if m:
                return list(m["logs"])[-max_lines:]
        # unmanaged: offer the probe/api errors we have seen
        with self._lock:
            rec = next((r for r in self._records if r.get("id") == server_id), None)
        if rec and rec.get("api_error"):
            return [f"{time.strftime('%H:%M:%S')} [probe] {rec['api_error']}"]
        return []

    def stop_server(self, server_id, force=False):
        cfg = self._get_config(server_id)
        if not cfg:
            return False, "unknown server id", None
        caps = self._capabilities(cfg)
        if not caps["can_stop"]:
            return False, "this server has no local executable configured", None
        if not force and self._server_busy(server_id):
            return False, ("server reports active requests — refusing to stop "
                           "inference in progress; repeat with force to "
                           "override"), "busy"
        pid = None
        with self._lock:
            m = self._managed.get(server_id)
            if m and m["popen"].poll() is None:
                pid = m["popen"].pid
        if pid is None:
            pid = self._find_process_pid(cfg)
        if pid is None:
            with self._lock:
                self._managed.pop(server_id, None)
            return True, "server is not running", None
        try:
            proc = self.psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
                proc.wait(timeout=5)
        except self.psutil.NoSuchProcess:
            pass
        except Exception as exc:
            return False, f"failed to stop pid {pid}: {exc}", None
        with self._lock:
            self._managed.pop(server_id, None)
            self._ops[server_id] = {"kind": "stop", "status": "done",
                                    "message": f"stopped pid {pid}",
                                    "ts": time.time()}
        self.request_refresh()
        return True, f"stopped (pid {pid})", None

    def restart_server(self, server_id, force=False, model=None):
        ok, msg, tag = self.stop_server(server_id, force=force)
        if not ok:
            return False, msg, tag
        # wait for the port to be released (best effort, ≤8 s)
        cfg = self._get_config(server_id)
        if cfg and cfg.get("port"):
            deadline = self._clock() + 8.0
            while self._clock() < deadline:
                if not self._port_open(int(cfg["port"])):
                    break
                self._sleeper(0.25)
        ok2, msg2, started_model = self.start_server(server_id, model=model)
        return ok2, f"restart: {msg}; {msg2}", started_model

    def _port_open(self, port, host="127.0.0.1"):
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except OSError:
            return False

    def load_model(self, server_id, model):
        cfg = self._get_config(server_id)
        if not cfg:
            return False, "unknown server id", None
        caps = self._capabilities(cfg)
        if not caps["can_load"]:
            return False, (f"loading models at runtime is not supported for "
                           f"server type '{cfg.get('type')}' — use restart "
                           f"with a selected model instead"), "unsupported"
        if not model:
            return False, "model name is required", None
        base = f"http://{cfg.get('host')}:{cfg.get('port')}"
        stype = cfg.get("type")
        op = {"kind": "load", "status": "running", "message": model,
              "ts": time.time()}
        with self._lock:
            self._ops[server_id] = op

        def job():
            if stype == "ollama":
                st, _, err = self._post(base + "/api/generate",
                                        {"model": model, "prompt": "",
                                         "stream": False, "keep_alive": -1},
                                        timeout=180)
                self._finish_op(server_id, st, err,
                                "model warm-up request sent")
            elif stype == "lm-studio":
                st, _, err = self._post(base + "/api/v0/models/load",
                                        {"model": model}, timeout=300)
                self._finish_op(server_id, st, err, "load request sent")
            elif stype == "lemonade":
                st, _, err = self._post(base + "/api/v1/load",
                                        {"model": model}, timeout=300)
                self._finish_op(server_id, st, err, "load request sent")
            else:
                with self._lock:
                    self._ops[server_id] = {"kind": "load", "status": "failed",
                                            "message": "unsupported",
                                            "ts": time.time()}
        self._ops_pool.submit(job)
        return True, "load request submitted (running in background)", model

    def unload_model(self, server_id, model):
        cfg = self._get_config(server_id)
        if not cfg:
            return False, "unknown server id", None
        caps = self._capabilities(cfg)
        if not caps["can_unload"]:
            return False, (f"unloading is not supported for server type "
                           f"'{cfg.get('type')}'"), "unsupported"
        if not model:
            return False, "model name is required", None
        base = f"http://{cfg.get('host')}:{cfg.get('port')}"
        stype = cfg.get("type")
        with self._lock:
            self._ops[server_id] = {"kind": "unload", "status": "running",
                                    "message": model, "ts": time.time()}

        def job():
            if stype == "ollama":
                st, _, err = self._post(base + "/api/generate",
                                        {"model": model, "prompt": "",
                                         "stream": False, "keep_alive": 0},
                                        timeout=60)
                self._finish_op(server_id, st, err, "unload request sent")
            elif stype == "lm-studio":
                st, _, err = self._post(base + "/api/v0/models/unload",
                                        {"model": model}, timeout=120)
                self._finish_op(server_id, st, err, "unload request sent")
            elif stype == "lemonade":
                st, _, err = self._post(base + "/api/v1/unload",
                                        {"model": model}, timeout=120)
                self._finish_op(server_id, st, err, "unload request sent")
            else:
                with self._lock:
                    self._ops[server_id] = {"kind": "unload", "status": "failed",
                                            "message": "unsupported",
                                            "ts": time.time()}
        self._ops_pool.submit(job)
        return True, "unload request submitted", model

    def _finish_op(self, server_id, status_code, error, ok_message):
        with self._lock:
            prev = self._ops.get(server_id) or {}
            if status_code is not None and 200 <= status_code < 300:
                self._ops[server_id] = {"kind": prev.get("kind", "op"),
                                        "status": "done",
                                        "message": ok_message,
                                        "ts": time.time()}
            else:
                self._ops[server_id] = {"kind": prev.get("kind", "op"),
                                        "status": "failed",
                                        "message": error or
                                        f"HTTP {status_code}",
                                        "ts": time.time()}
        self.request_refresh()

    def list_model_files(self, server_id):
        """Local model files (*.gguf + Ollama/LM Studio remote lists) for the
        UI's model picker. Returns (ok, models, message)."""
        cfg = self._get_config(server_id)
        if not cfg:
            return False, [], "unknown server id"
        models = []
        model_dir = cfg.get("model_dir") or ""
        if model_dir and os.path.isdir(model_dir):
            try:
                for fn in sorted(os.listdir(model_dir)):
                    if fn.lower().endswith((".gguf", ".bin", ".safetensors")):
                        path = os.path.join(model_dir, fn)
                        try:
                            size = os.path.getsize(path)
                        except OSError:
                            size = None
                        models.append({"name": fn, "path": path,
                                       "size_bytes": size, "source": "local"})
            except OSError as exc:
                return False, models, f"cannot read model dir: {exc}"
        # remote model lists from the server API (installed models)
        with self._lock:
            rec = next((r for r in self._records if r.get("id") == server_id
                        or r.get("key") == f"{cfg.get('host')}:{cfg.get('port')}"),
                       None)
        seen = {m.get("name") for m in models}
        for m in (rec or {}).get("installed_models") or []:
            if m.get("name") and m["name"] not in seen:
                models.append({"name": m["name"], "path": None,
                               "size_bytes": m.get("size_bytes"),
                               "quantization": m.get("quantization"),
                               "source": "server"})
                seen.add(m["name"])
        if not models:
            return True, [], "no model files found — set model_dir or start the server"
        return True, models, None

    # -- snapshot -----------------------------------------------------------------
    def snapshot(self):
        with self._lock:
            records = [dict(r) for r in self._records]
            config = [dict(c) for c in self._config]
            ops = {k: dict(v) for k, v in self._ops.items()}
            scan_ts = self._last_scan_ts
            scan_err = self._scan_error
            in_flight = self._scan_in_flight
            managed = {k: {"pid": v["popen"].pid,
                           "running": v["popen"].poll() is None,
                           "started_at": v["started_at"],
                           "model": v.get("model")}
                       for k, v in self._managed.items()}
        cfg_by_id = {c.get("id"): c for c in config}
        cfg_by_key = {}
        for c in config:
            cfg_by_key[f"{c.get('host')}:{c.get('port')}"] = c
        for r in records:
            cfg = None
            if r.get("id"):
                cfg = cfg_by_id.get(r["id"])
            if cfg is None:
                cfg = cfg_by_key.get(r.get("key"))
            if cfg:
                r["id"] = cfg["id"]
                r["configured"] = True
                r["name"] = cfg.get("name") or r.get("name")
                caps = self._capabilities(cfg, r)
                r["capabilities"] = caps
            else:
                r["capabilities"] = {"can_start": False, "can_stop": False,
                                     "can_restart": False, "can_load": False,
                                     "can_unload": False, "local": False}
            r["managed"] = bool(managed.get(r.get("id")))
            m = managed.get(r.get("id"))
            if m and m.get("running") and r.get("status") == "offline":
                # started by this dashboard, port not open yet — that is
                # 'loading', not 'offline'
                r["status"] = "loading"
                r["status_detail"] = ("managed process is running — waiting "
                                      "for its port to open")
                r["process_found"] = True
        return {
            "servers": sorted(records,
                              key=lambda r: (0 if r.get("configured") else 1,
                                             r.get("name") or "")),
            "config": config,
            "ops": ops,
            "managed": managed,
            "discovery": {
                "last_scan_ts": scan_ts,
                "probing": in_flight,
                "interval_seconds": self._discovery_interval,
                "last_error": scan_err,
                "known_ports": sorted(KNOWN_AI_PORTS),
            },
            "config_path": self._config_path,
        }
