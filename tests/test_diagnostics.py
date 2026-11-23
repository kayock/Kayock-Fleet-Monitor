"""Regression tests for Fleet Monitor v3.3 AI Diagnostics."""
from __future__ import annotations

import pytest

from app import create_app
from diagnostics import (
    DiagnosticStore,
    build_diagnostic_report,
    classify_stop_reason,
    detect_config_conflicts,
    format_report_text,
    inspect_llama_server,
    inspect_model_configuration,
    inspect_ollama_model,
    sanitize_report,
    troubleshoot_record,
)


@pytest.fixture
def client(seeded_sampler):
    app = create_app(sampler=seeded_sampler, start_sampler=False,
                     start_ai=False, start_fleet=False)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_classify_stop_reason_output_limit():
    r = classify_stop_reason(ok=True, done_reason="length",
                             completion_tokens=96, max_output=96)
    assert r["code"] == "output_limit_reached"
    assert r["confidence"] == "confirmed"


def test_classify_stop_reason_heuristic_probable():
    r = classify_stop_reason(ok=True, completion_tokens=96, max_output=96)
    assert r["code"] == "output_limit_reached"
    assert r["confidence"] == "probable"


def test_classify_stop_reason_timeout():
    r = classify_stop_reason(ok=False, timed_out=True, error="timed out")
    assert r["code"] == "request_timed_out"


def test_classify_stop_reason_context():
    r = classify_stop_reason(ok=False, error="context length exceeded")
    assert r["code"] == "context_limit_exceeded"


def test_classify_unknown_without_evidence():
    r = classify_stop_reason(ok=False, error="weird backend failure XYZ")
    assert r["code"] == "unknown"
    assert r["confidence"] == "insufficient_evidence"


def test_unknown_field_label():
    insp = inspect_model_configuration(
        engine="ollama", endpoint="", model="x")
    assert insp["backend"]["errors"]
    assert insp["read_only"] is True
    assert insp["mutated"] is False


def test_ollama_inspector_parses_show_and_ps(monkeypatch):
    show = {
        "details": {"quantization_level": "Q8_0", "parameter_size": "32B",
                    "family": "deepseek"},
        "parameters": "temperature 0.7\nnum_ctx 8192\ntop_p 0.9\n",
        "model_info": {"deepseek.context_length": 8192},
        "capabilities": ["completion", "thinking"],
    }
    ps = {"models": [{
        "name": "deepseek-r1:32b-q8_0",
        "size": 35_000_000_000,
        "size_vram": 20_000_000_000,
        "expires_at": "2026-01-01T00:00:00Z",
    }]}
    version = {"version": "0.12.0"}

    def fake_post(url, payload, timeout=4.0):
        assert "api/show" in url
        return 200, show, None

    def fake_get(url, timeout=4.0):
        if url.endswith("/api/ps"):
            return 200, ps, None
        if url.endswith("/api/version"):
            return 200, version, None
        return None, None, "miss"

    monkeypatch.setattr("diagnostics._post_json", fake_post)
    monkeypatch.setattr("diagnostics._fetch_json", fake_get)
    out = inspect_ollama_model("http://10.0.0.202:11434", "deepseek-r1:32b-q8_0")
    assert out["fields"]["quantization"]["value"] == "Q8_0"
    assert out["fields"]["context_window"]["value"] == 8192
    assert out["fields"]["model_status"]["value"] == "loaded"
    assert out["fields"]["temperature"]["layer"] == "model_default"
    assert out["fields"]["ram_bytes"]["known"] is True


def test_llama_inspector_uses_props_and_args(monkeypatch):
    props = {"default_generation_settings": {"n_ctx": 4096, "n_predict": 512}}

    def fake_get(url, timeout=4.0):
        if url.endswith("/props"):
            return 200, props, None
        if url.endswith("/health"):
            return 200, {"status": "ok"}, None
        return None, None, "miss"

    monkeypatch.setattr("diagnostics._fetch_json", fake_get)
    out = inspect_llama_server(
        "http://10.0.0.104:12434", "model.gguf",
        configured_args="-m /models/x.gguf -c 8192 -ngl 40 -t 8 -np 2 -b 512")
    assert out["fields"]["gpu_layers"]["value"] == 40
    assert out["fields"]["context_window"]["known"] is True
    assert out["fields"]["startup_arguments"]["layer"] == "server_configuration"


def test_profile_not_presented_as_live():
    insp = inspect_model_configuration(
        engine="ollama", endpoint="", model="m",
        profile={"request_settings": {"temperature": 0.2}},
        card={"machine": "Station", "model": "m"},
    )
    assert insp["profile_fields"]["temperature"]["layer"] == "saved_profile"
    assert "temperature" not in (insp["backend"].get("fields") or {})


def test_conflicts_distinguish_override():
    inspection = {
        "overview": {"model": "a"},
        "backend": {"fields": {
            "context_window": {"value": 8192, "known": True},
            "temperature": {"value": 0.7, "known": True},
        }},
        "profile_fields": {
            "thinking": {"value": False, "known": True},
        },
    }
    findings = detect_config_conflicts(
        inspection=inspection,
        request={"context": 2048, "temperature": 0.2, "think": True, "model": "a"},
    )
    kinds = {f["code"]: f["severity"] for f in findings}
    assert kinds["context_override"] == "intentional_override"
    assert kinds["thinking_conflict"] == "potential_conflict"


def test_troubleshoot_output_limit():
    rec = {
        "ok": True,
        "completion_tokens": 96,
        "request": {"max_tokens": 96},
        "stop_reason": {
            "code": "output_limit_reached",
            "confidence": "confirmed",
            "evidence": ["backend reason=length"],
            "raw_done_reason": "length",
        },
    }
    findings = troubleshoot_record(rec)
    assert any("output-token limit" in f["what_happened"] for f in findings)
    assert findings[0]["likely_cause"]


def test_store_privacy_and_bounds(tmp_path):
    store = DiagnosticStore(path=str(tmp_path / "h.json"), maxlen=5)
    for i in range(8):
        store.add({
            "ok": True,
            "model": "m",
            "client": "foxai-ai-lab",
            "completion_tokens": i,
            "request": {"max_tokens": 64, "messages": [{"role": "user", "content": "SECRET"}]},
            "content": "SECRET RESPONSE",
            "prompt": "SECRET PROMPT",
            "done_reason": "length",
        })
    rows = store.list(limit=20)
    assert len(rows) == 5
    assert all("content" not in r for r in rows)
    assert all("prompt" not in r for r in rows)
    assert all("messages" not in (r.get("request") or {}) for r in rows)
    assert rows[0]["stop_reason"]["code"] == "output_limit_reached"
    perf = store.performance_summary(model="m")
    assert perf["requests_completed"] == 5
    assert perf["output_limit_occurrences"] == 5


def test_sanitize_report_strips_secrets_and_home_paths():
    dirty = {
        "api_key": "sk-secret",
        "messages": [{"content": "hi"}],
        "path": "/home/kayock/secret/model.gguf",
        "nested": {"authorization": "Bearer x", "ok": True},
    }
    clean = sanitize_report(dirty)
    assert "api_key" not in clean
    assert "messages" not in clean
    assert "authorization" not in clean["nested"]
    assert "[redacted-path]" in clean["path"]


def test_report_generation():
    inspection = inspect_model_configuration(
        engine="ollama", endpoint="", model="m",
        card={"machine": "Station", "model": "m", "friendly_name": "DeepSeek"},
    )
    recent = [{
        "id": "abc", "ok": True, "client": "foxai-ai-lab",
        "completion_tokens": 96, "elapsed_ms": 100,
        "stop_reason": {"label": "Output Limit Reached", "code": "output_limit_reached"},
        "findings": [{"what_happened": "cap", "evidence": ["96"], "suggested_next_step": "check",
                      "confidence": "probable"}],
    }]
    report = build_diagnostic_report(inspection=inspection, recent=recent,
                                     performance={"requests_completed": 1})
    assert report["privacy"]["prompts_excluded"] is True
    text = format_report_text(report)
    assert "DeepSeek" in text or "m" in text
    assert "Output Limit" in text


def test_api_ingest_and_inspect(client, monkeypatch):
    def fake_show(*a, **k):
        return 200, {
            "details": {"quantization_level": "Q4_K_M"},
            "parameters": "num_ctx 4096\n",
            "model_info": {},
            "capabilities": [],
        }, None

    def fake_get(url, timeout=4.0):
        if "version" in url:
            return 200, {"version": "0.12.0"}, None
        if "ps" in url:
            return 200, {"models": []}, None
        return None, None, "skip"

    monkeypatch.setattr("diagnostics._post_json", fake_show)
    monkeypatch.setattr("diagnostics._fetch_json", fake_get)

    r = client.post("/api/diagnostics/ingest", json={
        "ok": True,
        "model": "test-model",
        "engine": "ollama",
        "endpoint": "http://127.0.0.1:11434",
        "machine": "Ubuntu Station",
        "client": "foxai-ai-lab",
        "completion_tokens": 96,
        "prompt_tokens": 12,
        "done_reason": "length",
        "request": {"max_tokens": 96},
        "content": "SHOULD NOT PERSIST",
        "messages": [{"role": "user", "content": "nope"}],
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["data"]["stop_reason"]["code"] == "output_limit_reached"
    assert body["data"]["id"]

    hist = client.get("/api/diagnostics/history?model=test-model")
    rows = hist.get_json()["data"]
    assert len(rows) >= 1
    assert "content" not in rows[0]
    assert "messages" not in (rows[0].get("request") or {})

    insp = client.post("/api/diagnostics/inspect", json={
        "model": "test-model",
        "engine": "ollama",
        "endpoint": "http://127.0.0.1:11434",
        "machine": "Ubuntu Station",
    })
    assert insp.status_code == 200
    data = insp.get_json()["data"]
    assert data["read_only"] is True
    assert data["inspection"]["backend"]["fields"]["quantization"]["value"] == "Q4_K_M"

    rep = client.post("/api/diagnostics/report", json={
        "model": "test-model", "engine": "ollama",
        "endpoint": "http://127.0.0.1:11434", "format": "text",
    })
    assert rep.status_code == 200
    assert "Diagnostic Report" in rep.get_json()["data"]["text"]


def test_app_version_is_v33_dev():
    from app import APP_VERSION
    assert APP_VERSION.startswith("3.3")
