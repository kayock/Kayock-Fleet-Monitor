"""Model Control Center unit tests (no live inference / no destructive ops)."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from mcc import (
    ProfileStore,
    attach_profiles_to_cards,
    build_model_cards,
    build_server_inventory,
    capabilities_for,
    classify_pending_for_apply,
    estimate_memory_warning,
    fleet_setup_preview,
    guess_association,
    management_setup_preview,
    management_status,
    merge_startup_args,
    parse_startup_settings,
    preview_apply_profile,
    settings_view,
    stable_model_identity,
    stable_server_identity,
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
    # Unloaded cards must not inherit server-process RSS as model RAM
    assert unloaded["ram_bytes"] is None
    assert unloaded["model_ram_bytes"] is None
    assert unloaded["server_ram_bytes"] == 123
    assert loaded["management_status"]["manageable"] is True
    assert loaded["identity"]["key"]


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
    assert exported["version"] == 2
    store2 = ProfileStore(path=str(tmp_path / "other.json"))
    ok3, msg3, n = store2.import_all(exported, merge=False)
    assert ok3 and n >= 1
    ok4, msg4 = store.delete(p["id"])
    assert ok4


def test_stable_identity_multi_machine():
    a = stable_model_identity(
        machine="Ubuntu Station", machine_id="station",
        server={"id": "s1", "key": "127.0.0.1:11434", "host": "127.0.0.1", "port": 11434},
        model="qwen2.5:3b",
    )
    b = stable_model_identity(
        machine="Ubuntu Portable", machine_id="portable",
        server={"id": "s1", "key": "127.0.0.1:11434", "host": "127.0.0.1", "port": 11434},
        model="qwen2.5:3b",
    )
    assert a["key"] != b["key"]
    assert "qwen2.5:3b" in a["key"]


def test_profile_sync_no_duplicates_and_preserves_friendly(tmp_path):
    store = ProfileStore(path=str(tmp_path / "profiles.json"))
    servers = [{
        "id": "abc", "key": "127.0.0.1:11434", "name": "ollama", "type": "ollama",
        "status": "online", "configured": True,
        "capabilities": {"can_load": True, "can_unload": True,
                         "can_start": True, "can_stop": True, "can_restart": True},
        "loaded_models": [{"name": "saturday-gm-gemmasutra:latest"}],
        "installed_models": ["saturday-gm-gemmasutra:latest", "qwen2.5:3b"],
        "host": "127.0.0.1", "port": 11434, "url": "http://127.0.0.1:11434",
    }]
    cards = build_model_cards(
        machine="Ubuntu Portable", machine_id="port1", allow_control=True,
        is_self=True, servers=servers,
    )
    s1 = store.sync_from_cards(cards)
    assert s1["created"] >= 2
    first = store.list_profiles()
    assert len(first) == s1["created"]
    # Customize friendly name
    pid = first[0]["id"]
    store.patch_user_fields(pid, {"friendly_name": "Saturday Multiverse — Game Master"})
    s2 = store.sync_from_cards(cards)
    assert s2["created"] == 0
    assert len(store.list_profiles()) == len(first)
    p = store.get(pid)
    assert p["friendly_name"] == "Saturday Multiverse — Game Master"
    # Offline: mark unavailable, do not delete
    s3 = store.sync_from_cards([])
    assert s3["unavailable"] >= 1
    assert len(store.list_profiles()) == len(first)
    assert any(x.get("available") is False for x in store.list_profiles())


def test_profile_sync_adopts_legacy_orphan(tmp_path):
    store = ProfileStore(path=str(tmp_path / "profiles.json"))
    # Legacy pre-identity profile with custom request prefs
    store.save({
        "name": "gpt-oss:20b custom",
        "model": "gpt-oss:20b",
        "machine": "Portable",
        "engine": "ollama",
        "request_settings": {"temperature": 1, "top_p": 0.95, "max_output": 2048},
    })
    cards = build_model_cards(
        machine="Portable", machine_id="port1", allow_control=True, is_self=False,
        servers=[{
            "id": "o1", "key": "127.0.0.1:11434", "name": "ollama", "type": "ollama",
            "status": "online", "configured": True,
            "capabilities": {"can_load": True, "can_unload": True,
                             "can_start": True, "can_stop": True, "can_restart": True},
            "loaded_models": [],
            "installed_models": ["gpt-oss:20b"],
            "host": "127.0.0.1", "port": 11434, "url": "http://127.0.0.1:11434",
        }],
    )
    s1 = store.sync_from_cards(cards)
    assert s1["created"] == 0  # adopted orphan instead of creating
    profs = store.list_profiles()
    assert len(profs) == 1
    p = profs[0]
    assert (p.get("identity") or {}).get("key")
    assert p["request_settings"]["temperature"] == 1
    assert p["friendly_name"] == "gpt-oss:20b custom"


def test_profile_sync_merges_orphan_into_existing_identity(tmp_path):
    store = ProfileStore(path=str(tmp_path / "profiles.json"))
    cards = build_model_cards(
        machine="Portable", machine_id="port1", allow_control=True, is_self=False,
        servers=[{
            "id": "o1", "key": "127.0.0.1:11434", "name": "ollama", "type": "ollama",
            "status": "online", "configured": True,
            "capabilities": {"can_load": True, "can_unload": True,
                             "can_start": True, "can_stop": True, "can_restart": True},
            "loaded_models": [],
            "installed_models": ["gpt-oss:20b"],
            "host": "127.0.0.1", "port": 11434, "url": "http://127.0.0.1:11434",
        }],
    )
    store.sync_from_cards(cards)
    # Simulate leftover legacy orphan alongside inventory profile
    store.save({
        "name": "gpt-oss:20b custom",
        "model": "gpt-oss:20b",
        "machine": "Portable",
        "engine": "ollama",
        "request_settings": {"temperature": 1, "top_p": 0.95},
        "identity": {},
    })
    # Clear identity that save may have left empty — force orphan
    with store._lock:
        for p in store._profiles:
            if p.get("name") == "gpt-oss:20b custom":
                p["identity"] = {}
        store._save()
    s2 = store.sync_from_cards(cards)
    assert s2["created"] == 0
    inv = [p for p in store.list_profiles() if (p.get("identity") or {}).get("key")]
    assert len(inv) == 1
    assert inv[0]["request_settings"]["temperature"] == 1
    assert inv[0]["friendly_name"] == "gpt-oss:20b custom"
    # Orphan kept, not deleted
    orphans = [p for p in store.list_profiles() if not (p.get("identity") or {}).get("key")]
    assert len(orphans) == 1
    assert orphans[0].get("available") is False


def test_guess_association_unverified_only():
    a = guess_association("saturday-gm-gemmasutra:latest")
    assert a and a["status"] == "possible_unverified"
    assert "unverified" in a["label"].lower()
    assert guess_association("qwen2.5:3b") is None


def test_management_status_and_setup():
    ms = management_status(
        configured=False, is_self=True, allow_control=True,
        caps=capabilities_for("ollama"),
        server_caps={"can_load": True}, server_status="online",
    )
    assert ms["code"] == "setup_required"
    assert ms["needs_setup"] is True
    # Unconfigured remote still setup_required (not remote_control first)
    remote_unreg = management_status(
        configured=False, is_self=False, allow_control=False,
        caps=capabilities_for("ollama"),
        server_caps={}, server_status="online",
    )
    assert remote_unreg["code"] == "setup_required"
    remote = management_status(
        configured=True, is_self=False, allow_control=False,
        caps=capabilities_for("ollama"),
        server_caps={"can_load": True, "can_unload": True},
        server_status="online",
    )
    assert remote["code"] == "remote_control_disabled"
    assert remote["needs_setup"] is False
    card = {
        "model": "phi", "machine": "Windows", "engine": "ollama",
        "configured": False, "is_self": True, "allow_control": True,
        "server_key": "127.0.0.1:11434",
        "management_status": ms,
        "capabilities": capabilities_for("ollama"),
    }
    preview = management_setup_preview(card)
    assert preview["applies_changes"] is False
    assert any(a["action"] == "register_server" for a in preview["proposed_actions"])


def test_merge_startup_args_preserves_unknown():
    new, notes = merge_startup_args(
        '-m /models/qwen.gguf -c 40960 --foo bar -ngl 0',
        {"context": 65536, "gpu_layers": 10},
        "llama-server",
    )
    assert "--foo" in new and "bar" in new
    assert "/models/qwen.gguf" in new
    assert "65536" in new
    assert "10" in new
    assert notes


def test_classify_pending_request_vs_restart():
    c = classify_pending_for_apply(
        "llama-server",
        {"temperature": 0.5, "context": 8192, "gpu_layers": 20},
    )
    assert "temperature" in c["request_preferences"]
    assert "context" in c["startup_settings"]
    assert c["restart_required"] is True


def test_attach_profiles_and_protected_patch(tmp_path):
    store = ProfileStore(path=str(tmp_path / "p.json"))
    cards = build_model_cards(
        machine="Station", machine_id=None, allow_control=True, is_self=True,
        servers=[{
            "id": "x", "key": "127.0.0.1:11434", "name": "o", "type": "ollama",
            "status": "online", "configured": True,
            "capabilities": {"can_load": True, "can_unload": True,
                             "can_start": True, "can_stop": True, "can_restart": True},
            "loaded_models": [{"name": "m1"}],
            "installed_models": ["m1"],
            "host": "127.0.0.1", "port": 11434,
        }],
    )
    store.sync_from_cards(cards)
    profs = store.list_profiles()
    store.patch_user_fields(profs[0]["id"], {
        "protected": True, "tags": ["Application Models", "Creative"],
    })
    attached = attach_profiles_to_cards(cards, store.list_profiles())
    assert attached[0]["protected"] is True
    assert "Creative" in attached[0]["tags"]
    assert attached[0]["friendly_name"]


def test_settings_apply_supported_and_restart_preview(tmp_path, monkeypatch):
    from app import create_app
    from sampler import SystemSampler
    from ai import AIManager
    from fleet import FleetManager

    cfg = tmp_path / "ai_servers.json"
    cfg.write_text(json.dumps([{
        "id": "ls1", "name": "llama", "type": "llama-server",
        "host": "127.0.0.1", "port": 11435,
        "exe": "/bin/true", "args": "-m /m.gguf -c 4096 -ngl 0 --keep-extra",
        "model_dir": "",
    }]), encoding="utf-8")

    real_init = ProfileStore.__init__

    def _init(self, path=None):
        real_init(self, path=str(tmp_path / "model_profiles.json"))

    monkeypatch.setattr(ProfileStore, "__init__", _init)

    app = create_app(
        sampler=SystemSampler(),
        ai_manager=AIManager(config_path=str(cfg)),
        fleet_manager=FleetManager(config_path=str(tmp_path / "fleet.json"),
                                   discovery_enabled=False),
        start_sampler=False, start_ai=False, start_fleet=False,
        self_port=5000, machine_label="test-station",
    )
    client = app.test_client()

    r = client.post("/api/mcc/settings/apply", json={
        "mode": "supported",
        "engine": "llama-server",
        "configured_args": "-m /m.gguf -c 4096 -ngl 0 --keep-extra",
        "pending": {"temperature": 0.2, "context": 8192},
        "model": "kayock-local",
        "machine": "test-station",
        "server_id": "ls1",
        "is_self": True,
        "identity": {"key": "test-station|127.0.0.1:11435|kayock-local"},
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    data = body["data"]
    assert any(x["parameter"] == "temperature" for x in data["saved"])
    assert any(x["parameter"] == "context" for x in data["pending"])

    # Preview with_restart without confirm — must not mutate args
    r2 = client.post("/api/mcc/settings/apply", json={
        "mode": "with_restart",
        "engine": "llama-server",
        "configured_args": "-m /m.gguf -c 4096 -ngl 0 --keep-extra",
        "pending": {"context": 8192, "gpu_layers": 5},
        "model": "kayock-local",
        "server_id": "ls1",
        "is_self": True,
        "confirm_restart": False,
        "do_restart": False,
        "identity": {"key": "test-station|127.0.0.1:11435|kayock-local"},
    })
    assert r2.status_code == 200
    d2 = r2.get_json()["data"]
    assert d2.get("needs_confirm") is True
    assert "--keep-extra" in (d2.get("new_args") or "")
    # File unchanged
    saved_cfg = json.loads(cfg.read_text(encoding="utf-8"))
    assert "-c 4096" in saved_cfg[0]["args"]

    # Apply args without restart
    r3 = client.post("/api/mcc/settings/apply", json={
        "mode": "with_restart",
        "engine": "llama-server",
        "pending": {"context": 8192, "gpu_layers": 5},
        "model": "kayock-local",
        "server_id": "ls1",
        "is_self": True,
        "confirm_restart": True,
        "do_restart": False,
        "identity": {"key": "test-station|127.0.0.1:11435|kayock-local"},
    })
    assert r3.status_code == 200
    saved_cfg = json.loads(cfg.read_text(encoding="utf-8"))
    assert "8192" in saved_cfg[0]["args"]
    assert "--keep-extra" in saved_cfg[0]["args"]

    r4 = client.post("/api/mcc/profiles/sync", json={})
    assert r4.status_code == 200
    assert "summary" in r4.get_json()["data"]

    r5 = client.post("/api/mcc/setup/preview", json={
        "model": "x", "machine": "test", "engine": "ollama",
        "configured": False, "is_self": True, "allow_control": True,
        "server_key": "127.0.0.1:11434",
        "management_status": {"code": "setup_required", "manageable": False},
        "capabilities": capabilities_for("ollama"),
    })
    assert r5.status_code == 200
    assert r5.get_json()["data"]["applies_changes"] is False


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


def _ollama_server(**kw):
    base = {
        "id": None, "key": "127.0.0.1:11434", "name": "ollama", "type": "ollama",
        "status": "online", "configured": False,
        "capabilities": {"can_load": False, "can_unload": False,
                         "can_start": False, "can_stop": False, "can_restart": False},
        "loaded_models": [],
        "installed_models": ["m1", "m2", "m3"],
        "host": "127.0.0.1", "port": 11434, "url": "http://127.0.0.1:11434",
    }
    base.update(kw)
    return base


def test_stable_server_identity_and_inventory_dedupe():
    a = stable_server_identity(
        machine="Station", machine_id="st",
        server={"id": None, "key": "127.0.0.1:11434", "host": "127.0.0.1",
                "port": 11434, "type": "ollama"},
    )
    b = stable_server_identity(
        machine="Portable", machine_id="pt",
        server={"id": None, "key": "127.0.0.1:11434", "host": "127.0.0.1",
                "port": 11434, "type": "ollama"},
    )
    assert a["key"] != b["key"]
    cards = build_model_cards(
        machine="Station", machine_id=None, allow_control=True, is_self=True,
        servers=[_ollama_server()],
    )
    inv = build_server_inventory(cards)
    assert len(inv) == 1
    assert inv[0]["model_count"] == 3
    assert inv[0]["eligible_for_registration"] is True
    assert inv[0]["setup_required"] is True


def test_fleet_setup_preview_groups_models_not_per_model_servers():
    local = build_model_cards(
        machine="Station", machine_id=None, allow_control=True, is_self=True,
        servers=[_ollama_server()],
    )
    peer = build_model_cards(
        machine="Portable", machine_id="port1", allow_control=True, is_self=False,
        servers=[_ollama_server()],
    )
    win = build_model_cards(
        machine="Windows", machine_id="win1", allow_control=True, is_self=False,
        servers=[{
            "id": "ls1", "key": "127.0.0.1:8080", "name": "llama", "type": "llama-server",
            "status": "online", "configured": True, "ready": True,
            "capabilities": {"can_load": False, "can_unload": False,
                             "can_start": True, "can_stop": True, "can_restart": True},
            "model_name": "qwen", "loaded_models": [{"name": "qwen"}],
            "host": "127.0.0.1", "port": 8080,
        }],
    )
    preview = fleet_setup_preview(local + peer + win)
    assert preview["applies_changes"] is False
    assert preview["summary"]["servers_discovered"] == 3
    assert preview["summary"]["servers_registered"] == 1
    assert preview["summary"]["servers_eligible"] == 2
    assert len(preview["proposed_registrations"]) == 2
    assert preview["summary"]["models_discovered"] >= 7
    assert all(p["action"] == "register_server" for p in preview["proposed_registrations"])


def test_adopt_server_idempotent_preserves_config(tmp_path):
    from ai import AIManager
    cfg = tmp_path / "ai_servers.json"
    mgr = AIManager(config_path=str(cfg), discovery_interval=9999)
    with mgr._lock:
        mgr._records = [{
            "key": "127.0.0.1:11434", "host": "127.0.0.1", "port": 11434,
            "type": "ollama", "name": "ollama", "status": "online",
        }]
    with mgr._lock:
        mgr._records[0]["exe"] = "/usr/bin/ssh"  # discovery noise must not grant process control
    ok, msg, warnings = mgr.adopt_server("127.0.0.1:11434")
    assert ok, msg
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert len(saved) == 1
    assert saved[0].get("exe") == ""
    saved[0]["args"] = "--keep-me"
    saved[0]["exe"] = "/usr/bin/ollama"
    cfg.write_text(json.dumps(saved), encoding="utf-8")
    mgr._load_config()
    with mgr._lock:
        mgr._records = [{
            "key": "127.0.0.1:11434", "host": "127.0.0.1", "port": 11434,
            "type": "ollama", "name": "ollama", "status": "online",
        }]
    ok2, msg2, _ = mgr.adopt_server("127.0.0.1:11434")
    assert ok2 and "already" in msg2
    saved2 = json.loads(cfg.read_text(encoding="utf-8"))
    assert len(saved2) == 1
    assert saved2[0]["args"] == "--keep-me"
    assert saved2[0]["exe"] == "/usr/bin/ollama"


def test_fleet_setup_apply_local_and_remote_permission(tmp_path, monkeypatch):
    from app import create_app
    from sampler import SystemSampler
    from ai import AIManager
    from fleet import FleetManager

    real_init = ProfileStore.__init__

    def _init(self, path=None):
        real_init(self, path=str(tmp_path / "model_profiles.json"))

    monkeypatch.setattr(ProfileStore, "__init__", _init)

    ai = AIManager(config_path=str(tmp_path / "ai_servers.json"),
                   discovery_interval=9999)
    with ai._lock:
        ai._records = [{
            "key": "127.0.0.1:11434", "host": "127.0.0.1", "port": 11434,
            "type": "ollama", "name": "ollama", "status": "online",
            "installed_models": ["m1"], "loaded_models": [],
        }]

    calls = []

    def fake_fetch(url, timeout):
        calls.append(("GET", url))
        if url.endswith("/metrics"):
            return 200, json.dumps({
                "ai_servers": {
                    "servers": [{
                        "key": "127.0.0.1:11434", "host": "127.0.0.1", "port": 11434,
                        "type": "ollama", "status": "online", "configured": True,
                        "id": "remote1", "installed_models": ["m1"],
                        "capabilities": {"can_load": True, "can_unload": True},
                    }],
                    "config": [{
                        "id": "remote1", "host": "127.0.0.1", "port": 11434,
                        "type": "ollama", "name": "ollama", "args": "", "exe": "",
                    }],
                }
            }), None
        return 200, "{}", None

    def fake_post(url, payload, timeout):
        calls.append(("POST", url, payload))
        if url.endswith("/adopt"):
            return 200, json.dumps({"ok": True, "message": "added",
                                    "data": {"warnings": []}}), None
        if url.endswith("/discovery/refresh"):
            return 200, json.dumps({"ok": True}), None
        return 200, json.dumps({"ok": True}), None

    fm = FleetManager(
        config_path=str(tmp_path / "fleet.json"),
        fetch=fake_fetch, post=fake_post, discovery_enabled=False,
    )
    fm.save_machine({
        "name": "Portable", "host": "10.0.0.45", "port": 5001,
        "allow_control": True,
    })
    fm.save_machine({
        "name": "Locked", "host": "10.0.0.99", "port": 5001,
        "allow_control": False,
    })
    mid = [m for m in fm.list_machines() if m["name"] == "Portable"][0]["id"]
    locked = [m for m in fm.list_machines() if m["name"] == "Locked"][0]["id"]
    with fm._lock:
        fm._status[mid] = {
            "id": mid, "name": "Portable", "online": True, "allow_control": True,
            "detail": {"ai_servers": {
                "servers": [{
                    "key": "127.0.0.1:11434", "host": "127.0.0.1", "port": 11434,
                    "type": "ollama", "status": "online", "configured": False,
                    "installed_models": ["peer-m1"], "capabilities": {},
                }],
                "config": [],
            }},
        }
        fm._status[locked] = {
            "id": locked, "name": "Locked", "online": True, "allow_control": False,
            "detail": {"ai_servers": {
                "servers": [{
                    "key": "127.0.0.1:11434", "host": "127.0.0.1", "port": 11434,
                    "type": "ollama", "status": "online", "configured": False,
                    "installed_models": ["x"], "capabilities": {},
                }],
                "config": [],
            }},
        }

    app = create_app(
        sampler=SystemSampler(), ai_manager=ai, fleet_manager=fm,
        start_sampler=False, start_ai=False, start_fleet=False,
        self_port=5000, machine_label="Station",
    )
    client = app.test_client()

    prev = client.get("/api/mcc/fleet-setup/preview")
    assert prev.status_code == 200
    pdata = prev.get_json()["data"]
    assert pdata["summary"]["servers_discovered"] >= 2
    eligible = [s for s in pdata["servers"] if s["eligible_for_registration"]]
    assert any(s["is_self"] for s in eligible)
    assert any(s["machine"] == "Portable" for s in eligible)
    assert all(
        (s["machine"] != "Locked") or (not s["eligible_for_registration"])
        for s in pdata["servers"])

    bad = client.post("/api/mcc/fleet-setup/apply",
                      json={"servers": [eligible[0]["id"]]})
    assert bad.status_code == 400

    local = next(s for s in eligible if s["is_self"])
    remote = next(s for s in eligible if s["machine"] == "Portable")
    applied = client.post("/api/mcc/fleet-setup/apply", json={
        "confirm": True,
        "servers": [local["id"], remote["id"]],
    })
    body = applied.get_json()
    assert body["data"]["inference_changed"] is False
    assert any(r["ok"] for r in body["data"]["results"])
    local_cfg = json.loads((tmp_path / "ai_servers.json").read_text(encoding="utf-8"))
    assert any(int(c.get("port") or 0) == 11434 for c in local_cfg)
    assert any(c[0] == "POST" and "/adopt" in c[1] for c in calls)

    locked_srv = next(s for s in pdata["servers"] if s["machine"] == "Locked")
    denied = client.post("/api/mcc/fleet-setup/apply", json={
        "confirm": True, "servers": [locked_srv["id"]],
    })
    assert denied.get_json()["data"]["results"][0]["ok"] is False

    with ai._lock:
        ai._records[0]["installed_models"] = ["m1", "m2"]
    cards = build_model_cards(
        machine="Station", machine_id=None, allow_control=True, is_self=True,
        servers=ai.snapshot()["servers"],
    )
    assert any(c.get("configured") for c in cards)
    assert any((c.get("management_status") or {}).get("manageable")
               for c in cards if c.get("model"))
