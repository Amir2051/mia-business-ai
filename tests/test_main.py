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


def test_hermes_default_provider_fallback(app, monkeypatch):
    def fake_openrouter(messages, model, temperature, max_tokens):
        raise RuntimeError("openrouter should not be called")

    def fake_hermes(messages, model, temperature, max_tokens):
        return {"choices": [{"message": {"content": "hermes reply"}}]}

    with mock.patch.object(app, "_call_openrouter", fake_openrouter), \
         mock.patch.object(app, "_call_hermes", fake_hermes):
        resp = app._complete_message(
            "s", "from hermes", "sess2", None, None, None
        )

    assert resp.reply == "hermes reply"
    assert resp.provider == "hermes"


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
