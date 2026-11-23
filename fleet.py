"""Fleet Servers v3 — three-machine fleet monitoring for Kayock Fleet Monitor.

Design rules:
* Machines are configured manually (name + host + port of the remote
  monitor) OR discovered via LAN UDP broadcast. No fixed-IP assumptions and
  no remote changes — a peer simply runs this same application.
* Polling runs on a dedicated thread with per-request timeouts; a slow or
  offline machine never blocks local monitoring or Flask requests. Each
  machine keeps its LAST KNOWN GOOD detail snapshot, clearly labelled with
  last_seen, so the UI can still show stale-but-real data.
* Remote control is an EXPLICIT operator capability: a machine configured
  with allow_control=False (the default) can only ever be monitored. Control
  requests are proxied by app.py and refused before any HTTP is sent.
* Local Windows monitoring works with zero fleet machines configured.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid

POLL_INTERVAL = 10.0
POLL_TIMEOUT = 5.0
DISCOVERY_PORT = 51015
DISCOVERY_INTERVAL = 15.0
PEER_TTL = 45.0  # forget LAN peers not heard from for this long


def default_poster(url, payload, timeout):
    from ai import default_poster as _p
    return _p(url, payload, timeout)


class FleetManager:
    def __init__(self, config_path=None, fetch=None, post=None,
                 self_port=None, machine_label=None,
                 poll_interval=POLL_INTERVAL, poll_timeout=POLL_TIMEOUT,
                 discovery_enabled=True, discovery_port=DISCOVERY_PORT,
                 discovery_interval=DISCOVERY_INTERVAL,
                 clock=time.monotonic, sleeper=time.sleep):
        self._config_path = config_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fleet.json")
        self._fetch = fetch
        self._post = post or default_poster
        self._self_port = self_port
        self._machine_label = machine_label
        self._poll_interval = float(poll_interval)
        self._poll_timeout = float(poll_timeout)
        self._discovery_enabled = bool(discovery_enabled)
        self._discovery_port = int(discovery_port)
        self._discovery_interval = float(discovery_interval)
        self._clock = clock
        self._sleeper = sleeper
        self._instance_uuid = uuid.uuid4().hex

        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._poll_thread = None
        self._disco_thread = None
        self._machines = []          # config entries
        self._status = {}            # machine id -> last known state + detail
        self._peers = {}             # ip -> discovered peer info
        self._poll_error = None
        self._disco_error = None
        self._next_poll = 0.0
        self._disco_sock = None

        self._load_config()

    # -- config ---------------------------------------------------------------
    def _load_config(self):
        try:
            with open(self._config_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                self._machines = [m for m in data
                                  if isinstance(m, dict) and m.get("id")]
        except (OSError, ValueError):
            self._machines = []

    def _save_config(self):
        tmp = self._config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._machines, fh, indent=2)
        os.replace(tmp, self._config_path)

    def list_machines(self):
        with self._lock:
            return [dict(m) for m in self._machines]

    def save_machine(self, entry):
        entry = dict(entry or {})
        name = (entry.get("name") or "").strip()
        host = (entry.get("host") or "").strip()
        try:
            port = int(entry.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not name:
            return False, "name is required"
        if not host:
            return False, "host is required (hostname or IP — no fixed-IP assumption)"
        if not 1 <= port <= 65535:
            return False, "port must be between 1 and 65535"
        mid = entry.get("id") or uuid.uuid4().hex[:12]
        record = {"id": mid, "name": name, "host": host, "port": port,
                  "note": (entry.get("note") or "").strip(),
                  "allow_control": bool(entry.get("allow_control", False)),
                  "auto_discovered": bool(entry.get("auto_discovered", False))}
        with self._lock:
            replaced = False
            for i, m in enumerate(self._machines):
                if m.get("id") == mid:
                    record["allow_control"] = bool(
                        entry.get("allow_control",
                                  m.get("allow_control", False)))
                    self._machines[i] = record
                    replaced = True
                    break
            if not replaced:
                self._machines.append(record)
            try:
                self._save_config()
            except OSError as exc:
                return False, f"failed to save config: {exc}"
        self._next_poll = 0.0  # poll immediately
        return True, ("updated" if replaced else "added")

    def set_control(self, machine_id, enabled):
        """Explicitly enable/disable remote control for one machine."""
        with self._lock:
            for m in self._machines:
                if m.get("id") == machine_id:
                    m["allow_control"] = bool(enabled)
                    try:
                        self._save_config()
                    except OSError as exc:
                        return False, f"failed to save config: {exc}"
                    return True, ("remote control "
                                  f"{'enabled' if enabled else 'disabled'} "
                                  f"for {m.get('name')}")
        return False, "unknown machine id"

    def delete_machine(self, machine_id):
        with self._lock:
            before = len(self._machines)
            self._machines = [m for m in self._machines if m.get("id") != machine_id]
            removed = len(self._machines) < before
            self._status.pop(machine_id, None)
            if removed:
                try:
                    self._save_config()
                except OSError as exc:
                    return False, f"removed in memory but save failed: {exc}"
        return removed, ("deleted" if removed else "unknown machine id")

    # -- remote control proxy ---------------------------------------------------
    def control_request(self, machine_id, remote_path, payload=None,
                        timeout=12.0, method: str = "POST"):
        """Forward a management action to a peer. Refused BEFORE any network
        activity unless the operator enabled allow_control for that machine.
        remote_path must be one of the whitelisted /api/ai/... actions."""
        method = (method or "POST").upper()
        allowed_suffixes = ("/start", "/stop", "/restart", "/load", "/unload",
                            "/adopt", "/discovery/refresh", "/models", "/logs")
        if not any(remote_path.endswith(sfx) for sfx in allowed_suffixes):
            return 403, {"ok": False, "message": "action not whitelisted"}
        # Read-only model/log listing may use GET; mutating actions stay POST.
        if method == "GET" and not remote_path.endswith(("/models", "/logs")):
            return 403, {"ok": False, "message": "GET not allowed for this action"}
        if method != "GET" and method != "POST":
            return 405, {"ok": False, "message": "method not allowed"}
        with self._lock:
            m = next((x for x in self._machines if x.get("id") == machine_id),
                     None)
        if not m:
            return 404, {"ok": False, "message": "unknown machine id"}
        if not m.get("allow_control"):
            return 403, {"ok": False,
                         "message": (f"remote control is disabled for "
                                     f"'{m.get('name')}' — enable it in the "
                                     f"machine's configuration first")}
        url = f"http://{m['host']}:{m['port']}{remote_path}"
        if method == "GET":
            fetch = self._fetch or self._default_fetch
            status, body, error = fetch(url, timeout)
        else:
            status, body, error = self._post(url, payload or {}, timeout)
        if status is None:
            return 502, {"ok": False,
                         "message": f"peer unreachable: {error}"}
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            data = {"ok": False,
                    "message": f"peer returned unparsable body (HTTP {status})"}
        return status, data

    # -- polling --------------------------------------------------------------
    def start(self):
        if self._poll_thread is not None:
            return
        self._poll_thread = threading.Thread(target=self._loop,
                                             name="fleet-poll", daemon=True)
        self._poll_thread.start()
        if self._discovery_enabled:
            self._disco_thread = threading.Thread(
                target=self._discovery_loop, name="fleet-discover",
                daemon=True)
            self._disco_thread.start()

    def stop(self):
        self._stop_evt.set()
        try:
            if self._disco_sock:
                self._disco_sock.close()
        except OSError:
            pass

    @property
    def alive(self):
        return bool(self._poll_thread and self._poll_thread.is_alive())

    def _loop(self):
        while not self._stop_evt.is_set():
            now = self._clock()
            if now >= self._next_poll:
                self._next_poll = now + self._poll_interval
                try:
                    self._poll_all()
                    with self._lock:
                        self._poll_error = None
                except Exception as exc:
                    with self._lock:
                        self._poll_error = f"{type(exc).__name__}: {exc}"
            self._sleeper(0.5)

    def _default_fetch(self, url, timeout):
        from ai import default_fetcher
        return default_fetcher(url, timeout)

    # keys copied from a peer /metrics response into the cached detail
    _DETAIL_KEYS = ("ts", "meta", "cpu", "memory", "storage", "network",
                    "processes", "gpu", "histories", "history_len",
                    "refresh_seconds", "ai_servers", "app_version")

    def _poll_all(self):
        with self._lock:
            machines = [dict(m) for m in self._machines]
        fetch = self._fetch or self._default_fetch
        for m in machines:
            if self._stop_evt.is_set():
                return
            url = f"http://{m['host']}:{m['port']}/metrics"
            status, body, error = fetch(url, self._poll_timeout)
            entry = {"id": m["id"], "name": m["name"], "host": m["host"],
                     "port": m["port"], "note": m.get("note"),
                     "allow_control": bool(m.get("allow_control")),
                     "auto_discovered": bool(m.get("auto_discovered"))}
            prev = self._status.get(m["id"]) or {}
            if status == 200 and body:
                try:
                    data = json.loads(body)
                    detail = {k: data[k] for k in self._DETAIL_KEYS
                              if k in data}
                    entry.update({
                        "online": True,
                        "last_seen": time.time(),
                        "last_error": None,
                        "detail": detail,
                        "summary": self._summarize(data),
                        "ai": self._summarize_ai(data.get("ai_servers")),
                    })
                except ValueError as exc:
                    entry.update({"online": False,
                                  "last_error": f"bad JSON from remote: {exc}",
                                  "last_seen": prev.get("last_seen"),
                                  "detail": prev.get("detail"),
                                  "summary": prev.get("summary"),
                                  "ai": prev.get("ai")})
            else:
                entry.update({"online": False,
                              "last_error": error or f"HTTP {status}",
                              # keep last-known-good data, clearly labelled
                              "last_seen": prev.get("last_seen"),
                              "detail": prev.get("detail"),
                              "summary": prev.get("summary"),
                              "ai": prev.get("ai")})
            with self._lock:
                self._status[m["id"]] = entry

    @staticmethod
    def _summarize(data):
        meta = data.get("meta") or {}
        io = (data.get("storage") or {}).get("io") or {}
        return {
            "hostname": meta.get("hostname"),
            "os_name": meta.get("os_name"),
            "architecture": meta.get("architecture"),
            "uptime_seconds": meta.get("uptime_seconds"),
            "cpu_percent": (data.get("cpu") or {}).get("percent"),
            "cpu_clock_mhz": (data.get("cpu") or {}).get("clock_mhz"),
            "memory_percent": (data.get("memory") or {}).get("percent"),
            "gpu_status": (data.get("gpu") or {}).get("status"),
            "gpu_percent": ((data.get("gpu") or {}).get("utilization") or {})
            .get("percent"),
            "gpu_available": ((data.get("gpu") or {}).get("utilization") or {})
            .get("available"),
            "gpu_vram_used": ((data.get("gpu") or {}).get("memory") or {})
            .get("dedicated_bytes"),
            "gpu_vram_total": ((data.get("gpu") or {}).get("memory") or {})
            .get("total_adapter_bytes"),
            "net_recv_bps": ((data.get("network") or {}).get("totals") or {})
            .get("recv_bps"),
            "net_sent_bps": ((data.get("network") or {}).get("totals") or {})
            .get("sent_bps"),
            "disk_read_bps": io.get("read_bps"),
            "disk_write_bps": io.get("write_bps"),
        }

    @staticmethod
    def _summarize_ai(ai_section):
        if not isinstance(ai_section, dict):
            return None
        servers = []
        ready = 0
        online = 0
        for s in ai_section.get("servers") or []:
            loaded = s.get("loaded_models") or []
            servers.append({
                "id": s.get("id") or s.get("key"),
                "name": s.get("name"),
                "type": s.get("type"),
                "status": s.get("status"),
                "ready": s.get("ready"),
                "model": (loaded[0].get("name") if loaded
                          else s.get("model_name")),
                "quantization": (loaded[0] or {}).get("quantization")
                if loaded else None,
                "cpu_percent": s.get("cpu_percent"),
                "ram_bytes": s.get("ram_bytes"),
                "vram_bytes": s.get("vram_bytes"),
                "tokens_per_sec": s.get("tokens_per_sec"),
                "active_requests": s.get("active_requests"),
            })
            if s.get("ready"):
                ready += 1
            if s.get("status") in ("online", "busy"):
                online += 1
        return {"online_count": online, "ready_count": ready,
                "servers": servers}

    # -- LAN discovery (UDP broadcast; practical when the network allows it) ---
    def _discovery_loop(self):
        payload = json.dumps({
            "kayock": "fleet-monitor",
            "uuid": self._instance_uuid,
            "http_port": self._self_port,
            "name": self._machine_label or socket.gethostname(),
            "hostname": socket.gethostname(),
        }).encode("utf-8")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass
            sock.settimeout(1.0)
            try:
                sock.bind(("", self._discovery_port))
            except OSError as exc:
                sock.close()
                with self._lock:
                    self._disco_error = (f"LAN discovery disabled — UDP port "
                                         f"{self._discovery_port} unavailable "
                                         f"({exc}). Manual configuration "
                                         f"still works.")
                return
            self._disco_sock = sock
        except OSError as exc:
            with self._lock:
                self._disco_error = f"LAN discovery disabled ({exc})"
            return

        last_send = 0.0
        while not self._stop_evt.is_set():
            now = time.time()
            if now - last_send >= self._discovery_interval:
                last_send = now
                try:
                    sock.sendto(payload, ("255.255.255.255",
                                          self._discovery_port))
                except OSError:
                    try:
                        sock.sendto(payload, ("<broadcast>",
                                              self._discovery_port))
                    except OSError:
                        pass  # firewall/no route — manual config still works
            try:
                data, addr = sock.recvfrom(4096)
                self._handle_announcement(data, addr)
            except socket.timeout:
                pass
            except OSError:
                break  # socket closed by stop()
            self._prune_peers(PEER_TTL)

    def _handle_announcement(self, data, addr):
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError):
            return
        if not isinstance(msg, dict) or msg.get("kayock") != "fleet-monitor":
            return
        if msg.get("uuid") == self._instance_uuid:
            return  # our own announcement reflected back
        ip = addr[0]
        try:
            port = int(msg.get("http_port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not port:
            return
        peer = {"ip": ip, "port": port,
                "name": msg.get("name") or msg.get("hostname") or ip,
                "hostname": msg.get("hostname") or ip,
                "uuid": msg.get("uuid"), "last_seen": time.time()}
        with self._lock:
            configured = {f"{m.get('host')}:{m.get('port')}"
                          for m in self._machines}
            if f"{peer['ip']}:{peer['port']}" in configured:
                # already a configured machine — not a discovered peer anymore
                self._peers.pop(ip, None)
                return
            existing = self._peers.get(ip)
            if existing and existing.get("uuid") == peer["uuid"]:
                peer["last_seen"] = max(existing.get("last_seen") or 0,
                                        peer["last_seen"])
            self._peers[ip] = peer

    def _prune_peers(self, ttl):
        cutoff = time.time() - ttl
        with self._lock:
            stale = [ip for ip, p in self._peers.items()
                     if (p.get("last_seen") or 0) < cutoff]
            for ip in stale:
                self._peers.pop(ip, None)

    def discovered_peers(self):
        with self._lock:
            return sorted((dict(p) for p in self._peers.values()),
                          key=lambda p: p.get("last_seen") or 0,
                          reverse=True)

    def add_discovered_peer(self, ip):
        with self._lock:
            peer = self._peers.get(ip)
        if not peer:
            return False, f"no discovered peer at {ip}"
        return self.save_machine({
            "id": uuid.uuid4().hex[:12],
            "name": peer.get("name") or peer.get("hostname") or ip,
            "host": peer["ip"], "port": peer["port"],
            "note": "auto-discovered on LAN",
            "allow_control": False,          # control stays off until enabled
            "auto_discovered": True,
        })

    def discovery_state(self):
        with self._lock:
            return {"enabled": self._discovery_enabled,
                    "port": self._discovery_port,
                    "error": self._disco_error,
                    "peer_count": len(self._peers)}

    # -- snapshot -------------------------------------------------------------
    def detail(self, machine_id):
        with self._lock:
            s = self._status.get(machine_id)
            if s and s.get("detail"):
                return {"id": machine_id, "name": s.get("name"),
                        "online": s.get("online"),
                        "last_seen": s.get("last_seen"),
                        "detail": s.get("detail")}
        return None

    def snapshot(self):
        with self._lock:
            machines = [dict(m) for m in self._machines]
            status = {k: dict(v) for k, v in self._status.items()}
            err = self._poll_error
        out = []
        inventory = []
        for m in machines:
            s = status.get(m["id"], {})
            ai = s.get("ai")
            out.append({
                "id": m["id"], "name": m["name"], "host": m["host"],
                "port": m["port"], "note": m.get("note"),
                "allow_control": bool(m.get("allow_control")),
                "auto_discovered": bool(m.get("auto_discovered")),
                "online": s.get("online", False),
                "last_seen": s.get("last_seen"),
                "last_error": s.get("last_error"),
                "summary": s.get("summary"),
                "ai": ai,
            })
            for srv in (ai or {}).get("servers") or []:
                inventory.append({
                    "machine_id": m["id"], "machine_name": m["name"],
                    "machine_host": f"{m['host']}:{m['port']}",
                    "allow_control": bool(m.get("allow_control")),
                    **srv,
                })
        return {
            "machines": out,
            "ai_inventory": inventory,
            "poll_interval_seconds": self._poll_interval,
            "poll_error": err,
            "discovery": self.discovery_state(),
            "config_path": self._config_path,
        }
