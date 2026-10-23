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

class _PostgresCompatConnection:
    """Small adapter so this module can keep SQLite placeholders locally.

    PostgreSQL is selected only when this clone's own DATABASE_URL is set.
    Never point it at the original RZ Agent or SafeNestT database.
    """
    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=()):
        return self._connection.execute(sql.replace("?", "%s"), params)

    def commit(self):
        return self._connection.commit()

    def close(self):
        return self._connection.close()


def _db():
    dsn = os.getenv("DATABASE_URL", "").strip()
    if dsn:
        import psycopg
        conn = _PostgresCompatConnection(psycopg.connect(dsn, connect_timeout=5))
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS business_files(
              user_id TEXT NOT NULL, file_id TEXT NOT NULL, title TEXT NOT NULL,
              format TEXT NOT NULL, content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'document',
              created_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(user_id,file_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS business_profiles(
              user_id TEXT PRIMARY KEY, business_name TEXT NOT NULL DEFAULT '',
              industry TEXT NOT NULL DEFAULT '', target_customers TEXT NOT NULL DEFAULT '',
              offer TEXT NOT NULL DEFAULT '', goals TEXT NOT NULL DEFAULT '',
              brand_voice TEXT NOT NULL DEFAULT '', constraints TEXT NOT NULL DEFAULT '',
              updated_at DOUBLE PRECISION NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS business_clients(
              user_id TEXT NOT NULL, client_id TEXT NOT NULL, name TEXT NOT NULL,
              company TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
              stage TEXT NOT NULL, expected_value TEXT NOT NULL DEFAULT '0', next_follow_up TEXT NOT NULL DEFAULT '',
              notes TEXT NOT NULL DEFAULT '', created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
              PRIMARY KEY(user_id,client_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS business_growth_plans(
              user_id TEXT NOT NULL, plan_date TEXT NOT NULL, primary_goal TEXT NOT NULL,
              hours_available INTEGER NOT NULL, success_metric TEXT NOT NULL DEFAULT '',
              plan_text TEXT NOT NULL, reflection TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'active', created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
              PRIMARY KEY(user_id,plan_date))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS business_client_activities(
              user_id TEXT NOT NULL, activity_id TEXT NOT NULL, client_id TEXT NOT NULL,
              activity_type TEXT NOT NULL, summary TEXT NOT NULL, next_step TEXT NOT NULL DEFAULT '',
              created_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(user_id,activity_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS business_workflows(
              user_id TEXT NOT NULL, workflow_id TEXT NOT NULL, title TEXT NOT NULL,
              goal TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
              approval_status TEXT NOT NULL DEFAULT 'pending', steps_json TEXT NOT NULL,
              audit_json TEXT NOT NULL DEFAULT '[]', created_at DOUBLE PRECISION NOT NULL,
              updated_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(user_id,workflow_id))""")
            conn.commit()
            return conn
        except Exception:
            conn.close()
            raise

    # Local development fallback only. Hosted SQLite is not durable on an
    # ephemeral filesystem; configure this clone's dedicated PostgreSQL URL.
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
    conn.execute("""CREATE TABLE IF NOT EXISTS business_growth_plans(
      user_id TEXT NOT NULL, plan_date TEXT NOT NULL, primary_goal TEXT NOT NULL,
      hours_available INTEGER NOT NULL, success_metric TEXT NOT NULL DEFAULT '',
      plan_text TEXT NOT NULL, reflection TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL, updated_at REAL NOT NULL,
      PRIMARY KEY(user_id,plan_date))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS business_client_activities(
      user_id TEXT NOT NULL, activity_id TEXT NOT NULL, client_id TEXT NOT NULL,
      activity_type TEXT NOT NULL, summary TEXT NOT NULL, next_step TEXT NOT NULL DEFAULT '',
      created_at REAL NOT NULL, PRIMARY KEY(user_id,activity_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS business_workflows(
      user_id TEXT NOT NULL, workflow_id TEXT NOT NULL, title TEXT NOT NULL,
      goal TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
      approval_status TEXT NOT NULL DEFAULT 'pending', steps_json TEXT NOT NULL,
      audit_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL,
      updated_at REAL NOT NULL, PRIMARY KEY(user_id,workflow_id))""")
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


class GrowthPlanSave(BaseModel):
    plan_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    primary_goal: str = Field(min_length=1, max_length=1500)
    hours_available: int = Field(ge=1, le=16)
    success_metric: str = Field(default="", max_length=500)
    plan_text: str = Field(min_length=1, max_length=12000)
    reflection: str = Field(default="", max_length=2500)
    status: Literal["active", "completed"] = "active"


def _growth_dict(row):
    return {"plan_date": row[0], "primary_goal": row[1], "hours_available": row[2],
            "success_metric": row[3], "plan_text": row[4], "reflection": row[5],
            "status": row[6], "created_at": row[7], "updated_at": row[8]}


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


class ClientActivityCreate(BaseModel):
    activity_type: Literal["call", "email", "meeting", "proposal", "note"] = "note"
    summary: str = Field(min_length=1, max_length=1500)
    next_step: str = Field(default="", max_length=500)

class WorkflowStepCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    action_type: Literal["prepare", "internal_update", "external_action"] = "prepare"
    description: str = Field(default="", max_length=1500)

class WorkflowCreate(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    goal: str = Field(min_length=1, max_length=1200)
    steps: list[WorkflowStepCreate] = Field(min_length=1, max_length=20)

class WorkflowStepUpdate(BaseModel):
    status: Literal["in_progress", "completed", "blocked", "pending"]
    evidence: str = Field(default="", max_length=2000)


class InvoiceItem(BaseModel):
    description: str = Field(min_length=1, max_length=300)
    quantity: Decimal = Field(gt=0, le=1000000)
    unit_price: Decimal = Field(ge=0, le=100000000)

class InvoiceCreate(BaseModel):
    invoice_number: str = Field(default="", max_length=80)
    business_name: str = Field(default="", max_length=180)
    business_email: str = Field(default="", max_length=254)
    business_address: str = Field(default="", max_length=500)
    business_address_line1: str = Field(default="", max_length=180)
    business_address_line2: str = Field(default="", max_length=180)
    business_city: str = Field(default="", max_length=100)
    business_state: str = Field(default="", max_length=100)
    business_zip: str = Field(default="", max_length=30)
    business_country: str = Field(default="", max_length=100)
    business_phone: str = Field(default="", max_length=80)
    company_logo_data: str = Field(default="", max_length=200000)
    customer_name: str = Field(min_length=1, max_length=180)
    customer_company: str = Field(default="", max_length=180)
    customer_attention: str = Field(default="", max_length=180)
    customer_email: str = Field(default="", max_length=254)
    customer_phone: str = Field(default="", max_length=80)
    customer_address: str = Field(default="", max_length=500)
    customer_address_line1: str = Field(default="", max_length=180)
    customer_address_line2: str = Field(default="", max_length=180)
    customer_city: str = Field(default="", max_length=100)
    customer_state: str = Field(default="", max_length=100)
    customer_zip: str = Field(default="", max_length=30)
    customer_country: str = Field(default="", max_length=100)
    issue_date: str = Field(default="", max_length=30)
    due_date: str = Field(default="", max_length=30)
    currency: str = Field(default="USD", pattern="^[A-Z]{3}$")
    tax_rate: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    discount: Decimal = Field(default=Decimal("0"), ge=0, le=100000000)
    payment_terms: str = Field(default="Due upon receipt", max_length=300)
    payment_method: str = Field(default="", max_length=120)
    bank_name: str = Field(default="", max_length=180)
    account_number: str = Field(default="", max_length=100)
    routing_number: str = Field(default="", max_length=100)
    swift_code: str = Field(default="", max_length=100)
    additional_notes: str = Field(default="", max_length=3000)
    notes: str = Field(default="", max_length=3000)
    prepared_by_name: str = Field(default="", max_length=180)
    prepared_by_title: str = Field(default="", max_length=180)
    prepared_by_date: str = Field(default="", max_length=30)
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

def _invoice_pdf_bytes(title: str, content: str) -> bytes:
    """Render an invoice as a clean, paginated PDF with line-item and totals tables."""
    import base64
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT, TA_LEFT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, KeepTogether
    marker = "\n<!--MIA_INVOICE_DATA:"
    data = {}
    if marker in content:
        try:
            data = json.loads(content.split(marker, 1)[1].split("-->", 1)[0])
        except (ValueError, IndexError, TypeError):
            data = {}
    if not data:
        return _pdf_bytes(title, content)
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=letter, rightMargin=.58*inch, leftMargin=.58*inch, topMargin=.5*inch, bottomMargin=.55*inch, title=title, author=str(data.get("prepared_by_name") or data.get("business_name") or "MIA Business AI"))
    navy = colors.HexColor("#24364b"); blue = colors.HexColor("#426a9c"); pale = colors.HexColor("#eef3f8"); line = colors.HexColor("#d6dee8"); muted = colors.HexColor("#596779"); ink = colors.HexColor("#202b39")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="InvoiceTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=24, leading=28, textColor=navy, alignment=TA_RIGHT, spaceAfter=2))
    styles.add(ParagraphStyle(name="InvoiceMeta", parent=styles["Normal"], fontSize=9, leading=13, textColor=muted, alignment=TA_RIGHT))
    styles.add(ParagraphStyle(name="InvoiceSection", parent=styles["Heading3"], fontSize=9, leading=12, textColor=blue, spaceAfter=5, uppercase=True))
    styles.add(ParagraphStyle(name="InvoiceBody", parent=styles["BodyText"], fontSize=9, leading=13, textColor=ink, spaceAfter=1))
    styles.add(ParagraphStyle(name="InvoiceSmall", parent=styles["BodyText"], fontSize=8, leading=11, textColor=muted))
    styles.add(ParagraphStyle(name="InvoiceCell", parent=styles["BodyText"], fontSize=8.5, leading=11, textColor=ink))
    styles.add(ParagraphStyle(name="InvoiceCellRight", parent=styles["InvoiceCell"], alignment=TA_RIGHT))
    styles.add(ParagraphStyle(name="InvoiceWhite", parent=styles["InvoiceCell"], fontName="Helvetica-Bold", textColor=colors.white))
    P=lambda value, style="InvoiceBody": Paragraph(escape(str(value or "")).replace("\n","<br/>") or "—", styles[style])
    def address(prefix):
        result=[]
        for key in (f"{prefix}_address_line1", f"{prefix}_address_line2"):
            if data.get(key): result.append(str(data[key]))
        cityline = ", ".join(str(data[k]) for k in (f"{prefix}_city", f"{prefix}_state") if data.get(k))
        if data.get(f"{prefix}_zip"): cityline = (cityline + " " if cityline else "") + str(data[f"{prefix}_zip"])
        if cityline: result.append(cityline)
        if data.get(f"{prefix}_country"): result.append(str(data[f"{prefix}_country"]))
        if not result and data.get(f"{prefix}_address"): result.append(str(data[f"{prefix}_address"]))
        return result
    story=[]
    brand=[]
    logo=data.get("company_logo_data", "")
    if logo:
        try:
            raw=base64.b64decode(logo.split(",",1)[-1], validate=True)
            if len(raw) <= 140000 and (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")):
                im=Image(io.BytesIO(raw), width=1.35*inch, height=.65*inch, kind="proportional"); im._restrictSize(1.35*inch,.65*inch); brand.append(im)
        except Exception:
            pass
    if not brand: brand.append(Paragraph("<b>"+escape(str(data.get("business_name") or "Your Business"))+"</b>", ParagraphStyle("Wordmark",parent=styles["Heading2"],fontSize=15,leading=18,textColor=navy)))
    brand.append(Spacer(1,5))
    issuer=[P(x) for x in [*address("business"), data.get("business_email"), data.get("business_phone")] if x]
    brand.extend(issuer or [P("Business contact details not provided.","InvoiceSmall")])
    meta=[Paragraph("INVOICE",styles["InvoiceTitle"]),Paragraph("Invoice # " + escape(str(data.get("invoice_number") or "")),styles["InvoiceMeta"]),Paragraph("Date: " + escape(str(data.get("issue_date") or "Not specified")),styles["InvoiceMeta"]),Paragraph("Due: " + escape(str(data.get("due_date") or "Not specified")),styles["InvoiceMeta"])]
    header=Table([[brand,meta]],colWidths=[3.45*inch,3.45*inch]);header.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("ALIGN",(1,0),(1,0),"RIGHT"),("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),12)]));story += [header,Spacer(1,12)]
    customer=[P("BILL TO","InvoiceSection")]
    if data.get("customer_company"): customer.append(P(data["customer_company"]))
    customer.append(P(data.get("customer_name")))
    if data.get("customer_attention"): customer.append(P("Attention: "+str(data["customer_attention"]),"InvoiceSmall"))
    customer += [P(x) for x in [*address("customer"),data.get("customer_email"),data.get("customer_phone")] if x]
    if data.get("customer_address") and not address("customer"): customer.append(P(data["customer_address"]))
    if data.get("customer_name"):
        billbox=Table([[customer]],colWidths=[3.45*inch]);billbox.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),pale),("BOX",(0,0),(-1,-1),.5,line),("LEFTPADDING",(0,0),(-1,-1),10),("RIGHTPADDING",(0,0),(-1,-1),10),("TOPPADDING",(0,0),(-1,-1),9),("BOTTOMPADDING",(0,0),(-1,-1),9)]));story += [billbox,Spacer(1,18)]
    item_rows=[[P("DESCRIPTION","InvoiceWhite"),P("QTY","InvoiceWhite"),P("UNIT PRICE","InvoiceWhite"),P("TOTAL","InvoiceWhite")]]
    currency=str(data.get("currency") or "USD")
    for item in data.get("items",[]):
        item_rows.append([P(item.get("description")),P(item.get("quantity"),"InvoiceCellRight"),P(currency+" "+str(item.get("unit_price")),"InvoiceCellRight"),P(currency+" "+str(item.get("amount")),"InvoiceCellRight")])
    items_table=Table(item_rows,colWidths=[3.5*inch,.65*inch,1.25*inch,1.25*inch],repeatRows=1,hAlign="LEFT")
    items_table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),navy),("VALIGN",(0,0),(-1,-1),"TOP"),("GRID",(0,0),(-1,-1),.35,line),("LEFTPADDING",(0,0),(-1,-1),7),("RIGHTPADDING",(0,0),(-1,-1),7),("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,pale])]))
    story += [items_table,Spacer(1,10)]
    totals=[]
    for label,key in (("Subtotal","subtotal"),("Discount","discount"),("Tax ("+str(data.get("tax_rate",0))+"%)","tax"),("TOTAL AMOUNT DUE","total")):
        if label=="Discount" and Decimal(str(data.get(key,"0")))==0: continue
        style="InvoiceWhite" if key=="total" else "InvoiceCellRight"
        totals.append([P(label,style if key=="total" else "InvoiceCell"),P(currency+" "+str(data.get(key,"0.00")),style)])
    total_table=Table(totals,colWidths=[1.8*inch,1.45*inch],hAlign="RIGHT")
    total_table.setStyle(TableStyle([("ALIGN",(1,0),(-1,-1),"RIGHT"),("BACKGROUND",(0,-1),(-1,-1),navy),("LINEABOVE",(0,0),(-1,-2),.4,line),("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
    story += [total_table,Spacer(1,18)]
    payment=[P("PAYMENT INSTRUCTIONS","InvoiceSection")]
    for label,key in (("Payment method","payment_method"),("Terms","payment_terms"),("Bank","bank_name"),("Account number","account_number"),("Routing number","routing_number"),("SWIFT code","swift_code")):
        if data.get(key): payment.append(P(label+": "+str(data[key])))
    if not any(data.get(k) for k in ("payment_method","bank_name","account_number","routing_number","swift_code")) and not data.get("payment_terms"):
        payment.append(P("Payment instructions not provided; add them before sending.","InvoiceSmall"))
    story.append(KeepTogether(payment))
    notes=data.get("additional_notes") or data.get("notes")
    if notes: story += [Spacer(1,10),P("NOTE / TERMS","InvoiceSection"),P(notes)]
    prepared=[x for x in (data.get("prepared_by_name"),data.get("prepared_by_title"),data.get("prepared_by_date")) if x]
    if prepared: story += [Spacer(1,16),P("PREPARED BY","InvoiceSection"),P(" · ".join(prepared))]
    contact=[x for x in (data.get("business_email"),data.get("business_phone")) if x]
    if contact: story += [Spacer(1,8),P("CONTACT","InvoiceSection"),P(" · ".join(contact),"InvoiceSmall")]
    story += [Spacer(1,18),P("Thank you for your business.","InvoiceSection")]
    def footer(canvas, document):
        canvas.saveState();canvas.setStrokeColor(line);canvas.line(.58*inch,.4*inch,7.92*inch,.4*inch);canvas.setFont("Helvetica",7.5);canvas.setFillColor(muted);canvas.drawString(.58*inch,.25*inch,str(data.get("business_name") or "Your Business"));canvas.drawRightString(7.92*inch,.25*inch,f"Page {document.page}");canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    return out.getvalue()



def _invoice_markdown_as_structured(title: str, content: str) -> str | None:
    """Recognize an AI-written invoice template and route it through the designed invoice PDF."""
    plain = content.replace("\r", "")
    if not re.search(r"(?im)^\s*(?:#{1,3}\s*)?(?:\*\*)?Invoice(?:\*\*)?\s*$", plain):
        return None
    if not re.search(r"(?i)Invoice To:|Invoice From:|Bill To:|Description of Services", plain):
        return None

    def field(pattern: str, default: str = "") -> str:
        match = re.search(pattern, plain, re.I | re.M)
        if not match:
            return default
        value = re.sub(r"\*+", "", match.group(1)).strip().strip("[]").strip()
        if not value or re.search(r"placeholder|not provided|to be filled|your business", value, re.I):
            return ""
        return value

    data = {
        "business_name": field(r"Business Name:\s*([^\n]+)", "SafeNestT"),
        "business_email": field(r"(?:Business )?Email:\s*([^\n]+)"),
        "business_phone": field(r"(?:Business )?Phone:\s*([^\n]+)"),
        "business_address": field(r"(?:Business )?Address:\s*([^\n]+)"),
        "customer_name": field(r"Client Name:\s*([^\n]+)"),
        "customer_email": field(r"Email:\s*([^\n]+)"),
        "customer_phone": field(r"Phone:\s*([^\n]+)"),
        "customer_address": field(r"Address:\s*([^\n]+)"),
        "invoice_number": field(r"Invoice Number:\s*([^\n]+)") or "DRAFT",
        "issue_date": field(r"Invoice Date:\s*([^\n]+)") or "Not specified",
        "due_date": field(r"Due Date:\s*([^\n]+)") or "Not specified",
        "currency": "USD",
        "items": [],
        "subtotal": "Pending",
        "discount": "0.00",
        "tax_rate": "—",
        "tax": "Pending",
        "total": "Pending details",
        "payment_terms": field(r"Payment due by\s*([^\n]+)"),
        "payment_method": field(r"\[([^\]\n]*(?:bank|payment|transfer)[^\]\n]*)\]"),
        "additional_notes": field(r"Notes:\s*([^\n]+)"),
    }
    # Extract service names and details from numbered or bulleted AI invoice templates.
    sections = re.split(r"(?im)^\s*(?:\d+\.\s*)?(?:[-*]\s*)?\*\*Service\s+\d+\s*:\*\*\s*", plain)
    for section in sections[1:]:
        name_match = re.match(r"([^\n]+)", section)
        if not name_match:
            continue
        name = name_match.group(1).strip().rstrip(":")
        detail_match = re.search(r"(?im)^\s*[-*]?\s*\*\*Details:\*\*\s*([^\n]+)", section)
        quantity_match = re.search(r"(?im)^\s*[-*]?\s*\*\*(?:Hours/Units|Quantity|Qty):\*\*\s*([^\n]+)", section)
        rate_match = re.search(r"(?im)^\s*[-*]?\s*\*\*Rate:\*\*\s*([^\n]+)", section)
        detail = (detail_match.group(1).strip() if detail_match else "")
        item_description = name + (": " + detail if detail and not re.search(r"placeholder", detail, re.I) else "")
        quantity = (quantity_match.group(1).strip() if quantity_match else "")
        rate = (rate_match.group(1).strip() if rate_match else "")
        for value in (quantity, rate):
            if re.search(r"placeholder|^\[?\s*\]?$", value, re.I):
                if value == quantity: quantity = ""
                if value == rate: rate = ""
        data["items"].append({
            "description": item_description,
            "quantity": quantity or "—",
            "unit_price": rate.replace("$", "").strip() or "—",
            "amount": "—",
        })
    if not data["items"]:
        # Some models render service names as bold bullets rather than numbered sections.
        for match in re.finditer(r"(?im)^\s*[-*]\s*\*\*([^*]+)\*\*\s*$", plain):
            name = match.group(1).strip()
            if name.lower() not in {"invoice to:", "invoice from:", "description of services"}:
                data["items"].append({"description": name, "quantity": "—", "unit_price": "—", "amount": "—"})
    if not data["items"]:
        return None
    data["additional_notes"] = data["additional_notes"] or "Draft invoice — complete all missing client, billing, and service details before sending."
    return content.split("\n<!--MIA_INVOICE_DATA:", 1)[0] + "\n<!--MIA_INVOICE_DATA:" + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "-->"

def _render(title: str, content: str, fmt: str) -> tuple[bytes,str]:
    if fmt not in FORMATS: raise HTTPException(status_code=422, detail=f"Unsupported format. Choose: {', '.join(sorted(FORMATS))}")
    if fmt == "pdf":
        if "<!--MIA_INVOICE_DATA:" in content: return _invoice_pdf_bytes(title, content), MIME[fmt]
        structured_invoice = _invoice_markdown_as_structured(title, content)
        if structured_invoice: return _invoice_pdf_bytes(title, structured_invoice), MIME[fmt]
        return _pdf_bytes(title, content), MIME[fmt]
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

def _workflow_dict(row):
    return {"id": row[0], "title": row[1], "goal": row[2], "status": row[3],
            "approval_status": row[4], "steps": json.loads(row[5]), "audit": json.loads(row[6]),
            "created_at": row[7], "updated_at": row[8]}


def create_router(auth_dependency):
    router=APIRouter(prefix="/v1/business",tags=["MIA Business Documents"])

    @router.get("/overview")
    def business_overview(user:dict=Depends(auth_dependency)):
        """Return owner-scoped business KPIs and actionable follow-ups."""
        from datetime import date, timedelta
        uid = str(user["id"])
        today = date.today().isoformat()
        next_week = (date.today() + timedelta(days=7)).isoformat()
        conn = _db()
        try:
            clients = conn.execute(
                "SELECT client_id,name,company,stage,expected_value,next_follow_up FROM business_clients WHERE user_id=? ORDER BY CASE WHEN next_follow_up='' THEN 1 ELSE 0 END, next_follow_up ASC, updated_at DESC",
                (uid,),
            ).fetchall()
            file_counts = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN kind='invoice' THEN 1 ELSE 0 END) FROM business_files WHERE user_id=?",
                (uid,),
            ).fetchone()
            plans = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) FROM business_growth_plans WHERE user_id=?",
                (uid,),
            ).fetchone()
        finally:
            conn.close()
        active = [r for r in clients if r[3] not in {"won", "lost"}]
        pipeline = sum((Decimal(r[4] or "0") for r in active), Decimal("0"))
        stage_counts = {stage: sum(1 for r in clients if r[3] == stage) for stage in ("lead", "contacted", "qualified", "proposal", "won", "lost")}
        closed = stage_counts["won"] + stage_counts["lost"]
        closed_win_rate = round(stage_counts["won"] * 100 / closed, 1) if closed else None
        due = [r for r in active if r[5] and r[5] <= today]
        upcoming = [r for r in active if r[5] and today < r[5] <= next_week]
        followups = due + upcoming
        return {
            "as_of": today,
            "clients_total": len(clients),
            "active_leads": len(active),
            "won_clients": stage_counts["won"],
            "stage_counts": stage_counts,
            "closed_deals": closed,
            "closed_win_rate_percent": closed_win_rate,
            "open_pipeline_value": str(pipeline.quantize(Decimal("0.01"))),
            "followups_due": len(due),
            "followups_next_7_days": len(upcoming),
            "documents_total": int(file_counts[0] or 0),
            "invoices_total": int(file_counts[1] or 0),
            "growth_plans_total": int(plans[0] or 0),
            "growth_plans_completed": int(plans[1] or 0),
            "followups": [{"id": r[0], "name": r[1], "company": r[2], "stage": r[3], "expected_value": str(r[4] or "0"), "next_follow_up": r[5]} for r in followups[:8]],
        }

    @router.get("/workflows")
    def list_business_workflows(limit: int = 20, user: dict = Depends(auth_dependency)):
        if not 1 <= limit <= 50:
            raise HTTPException(status_code=422, detail="limit must be from 1 to 50")
        conn = _db()
        try:
            rows = conn.execute("SELECT workflow_id,title,goal,status,approval_status,steps_json,audit_json,created_at,updated_at FROM business_workflows WHERE user_id=? ORDER BY updated_at DESC LIMIT ?", (str(user["id"]), limit)).fetchall()
        finally:
            conn.close()
        return {"workflows": [_workflow_dict(row) for row in rows], "count": len(rows)}

    @router.post("/workflows")
    def create_business_workflow(payload: WorkflowCreate, user: dict = Depends(auth_dependency)):
        title, goal = payload.title.strip(), payload.goal.strip()
        if not title or not goal:
            raise HTTPException(status_code=422, detail="Workflow title and goal cannot be blank")
        steps = [{"index": i, "title": item.title.strip(), "action_type": item.action_type,
                  "description": item.description.strip(), "status": "pending", "evidence": ""}
                 for i, item in enumerate(payload.steps)]
        if any(not step["title"] for step in steps):
            raise HTTPException(status_code=422, detail="Workflow step titles cannot be blank")
        wid, now = uuid.uuid4().hex, time.time()
        audit = [{"at": now, "event": "workflow_created", "detail": "Draft created; no actions executed."}]
        conn = _db()
        try:
            conn.execute("INSERT INTO business_workflows(user_id,workflow_id,title,goal,status,approval_status,steps_json,audit_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (str(user["id"]), wid, title, goal, "draft", "pending", json.dumps(steps), json.dumps(audit), now, now))
            conn.commit()
        finally:
            conn.close()
        return {"workflow": {"id": wid, "title": title, "goal": goal, "status": "draft", "approval_status": "pending", "steps": steps, "audit": audit, "created_at": now, "updated_at": now}}

    @router.post("/workflows/{workflow_id}/approve")
    def approve_business_workflow(workflow_id: str, user: dict = Depends(auth_dependency)):
        if not re.fullmatch(r"[a-f0-9]{32}", workflow_id):
            raise HTTPException(status_code=400, detail="Invalid workflow id")
        uid, now, conn = str(user["id"]), time.time(), _db()
        try:
            row = conn.execute("SELECT title,goal,status,approval_status,steps_json,audit_json,created_at FROM business_workflows WHERE user_id=? AND workflow_id=?", (uid, workflow_id)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Workflow not found")
            if row[3] == "approved":
                return {"workflow": {"id": workflow_id, "title": row[0], "goal": row[1], "status": row[2], "approval_status": row[3], "steps": json.loads(row[4]), "audit": json.loads(row[5]), "created_at": row[6], "updated_at": now}, "already_approved": True}
            if row[3] != "pending" or row[2] != "draft":
                raise HTTPException(status_code=409, detail="Only a pending draft can be approved")
            steps, audit = json.loads(row[4]), json.loads(row[5])
            audit.append({"at": now, "event": "workflow_approved", "detail": "Owner explicitly approved this workflow plan."})
            conn.execute("UPDATE business_workflows SET status='approved',approval_status='approved',audit_json=?,updated_at=? WHERE user_id=? AND workflow_id=?", (json.dumps(audit), now, uid, workflow_id)); conn.commit()
            result = {"id": workflow_id, "title": row[0], "goal": row[1], "status": "approved", "approval_status": "approved", "steps": steps, "audit": audit, "created_at": row[6], "updated_at": now}
        finally:
            conn.close()
        return {"workflow": result, "note": "Approval authorizes this workflow plan, but does not claim an external integration action has run. Each step requires a verified result."}

    @router.patch("/workflows/{workflow_id}/steps/{step_index}")
    def update_business_workflow_step(workflow_id: str, step_index: int, payload: WorkflowStepUpdate, user: dict = Depends(auth_dependency)):
        if not re.fullmatch(r"[a-f0-9]{32}", workflow_id):
            raise HTTPException(status_code=400, detail="Invalid workflow id")
        uid, now, conn = str(user["id"]), time.time(), _db()
        try:
            row = conn.execute("SELECT title,goal,status,approval_status,steps_json,audit_json,created_at FROM business_workflows WHERE user_id=? AND workflow_id=?", (uid, workflow_id)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Workflow not found")
            if row[3] != "approved":
                raise HTTPException(status_code=409, detail="Approve the workflow before updating steps")
            steps, audit = json.loads(row[4]), json.loads(row[5])
            if not 0 <= step_index < len(steps):
                raise HTTPException(status_code=404, detail="Workflow step not found")
            evidence = payload.evidence.strip()
            if payload.status == "completed" and not evidence:
                raise HTTPException(status_code=422, detail="Completed steps require verification evidence")
            step = steps[step_index]
            step["status"], step["evidence"] = payload.status, evidence
            audit.append({"at": now, "event": "step_" + payload.status, "step_index": step_index, "detail": evidence or "Status updated without completion claim."})
            all_done = all(item["status"] == "completed" for item in steps)
            any_active = any(item["status"] == "in_progress" for item in steps)
            any_blocked = any(item["status"] == "blocked" for item in steps)
            status = "completed" if all_done else "blocked" if any_blocked and not any_active else "in_progress" if any_active or any(item["status"] == "completed" for item in steps) else "approved"
            conn.execute("UPDATE business_workflows SET status=?,steps_json=?,audit_json=?,updated_at=? WHERE user_id=? AND workflow_id=?", (status, json.dumps(steps), json.dumps(audit), now, uid, workflow_id)); conn.commit()
            result = {"id": workflow_id, "title": row[0], "goal": row[1], "status": status, "approval_status": row[3], "steps": steps, "audit": audit, "created_at": row[6], "updated_at": now}
        finally:
            conn.close()
        return {"workflow": result}

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

    @router.get("/growth-plans")
    def list_growth_plans(limit:int=14,user:dict=Depends(auth_dependency)):
        if not 1 <= limit <= 60:
            raise HTTPException(status_code=422, detail="limit must be from 1 to 60")
        conn = _db()
        try:
            rows = conn.execute("SELECT plan_date,primary_goal,hours_available,success_metric,plan_text,reflection,status,created_at,updated_at FROM business_growth_plans WHERE user_id=? ORDER BY plan_date DESC LIMIT ?", (str(user["id"]),limit)).fetchall()
        finally:
            conn.close()
        return {"plans": [_growth_dict(row) for row in rows], "count": len(rows)}

    @router.post("/growth-plans")
    def save_growth_plan(payload:GrowthPlanSave,user:dict=Depends(auth_dependency)):
        from datetime import date
        try:
            date.fromisoformat(payload.plan_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="plan_date must be a valid calendar date") from None
        values = payload.model_dump()
        for key in ("primary_goal", "success_metric", "plan_text", "reflection"):
            values[key] = values[key].strip()
        if not values["primary_goal"] or not values["plan_text"]:
            raise HTTPException(status_code=422, detail="Goal and plan text cannot be blank")
        now = time.time(); uid = str(user["id"]); conn = _db()
        try:
            existing = conn.execute("SELECT created_at FROM business_growth_plans WHERE user_id=? AND plan_date=?", (uid, values["plan_date"])).fetchone()
            created = existing[0] if existing else now
            conn.execute("""INSERT INTO business_growth_plans(user_id,plan_date,primary_goal,hours_available,success_metric,plan_text,reflection,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,plan_date) DO UPDATE SET primary_goal=excluded.primary_goal,
                hours_available=excluded.hours_available,success_metric=excluded.success_metric,plan_text=excluded.plan_text,
                reflection=excluded.reflection,status=excluded.status,updated_at=excluded.updated_at""",
                (uid, values["plan_date"], values["primary_goal"], values["hours_available"], values["success_metric"], values["plan_text"], values["reflection"], values["status"], created, now))
            conn.commit()
        finally:
            conn.close()
        return {"plan": {**values, "created_at": created, "updated_at": now}, "saved": True}

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

    @router.get("/clients/{client_id}/activities")
    def list_client_activities(client_id:str,user:dict=Depends(auth_dependency)):
        if not re.fullmatch(r"[a-f0-9]{32}", client_id):
            raise HTTPException(status_code=400, detail="Invalid client id")
        uid=str(user["id"]); conn=_db()
        try:
            owner=conn.execute("SELECT 1 FROM business_clients WHERE user_id=? AND client_id=?",(uid,client_id)).fetchone()
            if not owner: raise HTTPException(status_code=404,detail="Client not found")
            rows=conn.execute("SELECT activity_id,activity_type,summary,next_step,created_at FROM business_client_activities WHERE user_id=? AND client_id=? ORDER BY created_at DESC LIMIT 100",(uid,client_id)).fetchall()
        finally: conn.close()
        return {"activities":[{"id":r[0],"type":r[1],"summary":r[2],"next_step":r[3],"created_at":r[4]} for r in rows],"count":len(rows)}

    @router.post("/clients/{client_id}/activities")
    def create_client_activity(client_id:str,payload:ClientActivityCreate,user:dict=Depends(auth_dependency)):
        if not re.fullmatch(r"[a-f0-9]{32}", client_id):
            raise HTTPException(status_code=400, detail="Invalid client id")
        uid=str(user["id"]); summary=payload.summary.strip(); next_step=payload.next_step.strip()
        if not summary: raise HTTPException(status_code=422,detail="Activity summary cannot be blank")
        aid=uuid.uuid4().hex; now=time.time(); conn=_db()
        try:
            owner=conn.execute("SELECT 1 FROM business_clients WHERE user_id=? AND client_id=?",(uid,client_id)).fetchone()
            if not owner: raise HTTPException(status_code=404,detail="Client not found")
            conn.execute("INSERT INTO business_client_activities(user_id,activity_id,client_id,activity_type,summary,next_step,created_at) VALUES(?,?,?,?,?,?,?)",(uid,aid,client_id,payload.activity_type,summary,next_step,now))
            if next_step:
                conn.execute("UPDATE business_clients SET notes=CASE WHEN notes='' THEN ? ELSE notes || ? END,updated_at=? WHERE user_id=? AND client_id=?",(f"Next step: {next_step}",f"\nNext step: {next_step}",now,uid,client_id))
            conn.commit()
        finally: conn.close()
        return {"activity":{"id":aid,"type":payload.activity_type,"summary":summary,"next_step":next_step,"created_at":now},"saved":True}

    @router.delete("/clients/{client_id}")
    def delete_business_client(client_id:str,user:dict=Depends(auth_dependency)):
        if not re.fullmatch(r"[a-f0-9]{32}", client_id):
            raise HTTPException(status_code=400, detail="Invalid client id")
        conn = _db()
        try:
            uid=str(user["id"])
            conn.execute("DELETE FROM business_client_activities WHERE user_id=? AND client_id=?",(uid,client_id))
            cur = conn.execute("DELETE FROM business_clients WHERE user_id=? AND client_id=?", (uid, client_id)); conn.commit()
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
        if payload.company_logo_data:
            import base64
            try: raw_logo=base64.b64decode(payload.company_logo_data.split(",",1)[-1],validate=True)
            except Exception: raise HTTPException(status_code=422,detail="Company logo must be a valid PNG or JPEG image") from None
            if len(raw_logo)>140000 or not (raw_logo.startswith(b"\x89PNG\r\n\x1a\n") or raw_logo.startswith(b"\xff\xd8\xff")):
                raise HTTPException(status_code=422,detail="Company logo must be PNG or JPEG and no larger than 140 KB")
        data=payload.model_dump(mode="json")
        data.update({"invoice_number":number,"items":items,"subtotal":str(cents(subtotal)),"discount":str(discount),"tax":str(tax),"total":str(total)})
        lines=[f"Invoice {number}","",payload.business_name.strip(),payload.business_email.strip(),payload.business_phone.strip(),payload.business_address.strip(),"",f"Bill to: {payload.customer_company.strip() or payload.customer_name.strip()}",payload.customer_name.strip(),payload.customer_email.strip(),payload.customer_phone.strip(),payload.customer_address.strip(),"",f"Issue date: {payload.issue_date or 'Not specified'}",f"Due date: {payload.due_date or 'Not specified'}",f"Currency: {payload.currency}","","ITEMS"]
        lines += [f"{i+1}. {x['description']} | Qty {x['quantity']} × {x['unit_price']} = {x['amount']} {payload.currency}" for i,x in enumerate(items)]
        lines += ["",f"Subtotal: {cents(subtotal)} {payload.currency}",f"Discount: {discount} {payload.currency}",f"Tax ({payload.tax_rate}%): {tax} {payload.currency}",f"TOTAL DUE: {total} {payload.currency}","",f"Payment terms: {payload.payment_terms.strip()}"]
        if payload.notes.strip() or payload.additional_notes.strip(): lines += ["", "Notes:",payload.additional_notes.strip() or payload.notes.strip()]
        content="\n".join(x for x in lines if x is not None)+"\n<!--MIA_INVOICE_DATA:"+json.dumps(data,ensure_ascii=False,separators=(",",":"))+"-->"
        fid=_save(str(user["id"]),f"Invoice {number}",content,"pdf","invoice")
        return {"invoice":{"id":fid,"invoice_number":number,"currency":payload.currency,"items":items,"subtotal":str(cents(subtotal)),"discount":str(discount),"tax_rate":str(payload.tax_rate),"tax":str(tax),"total":str(total),"title":f"Invoice {number}","content":content.split("\n<!--MIA_INVOICE_DATA:",1)[0],"preview_url":f"/v1/business/files/{fid}","download_url":f"/v1/business/files/{fid}/download"}}
    return router
