"""MIA web tool control plane: permissions, approvals, audit trail, and bounded connected actions.

This module deliberately does not import or call the local-terminal bridge. All write
actions are allowlisted, owner-scoped, and executed only after a fresh explicit approval.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import capabilities as cap_services

router = APIRouter(prefix="/v1/tool-center", tags=["tool-center"])

# The registry is code-owned; user settings can only disable or enable these known tools.
REGISTRY = {
    "web_research": {"label": "Web research", "category": "research", "risk": "low", "mode": "read", "description": "Search the public web and return source snippets.", "approval_required": False},
    "document_analysis": {"label": "Document analysis", "category": "documents", "risk": "low", "mode": "read", "description": "Analyze a user-supplied document; never execute embedded content.", "approval_required": False},
    "code_review": {"label": "Code review", "category": "development", "risk": "low", "mode": "read", "description": "Review supplied code without executing it.", "approval_required": False},
    "saved_memory": {"label": "Saved memory", "category": "personalization", "risk": "medium", "mode": "write", "description": "Create or delete explicitly selected saved memories.", "approval_required": True},
    "task_plans": {"label": "Task plans", "category": "planning", "risk": "medium", "mode": "write", "description": "Create and update persistent task plans; does not execute them.", "approval_required": True},
    "github_read": {"label": "GitHub read", "category": "connected services", "risk": "low", "mode": "read", "description": "Read repository metadata, issues, pull requests, and text files for Amir2051-owned repositories.", "approval_required": False},
    "github_create_issue": {"label": "Create GitHub issue", "category": "connected services", "risk": "high", "mode": "external_write", "description": "Create an issue in an allowlisted Amir2051 repository.", "approval_required": True},
    "github_comment_pr": {"label": "Comment on pull request", "category": "connected services", "risk": "high", "mode": "external_write", "description": "Post a comment to a pull request in an allowlisted Amir2051 repository.", "approval_required": True},
    "image_generation": {"label": "Image generation", "category": "creative", "risk": "cost", "mode": "paid_action", "description": "Generate an image through the configured provider; may incur usage charges.", "approval_required": True},
}
DEFAULT_PERMISSIONS = {name: (name not in {"github_create_issue", "github_comment_pr", "image_generation"}) for name in REGISTRY}
HIGH_RISK = {"github_create_issue", "github_comment_pr", "image_generation", "saved_memory", "task_plans"}
OWNER = "Amir2051"


def _conn():
    conn, kind = cap_services._db()
    # _db() has created its base tables; these additions share the same durable store.
    if kind == "postgres":
        conn.execute("""CREATE TABLE IF NOT EXISTS mia_tool_permissions (
            user_id TEXT NOT NULL, tool_id TEXT NOT NULL, enabled BOOLEAN NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(user_id, tool_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS mia_tool_approvals (
            approval_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, tool_id TEXT NOT NULL,
            action JSONB NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
            decided_at DOUBLE PRECISION, result JSONB)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS mia_tool_audit (
            audit_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, tool_id TEXT NOT NULL,
            event TEXT NOT NULL, outcome TEXT NOT NULL, details JSONB NOT NULL,
            created_at DOUBLE PRECISION NOT NULL)""")
    else:
        conn.execute("""CREATE TABLE IF NOT EXISTS mia_tool_permissions (
            user_id TEXT NOT NULL, tool_id TEXT NOT NULL, enabled INTEGER NOT NULL,
            updated_at REAL NOT NULL, PRIMARY KEY(user_id, tool_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS mia_tool_approvals (
            approval_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, tool_id TEXT NOT NULL,
            action TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL,
            created_at REAL NOT NULL, expires_at REAL NOT NULL, decided_at REAL, result TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS mia_tool_audit (
            audit_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, tool_id TEXT NOT NULL,
            event TEXT NOT NULL, outcome TEXT NOT NULL, details TEXT NOT NULL,
            created_at REAL NOT NULL)""")
        conn.commit()
    return conn, kind


def _user_id(user: dict) -> str:
    return str(user.get("id", ""))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _audit(user_id: str, tool_id: str, event: str, outcome: str, details: dict | None = None):
    conn, kind = _conn()
    try:
        values = (uuid.uuid4().hex, user_id, tool_id, event, outcome, _json(details or {}), time.time())
        if kind == "postgres":
            conn.execute("INSERT INTO mia_tool_audit VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s)", values)
        else:
            conn.execute("INSERT INTO mia_tool_audit VALUES(?,?,?,?,?,?,?)", values)
        conn.commit()
    finally:
        conn.close()


def _enabled(user_id: str, tool_id: str) -> bool:
    if tool_id not in REGISTRY:
        return False
    conn, kind = _conn()
    try:
        row = conn.execute(
            "SELECT enabled FROM mia_tool_permissions WHERE user_id=%s AND tool_id=%s" if kind == "postgres" else "SELECT enabled FROM mia_tool_permissions WHERE user_id=? AND tool_id=?",
            (user_id, tool_id),
        ).fetchone()
        return bool(row[0]) if row else bool(DEFAULT_PERMISSIONS.get(tool_id, False))
    finally:
        conn.close()


def _require_enabled(user_id: str, tool_id: str):
    if not _enabled(user_id, tool_id):
        _audit(user_id, tool_id, "tool_invocation", "blocked_permission", {})
        raise HTTPException(status_code=403, detail=f"Tool '{tool_id}' is disabled in your permissions.")


class PermissionUpdate(BaseModel):
    enabled: bool


class ApprovalRequest(BaseModel):
    tool_id: str = Field(min_length=1, max_length=80)
    action: dict = Field()
    summary: str = Field(min_length=1, max_length=500)


class DecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|deny)$")


def _validate_action(tool_id: str, action: dict) -> dict:
    """Normalize actions at the server boundary; never accept arbitrary URLs or API paths."""
    if tool_id == "github_create_issue":
        repo = action.get("repo")
        title = action.get("title")
        body = action.get("body", "")
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo) or repo in {".", ".."}:
            raise HTTPException(status_code=422, detail="repo must be a repository name under Amir2051")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise HTTPException(status_code=422, detail="title must contain 1–200 characters")
        if not isinstance(body, str) or len(body) > 10000:
            raise HTTPException(status_code=422, detail="body must be at most 10,000 characters")
        return {"repo": repo, "title": title.strip(), "body": body}
    if tool_id == "github_comment_pr":
        repo = action.get("repo")
        number = action.get("pr_number")
        body = action.get("body")
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo) or repo in {".", ".."}:
            raise HTTPException(status_code=422, detail="repo must be a repository name under Amir2051")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise HTTPException(status_code=422, detail="pr_number must be a positive integer")
        if not isinstance(body, str) or not body.strip() or len(body) > 10000:
            raise HTTPException(status_code=422, detail="body must contain 1–10,000 characters")
        return {"repo": repo, "pr_number": number, "body": body.strip()}
    if tool_id == "image_generation":
        prompt = action.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
            raise HTTPException(status_code=422, detail="prompt must contain 1–4,000 characters")
        return {"prompt": prompt.strip()}
    if tool_id == "saved_memory":
        op, content = action.get("operation"), action.get("content")
        if op not in {"save", "delete"}:
            raise HTTPException(status_code=422, detail="operation must be save or delete")
        if op == "save" and (not isinstance(content, str) or not content.strip() or len(content) > 2000):
            raise HTTPException(status_code=422, detail="content must contain 1–2,000 characters")
        if op == "delete" and (not isinstance(content, str) or not re.fullmatch(r"[a-f0-9]{32}", content)):
            raise HTTPException(status_code=422, detail="content must be the memory ID to delete")
        return {"operation": op, "content": content.strip()}
    if tool_id == "task_plans":
        title, steps = action.get("title"), action.get("steps")
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            raise HTTPException(status_code=422, detail="title must contain 1–300 characters")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 20 or any(not isinstance(s, str) or not s.strip() or len(s) > 500 for s in steps):
            raise HTTPException(status_code=422, detail="steps must contain 1–20 short text items")
        return {"title": title.strip(), "steps": [s.strip() for s in steps]}
    raise HTTPException(status_code=422, detail="This tool does not accept approval-gated actions.")


def _github_write(path: str, payload: dict) -> dict:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=503, detail="GitHub write actions are not configured. Add a server-side GITHUB_TOKEN with minimal repository issue/PR-comment permissions.")
    # URL is constructed exclusively from validated repository names and fixed endpoints.
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
    url = "https://api.github.com" + path
    req = Request(url, data=_json(payload).encode(), headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "MIA-RZ-Tool-Center",
    }, method="POST")
    try:
        with urlopen(req, timeout=15) as response:
            raw = response.read(100001)
            if len(raw) > 100000:
                raise HTTPException(status_code=502, detail="GitHub response exceeded the safety limit")
            data = json.loads(raw.decode())
            return {k: data.get(k) for k in ("number", "title", "html_url", "state", "id")}
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise HTTPException(status_code=502, detail=f"GitHub rejected the action (HTTP {exc.code}); verify the token's minimal repository permissions.") from None
        if exc.code == 404:
            raise HTTPException(status_code=404, detail="Repository or pull request was not found or is not accessible.") from None
        raise HTTPException(status_code=502, detail=f"GitHub action failed with HTTP {exc.code}.") from None
    except URLError:
        raise HTTPException(status_code=502, detail="Could not connect to GitHub.") from None


def _execute(user_id: str, tool_id: str, action: dict) -> dict:
    if tool_id == "github_create_issue":
        return _github_write(f"/repos/{OWNER}/{action['repo']}/issues", {"title": action["title"], "body": action["body"]})
    if tool_id == "github_comment_pr":
        return _github_write(f"/repos/{OWNER}/{action['repo']}/issues/{action['pr_number']}/comments", {"body": action["body"]})
    if tool_id == "image_generation":
        # This calls the existing bounded image provider service, not a second model/chat route.
        from app.main import _generate_image
        return _generate_image(action["prompt"])
    if tool_id == "saved_memory":
        if action["operation"] == "save":
            return cap_services.save_memory(user_id, action["content"])
        deleted = cap_services.delete_memory(user_id, action["content"])
        return {"deleted": deleted, "memory_id": action["content"]}
    if tool_id == "task_plans":
        return cap_services.create_task(user_id, action["title"], action["steps"])
    raise HTTPException(status_code=422, detail="Unknown or unsupported tool action")


@router.get("/registry")
def registry(current_user: dict = Depends(__import__("app.main", fromlist=["require_app_user"]).require_app_user)):
    user_id = _user_id(current_user)
    tools = []
    for tool_id, meta in REGISTRY.items():
        tools.append({"id": tool_id, **meta, "enabled": _enabled(user_id, tool_id)})
    _audit(user_id, "tool_center", "registry_view", "ok", {"tool_count": len(tools)})
    return {"tools": tools, "approval_ttl_seconds": 600}


@router.put("/permissions/{tool_id}")
def set_permission(tool_id: str, body: PermissionUpdate, current_user: dict = Depends(__import__("app.main", fromlist=["require_app_user"]).require_app_user)):
    user_id = _user_id(current_user)
    if tool_id not in REGISTRY:
        raise HTTPException(status_code=404, detail="Unknown tool")
    # Do not let a permission toggle bypass approval: this only enables/disables invocation.
    conn, kind = _conn()
    now = time.time()
    try:
        values = (user_id, tool_id, body.enabled, now)
        if kind == "postgres":
            conn.execute("""INSERT INTO mia_tool_permissions(user_id,tool_id,enabled,updated_at) VALUES(%s,%s,%s,%s)
                ON CONFLICT(user_id,tool_id) DO UPDATE SET enabled=EXCLUDED.enabled,updated_at=EXCLUDED.updated_at""", values)
        else:
            conn.execute("""INSERT INTO mia_tool_permissions(user_id,tool_id,enabled,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(user_id,tool_id) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at""", values)
        conn.commit()
    finally:
        conn.close()
    _audit(user_id, tool_id, "permission_changed", "enabled" if body.enabled else "disabled", {})
    return {"tool_id": tool_id, "enabled": body.enabled}


@router.get("/audit")
def audit(limit: int = 50, current_user: dict = Depends(__import__("app.main", fromlist=["require_app_user"]).require_app_user)):
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="limit must be 1–200")
    user_id = _user_id(current_user)
    conn, kind = _conn()
    try:
        rows = conn.execute(
            "SELECT audit_id,tool_id,event,outcome,details,created_at FROM mia_tool_audit WHERE user_id=%s ORDER BY created_at DESC LIMIT %s" if kind == "postgres" else "SELECT audit_id,tool_id,event,outcome,details,created_at FROM mia_tool_audit WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        details = row[4]
        if isinstance(details, str):
            try: details = json.loads(details)
            except ValueError: details = {}
        items.append({"id": row[0], "tool_id": row[1], "event": row[2], "outcome": row[3], "details": details, "created_at": row[5]})
    return {"events": items}


@router.get("/approvals")
def approvals(current_user: dict = Depends(__import__("app.main", fromlist=["require_app_user"]).require_app_user)):
    user_id = _user_id(current_user)
    conn, kind = _conn()
    try:
        rows = conn.execute(
            "SELECT approval_id,tool_id,action,summary,status,created_at,expires_at FROM mia_tool_approvals WHERE user_id=%s ORDER BY created_at DESC LIMIT 100" if kind == "postgres" else "SELECT approval_id,tool_id,action,summary,status,created_at,expires_at FROM mia_tool_approvals WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    now = time.time()
    items = []
    for row in rows:
        action = row[2]
        if isinstance(action, str):
            try: action = json.loads(action)
            except ValueError: action = {}
        status = row[4]
        if status == "pending" and row[6] < now:
            status = "expired"
        items.append({"id": row[0], "tool_id": row[1], "action": action, "summary": row[3], "status": status, "created_at": row[5], "expires_at": row[6]})
    return {"approvals": items}


@router.post("/approvals")
def request_approval(body: ApprovalRequest, current_user: dict = Depends(__import__("app.main", fromlist=["require_app_user"]).require_app_user)):
    user_id = _user_id(current_user)
    tool_id = body.tool_id
    if tool_id not in HIGH_RISK or tool_id not in REGISTRY:
        raise HTTPException(status_code=422, detail="Only approval-gated tools can be requested here.")
    _require_enabled(user_id, tool_id)
    action = _validate_action(tool_id, body.action)
    approval_id, now = uuid.uuid4().hex, time.time()
    summary = body.summary.strip()
    conn, kind = _conn()
    try:
        if kind == "postgres":
            conn.execute("INSERT INTO mia_tool_approvals VALUES(%s,%s,%s,%s::jsonb,%s,'pending',%s,%s,NULL,NULL)", (approval_id, user_id, tool_id, _json(action), summary, now, now + 600))
        else:
            conn.execute("INSERT INTO mia_tool_approvals VALUES(?,?,?,?,?,'pending',?,?,NULL,NULL)", (approval_id, user_id, tool_id, _json(action), summary, now, now + 600))
        conn.commit()
    finally:
        conn.close()
    _audit(user_id, tool_id, "approval_requested", "pending", {"approval_id": approval_id, "summary": summary})
    return {"approval_id": approval_id, "status": "pending", "expires_at": now + 600, "summary": summary}


@router.post("/approvals/{approval_id}/decision")
def decide_approval(approval_id: str, body: DecisionRequest, current_user: dict = Depends(__import__("app.main", fromlist=["require_app_user"]).require_app_user)):
    if not re.fullmatch(r"[a-f0-9]{32}", approval_id):
        raise HTTPException(status_code=400, detail="Invalid approval id")
    user_id, now = _user_id(current_user), time.time()
    conn, kind = _conn()
    try:
        row = conn.execute(
            "SELECT tool_id,action,summary,status,expires_at FROM mia_tool_approvals WHERE user_id=%s AND approval_id=%s" if kind == "postgres" else "SELECT tool_id,action,summary,status,expires_at FROM mia_tool_approvals WHERE user_id=? AND approval_id=?",
            (user_id, approval_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Approval not found")
        tool_id, action, summary, status, expires_at = row
        if status != "pending":
            raise HTTPException(status_code=409, detail="Approval has already been decided")
        if expires_at < now:
            conn.execute("UPDATE mia_tool_approvals SET status='expired',decided_at=%s WHERE approval_id=%s" if kind == "postgres" else "UPDATE mia_tool_approvals SET status='expired',decided_at=? WHERE approval_id=?", (now, approval_id))
            conn.commit()
            _audit(user_id, tool_id, "approval_decision", "expired", {"approval_id": approval_id})
            raise HTTPException(status_code=410, detail="Approval expired; request a new one")
        if body.decision == "deny":
            conn.execute("UPDATE mia_tool_approvals SET status='denied',decided_at=%s WHERE approval_id=%s" if kind == "postgres" else "UPDATE mia_tool_approvals SET status='denied',decided_at=? WHERE approval_id=?", (now, approval_id))
            conn.commit()
            _audit(user_id, tool_id, "approval_decision", "denied", {"approval_id": approval_id})
            return {"status": "denied", "approval_id": approval_id}
        if isinstance(action, str):
            action = json.loads(action)
        # Claim the approval atomically before executing to prevent duplicate side effects.
        cursor = conn.execute(
            "UPDATE mia_tool_approvals SET status='executing',decided_at=%s WHERE user_id=%s AND approval_id=%s AND status='pending'" if kind == "postgres" else "UPDATE mia_tool_approvals SET status='executing',decided_at=? WHERE user_id=? AND approval_id=? AND status='pending'",
            (now, user_id, approval_id),
        )
        conn.commit()
        if cursor.rowcount != 1:
            raise HTTPException(status_code=409, detail="Approval was already claimed")
    finally:
        conn.close()
    _audit(user_id, tool_id, "approval_decision", "approved", {"approval_id": approval_id})
    try:
        if not _enabled(user_id, tool_id):
            raise HTTPException(status_code=403, detail="Tool was disabled before execution.")
        result = _execute(user_id, tool_id, action)
        outcome = {"ok": True, "result": result}
        final_status = "completed"
        _audit(user_id, tool_id, "action_execution", "completed", {"approval_id": approval_id, "result_summary": {k: result.get(k) for k in ("id", "number", "title", "html_url", "deleted") if isinstance(result, dict) and k in result}})
    except HTTPException as exc:
        outcome = {"ok": False, "error": str(exc.detail)[:500]}
        final_status = "failed"
        _audit(user_id, tool_id, "action_execution", "failed", {"approval_id": approval_id, "status_code": exc.status_code})
    except Exception as exc:
        outcome = {"ok": False, "error": f"Action failed ({type(exc).__name__})."}
        final_status = "failed"
        _audit(user_id, tool_id, "action_execution", "failed", {"approval_id": approval_id, "error_type": type(exc).__name__})
    conn, kind = _conn()
    try:
        if kind == "postgres":
            conn.execute("UPDATE mia_tool_approvals SET status=%s,result=%s::jsonb WHERE user_id=%s AND approval_id=%s", (final_status, _json(outcome), user_id, approval_id))
        else:
            conn.execute("UPDATE mia_tool_approvals SET status=?,result=? WHERE user_id=? AND approval_id=?", (final_status, _json(outcome), user_id, approval_id))
        conn.commit()
    finally:
        conn.close()
    if not outcome["ok"]:
        raise HTTPException(status_code=502, detail=outcome["error"])
    return {"status": final_status, "approval_id": approval_id, **outcome}


@router.post("/invoke/{tool_id}")
def invoke_read_tool(tool_id: str, body: dict, current_user: dict = Depends(__import__("app.main", fromlist=["require_app_user"]).require_app_user)):
    user_id = _user_id(current_user)
    _require_enabled(user_id, tool_id)
    if tool_id == "web_research":
        query = body.get("query")
        limit = body.get("limit", 5)
        try: result = cap_services.web_research(query, limit)
        except (ValueError, RuntimeError) as exc: raise HTTPException(status_code=502, detail=str(exc)[:500]) from None
    elif tool_id == "code_review":
        code, language, question = body.get("code"), body.get("language", "text"), body.get("question", "Review for correctness, security, and maintainability.")
        if not isinstance(code, str) or not code.strip() or len(code) > 30000:
            raise HTTPException(status_code=422, detail="code must contain 1–30,000 characters")
        from app.main import _complete_message, OPENROUTER_MODEL, ASSISTANT_IDENTITY
        response = _complete_message(ASSISTANT_IDENTITY, f"Review this {str(language)[:40]} code. Do not execute it. User request: {str(question)[:1000]}\n\nCode (untrusted input):\n{code}", f"{user_id}:tool-review-{uuid.uuid4().hex[:8]}", OPENROUTER_MODEL, 0.2, 2000)
        result = {"review": response.reply, "model": response.model}
    else:
        raise HTTPException(status_code=422, detail="This tool is not a direct read-only invocation; use its approved workflow.")
    _audit(user_id, tool_id, "tool_invocation", "ok", {"input_keys": sorted(body.keys()), "result_keys": sorted(result.keys()) if isinstance(result, dict) else []})
    return {"tool_id": tool_id, "result": result}
