"""Database-backed email/password authentication for RZ Agent."""
from __future__ import annotations
import hashlib, os, secrets, time
from datetime import datetime, timedelta, timezone
import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field

router = APIRouter(prefix="/v1/auth", tags=["authentication"])
_hasher = PasswordHasher()
SESSION_COOKIE = "rz_session"
SESSION_DAYS = int(os.getenv("AUTH_SESSION_DAYS", "7"))
COOKIE_SECURE = os.getenv("APP_ENV", "development").lower() == "production"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)

class ApiTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(default_factory=lambda: ["mcp:read", "mcp:call"])
    expires_in_days: int | None = Field(default=90, ge=1, le=365)

ALLOWED_API_TOKEN_SCOPES = {"mcp:read", "mcp:call"}

def _db():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="Database authentication is not configured")
    return psycopg.connect(DATABASE_URL, connect_timeout=5)

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def _origin_check(request: Request):
    expected = os.getenv("APP_ORIGIN", "").strip().rstrip("/")
    origin = request.headers.get("origin", "").rstrip("/")
    if COOKIE_SECURE and not expected:
        raise HTTPException(status_code=503, detail="APP_ORIGIN must be configured in production")
    if expected and origin and origin != expected:
        raise HTTPException(status_code=403, detail="Origin not allowed")
    if COOKIE_SECURE and not origin:
        raise HTTPException(status_code=403, detail="Origin header required")

def _create_session(user_id: str, response: Response):
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    with _db() as conn:
        conn.execute("INSERT INTO auth_sessions (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
                     (user_id, _token_hash(token), expires))
    response.set_cookie("rz_session", token, max_age=SESSION_DAYS * 86400, expires=expires,
                        httponly=True, secure=COOKIE_SECURE, samesite="lax", path="/")

def get_current_user(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token or len(token) > 200:
        raise HTTPException(status_code=401, detail="Please sign in")
    with _db() as conn:
        row = conn.execute("""SELECT u.id::text, u.email FROM auth_sessions s
          JOIN auth_users u ON u.id = s.user_id
          WHERE s.token_hash = %s AND s.expires_at > now() AND u.disabled_at IS NULL""",
          (_token_hash(token),)).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Session expired; please sign in again")
        conn.execute("UPDATE auth_sessions SET last_seen_at = now() WHERE token_hash = %s",
                     (_token_hash(token),))
    return {"id": row[0], "email": row[1]}

def _limit(request: Request, action: str, maximum: int):
    now = time.time()
    attempts = request.app.state.auth_attempts
    bucket = f"{action}:{request.client.host if request.client else 'unknown'}"
    recent = [t for t in attempts.get(bucket, []) if now - t < 900]
    if len(recent) >= maximum:
        raise HTTPException(status_code=429, detail="Too many attempts; try again later")
    attempts[bucket] = recent + [now]

@router.get("/mode")
def auth_mode():
    return {"mode": "account" if DATABASE_URL else "api_key"}

@router.post("/register")
def register(credentials: Credentials, request: Request, response: Response):
    _origin_check(request)
    _limit(request, "register", 5)
    email = str(credentials.email).strip().lower()
    try:
        password_hash = _hasher.hash(credentials.password)
        with _db() as conn:
            row = conn.execute("INSERT INTO auth_users (email, password_hash) VALUES (%s, %s) RETURNING id::text, email",
                               (email, password_hash)).fetchone()
        _create_session(row[0], response)
        return {"authenticated": True, "user": {"id": row[0], "email": row[1]}}
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="An account with that email already exists") from None

@router.post("/login")
def login(credentials: Credentials, request: Request, response: Response):
    _origin_check(request)
    _limit(request, "login", 10)
    email = str(credentials.email).strip().lower()
    with _db() as conn:
        row = conn.execute("SELECT id::text, email, password_hash FROM auth_users WHERE email = %s AND disabled_at IS NULL",
                           (email,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    try:
        _hasher.verify(row[2], credentials.password)
    except (VerificationError, VerifyMismatchError):
        raise HTTPException(status_code=401, detail="Email or password is incorrect") from None
    if _hasher.check_needs_rehash(row[2]):
        with _db() as conn:
            conn.execute("UPDATE auth_users SET password_hash = %s WHERE id = %s",
                         (_hasher.hash(credentials.password), row[0]))
    _create_session(row[0], response)
    return {"authenticated": True, "user": {"id": row[0], "email": row[1]}}

@router.post("/logout")
def logout(request: Request, response: Response):
    _origin_check(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    if token and DATABASE_URL:
        with _db() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash = %s", (_token_hash(token),))
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return {"authenticated": False}

@router.post("/tokens")
def create_api_token(payload: ApiTokenCreate, user: dict = Depends(get_current_user)):
    name = payload.name.strip()
    scopes = sorted(set(payload.scopes))
    if not name:
        raise HTTPException(status_code=422, detail="Token name cannot be blank")
    if not scopes or not set(scopes).issubset(ALLOWED_API_TOKEN_SCOPES):
        raise HTTPException(status_code=422, detail="Allowed scopes: mcp:read, mcp:call")
    raw_token = "rzat_" + secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)
                  if payload.expires_in_days is not None else None)
    with _db() as conn:
        row = conn.execute(
            """INSERT INTO auth_api_tokens (user_id, name, token_hash, scopes, expires_at)
               VALUES (%s, %s, %s, %s, %s)
               RETURNING id::text, name, scopes, created_at, expires_at""",
            (user["id"], name, _token_hash(raw_token), scopes, expires_at),
        ).fetchone()
    return {"token": raw_token, "token_id": row[0], "name": row[1],
            "scopes": list(row[2]), "created_at": row[3], "expires_at": row[4],
            "warning": "Copy this token now. It cannot be displayed again."}

@router.get("/tokens")
def list_api_tokens(user: dict = Depends(get_current_user)):
    with _db() as conn:
        rows = conn.execute(
            """SELECT id::text, name, scopes, created_at, last_used_at, expires_at, revoked_at
               FROM auth_api_tokens WHERE user_id = %s ORDER BY created_at DESC""",
            (user["id"],),
        ).fetchall()
    return {"tokens": [{"id": r[0], "name": r[1], "scopes": list(r[2]),
                         "created_at": r[3], "last_used_at": r[4], "expires_at": r[5],
                         "revoked_at": r[6]} for r in rows]}

@router.delete("/tokens/{token_id}")
def revoke_api_token(token_id: str, user: dict = Depends(get_current_user)):
    import uuid
    try:
        token_uuid = str(uuid.UUID(token_id))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail="Token not found") from None
    with _db() as conn:
        row = conn.execute(
            """UPDATE auth_api_tokens SET revoked_at = now()
               WHERE id = %s AND user_id = %s AND revoked_at IS NULL RETURNING id::text""",
            (token_uuid, user["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"revoked": True, "token_id": row[0]}

def get_user_from_bearer(authorization: str, required_scope: str | None = None) -> dict:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.startswith("rzat_") or len(token) > 200:
        raise HTTPException(status_code=401, detail="Valid RZ Agent bearer token required")
    with _db() as conn:
        row = conn.execute(
            """SELECT u.id::text, u.email, t.scopes
               FROM auth_api_tokens t JOIN auth_users u ON u.id = t.user_id
               WHERE t.token_hash = %s AND t.revoked_at IS NULL
                 AND (t.expires_at IS NULL OR t.expires_at > now())
                 AND u.disabled_at IS NULL""",
            (_token_hash(token),),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="API token is invalid, expired, or revoked")
        scopes = set(row[2] or [])
        if required_scope and required_scope not in scopes:
            raise HTTPException(status_code=403, detail=f"Token lacks required scope: {required_scope}")
        conn.execute("UPDATE auth_api_tokens SET last_used_at = now() WHERE token_hash = %s",
                     (_token_hash(token),))
    return {"id": row[0], "email": row[1], "scopes": sorted(scopes), "auth_type": "api_token"}

@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return {"authenticated": True, "user": user}
