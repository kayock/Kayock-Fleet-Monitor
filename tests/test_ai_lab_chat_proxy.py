"""AI Lab chat_proxy: max-token defaults, streaming diagnostics, privacy."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_AI_LAB = Path(__file__).resolve().parents[1] / "ai-lab" / "chat_proxy.py"
_spec = importlib.util.spec_from_file_location("ai_lab_chat_proxy", _AI_LAB)
chat_proxy = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(chat_proxy)


def test_normalize_max_tokens_default():
    assert chat_proxy.normalize_max_tokens(None) == (None, None)
    assert chat_proxy.normalize_max_tokens("") == (None, None)
    assert chat_proxy.normalize_max_tokens("default") == (None, None)
    assert chat_proxy.normalize_max_tokens("auto") == (None, None)


def test_normalize_max_tokens_custom_and_invalid():
    assert chat_proxy.normalize_max_tokens(512) == (512, None)
    assert chat_proxy.normalize_max_tokens("128") == (128, None)
    n, err = chat_proxy.normalize_max_tokens(0)
    assert n is None and err
    n, err = chat_proxy.normalize_max_tokens(-5)
    assert n is None and err
    n, err = chat_proxy.normalize_max_tokens("abc")
    assert n is None and err


def test_request_meta_omits_num_predict_for_default():
    meta = chat_proxy._request_meta(
        max_tokens=None, think="disabled", stream=False, family="ollama")
    assert meta["max_tokens_mode"] == "model_default"
    assert meta["max_tokens"] is None
    assert "num_predict" not in meta

    meta2 = chat_proxy._request_meta(
        max_tokens=96, think="disabled", stream=False, family="ollama")
    assert meta2["max_tokens_mode"] == "custom"
    assert meta2["num_predict"] == 96


def test_ollama_payload_omits_num_predict_when_default(monkeypatch):
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({
                "message": {"content": "hi"},
                "done_reason": "stop",
                "eval_count": 3,
                "prompt_eval_count": 2,
                "eval_duration": 1_000_000_000,
            }).encode()

    def fake_request(method, url, payload=None, timeout=60.0, stream=False):
        captured["payload"] = payload
        return FakeResp()

    monkeypatch.setattr(chat_proxy, "_request", fake_request)
    monkeypatch.setattr(chat_proxy, "_emit_diagnostic", lambda *a, **k: None)
    monkeypatch.setattr(chat_proxy, "list_loaded_models", lambda *a, **k: {
        "ok": True, "engine": "ollama",
        "loaded": [{"id": "m"}], "available": [{"id": "m"}],
    })

    out = chat_proxy.chat_completion(
        endpoint="http://127.0.0.1:11434", engine="ollama", model="m",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=None, require_loaded=True,
    )
    assert out["ok"] is True
    assert "options" not in captured["payload"]
    assert out["request"]["max_tokens_mode"] == "model_default"
    assert out["done_reason"] == "stop"


def test_ollama_sends_num_predict_when_custom(monkeypatch):
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({
                "message": {"content": "hi"},
                "done_reason": "length",
                "eval_count": 96,
            }).encode()

    def fake_request(method, url, payload=None, timeout=60.0, stream=False):
        captured["payload"] = payload
        return FakeResp()

    monkeypatch.setattr(chat_proxy, "_request", fake_request)
    monkeypatch.setattr(chat_proxy, "_emit_diagnostic", lambda *a, **k: None)
    monkeypatch.setattr(chat_proxy, "list_loaded_models", lambda *a, **k: {
        "ok": True, "engine": "ollama",
        "loaded": [{"id": "m"}], "available": [{"id": "m"}],
    })

    out = chat_proxy.chat_completion(
        endpoint="http://127.0.0.1:11434", engine="ollama", model="m",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=96, require_loaded=True,
    )
    assert captured["payload"]["options"]["num_predict"] == 96
    assert out["request"]["num_predict"] == 96
    assert out["done_reason"] == "length"


def test_openai_omits_max_tokens_when_default(monkeypatch):
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            }).encode()

    def fake_request(method, url, payload=None, timeout=60.0, stream=False):
        captured["payload"] = payload
        return FakeResp()

    monkeypatch.setattr(chat_proxy, "_request", fake_request)
    monkeypatch.setattr(chat_proxy, "_emit_diagnostic", lambda *a, **k: None)
    monkeypatch.setattr(chat_proxy, "list_loaded_models", lambda *a, **k: {
        "ok": True, "engine": "openai-compatible",
        "loaded": [{"id": "m"}], "available": [{"id": "m"}],
    })

    out = chat_proxy.chat_completion(
        endpoint="http://127.0.0.1:8080", engine="llama-server", model="m",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=None, require_loaded=True,
    )
    assert "max_tokens" not in captured["payload"]
    assert out["finish_reason"] == "stop"
    assert out["request"]["max_tokens_mode"] == "model_default"


def test_streaming_ollama_emits_once(monkeypatch):
    emits = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            chunks = [
                {"message": {"content": "a"}, "done": False},
                {"message": {"content": "b"}, "done": True,
                 "done_reason": "stop", "eval_count": 2,
                 "prompt_eval_count": 1, "eval_duration": 500_000_000},
            ]
            for c in chunks:
                yield (json.dumps(c) + "\n").encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
    monkeypatch.setattr(chat_proxy, "_emit_diagnostic",
                        lambda result, client="foxai-ai-lab": emits.append(dict(result)))

    lines = list(chat_proxy.stream_ollama_chat(
        endpoint="http://127.0.0.1:11434", model="m",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=None,
    ))
    assert len(lines) == 2
    assert len(emits) == 1
    assert emits[0]["stream_complete"] is True
    assert emits[0]["done_reason"] == "stop"
    assert emits[0]["completion_tokens"] == 2
    assert emits[0]["request"]["max_tokens_mode"] == "model_default"
    assert emits[0].get("request_id")


def test_streaming_openai_interrupted_without_finish(monkeypatch):
    emits = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n'
            # ends without finish_reason / [DONE]

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
    monkeypatch.setattr(chat_proxy, "_emit_diagnostic",
                        lambda result, client="foxai-ai-lab": emits.append(dict(result)))

    list(chat_proxy.stream_openai_chat(
        endpoint="http://127.0.0.1:8080", model="m",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=32,
    ))
    assert len(emits) == 1
    assert emits[0]["stream_complete"] is False
    assert emits[0]["request"]["max_tokens"] == 32


def test_emit_strips_conversation_content(monkeypatch):
    posted = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return json.dumps({"ok": True, "data": {"id": "abc123",
                                                    "stop_reason": {"code": "completed_normally"},
                                                    "findings": []}}).encode()

    def fake_urlopen(req, timeout=1.5):
        posted["body"] = json.loads(req.data.decode())
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.delenv("FLEET_DIAGNOSTICS_DISABLE", raising=False)
    monkeypatch.setenv("FLEET_DIAGNOSTICS_URL", "http://127.0.0.1:5000/api/diagnostics/ingest")

    result = {
        "ok": True,
        "model": "m",
        "engine": "ollama",
        "endpoint": "http://127.0.0.1:11434",
        "content": "SECRET",
        "request": {"max_tokens": None, "messages": [{"content": "nope"}]},
        "request_id": "rid1",
        "completion_tokens": 5,
        "stream_complete": True,
    }
    chat_proxy._emit_diagnostic(result)
    assert "content" not in posted["body"]
    assert "messages" not in posted["body"].get("request", {})
    assert result["diagnostic_id"] == "abc123"


def test_store_dedupes_request_id(tmp_path):
    from diagnostics import DiagnosticStore
    store = DiagnosticStore(path=str(tmp_path / "d.json"), maxlen=20)
    a = store.add({"ok": True, "request_id": "same", "model": "m",
                   "completion_tokens": 1, "done_reason": "stop"})
    b = store.add({"ok": True, "request_id": "same", "model": "m",
                   "completion_tokens": 99, "done_reason": "length"})
    assert a["id"] == b["id"]
    assert len(store.list(limit=20)) == 1
    assert store.list(limit=1)[0]["completion_tokens"] == 1
