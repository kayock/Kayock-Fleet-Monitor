"""Three-machine fleet simulation with REAL HTTP servers on localhost, plus
remote-control safety tests (explicit opt-in, refusal before any network
activity, whitelisting).

This runs the complete create_app stack (sampler + AI manager + fleet
poller) in three threads — the same integration the live validation does,
kept in the automated suite.
"""
import json
import threading
import time
import urllib.request

import pytest

from app import create_app
from fleet import FleetManager


def _http_get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"raw": body}


def _http_post(url, payload, timeout=5):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class _Peer:
    def __init__(self, port, label, tmp_path):
        self.port = port
        self.label = label
        self.workdir = tmp_path / label
        self.workdir.mkdir(exist_ok=True)
        import ai as ai_mod
        self.ai = ai_mod.AIManager(
            config_path=str(self.workdir / "ai_servers.json"), self_pid=1,
            fetch=lambda u, t: (200, "{}", None),
            post=lambda u, p, t: (200, "{}", None))
        self.fleet = FleetManager(
            config_path=str(self.workdir / "fleet.json"), self_port=port,
            machine_label=label, poll_interval=2.0, poll_timeout=3.0,
            discovery_enabled=False)
        self.app = create_app(sampler=None, ai_manager=self.ai,
                              fleet_manager=self.fleet, start_sampler=True,
                              start_ai=False, start_fleet=False,
                              self_port=port, machine_label=label)
        self.srv = None
        self.thread = None

    def start(self):
        self.srv = threading.Thread(
            target=self.app.run, kwargs={"host": "127.0.0.1",
                                         "port": self.port, "threaded": True,
                                         "use_reloader": False}, daemon=True)
        self.srv.start()
        self.fleet.start()   # start_fleet=False in create_app — start poller here
        self.ai.start()      # start_ai=False in create_app — start discovery here

    def stop(self):
        pass  # daemon threads die with the test process


@pytest.fixture(scope="module")
def three_machines(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fleet3")
    peers = []
    for port, label in ((5331, "Dell"), (5332, "Station"), (5333, "Portable")):
        peers.append(_Peer(port, label, tmp))
    for p in peers:
        p.start()
    time.sleep(1.5)
    # cross-configure the fleet (monitor-only: allow_control off by default)
    for p in peers:
        for q in peers:
            if q is p:
                continue
            _http_post(f"http://127.0.0.1:{p.port}/api/fleet",
                       {"name": q.label, "host": "127.0.0.1",
                        "port": q.port})
    # give the pollers two cycles
    deadline = time.time() + 20
    while time.time() < deadline:
        _, m = _http_get("http://127.0.0.1:5331/metrics")
        online = [x for x in m["fleet"]["machines"] if x["online"]]
        if len(online) == 2:
            break
        time.sleep(1)
    yield peers
    for p in peers:
        p.fleet.stop()


def test_every_machine_sees_the_other_two(three_machines):
    for p in three_machines:
        _, m = _http_get(f"http://127.0.0.1:{p.port}/metrics")
        machines = m["fleet"]["machines"]
        assert len(machines) == 2
        assert {x["name"] for x in machines} == \
            {q.label for q in three_machines if q is not p}
        assert all(x["online"] for x in machines)
        assert all(x["allow_control"] is False for x in machines)


def test_fleet_detail_endpoint_serves_full_snapshots(three_machines):
    # ask the DELL (5331) for its cached detail of STATION — the id comes
    # from the Dell's own fleet config namespace
    st, j = _http_get("http://127.0.0.1:5331/api/fleet/"
                      + three_machines[0].fleet.list_machines()[0]["id"]
                      + "/detail")
    assert st == 200
    detail = j["data"]["detail"]
    for key in ("meta", "cpu", "memory", "storage", "network", "processes",
                "gpu", "histories", "ai_servers"):
        assert key in detail, f"peer detail missing {key}"
    assert detail["histories"]["t"]  # bounded history present


def test_ai_inventory_is_fleet_wide_and_labelled(three_machines):
    # give the Dell a configured (probe-only) AI server via its API
    _http_post("http://127.0.0.1:5331/api/ai/servers",
               {"name": "dell llama", "type": "llama-server",
                "host": "127.0.0.1", "port": 8080, "exe": "", "args": ""})
    deadline = time.time() + 15
    inv = []
    while time.time() < deadline:
        _, m = _http_get("http://127.0.0.1:5332/metrics")
        inv = [r for r in m["fleet"]["ai_inventory"]
               if r["machine_name"] == "Dell" and r["name"] == "dell llama"]
        if inv:
            break
        time.sleep(1)
    assert inv, "Station must list the Dell's AI server in its inventory"
    row = inv[0]
    assert row["machine_host"] == "127.0.0.1:5331"
    assert row["allow_control"] is False


def test_remote_control_refused_without_explicit_optin(three_machines):
    """Default posture: monitor-only. The proxy must refuse BEFORE any HTTP
    is sent — verified with a spy that fails the test if called."""
    fired = []

    def spy_post(url, payload, timeout):
        fired.append(url)
        return 200, "{}", None

    victim = three_machines[0]   # Dell
    observer = three_machines[1]  # Station
    mid = [x for x in observer.fleet.list_machines()
           if x["name"] == "Dell"][0]["id"]
    original_post = observer.fleet._post
    observer.fleet._post = spy_post
    try:
        status, data = observer.fleet.control_request(
            mid, "/api/ai/servers/x/start", {})
        assert status == 403
        assert "disabled" in data["message"]
        assert fired == []           # nothing was sent — refusal is pre-network

        # whitelisting: non-AI actions are refused too
        status2, _ = observer.fleet.control_request(mid, "/etc/passwd", {})
        assert status2 == 403

        # unknown machine
        status3, _ = observer.fleet.control_request(
            "nope", "/api/ai/servers/x/start", {})
        assert status3 == 404
    finally:
        observer.fleet._post = original_post  # restore for later tests


def test_remote_control_proxies_when_explicitly_enabled(three_machines):
    observer = three_machines[1]  # Station
    victim = three_machines[0]    # Dell
    mid = [x for x in observer.fleet.list_machines()
           if x["name"] == "Dell"][0]["id"]
    # operator explicitly enables remote control for the Dell
    st, j = _http_post(f"http://127.0.0.1:{observer.port}/api/fleet/"
                       f"{mid}/control", {"enabled": True})
    assert st == 200 and j["ok"] is True
    try:
        # a POST that reaches the Dell through the real proxy path
        status, data = observer.fleet.control_request(
            mid, "/api/ai/discovery/refresh", {})
        assert status == 200
        assert data["ok"] is True
        assert "refresh" in data["message"]
    finally:
        _http_post(f"http://127.0.0.1:{observer.port}/api/fleet/"
                   f"{mid}/control", {"enabled": False})
    # disabled again → refused
    status4, data4 = observer.fleet.control_request(
        mid, "/api/ai/discovery/refresh", {})
    assert status4 == 403


def test_peer_death_keeps_last_known_good_and_local_alive(three_machines):
    victim = three_machines[2]   # Portable
    observer = three_machines[0]  # Dell
    victim_port = victim.port
    mid = [x for x in observer.fleet.list_machines()
           if x["name"] == "Portable"][0]["id"]

    # confirm online first
    deadline = time.time() + 15
    while time.time() < deadline:
        _, m = _http_get(f"http://127.0.0.1:{observer.port}/metrics")
        ent = [x for x in m["fleet"]["machines"] if x["id"] == mid][0]
        if ent["online"]:
            break
        time.sleep(1)
    assert ent["online"]

    # "kill" the peer: stop its werkzeug thread by shutting the socket via a
    # new fleet config pointing to a dead port on the OBSERVER side
    observer.fleet.save_machine({"id": mid, "name": "Portable",
                                 "host": "127.0.0.1", "port": 5399})
    deadline = time.time() + 15
    while time.time() < deadline:
        _, m = _http_get(f"http://127.0.0.1:{observer.port}/metrics")
        ent = [x for x in m["fleet"]["machines"] if x["id"] == mid][0]
        if not ent["online"]:
            break
        time.sleep(1)
    assert ent["online"] is False
    assert ent["last_error"]
    # local monitoring on the observer is completely unaffected
    _, m2 = _http_get(f"http://127.0.0.1:{observer.port}/metrics")
    assert m2["cpu"]["percent"] is not None
    assert m2["histories"]["t"]
    # detail route still answers (empty when nothing was cached for 5399)
    st, _j = _http_get(
        f"http://127.0.0.1:{observer.port}/api/fleet/{mid}/detail")
    assert st in (200, 404)


def test_dashboard_page_renders_with_tabs(three_machines):
    with urllib.request.urlopen(
            "http://127.0.0.1:5331/", timeout=5) as r:
        html = r.read().decode()
    assert "Kayock Fleet Monitor" in html
    assert 'id="machine-tabs"' in html
    assert 'id="view-all"' in html
    assert 'id="inventory-tbody"' in html
    assert "All Machines" in html
