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
    monkeypatch.setattr(app, "_request_local_terminal_command", lambda command, workspace: {
        "status": "completed", "output": "actual local output"
    })
    result = app._complete_with_local_terminal_tools(
        app.ASSISTANT_IDENTITY, "run a command on my Kali machine", "test-user:session", None, None, 500
    )
    assert result["choices"][0]["message"]["content"].startswith("The local command")
    assert calls[0]["tool_choice"] == "required"
    assert calls[0]["tools"][0]["function"]["name"] == "mia_request_local_command"
