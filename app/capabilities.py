"""Bounded capability services for RZ Agent.

External actions are explicit, authenticated HTTP operations. No arbitrary shell,
filesystem, or write-capable GitHub tool is exposed through this registry.
"""
from __future__ import annotations

import json
import base64
import io
import zipfile
import xml.etree.ElementTree as ET
import os
import re
import sqlite3
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

MAX_QUERY = 500
MAX_RESULTS = 8
MAX_MEMORY = 2000
MAX_TASKS_PER_USER = 50

class _DDGParser(HTMLParser):
    """Extract complete DuckDuckGo result records without dropping snippets."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.current = None
        self.capture = None
        self.text = []

    def _finish_result(self):
        if self.current and self.current.get("title") and self.current.get("url"):
            url = self.current["url"].strip()
            # DuckDuckGo may wrap destinations in relative or absolute /l/?uddg= redirects.
            from urllib.parse import parse_qs, urljoin, urlparse
            if url.startswith("/") or url.startswith("//"):
                url = urljoin("https://duckduckgo.com", url)
            parsed = urlparse(url)
            if parsed.hostname in {"duckduckgo.com", "www.duckduckgo.com"} and parsed.path == "/l/":
                url = parse_qs(parsed.query).get("uddg", [""])[0]
            if url.startswith("https://") and not any(item["url"] == url for item in self.items):
                self.current["url"] = url[:2000]
                self.current["title"] = self.current["title"][:500]
                self.current["snippet"] = self.current.get("snippet", "")[:1200]
                self.items.append(self.current)
        self.current = None
        self.capture = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").split())
        if tag == "a" and "result__a" in classes:
            self._finish_result()
            self.current = {"title": "", "url": attrs.get("href", ""), "snippet": ""}
            self.capture = "title"
            self.text = []
        elif self.current and "result__snippet" in classes and tag in {"a", "div"}:
            self.capture = "snippet"
            self.text = []

    def handle_data(self, data):
        if self.capture:
            self.text.append(data)

    def handle_endtag(self, tag):
        if not self.capture:
            return
        if (self.capture == "title" and tag == "a") or (self.capture == "snippet" and tag in {"a", "div"}):
            value = " ".join(" ".join(self.text).split())
            if self.current is not None:
                self.current[self.capture] = value
            self.capture = None
            self.text = []

    def finish(self):
        self._finish_result()

def web_research(query: str, limit: int = 5) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY:
        raise ValueError(f"query must contain 1–{MAX_QUERY} characters")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"limit must be from 1 to {MAX_RESULTS}")
    brave_key = os.getenv('BRAVE_SEARCH_API_KEY', '').strip()
    if brave_key:
        url = 'https://api.search.brave.com/res/v1/web/search?q=' + quote_plus(query.strip()) + '&count=' + str(limit)
        req = Request(url, headers={'Accept': 'application/json', 'X-Subscription-Token': brave_key, 'User-Agent': 'RZ-Agent/1.0'}, method='GET')
        try:
            with urlopen(req, timeout=12) as response: raw = response.read(750_001)
            if len(raw) > 750_000: raise ValueError('Search response exceeded the size limit')
            data = json.loads(raw.decode('utf-8'))
            web = data.get('web', {}).get('results', []) if isinstance(data, dict) else []
            results = [{'title': str(item.get('title',''))[:500], 'url': str(item.get('url',''))[:2000], 'snippet': str(item.get('description',''))[:1200]} for item in web[:limit] if isinstance(item,dict) and str(item.get('url','')).startswith('https://')]
            if not results: raise RuntimeError('Search provider returned no usable results')
            return {'query': query.strip(), 'results': results, 'count': len(results), 'source': 'Brave Search'}
        except HTTPError as exc: raise RuntimeError(f'Brave Search returned HTTP {exc.code}') from None
        except URLError: raise RuntimeError('Brave Search is unreachable') from None
        except (UnicodeDecodeError, json.JSONDecodeError): raise RuntimeError('Brave Search returned invalid JSON') from None
    # Unauthenticated HTML search is attempted only as a convenience; if blocked,
    # return an explicit error rather than pretending research succeeded.
    url = 'https://html.duckduckgo.com/html/?q=' + quote_plus(query.strip())
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; RZ-Agent/1.0)', 'Accept': 'text/html'}, method='GET')
    try:
        with urlopen(req, timeout=12) as response: raw = response.read(750_001)
        if len(raw) > 750_000: raise ValueError('Search response exceeded the size limit')
        parser = _DDGParser(); parser.feed(raw.decode('utf-8', 'replace')); parser.finish()
        results = parser.items[:limit]
        if not results: raise RuntimeError('Unauthenticated web search returned no results. Configure BRAVE_SEARCH_API_KEY to enable reliable web research.')
        return {'query': query.strip(), 'results': results, 'count': len(results), 'source': 'DuckDuckGo HTML'}
    except HTTPError as exc: raise RuntimeError(f'Web search provider returned HTTP {exc.code}') from None
    except URLError: raise RuntimeError('Web search provider is unreachable') from None


def _db():
    """Persistent Postgres when DATABASE_URL exists; local SQLite for development."""
    dsn = os.getenv('DATABASE_URL', '').strip()
    if dsn:
        import psycopg
        conn = psycopg.connect(dsn, connect_timeout=5)
        conn.execute('''CREATE TABLE IF NOT EXISTS rz_agent_memory (
            user_id TEXT NOT NULL, memory_id TEXT NOT NULL, content TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(user_id, memory_id))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS rz_agent_tasks (
            user_id TEXT NOT NULL, task_id TEXT NOT NULL, title TEXT NOT NULL,
            status TEXT NOT NULL, steps JSONB NOT NULL, created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(user_id, task_id))''')
        return conn, 'postgres'
    path = os.getenv('RZ_LOCAL_DATA_PATH', str(Path.home() / '.rz-ai-agent' / 'capabilities.sqlite3'))
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(Path(path).expanduser()), timeout=5)
    conn.execute('''CREATE TABLE IF NOT EXISTS rz_agent_memory (
        user_id TEXT NOT NULL, memory_id TEXT NOT NULL, content TEXT NOT NULL,
        created_at REAL NOT NULL, PRIMARY KEY(user_id, memory_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS rz_agent_tasks (
        user_id TEXT NOT NULL, task_id TEXT NOT NULL, title TEXT NOT NULL,
        status TEXT NOT NULL, steps TEXT NOT NULL, created_at REAL NOT NULL,
        updated_at REAL NOT NULL, PRIMARY KEY(user_id, task_id))''')
    conn.commit()
    return conn, 'sqlite'

def save_memory(user_id: str, content: str) -> dict:
    if not isinstance(content, str) or not content.strip() or len(content) > MAX_MEMORY:
        raise ValueError(f'memory must contain 1–{MAX_MEMORY} characters')
    mid, now = uuid.uuid4().hex, time.time()
    conn, kind = _db()
    try:
        if kind == 'postgres':
            conn.execute('INSERT INTO rz_agent_memory(user_id,memory_id,content,created_at) VALUES(%s,%s,%s,%s)', (user_id, mid, content.strip(), now))
        else:
            conn.execute('INSERT INTO rz_agent_memory VALUES(?,?,?,?)', (user_id, mid, content.strip(), now)); conn.commit()
        if kind == 'postgres': conn.commit()
    finally: conn.close()
    return {'id': mid, 'content': content.strip(), 'created_at': now}

def list_memories(user_id: str, limit: int = 20) -> dict:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError('limit must be from 1 to 50')
    conn, kind = _db()
    try:
        if kind == 'postgres': rows = conn.execute('SELECT memory_id,content,created_at FROM rz_agent_memory WHERE user_id=%s ORDER BY created_at DESC LIMIT %s', (user_id, limit)).fetchall()
        else: rows = conn.execute('SELECT memory_id,content,created_at FROM rz_agent_memory WHERE user_id=? ORDER BY created_at DESC LIMIT ?', (user_id, limit)).fetchall()
    finally: conn.close()
    return {'memories': [{'id': r[0], 'content': r[1], 'created_at': r[2]} for r in rows], 'count': len(rows)}

def delete_memory(user_id: str, memory_id: str) -> bool:
    conn, kind = _db()
    try:
        cur = conn.execute('DELETE FROM rz_agent_memory WHERE user_id=%s AND memory_id=%s' if kind == 'postgres' else 'DELETE FROM rz_agent_memory WHERE user_id=? AND memory_id=?', (user_id, memory_id))
        conn.commit()
        return cur.rowcount > 0
    finally: conn.close()

def create_task(user_id: str, title: str, steps: list[str]) -> dict:
    if not isinstance(title, str) or not title.strip() or len(title) > 300: raise ValueError('title must contain 1–300 characters')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 20 or any(not isinstance(s, str) or not s.strip() or len(s) > 500 for s in steps):
        raise ValueError('steps must contain 1–20 non-empty strings of at most 500 characters')
    task_id, now = uuid.uuid4().hex, time.time()
    normalized = [{'title': s.strip(), 'status': 'pending'} for s in steps]
    conn, kind = _db()
    try:
        if kind == 'postgres': conn.execute('INSERT INTO rz_agent_tasks VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s)', (user_id, task_id, title.strip(), 'pending', json.dumps(normalized), now, now))
        else: conn.execute('INSERT INTO rz_agent_tasks VALUES(?,?,?,?,?,?,?)', (user_id, task_id, title.strip(), 'pending', json.dumps(normalized), now, now)); conn.commit()
        if kind == 'postgres': conn.commit()
    finally: conn.close()
    return {'id': task_id, 'title': title.strip(), 'status': 'pending', 'steps': normalized, 'created_at': now}

def list_tasks(user_id: str, limit: int = 20) -> dict:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50: raise ValueError('limit must be from 1 to 50')
    conn, kind = _db()
    try:
        if kind == 'postgres': rows = conn.execute('SELECT task_id,title,status,steps,created_at,updated_at FROM rz_agent_tasks WHERE user_id=%s ORDER BY updated_at DESC LIMIT %s', (user_id, limit)).fetchall()
        else: rows = conn.execute('SELECT task_id,title,status,steps,created_at,updated_at FROM rz_agent_tasks WHERE user_id=? ORDER BY updated_at DESC LIMIT ?', (user_id, limit)).fetchall()
    finally: conn.close()
    return {'tasks': [{'id':r[0], 'title':r[1], 'status':r[2], 'steps':r[3] if isinstance(r[3], list) else json.loads(r[3]), 'created_at':r[4], 'updated_at':r[5]} for r in rows], 'count':len(rows)}

def update_task_step(user_id: str, task_id: str, index: int, status: str) -> dict:
    if status not in {'pending','in_progress','completed','blocked'}: raise ValueError('invalid step status')
    conn, kind = _db()
    try:
        row = conn.execute('SELECT title,status,steps,created_at FROM rz_agent_tasks WHERE user_id=%s AND task_id=%s' if kind == 'postgres' else 'SELECT title,status,steps,created_at FROM rz_agent_tasks WHERE user_id=? AND task_id=?', (user_id, task_id)).fetchone()
        if not row: raise KeyError('Task not found')
        steps = row[2] if isinstance(row[2], list) else json.loads(row[2])
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(steps): raise ValueError('step index out of range')
        steps[index]['status'] = status
        task_status = 'completed' if all(s['status'] == 'completed' for s in steps) else ('blocked' if any(s['status'] == 'blocked' for s in steps) else 'in_progress')
        now = time.time()
        conn.execute('UPDATE rz_agent_tasks SET steps=%s::jsonb,status=%s,updated_at=%s WHERE user_id=%s AND task_id=%s' if kind == 'postgres' else 'UPDATE rz_agent_tasks SET steps=?,status=?,updated_at=? WHERE user_id=? AND task_id=?', (json.dumps(steps), task_status, now, user_id, task_id))
        conn.commit() if kind == 'postgres' else None
        if kind == 'sqlite': conn.commit()
        return {'id': task_id, 'title': row[0], 'status': task_status, 'steps': steps, 'created_at': row[3], 'updated_at': now}
    finally: conn.close()


def extract_document(filename: str, content_base64: str) -> dict:
    """Extract bounded text from txt/md/code, DOCX, and PDF uploads."""
    if not isinstance(filename, str) or not filename or len(filename) > 200 or Path(filename).name != filename:
        raise ValueError('filename must be a simple filename, not a path')
    if not isinstance(content_base64, str) or len(content_base64) > 4_000_000:
        raise ValueError('document payload exceeds the 3 MB upload limit')
    try: raw = base64.b64decode(content_base64, validate=True)
    except (ValueError, base64.binascii.Error): raise ValueError('content_base64 must be valid base64') from None
    if not raw or len(raw) > 3_000_000: raise ValueError('document must be between 1 byte and 3 MB')
    suffix = Path(filename).suffix.lower()
    if suffix in {'.txt','.md','.py','.js','.ts','.tsx','.jsx','.json','.csv','.yaml','.yml','.html','.css','.sql','.log'}:
        try: text = raw.decode('utf-8-sig')
        except UnicodeDecodeError: raise ValueError('document is not valid UTF-8 text') from None
    elif suffix == '.docx':
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                xml = ET.fromstring(archive.read('word/document.xml'))
            text = '\n'.join(''.join(node.itertext()) for node in xml.iter() if node.tag.endswith('}p'))
        except Exception: raise ValueError('DOCX file is invalid or could not be parsed') from None
    elif suffix == '.pdf':
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            if len(reader.pages) > 100: raise ValueError('PDF exceeds the 100-page limit')
            text = '\n'.join((page.extract_text() or '') for page in reader.pages)
        except ImportError: raise RuntimeError('PDF support is unavailable until pypdf is installed') from None
        except ValueError: raise
        except Exception: raise ValueError('PDF file is invalid or could not be parsed') from None
    else:
        raise ValueError('Supported formats: TXT, Markdown, source code, JSON, CSV, DOCX, PDF')
    text = text.strip()
    if not text: raise ValueError('No extractable text was found in this document')
    return {'filename': filename, 'text': text[:40000], 'characters': len(text), 'truncated': len(text) > 40000, 'format': suffix[1:]}

def capabilities() -> dict:
    return {
        'chat': {'available': True},
        'github': {'available': True, 'read_only': True, 'owner_scope': 'Amir2051'},
        'web_research': {'available': True, 'provider_configured': bool(os.getenv('BRAVE_SEARCH_API_KEY','').strip()), 'fallback': 'DuckDuckGo HTML (may be rate-limited)', 'configuration_required_for_reliable_search': ['brave_search_api_key'] if not os.getenv('BRAVE_SEARCH_API_KEY','').strip() else [], 'max_results': MAX_RESULTS},
        'image_generation': {
            'available': bool((os.getenv('IMAGE_GENERATION_API_KEY','').strip() or os.getenv('OPENROUTER_API_KEY','').strip())
                              and (os.getenv('IMAGE_GENERATION_ENDPOINT','https://openrouter.ai/api/v1/images').strip().startswith('https://'))
                              and (os.getenv('IMAGE_GENERATION_MODEL','bytedance-seed/seedream-4.5').strip() or 'bytedance-seed/seedream-4.5')),
            'provider': 'openrouter' if not os.getenv('IMAGE_GENERATION_ENDPOINT','').strip() or 'openrouter.ai' in os.getenv('IMAGE_GENERATION_ENDPOINT','') else 'custom',
            'model': os.getenv('IMAGE_GENERATION_MODEL','bytedance-seed/seedream-4.5').strip() or 'bytedance-seed/seedream-4.5',
            'configuration_required': [] if (os.getenv('IMAGE_GENERATION_API_KEY','').strip() or os.getenv('OPENROUTER_API_KEY','').strip()) else ['openrouter_api_key'],
        },
        'persistent_memory': {'available': True, 'storage': 'postgres' if os.getenv('DATABASE_URL','').strip() else 'local_sqlite', 'durable_across_host_restarts': bool(os.getenv('DATABASE_URL','').strip()), 'warning': None if os.getenv('DATABASE_URL','').strip() else 'Local SQLite may not survive ephemeral-host restarts or replacements'},
        'task_orchestration': {'available': True, 'max_steps': 20, 'execution': 'explicit step tracking; no arbitrary code execution'},
        'document_handling': {'available': True, 'formats': ['txt','md','source code','json','csv','docx','pdf'], 'max_upload_bytes': 3000000, 'max_extracted_characters': 40000},
        'coding_workflows': {'available': True, 'execution': 'analysis and repository reads only; no arbitrary shell execution'},
    }
