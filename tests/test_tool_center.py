import base64
import json
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(tmp_path, monkeypatch):
    import os, sys
    monkeypatch.setenv("RZ_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-provider-key")
    monkeypatch.setenv("RZ_LOCAL_DATA_PATH", str(tmp_path / "tool-center.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.delenv("IMAGE_GENERATION_ENDPOINT", raising=False)
    monkeypatch.delenv("IMAGE_GENERATION_API_KEY", raising=False)
    monkeypatch.delenv("IMAGE_GENERATION_MODEL", raising=False)
    sys.modules.pop("app.main", None)
    import app.main as module
    yield module
    with module._store_lock:
        module._conversations.clear()
    sys.modules.pop("app.main", None)


@pytest.fixture
def client(app):
    return TestClient(app.app), {"X-API-Key": "test-key"}


def test_tool_center_requires_auth_and_permissions_are_per_user(client):
    c, headers = client
    assert c.get("/v1/tool-center/registry").status_code == 401
    registry = c.get("/v1/tool-center/registry", headers=headers)
    assert registry.status_code == 200
    assert any(item["id"] == "document_analysis" for item in registry.json()["tools"])
    disabled = c.put("/v1/tool-center/permissions/web_research", headers=headers, json={"enabled": False})
    assert disabled.status_code == 200
    blocked = c.post("/v1/tool-center/invoke/web_research", headers=headers, json={"query": "test"})
    assert blocked.status_code == 403
    assert c.get("/v1/tool-center/audit", headers=headers).json()["events"]


def test_document_analysis_extracts_only_supported_uploads(client, monkeypatch):
    c, headers = client    monkeypatch.setattr("app.capabilities.extract_document", lambda filename, encoded: {
        "filename": filename, "text": "A short test document.", "characters": 22,
        "truncated": False, "format": "txt"
    })
    class Response:
        reply = "Summary: a short test document."
        model = "test-model"
    monkeypatch.setattr("app.main._complete_message", lambda *args, **kwargs: Response())
    response = c.post("/v1/tool-center/invoke/document_analysis", headers=headers, json={
        "filename": "notes.txt", "content_base64": base64.b64encode(b"notes").decode(),
        "question": "Summarize"
    })
    assert response.status_code == 200
    assert response.json()["result"]["analysis"].startswith("Summary")
    bad = c.post("/v1/tool-center/invoke/document_analysis", headers=headers, json={
        "filename": "../secret.txt", "content_base64": "bm90ZXM="
    })
    assert bad.status_code == 422


def test_unwired_image_generation_cannot_be_approved(client):
    c, headers = client
    response = c.post("/v1/tool-center/approvals", headers=headers, json={
        "tool_id": "image_generation", "action": {"prompt": "draw something"},
        "summary": "Generate an image"
    })
    assert response.status_code == 503


def test_approval_is_owner_scoped_and_duplicate_decision_blocked(client):
    c, headers = client
    requested = c.post("/v1/tool-center/approvals", headers=headers, json={
        "tool_id": "saved_memory", "action": {"operation": "save", "content": "test memory"},
        "summary": "Save a test memory"
    })
    assert requested.status_code == 200
    approval_id = requested.json()["approval_id"]
    denied = c.post(f"/v1/tool-center/approvals/{approval_id}/decision", headers=headers, json={"decision": "deny"})
    assert denied.status_code == 200
    duplicate = c.post(f"/v1/tool-center/approvals/{approval_id}/decision", headers=headers, json={"decision": "approve"})
    assert duplicate.status_code == 409
    approvals = c.get("/v1/tool-center/approvals", headers=headers).json()["approvals"]
    assert next(x for x in approvals if x["id"] == approval_id)["status"] == "denied"


def test_approved_memory_action_executes_once_and_is_audited(client):
    c, headers = client
    requested = c.post("/v1/tool-center/approvals", headers=headers, json={
        "tool_id": "saved_memory", "action": {"operation": "save", "content": "approval-gated memory"},
        "summary": "Save memory after approval"
    })
    assert requested.status_code == 200
    approval_id = requested.json()["approval_id"]
    result = c.post(f"/v1/tool-center/approvals/{approval_id}/decision", headers=headers, json={"decision": "approve"})
    assert result.status_code == 200
    assert result.json()["status"] == "completed"
    duplicate = c.post(f"/v1/tool-center/approvals/{approval_id}/decision", headers=headers, json={"decision": "approve"})
    assert duplicate.status_code == 409
    events = c.get("/v1/tool-center/audit", headers=headers).json()["events"]
    assert any(e["event"] == "action_execution" and e["outcome"] == "completed" for e in events)
