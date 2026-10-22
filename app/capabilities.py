"""Bounded capability services for RZ Agent.

External actions are explicit, authenticated HTTP operations. No arbitrary shell,
filesystem, or write-capable GitHub tool is exposed through this registry.
"""
from __future__ import annotations

import json
import base64
import io
import ipaddress
import socket
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
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen

MAX_QUERY = 500
MAX_RESULTS = 8
MAX_MEMORY = 2000
MAX_TASKS_PER_USER = 50

class _SearchParser(HTMLParser):
    """Extract safe organic results from DDG HTML/lite and Bing HTML pages."""
    def __init__(self, engine: str = "DuckDuckGo HTML"):
        super().__init__(convert_charrefs=True)
        self.engine = engine
        self.items = []
        self.current = None
        self.capture = None
        self.text = []
        self.in_bing_result = False

    def _finish_result(self):
        if self.current and self.current.get("title") and self.current.get("url"):
            from urllib.parse import parse_qs, urljoin, urlparse
            url = self.current["url"].strip()
            if url.startswith("/") or url.startswith("//"):
                url = urljoin("https://duckduckgo.com" if self.engine.startswith("DuckDuckGo") else "https://www.bing.com", url)
            parsed = urlparse(url)
            if parsed.hostname in {"duckduckgo.com", "www.duckduckgo.com"} and parsed.path == "/l/":
                url = parse_qs(parsed.query).get("uddg", [""])[0]
            parsed = urlparse(url)
            if parsed.scheme == "https" and parsed.hostname and parsed.hostname not in {
                "duckduckgo.com", "www.duckduckgo.com", "html.duckduckgo.com",
                "bing.com", "www.bing.com"
            } and not any(item["url"] == url for item in self.items):
                self.items.append({
                    "title": self.current["title"].strip()[:500],
                    "url": url[:2000],
                    "snippet": self.current.get("snippet", "").strip()[:1200],
                })
        self.current = None
        self.capture = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").split())
        if self.engine == "Bing HTML":
            if tag == "li" and "b_algo" in classes:
                self._finish_result()
                self.in_bing_result = True
                self.current = {"title": "", "url": "", "snippet": ""}
            elif self.in_bing_result and tag == "a" and attrs.get("href", "").startswith("https://") and not self.current.get("title"):
                self.current["url"] = attrs.get("href", "")
                self.capture = "title"
                self.text = []
            elif self.in_bing_result and ("b_caption" in classes or tag == "p") and self.current:
                self.capture = "snippet"
                self.text = []
        else:
            if tag == "a" and ({"result__a", "result-link"} & classes):
                self._finish_result()
                self.current = {"title": "", "url": attrs.get("href", ""), "snippet": ""}
                self.capture = "title"
                self.text = []
            elif self.current and ({"result__snippet", "result-snippet"} & classes):
                self.capture = "snippet"
                self.text = []

    def handle_data(self, data):
        if self.capture:
            self.text.append(data)

    def handle_endtag(self, tag):
        if not self.capture:
            if self.engine == "Bing HTML" and tag == "li" and self.in_bing_result:
                self._finish_result()
                self.in_bing_result = False
            return
        if (self.capture == "title" and tag == "a") or (self.capture == "snippet" and tag in {"a", "div", "p"}):
            value = " ".join(" ".join(self.text).split())
            if self.current is not None:
                self.current[self.capture] = value
            self.capture = None
            self.text = []

    def finish(self):
        self._finish_result()


# Backwards-compatible name used by existing parser tests and integrations.
_DDGParser = _SearchParser


def _html_search(url: str, engine: str, limit: int) -> list[dict]:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.8",
        },
        method="GET",
    )
    with urlopen(req, timeout=10) as response:
        raw = response.read(750_001)
    if len(raw) > 750_000:
        raise ValueError("Search response exceeded the size limit")
    parser = _SearchParser(engine)
    parser.feed(raw.decode("utf-8", "replace"))
    parser.finish()
    return parser.items[:limit]


def web_research(query: str, limit: int = 5) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY:
        raise ValueError(f"query must contain 1–{MAX_QUERY} characters")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"limit must be from 1 to {MAX_RESULTS}")
    query = query.strip()
    brave_key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    errors = []
    if brave_key:
        url = "https://api.search.brave.com/res/v1/web/search?q=" + quote_plus(query) + "&count=" + str(limit)
        req = Request(url, headers={"Accept": "application/json", "X-Subscription-Token": brave_key, "User-Agent": "MIA-RZ/1.0"}, method="GET")
        try:
            with urlopen(req, timeout=12) as response:
                raw = response.read(750_001)
            if len(raw) > 750_000:
                raise ValueError("Search response exceeded the size limit")
            data = json.loads(raw.decode("utf-8"))
            web = data.get("web", {}).get("results", []) if isinstance(data, dict) else []
            results = [{"title": str(item.get("title", ""))[:500], "url": str(item.get("url", ""))[:2000], "snippet": str(item.get("description", ""))[:1200]} for item in web[:limit] if isinstance(item, dict) and str(item.get("url", "")).startswith("https://")]
            if results:
                return {"query": query, "results": results, "count": len(results), "source": "Brave Search"}
            errors.append("Brave Search returned no usable results")
        except HTTPError as exc:
            # A configured but invalid key should be fixed rather than silently bypassed.
            raise RuntimeError(f"Brave Search returned HTTP {exc.code}; verify BRAVE_SEARCH_API_KEY") from None
        except URLError:
            errors.append("Brave Search is unreachable")
        except (UnicodeDecodeError, json.JSONDecodeError):
            errors.append("Brave Search returned invalid JSON")

    # Best-effort no-key search: try both DDG HTML variants and Bing, because any
    # single HTML endpoint may rate-limit cloud-hosted requests or change markup.
    encoded = quote_plus(query)
    engines = [
        ("DuckDuckGo HTML", "https://html.duckduckgo.com/html/?q=" + encoded),
        ("DuckDuckGo Lite", "https://lite.duckduckgo.com/lite/?q=" + encoded),
        ("Bing HTML", "https://www.bing.com/search?q=" + encoded),
    ]
    for engine, url in engines:
        try:
            results = _html_search(url, engine, limit)
            if results:
                return {"query": query, "results": results, "count": len(results), "source": engine}
            errors.append(engine + " returned no usable results")
        except HTTPError as exc:
            errors.append(f"{engine} returned HTTP {exc.code}")
        except (URLError, TimeoutError):
            errors.append(engine + " is unreachable")
    detail = "; ".join(errors[-3:])
    raise RuntimeError("Web search could not retrieve usable results from available providers. " + detail + ". Configure BRAVE_SEARCH_API_KEY for authenticated, more reliable search.")


class _PageTextParser(HTMLParser):
    """Extract bounded visible text while ignoring active or hidden page content."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "iframe", "form"}:
            self.skip_depth += 1
        elif tag in {"p", "div", "main", "article", "section", "li", "h1", "h2", "h3", "br"} and not self.skip_depth:
            self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "iframe", "form"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag in {"p", "div", "main", "article", "section", "li", "h1", "h2", "h3"} and not self.skip_depth:
            self.parts.append("\n")
    def handle_data(self, data):
        if not self.skip_depth and data.strip():
            self.parts.append(data.strip())


def web_open_page(url: str) -> dict:
    """Fetch a public HTTPS page without following redirects or allowing private hosts."""
    from urllib.error import HTTPError, URLError
    from urllib.request import HTTPRedirectHandler, build_opener
    if not isinstance(url, str) or len(url) > 2000:
        raise ValueError("url must be a valid HTTPS URL under 2000 characters")
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTPS URLs without embedded credentials are allowed")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".localhost")):
        raise ValueError("Local and internal hosts are not allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError:
        raise RuntimeError("Page host could not be resolved") from None
    if not addresses:
        raise RuntimeError("Page host did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise ValueError("Private, reserved, and non-public addresses are not allowed")
    class _NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    req = Request(url.strip(), headers={"User-Agent":"MIA-RZ/1.0 (public page text reader)","Accept":"text/html,text/plain;q=0.9","Accept-Language":"en-US,en;q=0.8"}, method="GET")
    try:
        with build_opener(_NoRedirect).open(req, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            raw = response.read(500_001)
            status = response.status
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise RuntimeError("Page redirected; open the destination URL directly after reviewing it") from None
        raise RuntimeError(f"Page request returned HTTP {exc.code}") from None
    except (URLError, TimeoutError):
        raise RuntimeError("Page could not be fetched") from None
    if len(raw) > 500_000:
        raise ValueError("Page exceeded the 500 KB read limit")
    if not ("text/html" in content_type or "text/plain" in content_type or "application/xhtml+xml" in content_type):
        raise ValueError("Only HTML and plain-text pages are supported")
    text = raw.decode("utf-8", "replace")
    if "html" in content_type:
        parser = _PageTextParser()
        parser.feed(text)
        text = " ".join(" ".join(parser.parts).split())
    return {"url": url.strip(), "status": status, "content_type": content_type, "text": text[:18000], "truncated": len(text) > 18000}

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
        'web_research': {'available': True, 'provider_configured': bool(os.getenv('BRAVE_SEARCH_API_KEY','').strip()), 'fallback': ['DuckDuckGo HTML', 'DuckDuckGo Lite', 'Bing HTML'], 'configuration_required_for_reliable_search': ['brave_search_api_key'] if not os.getenv('BRAVE_SEARCH_API_KEY','').strip() else [], 'max_results': MAX_RESULTS},
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
