"""AI Model Server tests: probe parsing, status machine (listening is NOT
health), discovery over fake psutil, config CRUD, and management controls
with injected Popen / psutil fakes — including the busy-inference guard and
port-conflict warnings."""
import threading

import pytest

import ai as ai_mod
from ai import (AIManager, parse_prometheus_metrics, probe_ollama,
                split_launch_args)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeConn:
    def __init__(self, pid, port, status="LISTEN"):
        class L:
            def __init__(self, port):
                self.port = port
        self.pid = pid
        self.laddr = L(port)
        self.status = status


class FakeProcObj:
    """psutil.Process stand-in."""

    def __init__(self, pid, exe=None, rss=100 * 2**20, cpu=12.5, create=1000.0):
        self._pid = pid
        self._exe = exe
        self._rss = rss
        self._cpu = cpu
        self._create = create
        self.terminated = False
        self.killed = False

    def pid(self):  # not used; Process(pid) has .pid attribute
        return self._pid

    @property
    def pid(self):
        return self._pid

    def cpu_percent(self, interval=None):
        return self._cpu

    def memory_info(self):
        class M:
            rss = self._rss
        return M()

    def create_time(self):
        return self._create

    def exe(self):
        return self._exe

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class FakePsutil:
    CONN_LISTEN = "LISTEN"
    # psutil exception surface used by the manager
    NoSuchProcess = ai_mod.psutil.NoSuchProcess
    AccessDenied = ai_mod.psutil.AccessDenied
    ZombieProcess = ai_mod.psutil.ZombieProcess

    def __init__(self, processes=(), conns=()):
        self._procs = {p._pid: p for p in processes}
        self._conns = list(conns)
        self.process_iter_items = [
            {"pid": p._pid, "name": (p._exe or "x.exe").rsplit("/", 1)[-1]
             if p._exe else None,
             "exe": p._exe, "cmdline": None, "create_time": p._create}
            for p in processes
        ]

    def process_iter(self, attrs=None, ad_value=None):
        class P:
            def __init__(self, info):
                self.info = info
        return [P(i) for i in self.process_iter_items]

    def net_connections(self, kind="inet"):
        return list(self._conns)

    def Process(self, pid):
        if pid not in self._procs:
            raise ai_mod.psutil.NoSuchProcess(pid)
        return self._procs[pid]


class FakePopen:
    def __init__(self, pid=4321, running=True):
        self.pid = pid
        self._running = running
        self.stdout = None
        self.stderr = None
        self.args = None

    def poll(self):
        return None if self._running else 0

    def terminate(self):
        self._running = False


def make_manager(tmp_path, fetch=None, post=None, psutil_module=None,
                 popen=None):
    return AIManager(
        config_path=str(tmp_path / "ai_servers.json"),
        self_pid=99999,
        fetch=fetch or (lambda url, timeout: (200, "{}", None)),
        post=post or (lambda url, payload, timeout: (200, "{}", None)),
        psutil_module=psutil_module or FakePsutil(),
        popen=popen,
        clock=lambda: 0.0,
        sleeper=lambda s: None,
    )


# ---------------------------------------------------------------------------
# probe parsing
# ---------------------------------------------------------------------------
OLLAMA_TAGS = {"models": [
    {"name": "llama3.1:8b", "model": "llama3.1:8b", "size": 4_900_000_000,
     "digest": "abc123def456...",
     "details": {"quantization_level": "Q4_K_M", "parameter_size": "8.0B",
                 "family": "llama"}},
]}
OLLAMA_PS = {"models": [
    {"name": "llama3.1:8b", "size": 4_900_000_000, "size_vram": 3_800_000_000},
]}


def test_probe_ollama_parses_models_and_loaded():
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        if url.endswith("/api/tags"):
            return 200, __import__("json").dumps(OLLAMA_TAGS), None
        if url.endswith("/api/ps"):
            return 200, __import__("json").dumps(OLLAMA_PS), None
        if url.endswith("/api/version"):
            return 200, '{"version":"0.5.4"}', None
        return 404, None, "HTTP 404"

    info, err = probe_ollama("http://127.0.0.1:11434", fetch,
                             lambda *a, **k: (200, "{}", None), 2.0)
    assert err is None
    assert info["api"] == "ollama"
    assert info["installed_models"][0]["name"] == "llama3.1:8b"
    assert info["installed_models"][0]["quantization"] == "Q4_K_M"
    assert info["loaded_models"][0]["vram_bytes"] == 3_800_000_000
    assert info["server_version"] == "0.5.4"
    assert info["active_requests"] is None  # honest: not exposed


def test_parse_prometheus_metrics():
    text = "# HELP some metric\nllamacpp:tokens_predicted_total 1234\n" \
           "llamacpp:requests_processing 2\nbroken line\n"
    m = parse_prometheus_metrics(text)
    assert m["llamacpp:tokens_predicted_total"] == 1234.0
    assert m["llamacpp:requests_processing"] == 2.0


def test_split_launch_args_quotes():
    args = split_launch_args('-m "C:\\models\\my model.gguf" -c 4096 --host 0.0.0.0')
    assert args == ["-m", "C:\\models\\my model.gguf", "-c", "4096",
                    "--host", "0.0.0.0"]
    assert split_launch_args("") == []


# ---------------------------------------------------------------------------
# status machine: a listening port is NOT health
# ---------------------------------------------------------------------------
def _target(port, hint, listening=True, pid=4242):
    return {"key": f"127.0.0.1:{port}", "host": "127.0.0.1", "port": port,
            "type_hint": hint, "pid": pid, "name": None, "exe": None,
            "cmdline": None, "create_time": 1000.0, "configured": False,
            "config": None, "port_listening": listening, "process_found": True}


def test_status_online_and_ready_llama(tmp_path):
    def fetch(url, timeout):
        if url.endswith("/props"):
            return 200, '{"model":{"path":"C:/models/m.gguf"},"n_ctx":4096}', None
        if url.endswith("/slots"):
            return 200, '[{"is_processing":false,"prompt":{"n_ctx_tokens":100}}]', None
        if url.endswith("/metrics"):
            return 200, "llamacpp:tokens_predicted_total 500\n", None
        return 200, "{}", None

    mgr = make_manager(tmp_path, fetch=fetch)
    rec = mgr._probe_one(_target(8080, "llama-server"))
    assert rec["status"] == "online"
    assert rec["ready"] is True
    assert rec["health_ok"] is True
    assert rec["model_path"] == "C:/models/m.gguf"
    assert rec["context_tokens"] == 4096
    assert rec["active_requests"] == 0


def test_status_busy_with_active_requests(tmp_path):
    def fetch(url, timeout):
        if url.endswith("/props"):
            return 200, '{"model":{"path":"m.gguf"},"n_ctx":100}', None
        if url.endswith("/slots"):
            return 200, '[{"is_processing":true,"prompt":{"n_ctx_tokens":50}}]', None
        return 200, "{}", None

    mgr = make_manager(tmp_path, fetch=fetch)
    rec = mgr._probe_one(_target(8080, "llama-server"))
    assert rec["status"] == "busy"
    assert rec["active_requests"] == 1
    assert rec["context_utilization"] == pytest.approx(0.5)


def test_status_loading_lm_studio(tmp_path):
    body = '{"data":[{"id":"m1","state":"loading","quantization":"Q4_K_M",' \
           '"max_context_length":4096}]}'

    def fetch(url, timeout):
        if url.endswith("/api/v0/models"):
            return 200, body, None
        return 404, None, "HTTP 404"

    mgr = make_manager(tmp_path, fetch=fetch)
    rec = mgr._probe_one(_target(1234, "lm-studio"))
    assert rec["status"] == "loading"
    assert rec["health_ok"] is True


def test_status_error_on_http_500(tmp_path):
    def fetch(url, timeout):
        if url.endswith("/api/tags"):
            return 500, "boom", "HTTP 500"
        return 500, "boom", "HTTP 500"

    mgr = make_manager(tmp_path, fetch=fetch)
    rec = mgr._probe_one(_target(11434, "ollama"))
    assert rec["status"] == "error"
    assert rec["health_ok"] is False


def test_status_listening_when_api_silent(tmp_path):
    """Port open, HTTP dead: 'listening', never 'online'."""
    def fetch(url, timeout):
        return None, None, "TimeoutError: timed out"

    mgr = make_manager(tmp_path, fetch=fetch)
    rec = mgr._probe_one(_target(8080, "llama-server"))
    assert rec["status"] == "listening"
    assert rec["health_ok"] is False
    assert rec["ready"] is None


def test_status_offline_when_nothing_there(tmp_path):
    def fetch(url, timeout):
        return None, None, "ConnectionRefusedError"

    mgr = make_manager(tmp_path, fetch=fetch)
    t = _target(8080, "llama-server", listening=False, pid=None)
    t["process_found"] = False
    rec = mgr._probe_one(t)
    assert rec["status"] == "offline"


def test_status_online_but_not_ready_ollama(tmp_path):
    """API healthy, zero loaded models: online + ready=False (not health=ready)."""
    def fetch(url, timeout):
        if url.endswith("/api/tags"):
            return 200, __import__("json").dumps(OLLAMA_TAGS), None
        if url.endswith("/api/ps"):
            return 200, '{"models":[]}', None
        return 200, '{"version":"1"}', None

    mgr = make_manager(tmp_path, fetch=fetch)
    rec = mgr._probe_one(_target(11434, "ollama"))
    assert rec["status"] == "online"
    assert rec["ready"] is False


def test_status_generic_openai_empty_server(tmp_path):
    """An OpenAI-compatible endpoint with no models: online, no invented model."""
    def fetch(url, timeout):
        if url.endswith("/v1/models"):
            return 200, '{"data":[]}', None
        return 404, None, "HTTP 404"

    mgr = make_manager(tmp_path, fetch=fetch)
    rec = mgr._probe_one(_target(8000, "vllm"))
    assert rec["status"] == "online"
    assert rec["ready"] is False
    assert rec["api_type"] == "openai-compatible"


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------
def test_discovery_finds_process_and_listening_port(tmp_path):
    exe = "C:/ai/llama-server.exe"
    psutil = FakePsutil(
        processes=[FakeProcObj(4242, exe=exe)],
        conns=[FakeConn(4242, 8080)],
    )

    def fetch(url, timeout):
        if url.endswith("/props"):
            return 200, '{"model":{"path":"m.gguf"},"n_ctx":2048}', None
        return 200, "{}", None

    mgr = make_manager(tmp_path, fetch=fetch, psutil_module=psutil)
    mgr._scan_cycle()
    snap = mgr.snapshot()
    servers = snap["servers"]
    assert len(servers) == 1
    s = servers[0]
    assert s["type"] == "llama-server"
    assert s["pid"] == 4242
    assert s["port"] == 8080
    assert s["configured"] is False
    assert s["capabilities"]["can_start"] is False  # unadopted -> no controls
    assert s["status"] == "online"


def test_discovery_ignores_monitor_own_pid(tmp_path):
    psutil = FakePsutil(processes=[], conns=[FakeConn(99999, 5000)])
    mgr = make_manager(tmp_path, psutil_module=psutil)
    mgr._scan_cycle()
    assert mgr.snapshot()["servers"] == []


def test_discovery_known_port_without_owner(tmp_path):
    psutil = FakePsutil(processes=[], conns=[FakeConn(777, 11434)])

    def fetch(url, timeout):
        if url.endswith("/api/tags"):
            return 200, __import__("json").dumps(OLLAMA_TAGS), None
        if url.endswith("/api/ps"):
            return 200, '{"models":[]}', None
        return 200, "{}", None

    mgr = make_manager(tmp_path, fetch=fetch, psutil_module=psutil)
    mgr._scan_cycle()
    servers = mgr.snapshot()["servers"]
    assert len(servers) == 1
    assert servers[0]["type"] == "ollama"


def test_unreachable_api_never_freezes(tmp_path):
    psutil = FakePsutil(processes=[], conns=[FakeConn(777, 11434)])

    def fetch(url, timeout):
        raise RuntimeError("network catastrophe")

    mgr = make_manager(tmp_path, fetch=fetch, psutil_module=psutil)
    mgr._scan_cycle()  # must not raise
    s = mgr.snapshot()["servers"]
    assert s and s[0]["status"] in ("listening", "error")


# ---------------------------------------------------------------------------
# configuration + controls
# ---------------------------------------------------------------------------
def test_config_crud_and_port_conflict_warning(tmp_path):
    mgr = make_manager(tmp_path)
    ok, msg, warn = mgr.save_server_config(
        {"name": "llama main", "type": "llama-server", "host": "127.0.0.1",
         "port": 8080, "exe": "C:/ai/llama-server.exe", "args": "-c 4096"})
    assert ok
    ok2, _, warn2 = mgr.save_server_config(
        {"name": "dup", "type": "llama-server", "host": "127.0.0.1",
         "port": 8080})
    assert ok2
    assert any("8080" in w for w in warn2)  # duplicate-port warning
    ok3, msg3, _ = mgr.save_server_config({"name": "bad", "port": 99999})
    assert not ok3
    assert len(mgr.list_config()) == 2
    # config survives a new manager instance (persistence)
    mgr2 = make_manager(tmp_path)
    assert len(mgr2.list_config()) == 2


def test_start_stop_managed_server(tmp_path):
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"MZ")  # a real file — start validates existence
    started = {}

    def fake_popen(cmd, **kwargs):
        started["cmd"] = cmd
        return FakePopen(pid=4321, running=True)

    psutil = FakePsutil(processes=[FakeProcObj(4321, exe=str(exe))],
                        conns=[FakeConn(4321, 8080)])
    mgr = make_manager(tmp_path, psutil_module=psutil, popen=fake_popen)
    mgr.save_server_config({"name": "llama", "type": "llama-server",
                            "port": 8080, "exe": str(exe),
                            "args": '-c 4096 --host 127.0.0.1'})
    ok, msg, _ = mgr.start_server(mgr.list_config()[0]["id"])
    assert ok
    assert started["cmd"][0] == str(exe)
    assert "-c" in started["cmd"]
    logs = mgr.server_logs(mgr.list_config()[0]["id"])
    assert logs == []
    ok2, msg2, _ = mgr.stop_server(mgr.list_config()[0]["id"])
    assert ok2


def test_stop_refuses_while_inference_active(tmp_path):
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"MZ")
    psutil = FakePsutil(processes=[FakeProcObj(4321, exe=str(exe))],
                        conns=[FakeConn(4321, 8080)])
    mgr = make_manager(tmp_path, psutil_module=psutil,
                       popen=lambda cmd, **kw: FakePopen(4321))
    mgr.save_server_config({"name": "llama", "type": "llama-server",
                            "port": 8080, "exe": str(exe)})
    sid = mgr.list_config()[0]["id"]
    mgr.start_server(sid)
    mgr._records = [{"id": sid, "active_requests": 2}]  # busy inference
    ok, msg, tag = mgr.stop_server(sid, force=False)
    assert not ok and tag == "busy"
    ok2, msg2, _ = mgr.stop_server(sid, force=True)  # explicit override
    assert ok2


def test_stop_never_kills_mismatched_process(tmp_path):
    """A configured server whose port is owned by a DIFFERENT executable
    must never be terminated (no guessed kills)."""
    psutil = FakePsutil(
        processes=[FakeProcObj(5555, exe="C:/windows/notepad.exe")],
        conns=[FakeConn(5555, 8080)],
    )
    mgr = make_manager(tmp_path, psutil_module=psutil)
    mgr.save_server_config({"name": "llama", "type": "llama-server",
                            "port": 8080, "exe": "C:/ai/llama-server.exe"})
    sid = mgr.list_config()[0]["id"]
    ok, msg, _ = mgr.stop_server(sid)
    assert ok  # nothing matched -> honest "not running", nothing killed
    proc = psutil._procs[5555]
    assert not proc.terminated


def test_load_unsupported_type_reports_safely(tmp_path):
    mgr = make_manager(tmp_path)
    mgr.save_server_config({"name": "llama", "type": "llama-server",
                            "port": 8080, "exe": "C:/ai/llama-server.exe"})
    sid = mgr.list_config()[0]["id"]
    ok, msg, tag = mgr.load_model(sid, "m.gguf")
    assert not ok and tag == "unsupported"


def test_adopt_discovered_server(tmp_path):
    mgr = make_manager(tmp_path)
    mgr._records = [{"key": "127.0.0.1:8080", "name": "llama-server",
                     "type": "llama-server", "host": "127.0.0.1", "port": 8080,
                     "exe": "C:/ai/llama-server.exe"}]
    ok, msg, _ = mgr.adopt_server("127.0.0.1:8080")
    assert ok
    cfg = mgr.list_config()
    assert len(cfg) == 1 and cfg[0]["port"] == 8080


def test_model_files_from_dir(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.gguf").write_bytes(b"x" * 10)
    (tmp_path / "models" / "b.txt").write_bytes(b"x")
    mgr = make_manager(tmp_path)
    mgr.save_server_config({"name": "llama", "type": "llama-server",
                            "port": 8080, "exe": "",
                            "model_dir": str(tmp_path / "models")})
    sid = mgr.list_config()[0]["id"]
    ok, models, _ = mgr.list_model_files(sid)
    assert ok
    names = [m["name"] for m in models]
    assert "a.gguf" in names and "b.txt" not in names


def test_start_refuses_missing_executable(tmp_path):
    mgr = make_manager(tmp_path)
    mgr.save_server_config({"name": "ghost", "type": "llama-server",
                            "port": 8080, "exe": "C:/nope/missing.exe"})
    sid = mgr.list_config()[0]["id"]
    ok, msg, _ = mgr.start_server(sid)
    assert not ok and "not found" in msg


def test_refresh_request_is_thread_safe_flag(tmp_path):
    mgr = make_manager(tmp_path)
    mgr.request_refresh()
    mgr.request_refresh()
    assert mgr._refresh_requested is True  # consumed by the loop thread


def test_discovery_orphan_known_port_without_pid(tmp_path):
    """Docker-published ports often report pid=None — still discover them."""
    psutil = FakePsutil(processes=[], conns=[FakeConn(None, 11434)])

    def fetch(url, timeout):
        if url.endswith("/api/tags"):
            return 200, __import__("json").dumps(OLLAMA_TAGS), None
        if url.endswith("/api/ps"):
            return 200, '{"models":[]}', None
        return 200, "{}", None

    mgr = make_manager(tmp_path, fetch=fetch, psutil_module=psutil)
    mgr._scan_cycle()
    servers = mgr.snapshot()["servers"]
    assert len(servers) == 1
    assert servers[0]["type"] == "ollama"
    assert servers[0]["port"] == 11434
    assert servers[0]["status"] != "error"


def test_probe_skips_missing_port_without_invalid_url(tmp_path):
    """AI processes with no listening port must not build http://host:None."""
    exe = "/usr/bin/ollama"
    psutil = FakePsutil(processes=[FakeProcObj(4242, exe=exe)], conns=[])
    mgr = make_manager(tmp_path, psutil_module=psutil)
    mgr._scan_cycle()
    servers = mgr.snapshot()["servers"]
    assert len(servers) == 1
    assert servers[0]["port"] is None
    assert servers[0].get("url") is None
    assert "InvalidURL" not in (servers[0].get("status_detail") or "")
    assert servers[0]["status"] in ("loading", "offline", "error")
    assert servers[0]["key"] != "unknown"


def test_discovery_dedupes_portless_process_onto_docker_port(tmp_path):
    """Host ollama PID + Docker-published 11434 (pid=None) → one target."""
    exe = "/usr/bin/ollama"
    psutil = FakePsutil(
        processes=[FakeProcObj(4242, exe=exe)],
        conns=[FakeConn(None, 11434)],
    )

    def fetch(url, timeout):
        if url.endswith("/api/tags"):
            return 200, __import__("json").dumps(OLLAMA_TAGS), None
        if url.endswith("/api/ps"):
            return 200, '{"models":[]}', None
        return 200, "{}", None

    mgr = make_manager(tmp_path, fetch=fetch, psutil_module=psutil)
    mgr._scan_cycle()
    servers = mgr.snapshot()["servers"]
    assert len(servers) == 1
    assert servers[0]["port"] == 11434
    assert servers[0]["pid"] == 4242
    assert servers[0]["type"] == "ollama"
    assert not any(str(s.get("key") or "").startswith("proc:") for s in servers)


def test_invalid_configured_port_is_skipped(tmp_path):
    mgr = make_manager(tmp_path)
    ok, msg, warn = mgr.save_server_config(
        {"name": "bad", "type": "ollama", "host": "127.0.0.1", "port": "None"})
    # save may coerce or reject; either way probing must not raise InvalidURL
    mgr._scan_cycle()
    for s in mgr.snapshot()["servers"]:
        assert "InvalidURL" not in (s.get("status_detail") or "")
        assert s.get("url") is None or ":None" not in str(s.get("url"))
