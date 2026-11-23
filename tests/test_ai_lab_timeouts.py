"""AI Lab timeout tiers: connect / first-token / inactivity / total."""
from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path

import pytest

_AI_LAB = Path(__file__).resolve().parents[1] / "ai-lab" / "chat_proxy.py"
_spec = importlib.util.spec_from_file_location("ai_lab_chat_proxy_to", _AI_LAB)
chat_proxy = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(chat_proxy)


def test_resolve_timeouts_defaults():
    cfg = chat_proxy.resolve_timeouts()
    assert cfg["connect"] == 10.0
    assert cfg["first_token"] == 90.0
    assert cfg["read_inactivity"] == 120.0
    assert cfg["total"] is None
    assert cfg["nonstream_total"] == 90.0


def test_resolve_timeouts_overrides_and_env(monkeypatch):
    monkeypatch.setenv("AI_LAB_FIRST_TOKEN_TIMEOUT", "45")
    monkeypatch.setenv("AI_LAB_TOTAL_TIMEOUT", "none")
    cfg = chat_proxy.resolve_timeouts({"read_inactivity": 30, "connect": 5})
    assert cfg["first_token"] == 45.0
    assert cfg["read_inactivity"] == 30.0
    assert cfg["connect"] == 5.0
    assert cfg["total"] is None


def test_classify_transport_timeout_kinds():
    assert chat_proxy._classify_transport_timeout(
        saw_data=False, timed_out=True, connection_error=True) == "connection"
    assert chat_proxy._classify_transport_timeout(
        saw_data=False, timed_out=True, connection_error=False) == "first_token"
    assert chat_proxy._classify_transport_timeout(
        saw_data=True, timed_out=True, connection_error=False) == "stream_inactivity"


def test_nonstream_timeout_is_total_kind(monkeypatch):
    monkeypatch.setattr(chat_proxy, "list_loaded_models", lambda *a, **k: {
        "ok": True, "engine": "ollama",
        "loaded": [{"id": "m"}], "available": [{"id": "m"}],
    })
    emits = []

    def boom(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(chat_proxy, "_request", boom)
    monkeypatch.setattr(
        chat_proxy, "_emit_diagnostic",
        lambda result, client="foxai-ai-lab": emits.append(dict(result)))

    out = chat_proxy.chat_completion(
        endpoint="http://127.0.0.1:11434", engine="ollama", model="m",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=None, require_loaded=True,
        timeouts={"nonstream_total": 90},
    )
    assert out["ok"] is False
    assert out["timed_out"] is True
    assert out["timeout_kind"] == "total"
    assert len(emits) == 1
    assert emits[0]["timeout_kind"] == "total"


def test_stream_first_token_timeout(monkeypatch):
    emits = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            raise socket.timeout("timed out")

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
    monkeypatch.setattr(
        chat_proxy, "_emit_diagnostic",
        lambda result, client="foxai-ai-lab": emits.append(dict(result)))

    lines = list(chat_proxy.stream_ollama_chat(
        endpoint="http://127.0.0.1:11434", model="m",
        messages=[{"role": "user", "content": "x"}],
        timeouts={"first_token": 2, "read_inactivity": 30},
    ))
    assert len(emits) == 1
    assert emits[0]["timed_out"] is True
    assert emits[0]["timeout_kind"] == "first_token"
    assert emits[0]["partial"] is False
    parsed = [json.loads(x.decode()) for x in lines]
    err = next(p for p in parsed if p.get("timeout_kind") or p.get("error"))
    assert err["timeout_kind"] == "first_token"


def test_stream_inactivity_after_partial(monkeypatch):
    emits = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield (json.dumps({
                "message": {"content": "Hello"}, "done": False,
            }) + "\n").encode()
            raise socket.timeout("timed out")

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
    monkeypatch.setattr(
        chat_proxy, "_emit_diagnostic",
        lambda result, client="foxai-ai-lab": emits.append(dict(result)))

    lines = list(chat_proxy.stream_ollama_chat(
        endpoint="http://127.0.0.1:11434", model="m",
        messages=[{"role": "user", "content": "x"}],
        timeouts={"first_token": 90, "read_inactivity": 5},
    ))
    assert len(emits) == 1
    assert emits[0]["timeout_kind"] == "stream_inactivity"
    assert emits[0]["partial"] is True
    parsed = [json.loads(x.decode()) for x in lines]
    assert any(p.get("message", {}).get("content") == "Hello" for p in parsed
               if isinstance(p.get("message"), dict))
    err = next(p for p in parsed if p.get("timeout_kind") == "stream_inactivity")
    assert err.get("partial") is True
    assert err.get("message") == "Hello"


def test_stream_explicit_total_timeout(monkeypatch):
    emits = []
    started = {"n": 0}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            import time
            # First chunk ok, then pretend wall clock already exceeded via monkeypatch
            yield (json.dumps({
                "message": {"content": "x"}, "done": False,
            }) + "\n").encode()
            # subsequent iterations should hit total check — advance fake clock
            started["n"] += 1
            yield (json.dumps({
                "message": {"content": "y"}, "done": False,
            }) + "\n").encode()

    real_time = chat_proxy.time

    class FakeTime:
        def __init__(self):
            self.t = 1000.0

        def time(self):
            # After first chunk processed, jump past total budget
            if started["n"]:
                return self.t + 100
            return self.t

    ft = FakeTime()
    monkeypatch.setattr(chat_proxy, "time", ft)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
    monkeypatch.setattr(
        chat_proxy, "_emit_diagnostic",
        lambda result, client="foxai-ai-lab": emits.append(dict(result)))

    list(chat_proxy.stream_ollama_chat(
        endpoint="http://127.0.0.1:11434", model="m",
        messages=[{"role": "user", "content": "x"}],
        timeouts={"total": 10, "first_token": 90, "read_inactivity": 120},
    ))
    assert len(emits) == 1
    assert emits[0]["timeout_kind"] == "total"


def test_stream_active_slow_does_not_use_90s_total(monkeypatch):
    """Slow-but-active streams must not die solely on historical 90s total."""
    emits = []
    cfg_seen = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            for i in range(3):
                yield (json.dumps({
                    "message": {"content": f"t{i}"}, "done": i == 2,
                    "done_reason": "stop" if i == 2 else None,
                    "eval_count": i + 1 if i == 2 else None,
                }) + "\n").encode()

    def capture_emit(result, client="foxai-ai-lab"):
        emits.append(dict(result))
        cfg_seen.update((result.get("request") or {}).get("timeouts") or {})

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
    monkeypatch.setattr(chat_proxy, "_emit_diagnostic", capture_emit)

    list(chat_proxy.stream_ollama_chat(
        endpoint="http://127.0.0.1:11434", model="m",
        messages=[{"role": "user", "content": "x"}],
    ))
    assert emits[0]["ok"] is True
    assert emits[0].get("timeout_kind") is None
    # Streaming default: no total wall-clock limit
    assert cfg_seen.get("total") is None
    assert cfg_seen.get("nonstream_total") == 90.0


def test_stream_cancel_emits_once(monkeypatch):
    emits = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield (json.dumps({
                "message": {"content": "partial"}, "done": False,
            }) + "\n").encode()
            yield (json.dumps({
                "message": {"content": " more"}, "done": True,
                "done_reason": "stop",
            }) + "\n").encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
    monkeypatch.setattr(
        chat_proxy, "_emit_diagnostic",
        lambda result, client="foxai-ai-lab": emits.append(dict(result)))

    gen = chat_proxy.stream_ollama_chat(
        endpoint="http://127.0.0.1:11434", model="m",
        messages=[{"role": "user", "content": "x"}],
    )
    first = next(gen)
    assert b"partial" in first
    gen.close()
    assert len(emits) == 1
    assert emits[0]["cancelled"] is True
    assert emits[0]["partial"] is True


def test_diagnostic_store_soft_id_lookup(tmp_path):
    from diagnostics import DiagnosticStore
    store = DiagnosticStore(path=str(tmp_path / "d.json"), maxlen=50)
    rec = store.add({
        "ok": False,
        "id": "a82d2c450a48",
        "request_id": "req-1",
        "model": "deepseek-q8:q8_0",
        "machine": "Ubuntu Portable",
        "timed_out": True,
        "timeout_kind": "total",
        "error": "TimeoutError: timed out",
        "elapsed_ms": 90344,
    })
    assert rec["id"] == "a82d2c450a48"

    exact, meta = store.get("a82d2c450a48")
    assert exact and meta["match"] == "exact_id"

    # User-reported typo a822 vs a82d (and swapped digit)
    soft, meta2 = store.get("a822dc450a48", soft=True)
    assert soft is not None
    assert soft["id"] == "a82d2c450a48"
    assert meta2["match"] == "soft_id"
    assert meta2["distance"] <= 2

    miss, meta3 = store.get("ffffffffffff", soft=True)
    assert miss is None
    assert meta3["match"] is None


def test_diagnostics_record_api_soft_match(seeded_sampler):
    from app import create_app
    app = create_app(sampler=seeded_sampler, start_sampler=False,
                     start_ai=False, start_fleet=False)
    app.config["TESTING"] = True
    with app.test_client() as client:
        r = client.post("/api/diagnostics/ingest", json={
            "ok": False,
            "id": "a82d2c450a48",
            "model": "deepseek-q8:q8_0",
            "machine": "Ubuntu Portable",
            "timed_out": True,
            "timeout_kind": "total",
            "error": "TimeoutError: timed out",
            "elapsed_ms": 90344.0,
            "engine": "ollama",
            "client": "foxai-ai-lab",
            "request_id": "unique-soft-test-1",
        })
        assert r.status_code == 200
        body = r.get_json()
        rid = body["data"]["id"]
        assert body["data"]["stop_reason"]["code"] == "request_timed_out"
        assert body["data"]["stop_reason"]["timeout_kind"] == "total"

        r2 = client.get(f"/api/diagnostics/record/{rid}")
        assert r2.status_code == 200
        data = r2.get_json()["data"]
        assert data["elapsed_ms"] == 90344.0
        assert data["timeout_kind"] == "total"

        typo = list(rid)
        typo[3] = "0" if typo[3] != "0" else "1"
        typo_id = "".join(typo)
        r3 = client.get(f"/api/diagnostics/record/{typo_id}?soft=1")
        assert r3.status_code == 200
        assert r3.get_json()["lookup"]["match"] == "soft_id"
        assert r3.get_json()["data"]["id"] == rid

        r4 = client.get("/api/diagnostics/record/a822dc450a48?soft=1")
        assert r4.status_code == 200
        assert r4.get_json()["data"]["id"] == "a82d2c450a48"
