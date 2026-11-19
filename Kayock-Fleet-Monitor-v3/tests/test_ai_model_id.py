"""v3 llama-server model-identification tests: the reported v2 bug was
'no model loaded' for a running llama-server on :8080. These tests force the
real-world endpoint shape variants and require the model to be identified."""
import json

from ai import probe_llama_server
from test_ai import FakePopen, FakeProcObj, FakePsutil, make_manager  # noqa: F401
from ai import AIManager


def _fetcher(responses):
    """responses: {path_suffix: (status, body)}; unmatched -> 404."""

    def fetch(url, timeout):
        for suffix, (st, body) in responses.items():
            if url.endswith(suffix):
                return st, body if isinstance(body, str) else json.dumps(body), None
        return 404, None, "HTTP 404"

    return fetch


def _target(port=8080):
    return {"key": f"127.0.0.1:{port}", "host": "127.0.0.1", "port": port,
            "type_hint": "llama-server", "pid": 4242, "name": None,
            "exe": None, "cmdline": None, "create_time": 1000.0,
            "configured": False, "config": None, "port_listening": True,
            "process_found": True}


def test_llama_modern_props_shape(tmp_path):
    """llama.cpp builds that report {"model": {"path": ...}, "n_ctx": N}."""
    fetch = _fetcher({
        "/health": (200, {"status": "ok"}),
        "/props": (200, {"model": {"path": "C:/models/qwen2.5-7b-q5_k_m.gguf"},
                         "n_ctx": 32768}),
        "/slots": (200, [{"is_processing": False,
                          "prompt": {"n_ctx_tokens": 4096}}]),
    })
    info, err = probe_llama_server("http://127.0.0.1:8080", fetch,
                                   lambda *a, **k: (200, "{}", None), 2.0)
    assert err is None
    assert info["api"] == "llama-server"
    assert info["model_path"] == "C:/models/qwen2.5-7b-q5_k_m.gguf"
    assert info["context_tokens"] == 32768
    assert info["loaded_models"][0]["name"] == "qwen2.5-7b-q5_k_m.gguf"

    mgr = make_manager(tmp_path, fetch=fetch)
    rec = mgr._probe_one(_target())
    assert rec["status"] == "online"
    assert rec["ready"] is True            # THE v2 BUG FIX: not 'no model loaded'
    assert rec["model_name"].endswith(".gguf")


def test_llama_legacy_props_shape(tmp_path):
    """Older/alternate builds expose model_path / model_alias at top level."""
    fetch = _fetcher({
        "/health": (200, {"status": "ok"}),
        "/props": (200, {"model_path": "/home/user/models/mistral.gguf",
                         "model_alias": "mistral-7b",
                         "n_ctx": 8192}),
    })
    mgr = make_manager(tmp_path, fetch=fetch)
    rec = mgr._probe_one(_target())
    assert rec["ready"] is True
    assert rec["model_name"] == "mistral-7b"
    assert rec["model_path"] == "/home/user/models/mistral.gguf"
    assert rec["context_tokens"] == 8192


def test_llama_props_404_falls_back_to_v1_models(tmp_path):
    """/props disabled/404 — /v1/models still identifies the served model."""
    fetch = _fetcher({
        "/health": (200, {"status": "ok"}),
        "/v1/models": (200, {"data": [{"id": "meta-llama-3-8b-instruct"}]}),
    })
    mgr = make_manager(tmp_path, fetch=fetch)
    rec = mgr._probe_one(_target())
    assert rec["status"] == "online"
    assert rec["ready"] is True
    assert rec["model_name"] == "meta-llama-3-8b-instruct"
    assert rec["loaded_models"][0]["name"] == "meta-llama-3-8b-instruct"


def test_llama_health_503_loading(tmp_path):
    """/health 503 with 'Loading model' → status loading (not online)."""
    fetch = _fetcher({
        "/health": (503, {"error": {"message": "Loading model"}}),
        "/props": (200, {"model": {"path": "m.gguf"}, "n_ctx": 4096}),
    })
    mgr = make_manager(tmp_path, fetch=fetch)
    rec = mgr._probe_one(_target())
    assert rec["status"] == "loading"
    assert rec.get("loading") is True


def test_llama_no_identity_is_honest(tmp_path):
    """API healthy but NO endpoint identifies a model → online, ready=None,
    never a fake model name."""
    fetch = _fetcher({
        "/health": (200, {"status": "ok"}),
        "/v1/models": (200, {"data": []}),
    })
    mgr = make_manager(tmp_path, fetch=fetch)
    rec = mgr._probe_one(_target())
    assert rec["status"] == "online"
    assert rec["ready"] is None
    assert rec.get("model_name") is None
    assert rec.get("loaded_models") is None


def test_llama_slots_dict_shape(tmp_path):
    """Some builds return {"slots": [...]} instead of a bare list."""
    fetch = _fetcher({
        "/health": (200, {"status": "ok"}),
        "/props": (200, {"model": {"path": "m.gguf"}, "n_ctx": 100}),
        "/slots": (200, {"slots": [
            {"is_processing": True, "prompt": {"n_ctx_tokens": 50}},
            {"is_processing": False, "prompt": {"n_ctx_tokens": 10}}]}),
    })
    mgr = make_manager(tmp_path, fetch=fetch)
    rec = mgr._probe_one(_target())
    assert rec["status"] == "busy"
    assert rec["active_requests"] == 1
    assert rec["context_utilization"] == 0.6
