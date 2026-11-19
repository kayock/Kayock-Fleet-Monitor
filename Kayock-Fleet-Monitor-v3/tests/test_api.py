"""Flask API tests against the real app with a fake-backed sampler."""
import json

import pytest

from app import create_app
from conftest import FakeGPU
from sampler import SystemSampler


@pytest.fixture
def client(seeded_sampler):
    app = create_app(sampler=seeded_sampler, start_sampler=False)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, seeded_sampler


EXPECTED_TOP_KEYS = {
    "ts", "meta", "cpu", "memory", "storage", "network", "processes",
    "gpu", "histories", "refresh_seconds", "history_len", "sampler",
}

EXPECTED_HIST_KEYS = {"t", "cpu", "mem", "net_recv_bps", "net_sent_bps",
                      "disk_read_bps", "disk_write_bps", "gpu_util"}

EXPECTED_GPU_KEYS = {"status", "reason", "platform", "adapters",
                     "utilization", "memory", "temperature", "nvidia",
                     "collected_at"}


def test_metrics_schema_matches_ui_expectations(client):
    c, _ = client
    resp = c.get("/metrics")
    assert resp.status_code == 200
    assert resp.content_type.startswith("application/json")
    data = resp.get_json()
    assert EXPECTED_TOP_KEYS <= set(data)

    assert {"hostname", "os_system", "os_name", "os_release", "os_version",
            "os_build", "architecture", "processor", "python_version",
            "python_implementation", "boot_time",
            "uptime_seconds"} <= set(data["meta"])

    assert {"percent", "per_core", "count_logical", "count_physical",
            "clock_mhz", "clock_source", "clock_note"} <= set(data["cpu"])

    assert {"total", "used", "available", "percent", "swap_total",
            "swap_used", "swap_percent"} <= set(data["memory"])

    assert {"volumes", "volumes_error", "io"} <= set(data["storage"])
    assert {"read_bps", "write_bps", "read_iops", "write_iops",
            "total_read_bytes", "total_write_bytes", "per_disk",
            "available", "elapsed_seconds", "reason"} <= set(data["storage"]["io"])

    assert {"adapters", "totals", "total_bytes_sent", "total_bytes_recv",
            "available", "reason"} <= set(data["network"])
    for adapter in data["network"]["adapters"]:
        assert {"name", "bytes_sent", "bytes_recv", "sent_bps", "recv_bps",
                "is_loopback"} <= set(adapter)

    assert {"list", "total", "error", "note"} <= set(data["processes"])
    for row in data["processes"]["list"]:
        assert {"pid", "name", "cpu_percent", "memory_percent",
                "rss"} <= set(row)

    assert EXPECTED_GPU_KEYS <= set(data["gpu"])
    assert EXPECTED_HIST_KEYS <= set(data["histories"])
    lengths = {len(v) for v in data["histories"].values()}
    assert lengths == {len(data["histories"]["t"])}


def test_metrics_is_valid_json_with_no_nan(client):
    c, _ = client
    body = c.get("/metrics").get_data(as_text=True)
    parsed = json.loads(body)  # would raise on NaN/Infinity tokens
    assert parsed["ts"] > 0


def test_metrics_uses_cached_snapshot_no_resampling(client, fake_psutil):
    c, sampler = client
    calls_before = dict(fake_psutil.calls)
    net_baseline = tuple(sampler._prev_net["Ethernet"])

    first = c.get("/metrics").get_json()
    fake_psutil.advance_net(sent=5_000_000, recv=5_000_000)
    fake_psutil.advance_disk(read_bytes=5_000_000, write_bytes=5_000_000)
    second = c.get("/metrics").get_json()

    assert first == second                     # cached payload, unchanged
    assert fake_psutil.calls == calls_before   # GET sampled nothing
    assert sampler._prev_net["Ethernet"] == net_baseline  # baselines intact


def test_index_page_injects_constants_and_local_assets(client):
    c, sampler = client
    resp = c.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert f"refreshMs: {int(sampler.refresh_seconds * 1000)}" in html
    assert f"historyLen: {sampler.history_len}" in html
    assert "/static/js/plotly.min.js" in html
    assert "/static/js/app.js" in html
    assert "/static/css/style.css" in html


def test_metrics_cache_headers(client):
    c, _ = client
    resp = c.get("/metrics")
    assert "no-store" in resp.headers.get("Cache-Control", "")


def test_healthz_reports_sampler_state(client):
    c, sampler = client
    resp = c.get("/healthz")
    # sampler not started (start_sampler=False) -> honest 503 + details
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["ok"] is False
    assert body["refresh_seconds"] == 2.0
    assert "samples" in body and "last_sample_age_seconds" in body


def test_healthz_ok_with_running_sampler(fake_psutil, fake_clock):
    class AliveSampler(SystemSampler):
        @property
        def alive(self):  # simulate a running thread without spawning one
            return True

    s = AliveSampler(gpu_collector=FakeGPU(), psutil_module=fake_psutil,
                     clock=fake_clock, sleeper=lambda _s: None)
    try:
        app = create_app(sampler=s, start_sampler=False)
        client = app.test_client()
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
    finally:
        s.stop()


# ---------------------------------------------------------------------------
# v2: AI servers + fleet sections and management routes
# ---------------------------------------------------------------------------
def _ai_manager(tmp_path):
    from ai import AIManager
    return AIManager(config_path=str(tmp_path / "ai_servers.json"),
                     self_pid=99999,
                     fetch=lambda url, t: (200, "{}", None),
                     post=lambda url, payload, t: (200, "{}", None),
                     psutil_module=None, clock=lambda: 0.0,
                     sleeper=lambda s: None)


def test_metrics_includes_ai_and_fleet_sections(seeded_sampler, tmp_path):
    from fleet import FleetManager
    ai_mgr = _ai_manager(tmp_path)
    fleet_mgr = FleetManager(config_path=str(tmp_path / "fleet.json"),
                             fetch=lambda u, t: (200, "{}", None),
                             clock=lambda: 0.0, sleeper=lambda s: None)
    app = create_app(sampler=seeded_sampler, ai_manager=ai_mgr,
                     fleet_manager=fleet_mgr, start_sampler=False,
                     start_ai=False, start_fleet=False)
    client = app.test_client()
    data = client.get("/metrics").get_json()
    assert "ai_servers" in data
    assert set(data["ai_servers"]) >= {"servers", "config", "ops",
                                       "discovery", "config_path"}
    assert "fleet" in data
    assert set(data["fleet"]) >= {"machines", "poll_interval_seconds",
                                  "ai_inventory", "discovery"}
    assert data["instance"]["app_version"]
    assert data["instance"]["hostname"]
    health = client.get("/healthz").get_json()
    assert health["version"]
    assert "ai_discovery_alive" in health and "fleet_alive" in health


def test_ai_config_routes_roundtrip(tmp_path):
    ai_mgr = _ai_manager(tmp_path)
    app = create_app(ai_manager=ai_mgr, start_sampler=False, start_ai=False,
                     start_fleet=False)
    client = app.test_client()

    resp = client.post("/api/ai/servers", json={
        "name": "llama main", "type": "llama-server", "host": "127.0.0.1",
        "port": 8080, "exe": "", "args": "-c 4096"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    listed = client.get("/api/ai/config").get_json()["data"]
    assert len(listed) == 1 and listed[0]["port"] == 8080

    sid = listed[0]["id"]
    bad = client.post("/api/ai/servers", json={"name": "x", "port": 0})
    assert bad.status_code == 400

    deleted = client.delete(f"/api/ai/servers/{sid}")
    assert deleted.status_code == 200
    assert client.get("/api/ai/config").get_json()["data"] == []


def test_ai_stop_busy_returns_409(tmp_path):
    from ai import AIManager
    from conftest import FakeGPU  # noqa: F401 (module import sanity)

    class StubProc:
        def __init__(self, pid):
            self._pid = pid

        @property
        def pid(self):
            return self._pid

        def cpu_percent(self, interval=None):
            return 0.0

        def memory_info(self):
            class M:
                rss = 1
            return M()

        def create_time(self):
            return 0.0

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    class StubPsutil:
        CONN_LISTEN = "LISTEN"

        def process_iter(self, attrs=None, ad_value=None):
            return []

        def net_connections(self, kind="inet"):
            return []

        def Process(self, pid):
            return StubProc(pid)

    exe = tmp_path / "server.exe"
    exe.write_bytes(b"MZ")
    ai_mgr = AIManager(config_path=str(tmp_path / "cfg.json"), self_pid=1,
                       fetch=lambda u, t: (200, "{}", None),
                       post=lambda u, p, t: (200, "{}", None),
                       psutil_module=StubPsutil(), clock=lambda: 0.0,
                       sleeper=lambda s: None)
    ok, _, _ = ai_mgr.save_server_config({"name": "s", "type": "llama-server",
                                          "port": 8080, "exe": str(exe)})
    assert ok
    sid = ai_mgr.list_config()[0]["id"]
    ai_mgr.start_server(sid)
    ai_mgr._records = [{"id": sid, "active_requests": 3}]

    app = create_app(ai_manager=ai_mgr, start_sampler=False, start_ai=False,
                     start_fleet=False)
    client = app.test_client()
    resp = client.post(f"/api/ai/servers/{sid}/stop", json={})
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False
    forced = client.post(f"/api/ai/servers/{sid}/stop", json={"force": True})
    assert forced.status_code == 200


def test_ai_discovery_refresh_route(seeded_sampler, tmp_path):
    ai_mgr = _ai_manager(tmp_path)
    app = create_app(sampler=seeded_sampler, ai_manager=ai_mgr,
                     start_sampler=False, start_ai=False, start_fleet=False)
    client = app.test_client()
    resp = client.post("/api/ai/discovery/refresh")
    assert resp.status_code == 200
    assert ai_mgr._refresh_requested is True


def test_fleet_routes_roundtrip(tmp_path):
    from fleet import FleetManager
    fleet_mgr = FleetManager(config_path=str(tmp_path / "fleet.json"),
                             fetch=lambda u, t: (200, "{}", None),
                             clock=lambda: 0.0, sleeper=lambda s: None)
    app = create_app(ai_manager=_ai_manager(tmp_path), fleet_manager=fleet_mgr,
                     start_sampler=False, start_ai=False, start_fleet=False)
    client = app.test_client()
    resp = client.post("/api/fleet", json={
        "name": "Linux Portable", "host": "linux-portable.local", "port": 5000})
    assert resp.status_code == 200
    machines = client.get("/api/fleet").get_json()["data"]
    assert len(machines) == 1
    bad = client.post("/api/fleet", json={"name": "", "host": "", "port": 0})
    assert bad.status_code == 400
    mid = machines[0]["id"]
    assert client.delete(f"/api/fleet/{mid}").status_code == 200
    assert client.get("/api/fleet").get_json()["data"] == []


def test_unknown_route_404(client):
    c, _ = client
    assert c.get("/nope").status_code == 404


def test_metrics_history_values_are_json_safe(seeded_sampler):
    """histories must be plain lists of numbers/None — never deque."""
    snap = seeded_sampler.snapshot()
    from collections import deque
    for key, values in snap["histories"].items():
        assert isinstance(values, list), key
        assert not isinstance(values, deque), key
        assert all(v is None or isinstance(v, (int, float)) for v in values), key
