"""Fleet manager: config CRUD, polling with injected fetch, last-known-good
behavior, and strict independence from the local machine."""
import json

from fleet import FleetManager


def make_fleet(tmp_path, fetch=None):
    return FleetManager(config_path=str(tmp_path / "fleet.json"),
                        fetch=fetch, poll_interval=15.0, poll_timeout=1.0,
                        clock=lambda: 0.0, sleeper=lambda s: None)


REMOTE_METRICS = {
    "meta": {"hostname": "linux-portable", "os_name": "Linux 6.8",
             "architecture": "x86_64", "uptime_seconds": 98765},
    "cpu": {"percent": 22.5},
    "memory": {"percent": 41.0},
    "ai_servers": {"servers": [
        {"name": "ollama", "status": "online", "ready": True},
        {"name": "llama", "status": "offline", "ready": False},
    ]},
}


def test_machine_config_validation(tmp_path):
    f = make_fleet(tmp_path)
    assert not f.save_machine({"name": "", "host": "x", "port": 1})[0]
    assert not f.save_machine({"name": "m", "host": "", "port": 1})[0]
    assert not f.save_machine({"name": "m", "host": "x", "port": 99999})[0]
    ok, msg = f.save_machine({"name": "Linux Portable",
                              "host": "linux-portable.local", "port": 5000,
                              "note": "runs the same monitor"})
    assert ok
    ok2, _ = f.save_machine({"name": "Ubuntu Station", "host": "192.168.1.50",
                             "port": 5001})
    assert ok2
    assert len(f.list_machines()) == 2
    # persistence
    assert len(make_fleet(tmp_path).list_machines()) == 2


def test_poll_collects_identity_and_ai_readiness(tmp_path):
    def fetch(url, timeout):
        assert url == "http://linux-portable.local:5000/metrics"
        return 200, json.dumps(REMOTE_METRICS), None

    f = make_fleet(tmp_path, fetch=fetch)
    f.save_machine({"name": "Linux Portable", "host": "linux-portable.local",
                    "port": 5000})
    f._poll_all()
    snap = f.snapshot()
    m = snap["machines"][0]
    assert m["online"] is True
    assert m["summary"]["hostname"] == "linux-portable"
    assert m["summary"]["cpu_percent"] == 22.5
    assert m["summary"]["memory_percent"] == 41.0
    # full detail is cached for the per-machine detailed view
    det = f.detail(m["id"])
    assert det["detail"]["meta"]["uptime_seconds"] == 98765
    assert m["ai"]["ready_count"] == 1
    assert m["ai"]["online_count"] == 1
    assert m["ai"]["servers"][1]["status"] == "offline"
    # fleet-wide AI inventory carries the machine label
    inv = snap["ai_inventory"]
    assert inv[0]["machine_name"] == "Linux Portable"
    assert inv[1]["allow_control"] is False


def test_offline_machine_reports_error_and_keeps_last_known_good(tmp_path):
    responses = {"count": 0}

    def fetch(url, timeout):
        responses["count"] += 1
        if responses["count"] == 1:
            return 200, json.dumps(REMOTE_METRICS), None
        return None, None, "ConnectionRefusedError: 111"

    f = make_fleet(tmp_path, fetch=fetch)
    f.save_machine({"name": "Ubuntu Station", "host": "192.168.1.50",
                    "port": 5001})
    f._poll_all()
    f._poll_all()  # now unreachable
    snap = f.snapshot()
    m = snap["machines"][0]
    assert m["online"] is False
    assert "ConnectionRefused" in (m["last_error"] or "")
    # last-known-good data is retained and clearly labelled by last_seen
    assert m["summary"]["hostname"] == "linux-portable"
    assert m["last_seen"]
    assert f.detail(m["id"])["detail"]["meta"]["uptime_seconds"] == 98765


def test_bad_json_from_remote_is_flagged(tmp_path):
    def fetch(url, timeout):
        return 200, "<html>not json</html>", None

    f = make_fleet(tmp_path, fetch=fetch)
    f.save_machine({"name": "weird", "host": "10.0.0.9", "port": 5000})
    f._poll_all()
    m = f.snapshot()["machines"][0]
    assert m["online"] is False
    assert "bad JSON" in (m["last_error"] or "")


def test_delete_machine(tmp_path):
    f = make_fleet(tmp_path)
    f.save_machine({"name": "m", "host": "h", "port": 5000})
    mid = f.list_machines()[0]["id"]
    ok, _ = f.delete_machine(mid)
    assert ok
    assert f.list_machines() == []
