"""Model Control Center unit tests (no live inference / no destructive ops)."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from mcc import (
    ProfileStore,
    build_model_cards,
    capabilities_for,
    estimate_memory_warning,
    parse_startup_settings,
    preview_apply_profile,
    settings_view,
)


def test_capabilities_ollama_vs_llama():
    o = capabilities_for("ollama")
    l = capabilities_for("llama-server")
    assert o["can_load"] is True
    assert o["can_unload"] is True
    assert l["can_load"] is False
    assert l["can_unload"] is False
    assert l["model_switch"] == "restart_with_model"
    assert o["parameters"]["temperature"]["apply"] == "per_request"
    assert l["parameters"]["context"]["apply"] == "restart"


def test_capabilities_openai_no_fake_load():
    c = capabilities_for("openai")
    assert c["can_load"] is False
    assert c["can_unload"] is False
    assert c["model_switch"] == "unsupported"


def test_parse_startup_settings_llama_args():
    cfg = parse_startup_settings(
        "llama-server",
        '-m /models/qwen.gguf -c 40960 -ngl 10 -t 8 --parallel 1',
    )
    assert cfg["context"] == 40960
    assert cfg["gpu_layers"] == 10
    assert cfg["threads"] == 8
    assert cfg["parallel"] == 1
    assert cfg["model_path"].endswith("qwen.gguf")


def test_settings_view_pending_vs_active():
    view = settings_view(
        engine="llama-server",
        configured_args="-c 40960 -ngl 0",
        live_context=40960,
        live_model="kayock-local",
        pending={"context": 65536, "gpu_layers": 10},
    )
    assert view["active"]["context"] == 40960
    assert view["configured"]["context"] == 40960
    assert view["restart_required"] is True
    params = {c["parameter"]: c for c in view["changes"]}
    assert params["context"]["current"] == 40960
    assert params["context"]["requested"] == 65536
    assert params["gpu_layers"]["current"] == 0
    assert "Unknown" in str(params["gpu_layers"]["current"]) or params["gpu_layers"]["current"] == 0


def test_settings_view_unknown_active_gpu_layers():
    view = settings_view(
        engine="llama-server",
        configured_args="-c 8192",
        live_context=None,
        pending={"gpu_layers": 20},
    )
    ch = view["changes"][0]
    assert ch["parameter"] == "gpu_layers"
    assert "Unknown" in str(ch["current"])


def test_memory_warning_estimate_not_guaranteed():
    w = estimate_memory_warning(
        model_size_bytes=9 * 1024**3,
        available_ram_bytes=10 * 1024**3,
        context_tokens=8192,
    )
    assert w["estimate_label"].startswith("estimate")
    assert "warn" in w
    # ~1 GiB headroom after estimate → warn should trip (<15% / <1 GiB rule).
    assert w["warn"] is True


def test_memory_warning_no_invented_when_unknown():
    w = estimate_memory_warning(
        model_size_bytes=None,
        available_ram_bytes=None,
    )
    assert w["warn"] is False
    assert w["reason"] == "insufficient measurements"


def test_build_model_cards_loaded_and_unloaded():
    servers = [{
        "id": "abc",
        "key": "127.0.0.1:11434",
        "name": "ollama",
        "type": "ollama",
        "status": "online",
        "configured": True,
        "capabilities": {
            "can_load": True, "can_unload": True,
            "can_start": True, "can_stop": True, "can_restart": True,
        },
        "loaded_models": [{"name": "qwen2.5:3b", "size_bytes": 1000}],
        "installed_models": ["qwen2.5:3b", "llama3.2:3b", "nomic-embed-text"],
        "host": "127.0.0.1",
        "port": 11434,
        "url": "http://127.0.0.1:11434",
        "ram_bytes": 123,
        "active_requests": None,
    }]
    cards = build_model_cards(
        machine="Ubuntu Station", machine_id=None, allow_control=True,
        is_self=True, servers=servers,
        memory={"available": 8 * 1024**3, "used": 4 * 1024**3, "total": 16 * 1024**3},
    )
    names = {c["model"] for c in cards}
    assert "qwen2.5:3b" in names
    assert "llama3.2:3b" in names
    assert "nomic-embed-text" not in names  # embeddings excluded
    loaded = [c for c in cards if c["model"] == "qwen2.5:3b"][0]
    unloaded = [c for c in cards if c["model"] == "llama3.2:3b"][0]
    assert loaded["actions"]["unload"] is True
    assert loaded["actions"]["load"] is False
    assert unloaded["actions"]["load"] is True


def test_build_model_cards_llama_no_unload_action():
    servers = [{
        "id": "ls1",
        "name": "llama",
        "type": "llama-server",
        "status": "online",
        "ready": True,
        "configured": True,
        "capabilities": {
            "can_load": False, "can_unload": False,
            "can_start": True, "can_stop": True, "can_restart": True,
        },
        "model_name": "kayock-local",
        "context_tokens": 65536,
        "loaded_models": [{"name": "kayock-local"}],
        "host": "127.0.0.1",
        "port": 11435,
    }]
    cards = build_model_cards(
        machine="Ubuntu Station", machine_id=None, allow_control=True,
        is_self=True, servers=servers,
    )
    c = cards[0]
    assert c["actions"]["unload"] is False
    assert c["actions"]["switch_via_restart"] is True
    assert "Unload" in c["disabled_reasons"]["unload"] or "resident" in c["disabled_reasons"]["unload"]


def test_build_model_cards_remote_without_allow_control():
    servers = [{
        "id": "o1", "name": "ollama", "type": "ollama", "status": "online",
        "configured": True,
        "capabilities": {"can_load": True, "can_unload": True,
                         "can_start": True, "can_stop": True, "can_restart": True},
        "loaded_models": [{"name": "phi"}],
        "installed_models": ["phi"],
    }]
    cards = build_model_cards(
        machine="Windows", machine_id="win1", allow_control=False,
        is_self=False, servers=servers,
    )
    assert cards[0]["actions"]["unload"] is False
    assert "remote" in cards[0]["disabled_reasons"]


def test_profile_store_crud_and_preview(tmp_path):
    store = ProfileStore(path=str(tmp_path / "model_profiles.json"))
    ok, msg, p = store.save({
        "name": "Balanced local",
        "engine": "llama-server",
        "model": "kayock-local",
        "settings": {"context": 65536, "temperature": 0.7},
    })
    assert ok and p["id"]
    listed = store.list_profiles()
    assert len(listed) == 1
    ok2, msg2, dup = store.duplicate(p["id"], "Balanced copy")
    assert ok2 and dup["name"] == "Balanced copy"
    view = preview_apply_profile(
        p, engine="llama-server", configured_args="-c 40960",
        live_context=40960, live_model="kayock-local",
    )
    assert view["applied"] is False
    assert view["restart_required"] is True
    assert any(c["parameter"] == "context" for c in view["changes"])
    exported = store.export_all()
    assert exported["version"] == 1
    store2 = ProfileStore(path=str(tmp_path / "other.json"))
    ok3, msg3, n = store2.import_all(exported, merge=False)
    assert ok3 and n >= 1
    ok4, msg4 = store.delete(p["id"])
    assert ok4


def test_mcc_api_capabilities_and_profiles(tmp_path, monkeypatch):
    from app import create_app
    from sampler import SystemSampler
    from ai import AIManager
    from fleet import FleetManager

    # Isolate profile path used by create_app's ProfileStore — patch after import
    # by constructing app and hitting capabilities (profiles use default path;
    # use preview endpoints that don't need disk isolation for capabilities).
    app = create_app(
        sampler=SystemSampler(),
        ai_manager=AIManager(config_path=str(tmp_path / "ai_servers.json")),
        fleet_manager=FleetManager(config_path=str(tmp_path / "fleet.json"),
                                   discovery_enabled=False),
        start_sampler=False, start_ai=False, start_fleet=False,
        self_port=5000, machine_label="test-station",
    )
    client = app.test_client()
    r = client.get("/api/mcc/capabilities?engine=ollama")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["data"]["can_load"] is True

    r2 = client.post("/api/mcc/settings/preview", json={
        "engine": "llama-server",
        "configured_args": "-c 8192 -ngl 0",
        "live_context": 8192,
        "pending": {"context": 16384},
    })
    assert r2.status_code == 200
    data = r2.get_json()["data"]
    assert data["restart_required"] is True

    r3 = client.post("/api/mcc/memory-warning", json={
        "model_size_bytes": 1000,
        "available_ram_bytes": 10_000_000_000,
    })
    assert r3.status_code == 200
    assert "estimate_label" in r3.get_json()["data"]

    r4 = client.get("/api/mcc/inventory")
    assert r4.status_code == 200
    assert r4.get_json()["ok"] is True
    assert "cards" in r4.get_json()["data"]


def test_fleet_control_get_models_whitelist(tmp_path):
    from fleet import FleetManager
    calls = []

    def fake_fetch(url, timeout):
        calls.append(("GET", url))
        return 200, json.dumps({"ok": True, "data": [{"name": "a"}]}), None

    def fake_post(url, payload, timeout):
        calls.append(("POST", url))
        return 200, json.dumps({"ok": True}), None

    fm = FleetManager(
        config_path=str(tmp_path / "fleet.json"),
        fetch=fake_fetch, post=fake_post,
        discovery_enabled=False,
    )
    ok, _ = fm.save_machine({
        "name": "Peer", "host": "10.0.0.45", "port": 5001,
        "allow_control": True,
    })
    assert ok
    mid = fm.list_machines()[0]["id"]

    status, data = fm.control_request(
        mid, "/api/ai/servers/abc/models", None, method="GET")
    assert status == 200
    assert calls and calls[0][0] == "GET"

    # Without allow_control
    fm2 = FleetManager(
        config_path=str(tmp_path / "fleet2.json"),
        fetch=fake_fetch, post=fake_post,
        discovery_enabled=False,
    )
    fm2.save_machine({
        "name": "Peer2", "host": "10.0.0.45", "port": 5001,
        "allow_control": False,
    })
    mid2 = fm2.list_machines()[0]["id"]
    status2, data2 = fm2.control_request(
        mid2, "/api/ai/servers/abc/load", {"model": "x"}, method="POST")
    assert status2 == 403
    assert "disabled" in data2["message"]
