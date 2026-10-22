import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import HTTPException  # noqa: E402
from unittest import mock  # noqa: E402


@pytest.fixture()
def app(monkeypatch):
    # Force local API-key mode even if the developer has a private .env file.
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    os.environ["RZ_API_KEY"] = "test-key"
    os.environ["OPENROUTER_API_KEY"] = "real"
    os.environ["HERMES_API_KEY"] = "hermes-secret"
    os.environ["HERMES_MODEL"] = "upstage/solar-mini4"
    os.environ["HERMES_ENABLED"] = "1"
    sys.modules.pop("app.main", None)
    sys.modules.pop("app.auth", None)
    import app.main as m  # noqa: F401

    yield m

    with m._store_lock:
        m._conversations.clear()
    for key in ("RZ_API_KEY", "OPENROUTER_API_KEY", "HERMES_API_KEY", "HERMES_MODEL", "HERMES_ENABLED"):
        os.environ.pop(key, None)


def test_mia_core_identity_always_knows_rz_and_rz_agent(app):
    identity = app.ASSISTANT_IDENTITY.lower()
    assert "rz, also known as ronzoro" in identity
    assert "creator and operator" in identity
    assert "rz agent is the platform" in identity
    assert "do not say rz is unknown" in identity
    assert "does not grant permission" in identity


def test_placeholder_openrouter_skips_to_502(app):
    try:
        app._complete_message("s", "hello", "sess1", None, None, None)
    except HTTPException as exc:
        assert exc.status_code == 502
    else:
        raise AssertionError("expected a 502 when no provider is usable")


def test_inworld_realtime_session_token_is_minted_without_exposing_api_key(app, monkeypatch):
    credential = app.base64.b64encode(b"voice-key-id:voice-key-secret").decode("ascii")
    monkeypatch.setattr(app, "INWORLD_API_KEY", credential)
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(req, timeout=10):
        calls.append((req.full_url, dict(req.header_items()), timeout))
        if req.full_url.endswith("/v1/sessionTokens/token:generate"):
            assert req.get_header("Authorization").startswith("IW1-HMAC-SHA256 ApiKey=voice-key-id,")
            assert "Signature=" in req.get_header("Authorization")
            assert "voice-key-secret" not in req.get_header("Authorization")
            return FakeResponse({"token": "temporary-session-token", "expirationTime": "2026-10-22T16:00:00Z"})
        assert req.full_url.endswith("/v1/realtime/ice-servers")
        assert req.get_header("Authorization") == "Basic " + credential
        return FakeResponse({"ice_servers": [{"urls": ["stun:example.invalid"]}]})

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    result = app._mint_inworld_realtime_session()
    assert result["token"] == "temporary-session-token"
    assert result["ice_servers"][0]["urls"][0] == "stun:example.invalid"
    assert result["url"] == "https://api.inworld.ai/v1/realtime/calls"
    assert credential not in json.dumps(result)
    assert len(calls) == 2


def test_openrouter_retries_free_router_when_model_is_zdr_blocked(app, monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError

    monkeypatch.setattr(app, "OPENROUTER_FALLBACK_MODEL", "openrouter/free")
    calls = []

    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b'{"choices":[{"message":{"content":"Hello from fallback"}}]}'

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data.decode())
        calls.append(payload["model"])
        if len(calls) == 1:
            raise HTTPError(request.full_url, 404, "Not Found", {}, BytesIO(
                b'{"error":{"message":"0 endpoints out of 1 requested; ZDR violation (account settings)"}}'
            ))
        return FakeResponse()

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    result = app._call_openrouter([{"role": "user", "content": "hello"}], "nvidia/nemotron-3.5-lightning:free", None, None)
    assert result["choices"][0]["message"]["content"] == "Hello from fallback"
    assert calls == ["nvidia/nemotron-3.5-lightning:free", "openrouter/free"]


def test_openrouter_provider_preferences_are_configurable(app, monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self):
            return b'{"choices":[{"message":{"content":"hello"}}]}'

    def fake_urlopen(req, timeout=45):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(app, "OPENROUTER_ZDR", False)
    monkeypatch.setattr(app, "OPENROUTER_ALLOW_FALLBACKS", True)
    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    result = app._call_openrouter([{"role": "user", "content": "hello"}], "nvidia/nemotron-3.5-lightning:free", None, 100)

    assert result["choices"][0]["message"]["content"] == "hello"
    assert captured["payload"]["provider"] == {"zdr": False, "allow_fallbacks": True}


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


def test_mcp_initialize_and_tool_discovery_are_authenticated(app):
    from fastapi.testclient import TestClient
    client = TestClient(app.app)
    denied = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    assert denied.status_code == 401
    initialized = client.post('/mcp', headers={'X-API-Key': 'test-key'}, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}},
    })
    assert initialized.status_code == 200
    assert initialized.json()['result']['capabilities']['tools']['listChanged'] is False
    listed = client.post('/mcp', headers={'X-API-Key': 'test-key'}, json={
        'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {},
    }).json()
    names = {tool['name'] for tool in listed['result']['tools']}
    assert names == {'get_runtime_status', 'list_conversations', 'chat', 'reset_conversation', 'github_get_repository', 'github_list_issues', 'github_list_pull_requests', 'github_get_file_contents'}


def test_mcp_status_does_not_disclose_credentials(app):
    from fastapi.testclient import TestClient
    client = TestClient(app.app)
    result = client.post('/mcp', headers={'X-API-Key': 'test-key'}, json={
        'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
        'params': {'name': 'get_runtime_status', 'arguments': {}},
    }).json()['result']['structuredContent']
    assert result['status'] == 'ok'
    assert 'OPENROUTER_API_KEY' not in str(result)
    assert 'hermes-secret' not in str(result)
    assert 'test-key' not in str(result)


def test_mcp_chat_uses_existing_provider_and_enforces_session_ownership(app, monkeypatch):
    from fastapi.testclient import TestClient
    client = TestClient(app.app)
    monkeypatch.setattr(app, '_call_openrouter', lambda *args: {
        'choices': [{'message': {'content': 'MCP hello'}}]
    })
    denied = client.post('/mcp', headers={'X-API-Key': 'test-key'}, json={
        'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
        'params': {'name': 'chat', 'arguments': {'message': 'hello', 'session_id': 'another-user:secret'}},
    }).json()['result']
    assert denied['isError'] is True
    assert 'does not belong' in denied['structuredContent']['error']
    response = client.post('/mcp', headers={'X-API-Key': 'test-key'}, json={
        'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
        'params': {'name': 'chat', 'arguments': {'message': 'hello'}},
    }).json()['result']['structuredContent']
    assert response['reply'] == 'MCP hello'
    assert response['session_id'].startswith('local:')


def test_mcp_reset_only_deletes_owned_conversation(app):
    from fastapi.testclient import TestClient
    client = TestClient(app.app)
    with app._store_lock:
        app._conversations['local:mine'] = {'history': [], 'updated': 9999999999, 'model': 'test'}
        app._conversations['other:theirs'] = {'history': [], 'updated': 9999999999, 'model': 'test'}
    denied = client.post('/mcp', headers={'X-API-Key': 'test-key'}, json={
        'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
        'params': {'name': 'reset_conversation', 'arguments': {'session_id': 'other:theirs'}},
    }).json()['result']
    assert denied['isError'] is True
    with app._store_lock:
        assert 'other:theirs' in app._conversations
    success = client.post('/mcp', headers={'X-API-Key': 'test-key'}, json={
        'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
        'params': {'name': 'reset_conversation', 'arguments': {'session_id': 'local:mine'}},
    }).json()['result']['structuredContent']
    assert success['reset'] is True
    with app._store_lock:
        assert 'local:mine' not in app._conversations
        app._conversations.pop('other:theirs', None)


def test_github_repository_tool_is_read_only_and_scoped_to_amir2051(app, monkeypatch):
    from fastapi.testclient import TestClient
    client = TestClient(app.app)
    calls = []
    monkeypatch.setattr(app, "_github_request", lambda path: calls.append(path) or {
        "full_name": "Amir2051/rz-ai-agent", "private": True, "html_url": "https://github.com/Amir2051/rz-ai-agent",
        "default_branch": "master", "description": "RZ Agent", "updated_at": "2026-10-21T00:00:00Z"
    })
    response = client.post('/mcp', headers={'X-API-Key': 'test-key'}, json={
        'jsonrpc': '2.0', 'id': 20, 'method': 'tools/call',
        'params': {'name': 'github_get_repository', 'arguments': {'repo': 'rz-ai-agent'}},
    }).json()['result']['structuredContent']
    assert response['full_name'] == 'Amir2051/rz-ai-agent'
    assert calls == ['/repos/Amir2051/rz-ai-agent']
    denied = client.post('/mcp', headers={'X-API-Key': 'test-key'}, json={
        'jsonrpc': '2.0', 'id': 21, 'method': 'tools/call',
        'params': {'name': 'github_get_repository', 'arguments': {'repo': '../other-owner/private'}},
    }).json()['result']
    assert denied['isError'] is True
    assert not any('other-owner' in call for call in calls)


def test_github_issue_listing_filters_pull_requests_and_bounds_limit(app, monkeypatch):
    data = [
        {'number': 1, 'title': 'Issue', 'state': 'open', 'html_url': 'https://github.com/Amir2051/rz-ai-agent/issues/1'},
        {'number': 2, 'title': 'PR masquerading in issues API', 'state': 'open', 'pull_request': {'url': 'x'}},
    ]
    monkeypatch.setattr(app, '_github_request', lambda path: data)
    result = app._github_tool_call('github_list_issues', {'repo': 'rz-ai-agent', 'limit': 2})
    assert [item['number'] for item in result] == [1]
    with pytest.raises(ValueError, match='limit'):
        app._github_tool_call('github_list_issues', {'repo': 'rz-ai-agent', 'limit': 100})


def test_github_file_tool_blocks_parent_traversal_and_returns_text(app, monkeypatch):
    import base64
    monkeypatch.setattr(app, '_github_request', lambda path: {
        'type': 'file', 'path': 'README.md', 'size': 5, 'html_url': 'https://github.com/Amir2051/rz-ai-agent/blob/master/README.md',
        'encoding': 'base64', 'content': base64.b64encode(b'hello').decode()
    })
    result = app._github_tool_call('github_get_file_contents', {'repo': 'rz-ai-agent', 'path': 'README.md'})
    assert result['content'] == 'hello'
    with pytest.raises(ValueError, match='path'):
        app._github_tool_call('github_get_file_contents', {'repo': 'rz-ai-agent', 'path': '../.env'})


def test_chat_github_request_fetches_repository_before_model_synthesis(app, monkeypatch):
    calls = []
    def fake_openrouter(messages, model, temperature, max_tokens, tools=None, tool_choice="auto"):
        calls.append(messages)
        assert "Direct read-only GitHub lookup result" in messages[-1]["content"]
        assert '"full_name": "Amir2051/rz-ai-agent"' in messages[-1]["content"]
        assert not tools
        return {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "I checked the Amir2051/rz-ai-agent repository."
        }}]}
    monkeypatch.setattr(app, "_call_openrouter", fake_openrouter)
    dispatched = []
    monkeypatch.setattr(app, "_github_tool_call", lambda name, args: dispatched.append((name,args)) or {
        "full_name": f"Amir2051/{args['repo']}", "private": False
    })
    response = app._complete_message("You are helpful", "Check my GitHub repository rz-ai-agent", "local:tools", None, None, None)
    assert response.reply == "I checked the Amir2051/rz-ai-agent repository."
    assert dispatched == [("github_get_repository", {"repo": "rz-ai-agent"})]
    assert len(calls) == 1


def test_github_tool_loop_rejects_unlisted_tool(app, monkeypatch):
    calls = 0

    def fake_openrouter(messages, model, temperature, max_tokens, tools=None, tool_choice="auto"):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"choices": [{"message": {"content": None, "tool_calls": [{
                "id": "call_bad", "type": "function", "function": {
                    "name": "delete_repository", "arguments": '{"repo":"rz-ai-agent"}'
                }
            }]}}]}
        assert '"error": "Tool is not allowed' in messages[-1]["content"]
        return {"choices": [{"message": {"content": "I only have read-only GitHub access."}}]}

    monkeypatch.setattr(app, "_call_openrouter", fake_openrouter)
    monkeypatch.setattr(app, "_github_tool_call", lambda *args: (_ for _ in ()).throw(AssertionError("must not dispatch unknown tools")))
    result = app._complete_with_github_tools("You are helpful", "Check GitHub repo rz-ai-agent", "local:tools2", app.OPENROUTER_MODEL, None, None)
    response = type("R", (), {"reply": app._extract_reply(result)})()
    assert response.reply == "I only have read-only GitHub access."
    assert calls == 2


def test_scoped_bearer_token_auth_for_mcp_and_individual_tool_routes(app, monkeypatch):
    from fastapi.testclient import TestClient
    import app.auth as auth

    monkeypatch.setattr(app, "DATABASE_AUTH_ENABLED", True)
    monkeypatch.setattr(app, "_get_database_user", lambda request: {"id": "session-user", "email": "session@example.com"}, raising=False)
    def fake_bearer(authorization, required_scope=None):
        token = authorization.partition(" ")[2]
        if token == "read-token":
            return {"id": "user-read", "email": "read@example.com", "scopes": ["mcp:read"], "auth_type": "api_token"}
        if token == "call-token":
            return {"id": "user-call", "email": "call@example.com", "scopes": ["mcp:read", "mcp:call"], "auth_type": "api_token"}
        raise HTTPException(status_code=401, detail="API token is invalid, expired, or revoked")
    monkeypatch.setattr(auth, "get_user_from_bearer", fake_bearer)
    monkeypatch.setattr(app.cap_services, "list_memories", lambda user_id, limit: {"memories": [], "count": 0, "owner": user_id})
    monkeypatch.setattr(app.cap_services, "save_memory", lambda user_id, content: {"id": "mem1", "content": content, "owner": user_id})
    monkeypatch.setattr(app.cap_services, "web_research", lambda query, limit: {"query": query, "count": 1, "results": [{"title": "Mock result"}]})
    client = TestClient(app.app)

    read_headers = {"Authorization": "Bearer read-token"}
    listed = client.post("/mcp", headers=read_headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listed.status_code == 200
    denied_call = client.post("/mcp", headers=read_headers, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "list_conversations", "arguments": {}}})
    assert denied_call.status_code == 403
    assert client.get("/v1/memory", headers=read_headers).status_code == 200
    assert client.post("/v1/memory", headers=read_headers, json={"content": "blocked"}).status_code == 403

    call_headers = {"Authorization": "Bearer call-token"}
    called = client.post("/mcp", headers=call_headers, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "list_conversations", "arguments": {}}})
    assert called.status_code == 200
    saved = client.post("/v1/memory", headers=call_headers, json={"content": "persist this"})
    assert saved.status_code == 200
    assert saved.json()["memory"]["owner"] == "user-call"
    research_denied = client.post("/v1/research", headers=read_headers, json={"query": "safe test"})
    assert research_denied.status_code == 403
    research = client.post("/v1/research", headers=call_headers, json={"query": "safe test"})
    assert research.status_code == 200
    assert research.json()["results"][0]["title"] == "Mock result"


def test_bearer_token_verification_checks_scope_and_revocation(app, monkeypatch):
    import app.auth as auth

    class FakeResult:
        def __init__(self, row): self.row = row
        def fetchone(self): return self.row
    class FakeConnection:
        def __init__(self, row): self.row = row
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, query, params=()): return FakeResult(self.row)

    monkeypatch.setattr(auth, "_token_hash", lambda token: "hashed-token")
    monkeypatch.setattr(auth, "_db", lambda: FakeConnection(("u1", "u@example.com", ["mcp:read"])))
    with pytest.raises(HTTPException) as denied:
        auth.get_user_from_bearer("Bearer rzat_test", "mcp:call")
    assert denied.value.status_code == 403

    monkeypatch.setattr(auth, "_db", lambda: FakeConnection(None))
    with pytest.raises(HTTPException) as invalid:
        auth.get_user_from_bearer("Bearer rzat_test")
    assert invalid.value.status_code == 401
    assert "revoked" in invalid.value.detail


def test_api_token_creation_returns_secret_once_and_stores_only_hash(app, monkeypatch):
    import app.auth as auth
    from datetime import datetime, timezone

    class FakeResult:
        def fetchone(self): return ("token-id", "RZ MCP", ["mcp:call", "mcp:read"], datetime.now(timezone.utc), None)
    class FakeConnection:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, query, params=()):
            assert "INSERT INTO auth_api_tokens" in query
            assert params[2].startswith("sha256:")
            assert params[2] != params[1]
            return FakeResult()

    import hashlib
    monkeypatch.setattr(auth, "_db", lambda: FakeConnection())
    monkeypatch.setattr(auth, "_token_hash", lambda token: "sha256:" + hashlib.sha256(token.encode()).hexdigest())
    result = auth.create_api_token(auth.ApiTokenCreate(name="RZ MCP"), {"id": "u1"})
    assert result["token"].startswith("rzat_")
    assert result["warning"].endswith("cannot be displayed again.")
    assert result["scopes"] == ["mcp:call", "mcp:read"]



def test_scoped_bearer_auth_is_accepted_for_api_clients(app, monkeypatch):
    import importlib

    monkeypatch.setattr(app, "DATABASE_AUTH_ENABLED", True)
    auth_module = importlib.import_module("app.auth")
    seen = {}

    def fake_get_user_from_bearer(authorization, required_scope=None):
        seen["authorization"] = authorization
        seen["required_scope"] = required_scope
        return {"id": "user-123", "email": "test@example.com", "scopes": ["mcp:call"]}

    monkeypatch.setattr(auth_module, "get_user_from_bearer", fake_get_user_from_bearer)
    user = app.require_app_user(None, "Bearer rzat_test-token", "")
    assert user["id"] == "user-123"
    assert seen == {
        "authorization": "Bearer rzat_test-token",
        "required_scope": "mcp:call",
    }
def test_local_terminal_bridge_requires_dedicated_token(app, monkeypatch):
    import queue
    from fastapi.testclient import TestClient
    client = TestClient(app.app)
    monkeypatch.setattr(app, "MIA_TERMINAL_BRIDGE_TOKEN", "bridge-test-secret")
    monkeypatch.setattr(app._LOCAL_TERMINAL_QUEUE, "get", lambda timeout=0: (_ for _ in ()).throw(queue.Empty))
    denied = client.get("/v1/local-terminal/next")
    assert denied.status_code == 401
    accepted = client.get(
        "/v1/local-terminal/next",
        headers={"X-Bridge-Token": "bridge-test-secret"},
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"job": None}


def test_local_terminal_bridge_returns_approved_result_to_waiting_job(app, monkeypatch):
    from fastapi.testclient import TestClient
    import queue
    import threading
    client = TestClient(app.app)
    monkeypatch.setattr(app, "MIA_TERMINAL_BRIDGE_TOKEN", "bridge-test-secret")
    job_id = "a" * 32
    event = threading.Event()
    job = {
        "id": job_id, "command": "pwd", "workspace": "dev-terminal",
        "owner_id": "local", "approval": "approved", "status": "queued",
        "event": event, "result": None, "created": app.time.time()
    }
    with app._LOCAL_TERMINAL_LOCK:
        app._LOCAL_TERMINAL_JOBS[job_id] = job
    app._LOCAL_TERMINAL_QUEUE.put({"id": job_id, "command": "pwd", "workspace": "dev-terminal"})
    next_job = client.get(
        "/v1/local-terminal/next",
        headers={"X-Bridge-Token": "bridge-test-secret"},
    ).json()["job"]
    assert next_job == {"id": job_id, "command": "pwd", "workspace": "dev-terminal"}
    response = client.post(
        f"/v1/local-terminal/results/{job_id}",
        headers={"X-Bridge-Token": "bridge-test-secret"},
        json={"status": "completed", "output": "actual output"},
    )
    assert response.status_code == 200
    assert event.is_set()
    assert job["result"] == {"status": "completed", "output": "actual output"}
    with app._LOCAL_TERMINAL_LOCK:
        app._LOCAL_TERMINAL_JOBS.pop(job_id, None)


def test_local_terminal_tool_loop_uses_real_tool_result_and_never_executes_directly(app, monkeypatch):
    calls = []
    def fake_openrouter(messages, model, temperature, max_tokens, tools=None, tool_choice="auto"):
        calls.append({"tools": tools, "tool_choice": tool_choice, "messages": messages})
        if len(calls) == 1:
            return {"choices": [{"message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_local_1", "type": "function",
                    "function": {"name": "mia_request_local_command",
                                 "arguments": json.dumps({"command": "pwd", "workspace": "dev-terminal"})}
                }]
            }}]}
        assert any(m.get("role") == "tool" and "actual local output" in m.get("content", "") for m in messages)
        return {"choices": [{"message": {"content": "The local command returned actual local output."}}]}
    monkeypatch.setattr(app, "_call_openrouter", fake_openrouter)
    monkeypatch.setattr(app, "_request_local_terminal_command", lambda command, workspace, owner_id="local": {
        "status": "completed", "output": "actual local output"
    })
    result = app._complete_with_local_terminal_tools(
        app.ASSISTANT_IDENTITY, "run a command on my Kali machine", "test-user:session", None, None, 500
    )
    assert result["choices"][0]["message"]["content"].startswith("The local command")
    assert calls[0]["tool_choice"] == "required"
    assert calls[0]["tools"][0]["function"]["name"] == "mia_request_local_command"


def test_local_terminal_pending_approval_is_user_scoped_and_enqueues_only_after_approval(app, monkeypatch):
    from fastapi.testclient import TestClient
    import threading, uuid
    client = TestClient(app.app)
    monkeypatch.setattr(app, "MIA_TERMINAL_BRIDGE_TOKEN", "bridge-test-secret")
    job_id = uuid.uuid4().hex
    event = threading.Event()
    job = {
        "id": job_id, "command": "pwd", "workspace": "dev-terminal",
        "owner_id": "local", "approval": "pending", "status": "pending_approval",
        "event": event, "result": None, "created": app.time.time()
    }
    app.cap_services.create_terminal_approval("local", job_id, "pwd", "dev-terminal")
    with app._LOCAL_TERMINAL_LOCK:
        app._LOCAL_TERMINAL_JOBS[job_id] = job
    assert client.get("/v1/local-terminal/pending").status_code == 401
    pending = client.get("/v1/local-terminal/pending", headers={"X-API-Key": "test-key"})
    assert pending.status_code == 200
    pending_job = next(item for item in pending.json()["jobs"] if item["id"] == job_id)
    assert {key: pending_job[key] for key in ("id", "command", "workspace")} == {"id": job_id, "command": "pwd", "workspace": "dev-terminal"}
    assert app._LOCAL_TERMINAL_QUEUE.empty()
    decision = client.post(f"/v1/local-terminal/decision/{job_id}", headers={"X-API-Key": "test-key"}, json={"decision": "approve"})
    assert decision.status_code == 200
    assert job["approval"] == "approved"
    next_job = client.get("/v1/local-terminal/next", headers={"X-Bridge-Token": "bridge-test-secret"})
    assert next_job.status_code == 200
    assert next_job.json()["job"] == {"id": job_id, "command": "pwd", "workspace": "dev-terminal"}
    with app._LOCAL_TERMINAL_LOCK:
        app._LOCAL_TERMINAL_JOBS.pop(job_id, None)


def test_local_terminal_denial_wakes_waiting_chat_without_queueing(app):
    from fastapi.testclient import TestClient
    import threading, uuid
    client = TestClient(app.app)
    job_id = uuid.uuid4().hex
    event = threading.Event()
    job = {
        "id": job_id, "command": "pwd", "workspace": "dev-terminal",
        "owner_id": "local", "approval": "pending", "status": "pending_approval",
        "event": event, "result": None, "created": app.time.time()
    }
    app.cap_services.create_terminal_approval("local", job_id, "pwd", "dev-terminal")
    with app._LOCAL_TERMINAL_LOCK:
        app._LOCAL_TERMINAL_JOBS[job_id] = job
    response = client.post(f"/v1/local-terminal/decision/{job_id}", headers={"X-API-Key": "test-key"}, json={"decision": "deny"})
    assert response.status_code == 200
    assert event.is_set()
    assert job["result"]["status"] == "denied"
    assert app._LOCAL_TERMINAL_QUEUE.empty()
    with app._LOCAL_TERMINAL_LOCK:
        app._LOCAL_TERMINAL_JOBS.pop(job_id, None)


def test_local_terminal_decision_cannot_approve_another_users_job(app):
    from fastapi.testclient import TestClient
    import threading
    client = TestClient(app.app)
    job_id = "d" * 32
    job = {
        "id": job_id, "command": "pwd", "workspace": "dev-terminal",
        "owner_id": "another-user", "approval": "pending", "status": "pending_approval",
        "event": threading.Event(), "result": None, "created": app.time.time()
    }
    with app._LOCAL_TERMINAL_LOCK:
        app._LOCAL_TERMINAL_JOBS[job_id] = job
    response = client.post(f"/v1/local-terminal/decision/{job_id}", headers={"X-API-Key": "test-key"}, json={"decision": "approve"})
    assert response.status_code == 404
    assert job["approval"] == "pending"
    assert app._LOCAL_TERMINAL_QUEUE.empty()
    with app._LOCAL_TERMINAL_LOCK:
        app._LOCAL_TERMINAL_JOBS.pop(job_id, None)


def test_cartesia_voice_endpoint_returns_wav_without_calling_llm(app, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app, "CARTESIA_API_KEY", "test-cartesia-key")
    monkeypatch.setattr(app, "CARTESIA_VOICE_ID", "test-voice-id")
    monkeypatch.setattr(app, "CARTESIA_TTS_MODEL", "sonic-3.5")
    monkeypatch.setattr(app, "CARTESIA_VERSION", "2026-08-14")
    fake_audio = b"RIFF" + (b"x" * 2048)
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return fake_audio

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("X-api-key")
        captured["version"] = request.get_header("Cartesia-version")
        captured["payload"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    response = TestClient(app.app).post(
        "/v1/voice/synthesize",
        headers={"X-API-Key": "test-key"},
        json={"text": "Hello from MIA", "language": "en-US"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == fake_audio
    assert captured["url"] == "https://api.cartesia.ai/tts/bytes"
    assert captured["auth"] == "test-cartesia-key"
    assert captured["version"] == "2026-08-14"
    assert captured["payload"]["voice"] == {"mode": "id", "id": "test-voice-id"}
    assert captured["payload"]["model_id"] == "sonic-3.5"
    assert captured["payload"]["transcript"] == "Hello from MIA"
    assert captured["payload"]["language"] == "en"


def test_cartesia_voice_endpoint_requires_server_configuration(app, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app, "CARTESIA_API_KEY", "")
    monkeypatch.setattr(app, "INWORLD_API_KEY", "")
    response = TestClient(app.app).post(
        "/v1/voice/synthesize",
        headers={"X-API-Key": "test-key"},
        json={"text": "Hello from MIA"},
    )
    assert response.status_code == 503
    assert "CARTESIA_API_KEY" in response.json()["detail"]



def test_ollama_is_used_as_local_fallback_after_openrouter_failure(app, monkeypatch):
    monkeypatch.setattr(app, "OLLAMA_ENABLED", True)
    monkeypatch.setattr(app, "OLLAMA_MODEL", "qwen3:1.7b")

    def fail_openrouter(*args, **kwargs):
        raise RuntimeError("simulated OpenRouter ZDR/provider rejection")

    def local_ollama(messages, model, temperature, max_tokens, tools=None, tool_choice="auto"):
        assert model == "qwen3:1.7b"
        assert messages[-1]["content"] == "Say local fallback works"
        return {"model": model, "choices": [{"message": {"role": "assistant", "content": "Local Qwen fallback works."}}]}

    monkeypatch.setattr(app, "_call_openrouter", fail_openrouter)
    monkeypatch.setattr(app, "_call_ollama", local_ollama)
    resp = app._complete_message("s", "Say local fallback works", "ollama-test", None, None, 80)
    assert resp.status == "ok"
    assert resp.provider == "ollama"
    assert resp.model == "qwen3:1.7b"
    assert resp.reply == "Local Qwen fallback works."


def test_ollama_fallback_can_recover_from_empty_openrouter_reply(app, monkeypatch):
    monkeypatch.setattr(app, "OLLAMA_ENABLED", True)
    monkeypatch.setattr(app, "OLLAMA_MODEL", "qwen3:1.7b")
    monkeypatch.setattr(app, "_call_openrouter", lambda *args, **kwargs: {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
    monkeypatch.setattr(app, "_call_ollama", lambda *args, **kwargs: {"model": "qwen3:1.7b", "choices": [{"message": {"content": "Recovered locally."}}]})
    resp = app._complete_message("s", "hello", "ollama-empty-test", None, None, 80)
    assert resp.provider == "ollama"
    assert resp.reply == "Recovered locally."


def test_mia_agent_tool_router_is_opt_in(app):
    assert app._should_use_mia_agent_tools("Act as an agent and research and create a plan")
    assert app._should_use_mia_agent_tools("Resume the task and track my progress")
    assert not app._should_use_mia_agent_tools("Hi MIA, how are you?")
    names = {item["function"]["name"] for item in app.MIA_AGENT_TOOLS}
    assert {"mia_web_search", "mia_open_page", "mia_memory_list", "mia_memory_save", "mia_task_create", "mia_task_list", "mia_task_update_step", "mia_read_document", "mia_review_code", "mia_github_read", "mia_request_local_command"} <= names


def test_mia_agent_tool_arguments_are_validated_server_side(app):
    app._validate_mia_agent_tool_args("mia_task_update_step", {"task_id": "task-1", "step_index": 0, "status": "completed"})
    with pytest.raises(ValueError, match="Unexpected"):
        app._validate_mia_agent_tool_args("mia_task_update_step", {"task_id": "task-1", "step_index": 0, "status": "completed", "owner_id": "someone-else"})
    with pytest.raises(ValueError, match="integer"):
        app._validate_mia_agent_tool_args("mia_task_update_step", {"task_id": "task-1", "step_index": True, "status": "completed"})
    with pytest.raises(ValueError, match="unsupported"):
        app._validate_mia_agent_tool_args("mia_task_update_step", {"task_id": "task-1", "step_index": 0, "status": "skip_auth"})


def test_mia_agent_task_tools_are_scoped_and_update_progress(app, monkeypatch):
    owner = "account-123"
    task = {"id": "task-1", "title": "Build feature", "status": "in_progress", "steps": [{"title": "Inspect", "status": "in_progress"}]}
    calls = []
    monkeypatch.setattr(app.cap_services, "list_tasks", lambda user_id, limit: calls.append(("list", user_id, limit)) or {"tasks": [task], "count": 1})
    monkeypatch.setattr(app.cap_services, "update_task_step", lambda user_id, task_id, index, status: calls.append(("update", user_id, task_id, index, status)) or task)
    listed = app._dispatch_mia_agent_tool("mia_task_list", {"limit": 5}, owner, owner + ":session")
    updated = app._dispatch_mia_agent_tool("mia_task_update_step", {"task_id": "task-1", "step_index": 0, "status": "completed"}, owner, owner + ":session")
    assert listed["count"] == 1 and listed["tasks"][0]["id"] == "task-1"
    assert updated["task"]["id"] == "task-1"
    assert calls == [("list", owner, 5), ("update", owner, "task-1", 0, "completed")]


def test_mia_agent_task_update_rejects_invalid_step_and_status(app, monkeypatch):
    monkeypatch.setattr(app.cap_services, "update_task_step", lambda *args: (_ for _ in ()).throw(AssertionError("must validate first")))
    with pytest.raises(ValueError):
        app._dispatch_mia_agent_tool("mia_task_update_step", {"task_id": "x", "step_index": True, "status": "completed"}, "owner", "owner:session")
    with pytest.raises(ValueError):
        app._dispatch_mia_agent_tool("mia_task_update_step", {"task_id": "x", "step_index": 0, "status": "invented"}, "owner", "owner:session")


def test_mia_agent_loop_executes_task_tool_then_summarizes_verified_result(app, monkeypatch):
    calls = []
    def fake_completion(messages, model, temperature, max_tokens, **kwargs):
        calls.append((messages, kwargs))
        if len(calls) == 1:
            return {"choices": [{"message": {"role": "assistant", "content": "I'll save the checklist.", "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "mia_task_create", "arguments": json.dumps({"title": "Launch feature", "steps": ["Inspect", "Test"]})}}]}}]}
        assert any(message.get("role") == "tool" and message.get("tool_call_id") == "call-1" and '"id": "task-42"' in message.get("content", "") for message in messages)
        return {"choices": [{"message": {"role": "assistant", "content": "Created the Launch feature checklist with two steps."}}]}
    monkeypatch.setattr(app, "_call_openrouter", fake_completion)
    monkeypatch.setattr(app.cap_services, "list_tasks", lambda owner, limit: {"tasks": [], "count": 0})
    monkeypatch.setattr(app.cap_services, "create_task", lambda owner, title, steps: {"id": "task-42", "title": title, "status": "pending", "steps": [{"title": step, "status": "pending"} for step in steps]})
    result = app._complete_with_mia_agent_tools("system", "Act as an agent and create a task", "owner-7:session-1", "test-model", 0, 200)
    assert result["choices"][0]["message"]["content"].startswith("Created the Launch feature")
    assert len(calls) == 2
    assert calls[0][1]["tools"] == app.MIA_AGENT_TOOLS

def test_agent_run_history_endpoint_is_authenticated_and_owner_scoped(app):
    from fastapi.testclient import TestClient
    client = TestClient(app.app)
    assert client.get('/v1/agent/runs').status_code == 401
    from app import capabilities
    capabilities.create_agent_run('local', 'local:session', 'My private agent request')
    response = client.get('/v1/agent/runs', headers={'X-API-Key': 'test-key'})
    assert response.status_code == 200
    data = response.json()
    assert data['count'] >= 1
    assert any(item['request'] == 'My private agent request' for item in data['runs'])
    assert client.get('/v1/agent/runs?limit=0', headers={'X-API-Key': 'test-key'}).status_code == 400


def test_agent_run_recovery_endpoint_requires_auth_and_is_owner_scoped(app):
    from fastapi.testclient import TestClient
    from app import capabilities
    client = TestClient(app.app)
    assert client.post('/v1/agent/runs/recover').status_code == 401
    run = capabilities.create_agent_run('local', 'local:session', 'Private run')
    response = client.post('/v1/agent/runs/recover?stale_seconds=30', headers={'X-API-Key': 'test-key'})
    assert response.status_code == 400
    response = client.post('/v1/agent/runs/recover?stale_seconds=900', headers={'X-API-Key': 'test-key'})
    assert response.status_code == 200
    assert all(item['status'] != 'running' for item in response.json()['recovered'])
    history = capabilities.list_agent_runs('local')['runs']
    assert next(item for item in history if item['id'] == run['id'])['status'] == 'running'


def test_orphaned_persisted_terminal_approval_cannot_be_executed_after_restart(app):
    from fastapi.testclient import TestClient
    import uuid
    client = TestClient(app.app)
    job_id = uuid.uuid4().hex
    app.cap_services.create_terminal_approval('local', job_id, 'pwd', 'dev-terminal')
    # No in-memory waiter exists, simulating a server restart after request persistence.
    response = client.post(f'/v1/local-terminal/decision/{job_id}', headers={'X-API-Key': 'test-key'}, json={'decision': 'approve'})
    assert response.status_code == 410
    assert app._LOCAL_TERMINAL_QUEUE.empty()
    history = app.cap_services.list_terminal_approvals('local', pending_only=False, limit=50)['jobs']
    item = next(row for row in history if row['id'] == job_id)
    assert item['approval'] == 'denied'
