"""Safe, owner-scoped website discovery for MIA Business AI.

Only public HTTP(S) pages are fetched. Crawling is bounded, same-host, and
read-only; page content is untrusted input and is never executed.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
import uuid
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl

from app.business_documents import _db, get_business_profile

MAX_PAGES = 15
MAX_BYTES = 1_000_000
MAX_TOTAL_CHARS = 100_000
TIMEOUT = 8
USER_AGENT = "MIA-Business-Discovery/1.0 (+public-site-profile)"
PROFILE_FIELDS = {
    "business_name": 180,
    "industry": 180,
    "target_customers": 500,
    "offer": 1000,
    "brand_voice": 500,
}


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self.title = []
        self.headings = []
        self.meta = {}
        self.structured = []
        self._in_title = False
        self._heading_level = 0
        self._in_jsonld = False
        self._jsonld_parts = []
        self._skip_stack = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        inherited_skip = self._skip_stack[-1] if self._skip_stack else False
        own_skip = tag in {"script", "style", "noscript", "svg", "iframe", "nav", "footer"}
        self._skip_stack.append(inherited_skip or own_skip)
        if tag == "title":
            self._in_title = True
        if tag in {"h1", "h2", "h3"}:
            self._heading_level = int(tag[1])
            self.parts.append("\n")
        if tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_jsonld = True
            self._jsonld_parts = []
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in {"description", "keywords", "og:site_name", "og:description", "og:title", "og:type", "og:url"}:
                self.meta[key] = a.get("content", "")[:1500]
        if tag == "link" and "canonical" in a.get("rel", "").lower():
            self.meta["canonical"] = a.get("href", "")[:2000]
        if tag == "a" and a.get("href"):
            self.links.append((a["href"][:2000], " ".join(a.get("title", "").split())[:180]))
        if tag in {"br", "p", "div", "li", "h1", "h2", "h3", "h4", "section", "article", "address"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in {"h1", "h2", "h3"}:
            self._heading_level = 0
            self.parts.append("\n")
        if tag == "script" and self._in_jsonld:
            try:
                value = json.loads("".join(self._jsonld_parts).strip())
                self.structured.extend(value if isinstance(value, list) else [value])
            except (ValueError, TypeError):
                pass
            self._in_jsonld = False
            self._jsonld_parts = []
        if self._skip_stack:
            self._skip_stack.pop()
        if tag in {"p", "div", "li", "h4", "section", "article", "address"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)
        if self._in_jsonld:
            self._jsonld_parts.append(data)
        skipped = self._skip_stack[-1] if self._skip_stack else False
        if self._heading_level and not skipped:
            heading = " ".join(data.split())
            if heading:
                self.headings.append(heading[:200])
        if not skipped and not self._in_jsonld:
            value = " ".join(data.split())
            if value:
                self.parts.append(value)

def _validate_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=422, detail="Enter a valid public HTTP or HTTPS website URL")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="URLs containing credentials are not allowed")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local") or host.endswith(".internal"):
        raise HTTPException(status_code=422, detail="Private or local network addresses are not allowed")
    try:
        answers = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        raise HTTPException(status_code=422, detail="Website hostname could not be resolved") from None
    if not answers:
        raise HTTPException(status_code=422, detail="Website hostname could not be resolved")
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0].split("%")[0])
        if not address.is_global:
            raise HTTPException(status_code=422, detail="Private or non-public network addresses are not allowed")
    return url


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_page(url: str):
    _validate_public_url(url)
    opener = build_opener(_SafeRedirect())
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9"})
    try:
        with opener.open(req, timeout=TIMEOUT) as response:
            final_url = _validate_public_url(response.geturl())
            content_type = response.headers.get("Content-Type", "").lower()
            if not any(t in content_type for t in ("text/html", "application/xhtml+xml")):
                return final_url, "", [], "Skipped non-HTML page"
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raw = raw[:MAX_BYTES]
            charset = response.headers.get_content_charset() or "utf-8"
            return final_url, raw.decode(charset, errors="replace"), [], ""
    except HTTPError as exc:
        return url, "", [], f"HTTP {exc.code}"
    except (URLError, TimeoutError, OSError, ValueError):
        return url, "", [], "Could not fetch page"


def _flatten_jsonld(value):
    found = []
    if isinstance(value, dict):
        found.append(value)
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                found.extend(_flatten_jsonld(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_flatten_jsonld(item))
    return found


def _page_type(url: str, title: str, text: str) -> str:
    value = (url + " " + title).lower()
    if re.search(r"privacy|cookie|acceptable-use|terms|refund|legal|gdpr", value):
        return "policy"
    if re.search(r"contact|location|hours|reach-us", value):
        return "contact"
    if re.search(r"pricing|price|plans|packages|rates", value):
        return "pricing"
    if re.search(r"service|solution|offering|product", value):
        return "services"
    if re.search(r"about|company-profile|team|who-we-are", value):
        return "about"
    if re.search(r"faq|frequently-asked", value):
        return "faq"
    if re.search(r"blog|news|article|post/", value):
        return "article"
    return "home" if urlparse(url).path in {"", "/"} else "other"


def _extract(url: str, raw_html: str):
    parser = _PageParser()
    try:
        parser.feed(raw_html)
    except Exception:
        pass
    text = "\n".join(" ".join(part.split()) for part in parser.parts if part.strip())
    text = re.sub(r"\n{2,}", "\n", text).strip()[:20_000]
    title = " ".join(" ".join(parser.title).split())[:250]
    links = []
    root_host = (urlparse(url).hostname or "").lower()
    for href, label in parser.links:
        absolute = urldefrag(urljoin(url, href))[0]
        parsed = urlparse(absolute)
        target = (absolute + " " + label).lower()
        if parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() == root_host:
            if re.search(r"privacy|cookie|acceptable-use|terms|refund|legal|gdpr", target):
                continue
            if any(term in target for term in (
                "about", "service", "pricing", "price", "contact", "faq", "team",
                "company", "location", "area", "hours", "booking", "home", "product",
                "solution", "package", "quote", "support"
            )):
                links.append(absolute)
    structured = []
    for item in parser.structured:
        structured.extend(_flatten_jsonld(item))
    return {
        "url": url, "title": title, "text": text,
        "headings": list(dict.fromkeys(parser.headings))[:80],
        "meta": parser.meta, "structured": structured[:50],
        "page_type": _page_type(url, title, text),
        "links": list(dict.fromkeys(links)),
    }


def _derive_profile(pages: list[dict], domain: str):
    def clean(value, limit=500):
        if isinstance(value, list):
            value = ", ".join(str(x) for x in value if x)
        if isinstance(value, dict):
            value = value.get("name") or value.get("streetAddress") or ""
        return " ".join(str(value or "").split())[:limit]

    business_nodes = []
    for page in pages:
        for node in page.get("structured", []):
            kind = node.get("@type", [])
            if isinstance(kind, str):
                kind = [kind]
            if any(x in {"Organization", "LocalBusiness", "ProfessionalService", "Store", "Corporation", "Person"} for x in kind):
                business_nodes.append(node)
    home = next((p for p in pages if p.get("page_type") == "home"), pages[0] if pages else {"title": "", "text": "", "meta": {}})
    meta = home.get("meta", {})
    primary_node = business_nodes[0] if business_nodes else {}
    title = meta.get("og:title", "") or home.get("title", "")
    business_name = clean(primary_node.get("name"), 180) or clean(meta.get("og:site_name"), 180)
    if not business_name:
        business_name = re.split(r"\s+[|–—-]\s+", title, maxsplit=1)[0].strip()[:180]
    description = clean(primary_node.get("description"), 1000) or clean(meta.get("description") or meta.get("og:description"), 1000)
    if not description:
        description = next((clean(p.get("meta", {}).get("description"), 1000) for p in pages if p.get("meta", {}).get("description")), "")

    def jsonld_values(key):
        values = []
        for node in business_nodes:
            value = node.get(key)
            if key == "address" and isinstance(value, dict):
                value = ", ".join(clean(value.get(k), 160) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry") if value.get(k))
            elif key == "contactPoint" and isinstance(value, (dict, list)):
                nodes = value if isinstance(value, list) else [value]
                value = ", ".join(clean(n.get("telephone") or n.get("email"), 180) for n in nodes if isinstance(n, dict))
            elif key == "openingHours" and isinstance(value, list):
                value = ", ".join(clean(x, 100) for x in value)
            elif isinstance(value, dict):
                value = value.get("name", "")
            if value:
                values.append(clean(value, 600))
        return list(dict.fromkeys(values))

    page_text = "\n".join(p.get("text", "") for p in pages if p.get("page_type") != "policy")
    service_values = []
    for node in business_nodes:
        for key in ("makesOffer", "hasOfferCatalog", "knowsAbout"):
            value = node.get(key)
            items = value if isinstance(value, list) else [value] if value else []
            for item in items:
                if isinstance(item, dict):
                    offer_item = item.get("itemOffered")
                    item = item.get("name") or (offer_item.get("name") if isinstance(offer_item, dict) else "")
                if item:
                    service_values.append(clean(item, 180))
    service_lines = []
    for page in pages:
        if page.get("page_type") not in {"services", "home", "about"}:
            continue
        for line in page.get("text", "").splitlines():
            line = " ".join(line.split()).strip(" •-\t")
            if 8 <= len(line) <= 180 and re.search(r"\b(cleaning|repair|installation|consulting|service|services|we offer|speciali[sz]e|our work|solutions|products|support|booking|design|maintenance|care|security|software|platform)\b", line, re.I):
                if line.lower() not in {x.lower() for x in service_lines}:
                    service_lines.append(line)
    offer = description or "; ".join(dict.fromkeys(service_values + service_lines))
    email_values = set(re.findall(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", page_text, re.I))
    phone_values = set(re.findall(r"(?<!\w)(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\w)", page_text))
    address_values = jsonld_values("address")
    hours_values = jsonld_values("openingHours")
    pricing_lines = []
    for page in pages:
        if page.get("page_type") == "pricing":
            pricing_lines.extend([line.strip() for line in page.get("text", "").splitlines() if re.search(r"(?:\$|USD\s*)\d|per (?:hour|visit|month|project)|starting at|from \$", line, re.I)])
    industry = clean(primary_node.get("additionalType"), 180)
    if not industry:
        industry = clean(primary_node.get("@type") if isinstance(primary_node.get("@type"), str) else "", 180)
    profile = {
        "business_name": business_name[:180], "industry": industry,
        "target_customers": "", "offer": offer[:1000], "brand_voice": "",
    }
    facts = {
        "summary": description,
        "services": list(dict.fromkeys(service_values + service_lines))[:20],
        "contact": {"emails": sorted(email_values)[:12], "phones": sorted(phone_values)[:12],
                    "addresses": list(dict.fromkeys(address_values))[:5], "hours": list(dict.fromkeys(hours_values))[:10]},
        "pricing_mentions": list(dict.fromkeys(pricing_lines))[:15],
        "pages_by_type": {kind: sum(1 for p in pages if p.get("page_type") == kind) for kind in ("home", "about", "services", "pricing", "contact", "faq", "policy", "other", "article")},
        "structured_data_found": bool(business_nodes),
        "structured_business_types": list(dict.fromkeys(clean(x, 80) for node in business_nodes for x in ([node.get("@type")] if isinstance(node.get("@type"), str) else node.get("@type", []))))[:10],
    }
    inventory = [{"url": p.get("url", ""), "title": p.get("title", ""), "type": p.get("page_type", "other")} for p in pages]
    return profile, facts, inventory


class WebsiteScanRequest(BaseModel):
    domain: str = Field(min_length=4, max_length=2000)
    max_pages: int = Field(default=10, ge=1, le=MAX_PAGES)


class WebsiteProfileApproval(BaseModel):
    fields: list[str] = Field(default_factory=lambda: list(PROFILE_FIELDS), max_length=len(PROFILE_FIELDS))


def create_router(auth_dependency):
    router = APIRouter(prefix="/v1/business/website", tags=["MIA Website Intelligence"])

    def _ensure_table(conn):
        conn.execute("""CREATE TABLE IF NOT EXISTS business_website_scans(
            user_id TEXT NOT NULL, scan_id TEXT NOT NULL, domain TEXT NOT NULL,
            status TEXT NOT NULL, pages_json TEXT NOT NULL, profile_json TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(user_id, scan_id))""")

    @router.post("/scan")
    def scan_website(payload: WebsiteScanRequest, user: dict = Depends(auth_dependency)):
        raw_url = payload.domain.strip()
        if "://" not in raw_url:
            raw_url = "https://" + raw_url
        _validate_public_url(raw_url)
        parsed = urlparse(raw_url)
        root_host = parsed.hostname.lower()
        root_url = f"{parsed.scheme}://{parsed.netloc}/"
        queue = [raw_url]
        seen = set()
        pages = []
        errors = []
        total_chars = 0
        while queue and len(pages) < min(payload.max_pages, MAX_PAGES) and total_chars < MAX_TOTAL_CHARS:
            url = urldefrag(queue.pop(0))[0]
            if url in seen:
                continue
            seen.add(url)
            try:
                final_url, raw, _, error = _fetch_page(url)
            except HTTPException:
                errors.append({"url": url, "error": "Blocked unsafe redirect or address"})
                continue
            if error:
                errors.append({"url": url, "error": error})
                continue
            if urlparse(final_url).hostname != root_host:
                continue
            page = _extract(final_url, raw)
            if not page["text"] and not page["title"]:
                continue
            pages.append(page)
            total_chars += len(page["text"])
            for link in page["links"]:
                lp = urlparse(link)
                if lp.hostname == root_host and link not in seen and len(queue) < MAX_PAGES * 4:
                    queue.append(link)
        if not pages:
            raise HTTPException(status_code=422, detail="MIA could not read public HTML pages from this website. It may block automated access or require JavaScript.")
        domain = root_host
        profile = _derive_profile(pages, domain)
        scan_id, now = uuid.uuid4().hex, time.time()
        # Persist bounded page evidence and extracted candidate fields for this account only.
        safe_pages = [{"url": p["url"], "title": p["title"], "text": p["text"][:8000], "meta": p["meta"]} for p in pages]
        conn = _db()
        try:
            _ensure_table(conn)
            conn.execute("INSERT INTO business_website_scans(user_id,scan_id,domain,status,pages_json,profile_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                         (str(user["id"]), scan_id, domain, "review", json.dumps(safe_pages), json.dumps(profile), now, now))
            conn.commit()
        finally:
            conn.close()
        return {
            "scan_id": scan_id, "domain": domain, "status": "review",
            "pages_crawled": len(pages), "pages": [{"url": p["url"], "title": p["title"]} for p in pages],
            "profile_suggestions": profile,
            "sources": [{"url": p["url"], "title": p["title"]} for p in pages],
            "warnings": errors[:20],
            "note": "Extracted content is unverified until you review and approve it. Pages may contain inaccurate or outdated claims.",
        }

    @router.get("/scans/{scan_id}")
    def get_scan(scan_id: str, user: dict = Depends(auth_dependency)):
        if not re.fullmatch(r"[a-f0-9]{32}", scan_id):
            raise HTTPException(status_code=400, detail="Invalid scan id")
        conn = _db()
        try:
            _ensure_table(conn)
            row = conn.execute("SELECT domain,status,pages_json,profile_json,created_at,updated_at FROM business_website_scans WHERE user_id=? AND scan_id=?",
                               (str(user["id"]), scan_id)).fetchone()
        finally:
            conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Website scan not found")
        return {"scan_id": scan_id, "domain": row[0], "status": row[1],
                "pages": json.loads(row[2]), "profile_suggestions": json.loads(row[3]),
                "created_at": row[4], "updated_at": row[5]}

    @router.post("/scans/{scan_id}/approve")
    def approve_scan(scan_id: str, payload: WebsiteProfileApproval, user: dict = Depends(auth_dependency)):
        if not re.fullmatch(r"[a-f0-9]{32}", scan_id):
            raise HTTPException(status_code=400, detail="Invalid scan id")
        selected = set(payload.fields)
        if not selected.issubset(PROFILE_FIELDS):
            raise HTTPException(status_code=422, detail="Unsupported profile field")
        conn = _db()
        try:
            _ensure_table(conn)
            row = conn.execute("SELECT domain,status,profile_json FROM business_website_scans WHERE user_id=? AND scan_id=?",
                               (str(user["id"]), scan_id)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Website scan not found")
            if row[1] == "approved":
                raise HTTPException(status_code=409, detail="This scan has already been approved")
            candidate = json.loads(row[2])
            existing = get_business_profile(str(user["id"]))
            merged = {k: (candidate.get(k, "") if k in selected and candidate.get(k, "") else existing.get(k, "")) for k in PROFILE_FIELDS}
            now = time.time()
            conn.execute("""INSERT INTO business_profiles(user_id,business_name,industry,target_customers,offer,goals,brand_voice,constraints,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                business_name=excluded.business_name, industry=excluded.industry,
                target_customers=excluded.target_customers, offer=excluded.offer,
                goals=excluded.goals, brand_voice=excluded.brand_voice,
                constraints=excluded.constraints, updated_at=excluded.updated_at""",
                (str(user["id"]), merged["business_name"], merged["industry"], merged["target_customers"],
                 merged["offer"], existing.get("goals", ""), merged["brand_voice"], existing.get("constraints", ""), now))
            conn.execute("UPDATE business_website_scans SET status='approved',updated_at=? WHERE user_id=? AND scan_id=?",
                         (now, str(user["id"]), scan_id))
            conn.commit()
        finally:
            conn.close()
        return {"approved": True, "domain": row[0], "profile": get_business_profile(str(user["id"])),
                "note": "Selected website-derived fields were saved to your MIA business profile. Blank or unselected fields were left unchanged."}

    return router
