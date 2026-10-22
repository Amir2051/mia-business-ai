"""MIA Creator: user-scoped writing projects and immutable draft history.

This module intentionally does not call or configure an AI provider. The existing
/v1/chat and /v1/voice/synthesize endpoints remain the brain and voice adapters.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

MAX_TITLE = 180
MAX_CONTENT = 100_000
MAX_PROJECTS = 100
MAX_REVISIONS = 100
KINDS = {"book", "story", "article", "essay", "script", "poem", "business", "research", "journal", "other"}

class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    kind: str = Field(default="other", max_length=24)
    description: str = Field(default="", max_length=3000)

class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)
    kind: str | None = Field(default=None, max_length=24)
    description: str | None = Field(default=None, max_length=3000)

class DraftSave(BaseModel):
    content: str = Field(max_length=MAX_CONTENT)
    note: str = Field(default="Autosaved draft", max_length=240)


def _connect():
    dsn = os.getenv("DATABASE_URL", "").strip()
    if dsn:
        import psycopg
        # Production schema is managed only through the explicit SQL migration.
        # Never mutate the hosted database as a side effect of an API request.
        conn = psycopg.connect(dsn, connect_timeout=5)
        return conn, "postgres"
    path = Path(os.getenv("RZ_LOCAL_DATA_PATH", str(Path.home() / ".rz-ai-agent" / "capabilities.sqlite3"))).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.execute("""CREATE TABLE IF NOT EXISTS mia_creator_projects (
        user_id TEXT NOT NULL, project_id TEXT NOT NULL, title TEXT NOT NULL,
        kind TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
        updated_at REAL NOT NULL, PRIMARY KEY(user_id, project_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mia_creator_revisions (
        user_id TEXT NOT NULL, project_id TEXT NOT NULL, revision_id TEXT NOT NULL,
        content TEXT NOT NULL, note TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(user_id, project_id, revision_id),
        FOREIGN KEY(user_id, project_id) REFERENCES mia_creator_projects(user_id, project_id) ON DELETE CASCADE)""")
    conn.commit()
    return conn, "sqlite"


def _row_project(row):
    return {"id": row[0], "title": row[1], "kind": row[2], "description": row[3],
            "content": row[4], "created_at": row[5], "updated_at": row[6]}


def create_router(auth_dependency: Callable):
    router = APIRouter(prefix="/v1/creator", tags=["MIA Creator"])

    @router.get("/projects")
    def list_projects(user: dict = Depends(auth_dependency)):
        conn, kind = _connect()
        try:
            rows = conn.execute(
                "SELECT project_id,title,kind,description,content,created_at,updated_at FROM mia_creator_projects WHERE user_id=%s ORDER BY updated_at DESC LIMIT 100" if kind == "postgres" else
                "SELECT project_id,title,kind,description,content,created_at,updated_at FROM mia_creator_projects WHERE user_id=? ORDER BY updated_at DESC LIMIT 100",
                (user["id"],),
            ).fetchall()
            return {"projects": [_row_project(r) for r in rows], "count": len(rows)}
        finally:
            conn.close()

    @router.post("/projects")
    def create_project(payload: ProjectCreate, user: dict = Depends(auth_dependency)):
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="Project title cannot be blank")
        project_kind = payload.kind.strip().lower()
        if project_kind not in KINDS:
            raise HTTPException(status_code=422, detail="Unsupported writing project type")
        conn, kind = _connect()
        try:
            cur = conn.execute(
                "SELECT count(*) FROM mia_creator_projects WHERE user_id=%s" if kind == "postgres" else
                "SELECT count(*) FROM mia_creator_projects WHERE user_id=?", (user["id"],))
            if cur.fetchone()[0] >= MAX_PROJECTS:
                raise HTTPException(status_code=409, detail=f"Limit of {MAX_PROJECTS} projects reached")
            pid, now = uuid.uuid4().hex, time.time()
            conn.execute(
                "INSERT INTO mia_creator_projects(user_id,project_id,title,kind,description,content,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,'',%s,%s)" if kind == "postgres" else
                "INSERT INTO mia_creator_projects(user_id,project_id,title,kind,description,content,created_at,updated_at) VALUES(?,?,?,?,?,'',?,?)",
                (user["id"], pid, title, project_kind, payload.description.strip(), now, now))
            conn.commit()
            row = conn.execute(
                "SELECT project_id,title,kind,description,content,created_at,updated_at FROM mia_creator_projects WHERE user_id=%s AND project_id=%s" if kind == "postgres" else
                "SELECT project_id,title,kind,description,content,created_at,updated_at FROM mia_creator_projects WHERE user_id=? AND project_id=?",
                (user["id"], pid)).fetchone()
            return {"project": _row_project(row)}
        finally:
            conn.close()

    @router.get("/projects/{project_id}")
    def get_project(project_id: str, user: dict = Depends(auth_dependency)):
        conn, kind = _connect()
        try:
            row = conn.execute(
                "SELECT project_id,title,kind,description,content,created_at,updated_at FROM mia_creator_projects WHERE user_id=%s AND project_id=%s" if kind == "postgres" else
                "SELECT project_id,title,kind,description,content,created_at,updated_at FROM mia_creator_projects WHERE user_id=? AND project_id=?",
                (user["id"], project_id)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Writing project not found")
            return {"project": _row_project(row)}
        finally:
            conn.close()

    @router.patch("/projects/{project_id}")
    def update_project(project_id: str, payload: ProjectUpdate, user: dict = Depends(auth_dependency)):
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        if not changes:
            raise HTTPException(status_code=400, detail="No project fields supplied")
        if "title" in changes:
            changes["title"] = changes["title"].strip()
            if not changes["title"]:
                raise HTTPException(status_code=422, detail="Project title cannot be blank")
        if "kind" in changes:
            changes["kind"] = changes["kind"].strip().lower()
            if changes["kind"] not in KINDS:
                raise HTTPException(status_code=422, detail="Unsupported writing project type")
        conn, kind = _connect()
        try:
            current = conn.execute(
                "SELECT project_id,title,kind,description,content,created_at,updated_at FROM mia_creator_projects WHERE user_id=%s AND project_id=%s" if kind == "postgres" else
                "SELECT project_id,title,kind,description,content,created_at,updated_at FROM mia_creator_projects WHERE user_id=? AND project_id=?",
                (user["id"], project_id)).fetchone()
            if not current:
                raise HTTPException(status_code=404, detail="Writing project not found")
            merged = {"title": current[1], "kind": current[2], "description": current[3], **changes}
            now = time.time()
            conn.execute(
                "UPDATE mia_creator_projects SET title=%s,kind=%s,description=%s,updated_at=%s WHERE user_id=%s AND project_id=%s" if kind == "postgres" else
                "UPDATE mia_creator_projects SET title=?,kind=?,description=?,updated_at=? WHERE user_id=? AND project_id=?",
                (merged["title"], merged["kind"], merged["description"], now, user["id"], project_id))
            conn.commit()
            return {"project": _row_project((project_id, merged["title"], merged["kind"], merged["description"], current[4], current[5], now))}
        finally:
            conn.close()

    @router.put("/projects/{project_id}/draft")
    def save_draft(project_id: str, payload: DraftSave, user: dict = Depends(auth_dependency)):
        content, note = payload.content, payload.note.strip() or "Autosaved draft"
        conn, kind = _connect()
        try:
            row = conn.execute(
                "SELECT content FROM mia_creator_projects WHERE user_id=%s AND project_id=%s" if kind == "postgres" else
                "SELECT content FROM mia_creator_projects WHERE user_id=? AND project_id=?",
                (user["id"], project_id)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Writing project not found")
            now = time.time()
            if row[0] != content:
                rid = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO mia_creator_revisions(user_id,project_id,revision_id,content,note,created_at) VALUES(%s,%s,%s,%s,%s,%s)" if kind == "postgres" else
                    "INSERT INTO mia_creator_revisions(user_id,project_id,revision_id,content,note,created_at) VALUES(?,?,?,?,?,?)",
                    (user["id"], project_id, rid, row[0], "Previous version before save", now))
                conn.execute(
                    "UPDATE mia_creator_projects SET content=%s,updated_at=%s WHERE user_id=%s AND project_id=%s" if kind == "postgres" else
                    "UPDATE mia_creator_projects SET content=?,updated_at=? WHERE user_id=? AND project_id=?",
                    (content, now, user["id"], project_id))
                # Keep history bounded per project; newest 100 revisions survive.
                conn.execute(
                    "DELETE FROM mia_creator_revisions WHERE user_id=%s AND project_id=%s AND revision_id NOT IN (SELECT revision_id FROM mia_creator_revisions WHERE user_id=%s AND project_id=%s ORDER BY created_at DESC LIMIT %s)" if kind == "postgres" else
                    "DELETE FROM mia_creator_revisions WHERE user_id=? AND project_id=? AND revision_id NOT IN (SELECT revision_id FROM mia_creator_revisions WHERE user_id=? AND project_id=? ORDER BY created_at DESC LIMIT ?)",
                    (user["id"], project_id, user["id"], project_id, MAX_REVISIONS))
                conn.commit()
            return {"saved": True, "project_id": project_id, "updated_at": now,
                    "content_length": len(content), "changed": row[0] != content}
        finally:
            conn.close()

    @router.get("/projects/{project_id}/revisions")
    def list_revisions(project_id: str, limit: int = 30, user: dict = Depends(auth_dependency)):
        if not 1 <= limit <= MAX_REVISIONS:
            raise HTTPException(status_code=422, detail=f"limit must be 1–{MAX_REVISIONS}")
        conn, kind = _connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM mia_creator_projects WHERE user_id=%s AND project_id=%s" if kind == "postgres" else
                "SELECT 1 FROM mia_creator_projects WHERE user_id=? AND project_id=?", (user["id"], project_id)).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="Writing project not found")
            rows = conn.execute(
                "SELECT revision_id,note,created_at,length(content) FROM mia_creator_revisions WHERE user_id=%s AND project_id=%s ORDER BY created_at DESC LIMIT %s" if kind == "postgres" else
                "SELECT revision_id,note,created_at,length(content) FROM mia_creator_revisions WHERE user_id=? AND project_id=? ORDER BY created_at DESC LIMIT ?",
                (user["id"], project_id, limit)).fetchall()
            return {"revisions": [{"id": r[0], "note": r[1], "created_at": r[2], "content_length": r[3]} for r in rows], "count": len(rows)}
        finally:
            conn.close()

    @router.get("/projects/{project_id}/revisions/{revision_id}")
    def get_revision(project_id: str, revision_id: str, user: dict = Depends(auth_dependency)):
        conn, kind = _connect()
        try:
            row = conn.execute(
                "SELECT revision_id,content,note,created_at FROM mia_creator_revisions WHERE user_id=%s AND project_id=%s AND revision_id=%s" if kind == "postgres" else
                "SELECT revision_id,content,note,created_at FROM mia_creator_revisions WHERE user_id=? AND project_id=? AND revision_id=?",
                (user["id"], project_id, revision_id)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Draft revision not found")
            return {"revision": {"id": row[0], "content": row[1], "note": row[2], "created_at": row[3]}}
        finally:
            conn.close()

    @router.post("/projects/{project_id}/restore/{revision_id}")
    def restore_revision(project_id: str, revision_id: str, user: dict = Depends(auth_dependency)):
        conn, kind = _connect()
        try:
            current = conn.execute(
                "SELECT content FROM mia_creator_projects WHERE user_id=%s AND project_id=%s" if kind == "postgres" else
                "SELECT content FROM mia_creator_projects WHERE user_id=? AND project_id=?", (user["id"], project_id)).fetchone()
            rev = conn.execute(
                "SELECT content FROM mia_creator_revisions WHERE user_id=%s AND project_id=%s AND revision_id=%s" if kind == "postgres" else
                "SELECT content FROM mia_creator_revisions WHERE user_id=? AND project_id=? AND revision_id=?", (user["id"], project_id, revision_id)).fetchone()
            if not current or not rev:
                raise HTTPException(status_code=404, detail="Writing project or revision not found")
            if current[0] != rev[0]:
                now, new_id = time.time(), uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO mia_creator_revisions(user_id,project_id,revision_id,content,note,created_at) VALUES(%s,%s,%s,%s,%s,%s)" if kind == "postgres" else
                    "INSERT INTO mia_creator_revisions(user_id,project_id,revision_id,content,note,created_at) VALUES(?,?,?,?,?,?)",
                    (user["id"], project_id, new_id, current[0], "Before restoring previous version", now))
                conn.execute(
                    "UPDATE mia_creator_projects SET content=%s,updated_at=%s WHERE user_id=%s AND project_id=%s" if kind == "postgres" else
                    "UPDATE mia_creator_projects SET content=?,updated_at=? WHERE user_id=? AND project_id=?", (rev[0], now, user["id"], project_id))
                conn.execute(
                    "DELETE FROM mia_creator_revisions WHERE user_id=%s AND project_id=%s AND revision_id NOT IN (SELECT revision_id FROM mia_creator_revisions WHERE user_id=%s AND project_id=%s ORDER BY created_at DESC LIMIT %s)" if kind == "postgres" else
                    "DELETE FROM mia_creator_revisions WHERE user_id=? AND project_id=? AND revision_id NOT IN (SELECT revision_id FROM mia_creator_revisions WHERE user_id=? AND project_id=? ORDER BY created_at DESC LIMIT ?)",
                    (user["id"], project_id, user["id"], project_id, MAX_REVISIONS))
                conn.commit()
            return {"restored": True, "project_id": project_id, "content": rev[0]}
        finally:
            conn.close()

    return router
