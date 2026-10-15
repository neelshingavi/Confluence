from __future__ import annotations

from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
import sys
import time
from urllib.parse import urlsplit
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.pool import NullPool
from starlette.middleware.sessions import SessionMiddleware

API_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(API_DIR))
BACKEND_DIR = API_DIR.parent / "backend"
if BACKEND_DIR.exists():
    sys.path.insert(0, str(BACKEND_DIR))

from app.models import Event, User, Attendee, Ticket, CheckIn, ScanAttempt, AuditLog, utcnow
from app.schemas import LoginInput, ScanInput
from app.security import verify_password, scan_fingerprint, validate_security_configuration

# Normalize database URL (handles postgres:// and Supabase pooler URIs)
RAW_DB_URL = os.getenv("SUPABASE_DATABASE_URL") or os.getenv("DATABASE_URL", "sqlite:///./confluence-dev.db")
if RAW_DB_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + RAW_DB_URL[len("postgres://"):]
elif RAW_DB_URL.startswith("postgresql://") and not RAW_DB_URL.startswith("postgresql+psycopg://"):
    DATABASE_URL = "postgresql+psycopg://" + RAW_DB_URL[len("postgresql://"):]
else:
    DATABASE_URL = RAW_DB_URL

# For serverless PostgreSQL on Vercel / Supabase, use NullPool
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True, connect_args=connect_args)
else:
    engine = create_engine(DATABASE_URL, poolclass=NullPool, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

_login_failures: dict[str, list[float]] = defaultdict(list)
_scan_requests: dict[str, list[float]] = defaultdict(list)
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_ATTEMPTS = 10
SCAN_WINDOW_SECONDS = 60
SCAN_MAX_ATTEMPTS_PER_USER = 600

# Hardcoded demo credentials (scanner-only access)
HARDCODED_EMAIL = "12345678"
HARDCODED_PASSWORD = "12345678"
HARDCODED_USER_ID = "00000000-0000-0000-0000-000000000001"
HARDCODED_SESSION_VERSION = "demo-v1"

def new_id() -> str:
    return str(uuid.uuid4())

def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def normalize_email(email: str | None) -> str | None:
    return (email or "").strip().casefold() or None

def audit(db: Session, event_id: str, actor_id: str | None, action: str, entity_type: str, entity_id: str | None = None, details: dict | None = None):
    db.add(AuditLog(id=new_id(), event_id=event_id, actor_user_id=actor_id, action=action, entity_type=entity_type,
                    entity_id=entity_id, details=details or {}))

def event_for(db: Session) -> Event:
    event = db.query(Event).filter(Event.slug == "confluence").first()
    if not event:
        raise HTTPException(503, "Confluence event is not initialized.")
    return event

def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(401, "Sign in required.")
    user = db.get(User, uid)
    if not user or not user.active or request.session.get("sv") != user.session_version:
        request.session.clear()
        raise HTTPException(401, "Session is invalid. Sign in again.")
    return user

def same_origin(request: Request):
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if origin and host and urlsplit(origin).netloc.casefold() != host.casefold():
        raise HTTPException(403, "Cross-origin request rejected.")

def require_csrf(request: Request, user: User = Depends(current_user)) -> User:
    same_origin(request)
    expected = request.session.get("csrf_token")
    supplied = request.headers.get("x-csrf-token")
    if not expected or not supplied or not secrets.compare_digest(str(expected), str(supplied)):
        raise HTTPException(403, "Security token missing or expired. Refresh the page and try again.")
    return user

def scan_status_http(result_code: str) -> int:
    return {"CHECKED_IN": 200, "ALREADY_CHECKED_IN": 409, "TICKET_REVOKED": 410, "INVALID_TICKET": 404}.get(result_code, 400)

def persist_scan_result(db: Session, event_id: str, user_id: str, request_id: str, fingerprint: str,
                        ticket_id: str | None, result_code: str, body: dict) -> JSONResponse:
    db.add(ScanAttempt(id=new_id(), event_id=event_id, scanner_user_id=user_id, request_id=request_id,
                       request_fingerprint=fingerprint, ticket_id=ticket_id, result_code=result_code,
                       response_json=body, created_at=utcnow()))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        previous = db.query(ScanAttempt).filter(ScanAttempt.scanner_user_id == user_id, ScanAttempt.request_id == request_id).first()
        if previous:
            if not secrets.compare_digest(previous.request_fingerprint, fingerprint):
                raise HTTPException(409, "This scan request ID was already used for a different ticket. Scan again.")
            return JSONResponse(status_code=scan_status_http(previous.result_code), content=previous.response_json)
        raise
    return JSONResponse(status_code=scan_status_http(result_code), content=body)

app = FastAPI(title="Confluence Gate Scanner", version="1.0.0", docs_url=None, redoc_url=None)

# Session middleware
session_secret = os.getenv("SESSION_SECRET", "session-secret-confluence-scanner-must-be-long-enough-32chars")
cookie_secure = os.getenv("COOKIE_SECURE", "true").lower() == "true"
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    session_cookie="confluence_session",
    max_age=60 * 60 * 12,
    same_site="lax",
    https_only=cookie_secure,
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
    if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/health/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response

# Health endpoints
@app.get("/api/health/live")
def live():
    return {"status": "ok", "mode": "scanner-only"}

@app.get("/api/health/ready")
def ready(db: Session = Depends(get_db)):
    try:
        db.execute(select(Event.id).limit(1)).first()
    except Exception as exc:
        raise HTTPException(503, "Database not ready.") from exc
    return {"status": "ready"}

# Auth endpoints
@app.get("/api/auth/me")
def auth_me(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("uid")
    if not uid:
        return {"authenticated": False, "scannerOnly": True}
    csrf = request.session.get("csrf_token")
    if not csrf:
        csrf = secrets.token_urlsafe(32)
        request.session["csrf_token"] = csrf
    # Hardcoded demo user session
    if request.session.get("hardcoded") and uid == HARDCODED_USER_ID:
        if request.session.get("sv") != HARDCODED_SESSION_VERSION:
            request.session.clear()
            return {"authenticated": False, "scannerOnly": True}
        event = db.query(Event).filter(Event.slug == "confluence").first()
        event_data = {"id": event.id, "name": event.name, "timezone": event.timezone} if event else {"id": "demo", "name": "Confluence", "timezone": "UTC"}
        return {
            "authenticated": True,
            "scannerOnly": True,
            "csrfToken": csrf,
            "user": {"id": HARDCODED_USER_ID, "email": HARDCODED_EMAIL, "role": "admin"},
            "event": event_data
        }
    user = db.get(User, uid)
    if not user or not user.active or request.session.get("sv") != user.session_version:
        request.session.clear()
        return {"authenticated": False, "scannerOnly": True}
    event = event_for(db)
    return {
        "authenticated": True,
        "scannerOnly": True,
        "csrfToken": csrf,
        "user": {"id": user.id, "email": user.email, "role": user.role},
        "event": {"id": event.id, "name": event.name, "timezone": event.timezone}
    }

@app.post("/api/auth/login")
def login(payload: LoginInput, request: Request, db: Session = Depends(get_db)):
    same_origin(request)
    now = time.time()
    # Hardcoded demo login — always works regardless of DB state
    if payload.email == HARDCODED_EMAIL and payload.password == HARDCODED_PASSWORD:
        request.session.clear()
        request.session["uid"] = HARDCODED_USER_ID
        request.session["sv"] = HARDCODED_SESSION_VERSION
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        request.session["created_at"] = int(now)
        request.session["hardcoded"] = True
        return {"authenticated": True, "scannerOnly": True}
    ip = request.client.host if request.client else "unknown"
    attempts = [ts for ts in _login_failures[ip] if now - ts < LOGIN_WINDOW_SECONDS]
    _login_failures[ip] = attempts
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        raise HTTPException(429, "Too many login attempts. Wait a few minutes and try again.")
    user = db.query(User).filter(User.email == normalize_email(payload.email)).first()
    if not user or not user.active or not verify_password(payload.password, user.password_hash):
        _login_failures[ip].append(now)
        raise HTTPException(401, "Email or password is incorrect.")
    _login_failures.pop(ip, None)
    request.session.clear()
    request.session["uid"] = user.id
    request.session["sv"] = user.session_version
    request.session["csrf_token"] = secrets.token_urlsafe(32)
    request.session["created_at"] = int(now)
    return {"authenticated": True, "scannerOnly": True}

@app.post("/api/auth/logout")
def logout(request: Request, _csrf: User = Depends(require_csrf)):
    request.session.clear()
    return {"ok": True}

# Check-in endpoints
@app.post("/api/check-ins")
def scan_checkin(payload: ScanInput, request: Request, user: User = Depends(current_user), _csrf: User = Depends(require_csrf), db: Session = Depends(get_db)):
    event = event_for(db)
    throttle_key = user.id
    now_epoch = time.time()
    recent_scan_requests = [ts for ts in _scan_requests[throttle_key] if now_epoch - ts < SCAN_WINDOW_SECONDS]
    if len(recent_scan_requests) >= SCAN_MAX_ATTEMPTS_PER_USER:
        _scan_requests[throttle_key] = recent_scan_requests
        raise HTTPException(429, "Scan rate limit reached. Pause briefly and try again.")
    recent_scan_requests.append(now_epoch)
    _scan_requests[throttle_key] = recent_scan_requests

    token_text = payload.qrToken.strip()
    if token_text.startswith("CFL1."):
        token_text = token_text[5:]
    try:
        fingerprint = scan_fingerprint(token_text)
    except RuntimeError as exc:
        raise HTTPException(503, "Ticket validation is not configured. Missing QR_TOKEN_PEPPER.") from exc

    # Check idempotency per scanner user
    previous = db.query(ScanAttempt).filter(ScanAttempt.scanner_user_id == user.id, ScanAttempt.request_id == payload.clientScanId).first()
    if previous:
        if not secrets.compare_digest(previous.request_fingerprint, fingerprint):
            raise HTTPException(409, "This scan request ID was already used for a different ticket. Scan again.")
        return JSONResponse(status_code=scan_status_http(previous.result_code), content=previous.response_json)

    # Lock ticket row
    ticket = db.query(Ticket).filter(Ticket.qr_token_hash == fingerprint).with_for_update().first()
    now = utcnow()
    if not ticket or ticket.event_id != event.id:
        body = {"result": "INVALID_TICKET", "message": "This QR code is not valid for Confluence."}
        return persist_scan_result(db, event.id, user.id, payload.clientScanId, fingerprint, None, "INVALID_TICKET", body)

    # Recheck idempotency after lock
    previous_after_lock = db.query(ScanAttempt).filter(ScanAttempt.scanner_user_id == user.id, ScanAttempt.request_id == payload.clientScanId).first()
    if previous_after_lock:
        if not secrets.compare_digest(previous_after_lock.request_fingerprint, fingerprint):
            raise HTTPException(409, "This scan request ID was already used for a different ticket. Scan again.")
        return JSONResponse(status_code=scan_status_http(previous_after_lock.result_code), content=previous_after_lock.response_json)

    if ticket.status != "ISSUED":
        body = {"result": "TICKET_REVOKED", "message": "This ticket has been revoked. Direct attendee to help desk."}
        return persist_scan_result(db, event.id, user.id, payload.clientScanId, fingerprint, ticket.id, "TICKET_REVOKED", body)

    checkin = db.query(CheckIn).filter(CheckIn.ticket_id == ticket.id).with_for_update().first()
    attendee = db.get(Attendee, ticket.attendee_id)
    if checkin and checkin.status == "CHECKED_IN":
        body = {
            "result": "ALREADY_CHECKED_IN",
            "message": "This ticket has already been used.",
            "ticketNumber": ticket.ticket_number,
            "attendee": {"name": attendee.full_name, "year": attendee.year},
            "checkedInAt": iso(checkin.checked_in_at)
        }
        return persist_scan_result(db, event.id, user.id, payload.clientScanId, fingerprint, ticket.id, "ALREADY_CHECKED_IN", body)

    if checkin:
        checkin.status = "CHECKED_IN"
        checkin.checked_in_at = now
        checkin.scanner_user_id = user.id
        action = "CHECKIN_RESTORED"
    else:
        checkin = CheckIn(id=new_id(), event_id=event.id, ticket_id=ticket.id, status="CHECKED_IN", checked_in_at=now, scanner_user_id=user.id)
        db.add(checkin)
        action = "CHECKED_IN"

    body = {
        "result": "CHECKED_IN",
        "message": "Entry approved.",
        "ticketNumber": ticket.ticket_number,
        "attendee": {"name": attendee.full_name, "year": attendee.year},
        "checkedInAt": iso(now)
    }
    db.add(ScanAttempt(id=new_id(), event_id=event.id, scanner_user_id=user.id, request_id=payload.clientScanId,
                       request_fingerprint=fingerprint, ticket_id=ticket.id, result_code="CHECKED_IN", response_json=body, created_at=now))
    audit(db, event.id, user.id, action, "check_in", checkin.id, {"ticketNumber": ticket.ticket_number})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        previous = db.query(ScanAttempt).filter(ScanAttempt.scanner_user_id == user.id, ScanAttempt.request_id == payload.clientScanId).first()
        if previous:
            if not secrets.compare_digest(previous.request_fingerprint, fingerprint):
                raise HTTPException(409, "This scan request ID was already used for a different ticket. Scan again.")
            return JSONResponse(status_code=scan_status_http(previous.result_code), content=previous.response_json)
        checkin_after = db.query(CheckIn).filter(CheckIn.ticket_id == ticket.id).first()
        if checkin_after and checkin_after.status == "CHECKED_IN":
            attendee_after = db.get(Attendee, ticket.attendee_id)
            ticket_after = db.get(Ticket, ticket.id)
            duplicate_body = {
                "result": "ALREADY_CHECKED_IN",
                "message": "This ticket has already been used.",
                "ticketNumber": ticket_after.ticket_number,
                "attendee": {"name": attendee_after.full_name, "year": attendee_after.year},
                "checkedInAt": iso(checkin_after.checked_in_at)
            }
            return persist_scan_result(db, event.id, user.id, payload.clientScanId, fingerprint, ticket.id, "ALREADY_CHECKED_IN", duplicate_body)
        raise HTTPException(409, "Check-in conflict. Scan again.")

    return JSONResponse(status_code=200, content=body)

@app.get("/api/check-ins/recent")
def recent_checkins(user: User = Depends(current_user), db: Session = Depends(get_db)):
    event = event_for(db)
    rows = db.query(CheckIn, Ticket, Attendee, User).join(Ticket, CheckIn.ticket_id == Ticket.id).join(
        Attendee, Ticket.attendee_id == Attendee.id).join(User, CheckIn.scanner_user_id == User.id).filter(
        CheckIn.event_id == event.id, CheckIn.status == "CHECKED_IN").order_by(CheckIn.checked_in_at.desc()).limit(30).all()
    return {"rows": [{"checkinId": c.id, "ticketNumber": t.ticket_number, "name": a.full_name, "year": a.year,
                      "checkedInAt": iso(c.checked_in_at), "scanner": u.email} for c, t, a, u in rows]}

# Block any other API endpoints with clear 403 Forbidden
@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def disabled_admin_endpoints(path: str):
    raise HTTPException(403, f"The endpoint '/api/{path}' is disabled on this scanner-only deployment.")
