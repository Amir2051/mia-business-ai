"""User-scoped document and invoice generation for the MIA Business clone.

Uses a dedicated SQLite database by default, intentionally separate from the
original RZ Agent capability database and any SafeNestT database.
"""
from __future__ import annotations
import csv, html, io, json, os, re, sqlite3, time, uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

MAX_CONTENT = 200_000
FORMATS = {"pdf", "docx", "txt", "md", "csv", "json", "html"}
MIME = {"pdf":"application/pdf", "docx":"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "txt":"text/plain; charset=utf-8", "md":"text/markdown; charset=utf-8", "csv":"text/csv; charset=utf-8", "json":"application/json; charset=utf-8", "html":"text/html; charset=utf-8"}

def _db():
    # Deliberately do not use DATABASE_URL: this clone must not inherit the
    # original app's database accidentally. Production can provision its own path.
    path = Path(os.getenv("MIA_BUSINESS_DATA_PATH", str(Path.home()/".mia-business-ai"/"business.sqlite3"))).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""CREATE TABLE IF NOT EXISTS business_files(
      user_id TEXT NOT NULL, file_id TEXT NOT NULL, title TEXT NOT NULL,
      format TEXT NOT NULL, content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'document',
      created_at REAL NOT NULL, PRIMARY KEY(user_id,file_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS business_profiles(
      user_id TEXT PRIMARY KEY, business_name TEXT NOT NULL DEFAULT '',
      industry TEXT NOT NULL DEFAULT '', target_customers TEXT NOT NULL DEFAULT '',
      offer TEXT NOT NULL DEFAULT '', goals TEXT NOT NULL DEFAULT '',
      brand_voice TEXT NOT NULL DEFAULT '', constraints TEXT NOT NULL DEFAULT '',
      updated_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS business_clients(
      user_id TEXT NOT NULL, client_id TEXT NOT NULL, name TEXT NOT NULL,
      company TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
      stage TEXT NOT NULL, expected_value TEXT NOT NULL DEFAULT '0', next_follow_up TEXT NOT NULL DEFAULT '',
      notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
      PRIMARY KEY(user_id,client_id))""")
    conn.commit()
    return conn

class FileCreate(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    content: str = Field(min_length=1, max_length=MAX_CONTENT)
    format: str = Field(default="pdf", max_length=8)

class BusinessProfile(BaseModel):
    business_name: str = Field(default="", max_length=180)
    industry: str = Field(default="", max_length=180)
    target_customers: str = Field(default="", max_length=500)
    offer: str = Field(default="", max_length=1000)
    goals: str = Field(default="", max_length=1500)
    brand_voice: str = Field(default="", max_length=500)
    constraints: str = Field(default="", max_length=1000)


def get_business_profile(user_id: str) -> dict:
    """Return one user's business profile from the clone's dedicated business store."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT business_name,industry,target_customers,offer,goals,brand_voice,constraints,updated_at FROM business_profiles WHERE user_id=?",
            (str(user_id),),
        ).fetchone()
    finally:
        conn.close()
    keys = ("business_name", "industry", "target_customers", "offer", "goals", "brand_voice", "constraints", "updated_at")
    return dict(zip(keys, row)) if row else {**{k: "" for k in keys[:-1]}, "updated_at": None}


class BusinessClient(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    company: str = Field(default="", max_length=180)
    email: str = Field(default="", max_length=254)
    phone: str = Field(default="", max_length=80)
    stage: Literal["lead", "contacted", "qualified", "proposal", "won", "lost"] = "lead"
    expected_value: Decimal = Field(default=Decimal("0"), ge=0, le=100000000)
    next_follow_up: str = Field(default="", pattern=r"^$|^\d{4}-\d{2}-\d{2}$", max_length=10)
    notes: str = Field(default="", max_length=3000)


def _client_dict(row):
    return {"id": row[0], "name": row[1], "company": row[2], "email": row[3], "phone": row[4],
            "stage": row[5], "expected_value": row[6], "next_follow_up": row[7], "notes": row[8],
            "created_at": row[9], "updated_at": row[10]}


class InvoiceItem(BaseModel):
    description: str = Field(min_length=1, max_length=300)
    quantity: Decimal = Field(gt=0, le=1000000)
    unit_price: Decimal = Field(ge=0, le=100000000)

class InvoiceCreate(BaseModel):
    invoice_number: str = Field(default="", max_length=80)
    business_name: str = Field(default="", max_length=180)
    business_email: str = Field(default="", max_length=254)
    business_address: str = Field(default="", max_length=500)
    customer_name: str = Field(min_length=1, max_length=180)
    customer_email: str = Field(default="", max_length=254)
    customer_address: str = Field(default="", max_length=500)
    issue_date: str = Field(default="", max_length=30)
    due_date: str = Field(default="", max_length=30)
    currency: str = Field(default="USD", pattern="^[A-Z]{3}$")
    tax_rate: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    discount: Decimal = Field(default=Decimal("0"), ge=0, le=100000000)
    payment_terms: str = Field(default="Due upon receipt", max_length=300)
    notes: str = Field(default="", max_length=3000)
    items: list[InvoiceItem] = Field(min_length=1, max_length=100)

def _safe_name(title: str, ext: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", title.strip()).strip("._-")[:100] or "mia-document"
    return f"{stem}.{ext}"

def _pdf_bytes(title: str, content: str) -> bytes:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Preformatted
        from xml.sax.saxutils import escape
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="PDF generation is unavailable; install the reportlab dependency") from exc
    out = io.BytesIO(); doc = SimpleDocTemplate(out, pagesize=letter, rightMargin=.7*inch, leftMargin=.7*inch, topMargin=.65*inch, bottomMargin=.65*inch)
    styles = getSampleStyleSheet(); story = [Paragraph(escape(title), styles["Title"]), Spacer(1, 12)]
    for block in re.split(r"\n\s*\n", content):
        block = block.strip()
        if not block: continue
        lines = block.splitlines()
        if len(lines)==1 and lines[0].startswith("# "):
            story.append(Paragraph(escape(lines[0][2:]), styles["Heading1"]))
        elif len(lines)==1 and lines[0].startswith("## "):
            story.append(Paragraph(escape(lines[0][3:]), styles["Heading2"]))
        else:
            story.append(Paragraph("<br/>".join(escape(line) for line in lines), styles["BodyText"]))
        story.append(Spacer(1, 8))
    doc.build(story); return out.getvalue()

def _render(title: str, content: str, fmt: str) -> tuple[bytes,str]:
    if fmt not in FORMATS: raise HTTPException(status_code=422, detail=f"Unsupported format. Choose: {', '.join(sorted(FORMATS))}")
    if fmt == "pdf": return _pdf_bytes(title, content), MIME[fmt]
    if fmt == "docx":
        try: from docx import Document
        except ImportError as exc: raise HTTPException(status_code=503, detail="DOCX generation is unavailable; install python-docx") from exc
        doc=Document(); doc.add_heading(title,0)
        for para in content.split("\n\n"): doc.add_paragraph(para)
        out=io.BytesIO(); doc.save(out); return out.getvalue(), MIME[fmt]
    if fmt == "csv":
        try:
            parsed=json.loads(content)
            if isinstance(parsed,list) and parsed and isinstance(parsed[0],dict):
                out=io.StringIO(newline=""); writer=csv.DictWriter(out,fieldnames=list(dict.fromkeys(k for row in parsed for k in row.keys()))); writer.writeheader(); writer.writerows(parsed); return out.getvalue().encode(),MIME[fmt]
        except (json.JSONDecodeError, TypeError): pass
    if fmt == "json":
        try: content=json.dumps(json.loads(content),ensure_ascii=False,indent=2)
        except json.JSONDecodeError: content=json.dumps({"title":title,"content":content},ensure_ascii=False,indent=2)
    if fmt == "html": content=f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title><main><h1>{html.escape(title)}</h1><pre>{html.escape(content)}</pre></main>"
    return content.encode("utf-8"), MIME[fmt]

def _save(user_id: str, title: str, content: str, fmt: str, kind: str="document") -> str:
    fid=uuid.uuid4().hex; conn=_db()
    try:
        conn.execute("INSERT INTO business_files(user_id,file_id,title,format,content,kind,created_at) VALUES(?,?,?,?,?,?,?)",(user_id,fid,title.strip(),fmt,content,kind,time.time())); conn.commit()
    finally: conn.close()
    return fid

def _file_row(user_id: str, fid: str):
    conn=_db()
    try: row=conn.execute("SELECT file_id,title,format,content,kind,created_at FROM business_files WHERE user_id=? AND file_id=?",(user_id,fid)).fetchone()
    finally: conn.close()
    return row

def create_router(auth_dependency):
    router=APIRouter(prefix="/v1/business",tags=["MIA Business Documents"])
    @router.get("/profile")
    def read_business_profile(user:dict=Depends(auth_dependency)):
        return {"profile": get_business_profile(str(user["id"]))}

    @router.put("/profile")
    def save_business_profile(payload:BusinessProfile,user:dict=Depends(auth_dependency)):
        uid = str(user["id"])
        values = {k: getattr(payload, k).strip() for k in ("business_name", "industry", "target_customers", "offer", "goals", "brand_voice", "constraints")}
        now = time.time()
        conn = _db()
        try:
            conn.execute("""INSERT INTO business_profiles(user_id,business_name,industry,target_customers,offer,goals,brand_voice,constraints,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                business_name=excluded.business_name,industry=excluded.industry,target_customers=excluded.target_customers,
                offer=excluded.offer,goals=excluded.goals,brand_voice=excluded.brand_voice,constraints=excluded.constraints,updated_at=excluded.updated_at""",
                (uid, values["business_name"], values["industry"], values["target_customers"], values["offer"], values["goals"], values["brand_voice"], values["constraints"], now))
            conn.commit()
        finally:
            conn.close()
        return {"profile": {**values, "updated_at": now}, "saved": True}

    @router.get("/clients")
    def list_business_clients(stage:str="",user:dict=Depends(auth_dependency)):
        allowed = {"lead", "contacted", "qualified", "proposal", "won", "lost"}
        if stage and stage not in allowed:
            raise HTTPException(status_code=422, detail="Unsupported client stage")
        uid = str(user["id"]); conn = _db()
        try:
            query = "SELECT client_id,name,company,email,phone,stage,expected_value,next_follow_up,notes,created_at,updated_at FROM business_clients WHERE user_id=?"
            params = [uid]
            if stage:
                query += " AND stage=?"; params.append(stage)
            query += " ORDER BY updated_at DESC LIMIT 200"
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        clients = [_client_dict(row) for row in rows]
        total = sum((Decimal(c["expected_value"]) for c in clients if c["stage"] not in {"won", "lost"}), Decimal("0"))
        return {"clients": clients, "count": len(clients), "open_pipeline_value": str(total.quantize(Decimal("0.01")))}

    @router.post("/clients")
    def create_business_client(payload:BusinessClient,user:dict=Depends(auth_dependency)):
        uid = str(user["id"]); cid = uuid.uuid4().hex; now = time.time()
        values = payload.model_dump()
        values["name"] = values["name"].strip()
        if not values["name"]:
            raise HTTPException(status_code=422, detail="Client name cannot be blank")
        values["expected_value"] = str(Decimal(values["expected_value"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        values = {k: (v.strip() if isinstance(v, str) else v) for k,v in values.items()}
        conn = _db()
        try:
            conn.execute("INSERT INTO business_clients(user_id,client_id,name,company,email,phone,stage,expected_value,next_follow_up,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid,cid,values["name"],values["company"],values["email"],values["phone"],values["stage"],values["expected_value"],values["next_follow_up"],values["notes"],now,now)); conn.commit()
        finally:
            conn.close()
        return {"client": {"id":cid,**{k:values[k] for k in ("name","company","email","phone","stage","expected_value","next_follow_up","notes")},"created_at":now,"updated_at":now}}

    @router.put("/clients/{client_id}")
    def update_business_client(client_id:str,payload:BusinessClient,user:dict=Depends(auth_dependency)):
        if not re.fullmatch(r"[a-f0-9]{32}", client_id):
            raise HTTPException(status_code=400, detail="Invalid client id")
        uid = str(user["id"]); values = payload.model_dump(); values["name"] = values["name"].strip()
        if not values["name"]:
            raise HTTPException(status_code=422, detail="Client name cannot be blank")
        values["expected_value"] = str(Decimal(values["expected_value"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        values = {k: (v.strip() if isinstance(v, str) else v) for k,v in values.items()}; now = time.time(); conn = _db()
        try:
            cur = conn.execute("UPDATE business_clients SET name=?,company=?,email=?,phone=?,stage=?,expected_value=?,next_follow_up=?,notes=?,updated_at=? WHERE user_id=? AND client_id=?",
                (values["name"],values["company"],values["email"],values["phone"],values["stage"],values["expected_value"],values["next_follow_up"],values["notes"],now,uid,client_id)); conn.commit()
            if not cur.rowcount:
                raise HTTPException(status_code=404, detail="Client not found")
        finally:
            conn.close()
        return {"client": {"id":client_id,**{k:values[k] for k in ("name","company","email","phone","stage","expected_value","next_follow_up","notes")},"updated_at":now}}

    @router.delete("/clients/{client_id}")
    def delete_business_client(client_id:str,user:dict=Depends(auth_dependency)):
        if not re.fullmatch(r"[a-f0-9]{32}", client_id):
            raise HTTPException(status_code=400, detail="Invalid client id")
        conn = _db()
        try:
            cur = conn.execute("DELETE FROM business_clients WHERE user_id=? AND client_id=?", (str(user["id"]), client_id)); conn.commit()
        finally:
            conn.close()
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="Client not found")
        return {"deleted": True, "id": client_id}

    @router.get("/files")
    def list_files(user:dict=Depends(auth_dependency)):
        conn=_db()
        try: rows=conn.execute("SELECT file_id,title,format,kind,created_at,length(content) FROM business_files WHERE user_id=? ORDER BY created_at DESC LIMIT 100",(str(user["id"]),)).fetchall()
        finally: conn.close()
        return {"files":[{"id":r[0],"title":r[1],"format":r[2],"kind":r[3],"created_at":r[4],"content_length":r[5],"preview_url":f"/v1/business/files/{r[0]}","download_url":f"/v1/business/files/{r[0]}/download"} for r in rows],"count":len(rows)}
    @router.post("/files")
    def create_file(payload:FileCreate,user:dict=Depends(auth_dependency)):
        title=payload.title.strip(); content=payload.content.strip(); fmt=payload.format.lower().strip()
        if not title or not content: raise HTTPException(status_code=422,detail="Title and content cannot be blank")
        if fmt not in FORMATS: raise HTTPException(status_code=422,detail=f"Unsupported format. Choose: {', '.join(sorted(FORMATS))}")
        fid=_save(str(user["id"]),title,content,fmt)
        return {"file":{"id":fid,"title":title,"format":fmt,"kind":"document","preview_url":f"/v1/business/files/{fid}","download_url":f"/v1/business/files/{fid}/download"}}
    @router.get("/files/{file_id}")
    def preview_file(file_id:str,user:dict=Depends(auth_dependency)):
        row=_file_row(str(user["id"]),file_id)
        if not row: raise HTTPException(status_code=404,detail="File not found")
        return {"file":{"id":row[0],"title":row[1],"format":row[2],"content":row[3],"kind":row[4],"created_at":row[5],"download_url":f"/v1/business/files/{row[0]}/download"}}
    @router.get("/files/{file_id}/render")
    def render_file_preview(file_id:str,user:dict=Depends(auth_dependency)):
        row=_file_row(str(user["id"]),file_id)
        if not row: raise HTTPException(status_code=404,detail="File not found")
        fid,title,fmt,content,kind,created=row
        # Inline rendering is restricted to PDF; other formats are previewed as
        # escaped text in the UI rather than executed as active content.
        output_fmt="pdf" if kind=="invoice" else fmt
        if output_fmt!="pdf": raise HTTPException(status_code=415,detail="Inline preview is available for PDF files; other formats use the text preview")
        data,mime=_render(title,content,"pdf")
        return Response(content=data,media_type=mime,headers={"Content-Disposition":f'inline; filename="{_safe_name(title, "pdf")}"',"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})

    @router.get("/files/{file_id}/download")
    def download_file(file_id:str,user:dict=Depends(auth_dependency)):
        row=_file_row(str(user["id"]),file_id)
        if not row: raise HTTPException(status_code=404,detail="File not found")
        fid,title,fmt,content,kind,created=row
        output_fmt="pdf" if kind=="invoice" else fmt
        data,mime=_render(title,content,output_fmt)
        return Response(content=data,media_type=mime,headers={"Content-Disposition":f'attachment; filename="{_safe_name(title, output_fmt)}"',"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})
    @router.post("/invoices")
    def create_invoice(payload:InvoiceCreate,user:dict=Depends(auth_dependency)):
        number=payload.invoice_number.strip() or ("INV-"+time.strftime("%Y%m%d")+"-"+uuid.uuid4().hex[:6].upper())
        cents=lambda v: Decimal(v).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
        items=[]; subtotal=Decimal("0.00")
        for item in payload.items:
            amount=cents(item.quantity*item.unit_price); subtotal+=amount
            items.append({"description":item.description.strip(),"quantity":str(item.quantity),"unit_price":str(cents(item.unit_price)),"amount":str(amount)})
        discount=min(cents(payload.discount),cents(subtotal)); taxable=max(Decimal("0.00"),subtotal-discount)
        tax=cents(taxable*payload.tax_rate/Decimal("100")); total=cents(taxable+tax)
        lines=[f"Invoice {number}","",payload.business_name.strip(),payload.business_email.strip(),payload.business_address.strip(),"",f"Bill to: {payload.customer_name.strip()}",payload.customer_email.strip(),payload.customer_address.strip(),"",f"Issue date: {payload.issue_date or 'Not specified'}",f"Due date: {payload.due_date or 'Not specified'}",f"Currency: {payload.currency}","","ITEMS"]
        lines += [f"{i+1}. {x['description']} | Qty {x['quantity']} × {x['unit_price']} = {x['amount']} {payload.currency}" for i,x in enumerate(items)]
        lines += ["",f"Subtotal: {cents(subtotal)} {payload.currency}",f"Discount: {discount} {payload.currency}",f"Tax ({payload.tax_rate}%): {tax} {payload.currency}",f"TOTAL DUE: {total} {payload.currency}","",f"Payment terms: {payload.payment_terms.strip()}"]
        if payload.notes.strip(): lines += ["", "Notes:",payload.notes.strip()]
        content="\n".join(x for x in lines if x is not None)
        fid=_save(str(user["id"]),f"Invoice {number}",content,"pdf","invoice")
        return {"invoice":{"id":fid,"invoice_number":number,"currency":payload.currency,"items":items,"subtotal":str(cents(subtotal)),"discount":str(discount),"tax_rate":str(payload.tax_rate),"tax":str(tax),"total":str(total),"title":f"Invoice {number}","content":content,"preview_url":f"/v1/business/files/{fid}","download_url":f"/v1/business/files/{fid}/download"}}
    return router
