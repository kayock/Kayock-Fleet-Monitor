"""LAN discovery tests with REAL UDP sockets on localhost (loopback
broadcast), plus peer registration and pruning."""
import json
import socket
import time

import pytest

from fleet import DISCOVERY_PORT, FleetManager


def make_fleet(tmp_path, discovery_port, self_port=5999, label="A"):
    return FleetManager(config_path=str(tmp_path / f"fleet-{label}.json"),
                        self_port=self_port, machine_label=label,
                        poll_interval=15.0, poll_timeout=1.0,
                        discovery_enabled=True,
                        discovery_port=discovery_port,
                        clock=lambda: 0.0, sleeper=lambda s: None)


def _announce(port, payload):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(json.dumps(payload).encode(), ("127.0.0.1", port))


def test_peer_announcement_is_discovered(tmp_path):
    mgr = make_fleet(tmp_path, discovery_port=51801, self_port=5001)
    mgr._discovery_loop_body_for_tests = True  # marker: no thread needed
    # simulate the listener loop pieces directly (no threads -> deterministic)
    payload = json.dumps({"kayock": "fleet-monitor", "uuid": "peer-uuid-1",
                          "http_port": 5002, "name": "Ubuntu Station",
                          "hostname": "station"}).encode()
    mgr._handle_announcement(payload, ("192.168.1.50", 51015))
    peers = mgr.discovered_peers()
    assert len(peers) == 1
    p = peers[0]
    assert p["ip"] == "192.168.1.50" and p["port"] == 5002
    assert p["name"] == "Ubuntu Station"

    # adding the peer registers it as a monitor-only machine
    ok, _ = mgr.add_discovered_peer("192.168.1.50")
    assert ok
    machines = mgr.list_machines()
    assert machines[0]["auto_discovered"] is True
    assert machines[0]["allow_control"] is False   # control OFF by default
    # once configured, the same host:port no longer appears as discovered
    mgr._handle_announcement(payload, ("192.168.1.50", 51015))
    assert mgr.discovered_peers() == []


def test_own_announcement_is_ignored(tmp_path):
    mgr = make_fleet(tmp_path, discovery_port=51802, self_port=5001)
    own_uuid = mgr._instance_uuid
    payload = json.dumps({"kayock": "fleet-monitor", "uuid": own_uuid,
                          "http_port": 5001, "name": "A",
                          "hostname": "a"}).encode()
    mgr._handle_announcement(payload, ("127.0.0.1", 51015))
    assert mgr.discovered_peers() == []


def test_foreign_packets_are_ignored(tmp_path):
    mgr = make_fleet(tmp_path, discovery_port=51803, self_port=5001)
    mgr._handle_announcement(b"not json at all", ("10.0.0.9", 51015))
    mgr._handle_announcement(json.dumps({"hello": "world"}).encode(),
                             ("10.0.0.9", 51015))
    assert mgr.discovered_peers() == []


def test_stale_peers_are_pruned(tmp_path):
    mgr = make_fleet(tmp_path, discovery_port=51804, self_port=5001)
    payload = json.dumps({"kayock": "fleet-monitor", "uuid": "u2",
                          "http_port": 5003, "name": "Portable",
                          "hostname": "portable"}).encode()
    mgr._handle_announcement(payload, ("192.168.1.60", 51015))
    assert len(mgr.discovered_peers()) == 1
    # age the peer beyond the TTL
    with mgr._lock:
        mgr._peers["192.168.1.60"]["last_seen"] = time.time() - 3600
    mgr._prune_peers(ttl=45.0)
    assert mgr.discovered_peers() == []


def test_real_udp_loop_two_listeners(tmp_path):
    """Real sockets on one host: two instances share the discovery port via
    SO_REUSEADDR; a manually sent announcement reaches both listeners and
    each records the other."""
    port = 51805
    a = make_fleet(tmp_path, discovery_port=port, self_port=5321, label="A")
    b = make_fleet(tmp_path, discovery_port=port, self_port=5322, label="B")
    a.start()
    b.start()
    try:
        # let both bind and broadcast at least once
        deadline = time.time() + 25
        while time.time() < deadline:
            peers_a = a.discovered_peers()
            peers_b = b.discovered_peers()
            if peers_a and peers_b:
                break
            time.sleep(0.5)
        names_a = {p["name"] for p in a.discovered_peers()}
        names_b = {p["name"] for p in b.discovered_peers()}
        assert "B" in names_a, f"A should discover B, saw {names_a}"
        assert "A" in names_b, f"B should discover A, saw {names_b}"
        # neither discovers itself
        assert "A" not in names_a and "B" not in names_b
        # discovery state is healthy
        assert a.discovery_state()["error"] is None
        assert b.discovery_state()["error"] is None
    finally:
        a.stop()
        b.stop()
