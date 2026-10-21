import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import HTTPException  # noqa: E402
from unittest import mock  # noqa: E402


@pytest.fixture()
def app():
    os.environ["RZ_API_KEY"] = "test-key"
    os.environ["OPENROUTER_API_KEY"] = "real"
    os.environ["HERMES_API_KEY"] = "hermes-secret"
    os.environ["HERMES_MODEL"] = "upstage/solar-mini4"
    os.environ["HERMES_ENABLED"] = "1"
    sys.modules.pop("app.main", None)
    import app.main as m  # noqa: F401

    yield m

    with m._store_lock:
        m._conversations.clear()
    for key in ("RZ_API_KEY", "OPENROUTER_API_KEY", "HERMES_API_KEY", "HERMES_MODEL", "HERMES_ENABLED"):
        os.environ.pop(key, None)


def test_placeholder_openrouter_skips_to_502(app):
    try:
        app._complete_message("s", "hello", "sess1", None, None, None)
    except HTTPException as exc:
        assert exc.status_code == 502
    else:
        raise AssertionError("expected a 502 when no provider is usable")


def test_openrouter_conversation_memory(app, monkeypatch):
    calls = []

    def fake_openrouter(messages, model, temperature, max_tokens):
        calls.append(messages)
        return {"choices": [{"message": {"content": "hello back"}}]}

    monkeypatch.setattr(app, "_call_openrouter", fake_openrouter)
    resp = app._complete_message("s", "hello", "sess1", None, None, None)

    assert resp.status == "ok"
    assert resp.reply == "hello back"
    assert resp.session_id == "sess1"
    assert resp.history_length == 2
    assert resp.provider == "openrouter"
    # a fresh session does not leak history
    r2 = app._complete_message("s", "second", "sess1", None, None, None)
    assert r2.history_length == 4


def test_conversation_threading_keeps_context(app, monkeypatch):
    def fake(messages, model, temperature, max_tokens):
        return {"choices": [{"message": {"content": "prior: " + messages[-1]["content"]}}]}

    with mock.patch.object(app, "_call_openrouter", fake):
        app._complete_message("s", "hello", "sess1", None, None, None)
        resp = app._complete_message("s", "what did I just say", "sess1", None, None, None)

    assert resp.reply == "prior: what did I just say"
    assert resp.history_length == 4


def test_reset_clears_conversation(app, monkeypatch):
    def fake(messages, model, temperature, max_tokens):
        return {"choices": [{"message": {"content": "ok"}} ]}

    with mock.patch.object(app, "_call_openrouter", fake):
        app._complete_message("s", "reset this", "sess1", None, None, None)
        with app._store_lock:
            app._conversations.pop("sess1", None)
        resp = app._complete_message("s", "what did I just say", "sess1", None, None, None)

    assert resp.reply == "ok"
    assert resp.history_length == 2
    assert "prior" not in resp.reply  # reset removed prior context


def test_does_not_fallback_to_unverified_hermes_when_openrouter_is_configured(app, monkeypatch):
    def fake_openrouter(messages, model, temperature, max_tokens):
        raise RuntimeError("ZDR endpoint unavailable")

    def fake_hermes(messages, model, temperature, max_tokens):
        raise AssertionError("must not route to Hermes after OpenRouter failure")

    monkeypatch.setattr(app, "HERMES_ENABLED", True)
    with mock.patch.object(app, "_call_openrouter", fake_openrouter), \
         mock.patch.object(app, "_call_hermes", fake_hermes):
        with pytest.raises(HTTPException) as exc:
            app._complete_message(
                "s", "from hermes", "sess2", "example/openrouter-model", None, None
            )

    assert exc.value.status_code == 502
    assert "ZDR endpoint unavailable" in exc.value.detail


def test_hermes_can_be_primary_when_openrouter_is_not_configured(app, monkeypatch):
    monkeypatch.setattr(app, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(app, "HERMES_ENABLED", True)

    def fake_hermes(messages, model, temperature, max_tokens):
        assert model == app.HERMES_MODEL
        return {"choices": [{"message": {"content": "hermes primary reply"}}]}

    with mock.patch.object(app, "_call_hermes", fake_hermes):
        resp = app._complete_message("s", "hello", "sess3", None, None, None)

    assert resp.reply == "hermes primary reply"
    assert resp.provider == "hermes"
    assert resp.model == app.HERMES_MODEL


def test_openrouter_endpoint_accepts_regional_api_base_urls(app):
    assert app._openrouter_chat_completions_url(
        "https://us.openrouter.ai/api/v1"
    ) == "https://us.openrouter.ai/api/v1/chat/completions"
    assert app._openrouter_chat_completions_url(
        "https://eu.openrouter.ai/api/v1/"
    ) == "https://eu.openrouter.ai/api/v1/chat/completions"


def test_openrouter_endpoint_preserves_full_chat_url(app):
    endpoint = "https://openrouter.ai/api/v1/chat/completions"
    assert app._openrouter_chat_completions_url(endpoint) == endpoint


def test_openrouter_endpoint_accepts_host_only(app):
    assert app._openrouter_chat_completions_url(
        "https://us.openrouter.ai"
    ) == "https://us.openrouter.ai/api/v1/chat/completions"


def test_task_model_routing_uses_configured_coding_model(app, monkeypatch):
    monkeypatch.setattr(app, "OPENROUTER_CODING_MODEL", "example/coding-model")
    monkeypatch.setattr(app, "OPENROUTER_ANALYSIS_MODEL", "")
    assert app._select_model_for_task("Please debug this Python traceback") == "example/coding-model"


def test_task_model_routing_uses_configured_analysis_model(app, monkeypatch):
    monkeypatch.setattr(app, "OPENROUTER_CODING_MODEL", "")
    monkeypatch.setattr(app, "OPENROUTER_ANALYSIS_MODEL", "example/analysis-model")
    assert app._select_model_for_task("Compare these options and analyze the tradeoffs") == "example/analysis-model"


def test_task_model_routing_preserves_default_and_explicit_override(app, monkeypatch):
    monkeypatch.setattr(app, "OPENROUTER_MODEL", "example/default-model")
    monkeypatch.setattr(app, "OPENROUTER_CODING_MODEL", "example/coding-model")
    assert app._select_model_for_task("Tell me a joke") == "example/default-model"
    assert app._select_model_for_task("Debug this code", "example/manual-model") == "example/manual-model"


def test_idle_sessions_expire_on_access(app, monkeypatch):
    monkeypatch.setattr(app, "SESSION_TTL_SECONDS", 60)
    with app._store_lock:
        app._conversations["expired"] = {
            "history": [{"role": "user", "content": "old"}],
            "updated": 100.0,
            "model": "example/model",
        }
        app._conversations["fresh"] = {
            "history": [],
            "updated": 950.0,
            "model": "example/model",
        }
        removed = app._prune_expired_sessions_locked(now=1000.0)
        assert removed == 1
        assert "expired" not in app._conversations
        assert "fresh" in app._conversations


def test_capabilities_endpoint_does_not_expose_secrets(app):
    app.OPENROUTER_CODING_MODEL = "example/coder"
    app.OPENROUTER_ANALYSIS_MODEL = ""
    result = app.capabilities({"id": "user-1"})
    assert result["provider"] == "openrouter"
    assert result["default_model"] == app.OPENROUTER_MODEL
    assert result["task_routing"]["coding_model_configured"] is True
    assert result["task_routing"]["analysis_model_configured"] is False
    assert result["privacy"]["zdr_requested"] is True
    assert "API_KEY" not in str(result)
    assert "secret" not in str(result)
