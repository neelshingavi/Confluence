from __future__ import annotations

import csv
from contextlib import asynccontextmanager
from copy import copy as copy_style
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import tempfile
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

import qrcode
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, Body
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware
from openpyxl import load_workbook, Workbook

from .db import SessionLocal, get_db
from .models import Event, User, Attendee, Ticket, CheckIn, ScanAttempt, AuditLog, ImportPreview, TicketTemplate, utcnow
from .schemas import LoginInput, PasswordChangeInput, ScanInput, StaffCreateInput, ReverseInput
from .security import hash_password, verify_password, encrypt_qr_token, decrypt_qr_token, qr_token_hash, scan_fingerprint, validate_security_configuration

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent.parent
FRONTEND_DIST = PROJECT_DIR / "frontend" / "dist"
DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_DIR / "data"))).resolve()
TEMPLATE_DIR = DATA_DIR / "templates"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)

@asynccontextmanager
async def lifespan(_: FastAPI):
    startup()
    yield


app = FastAPI(title="Confluence Ticketing & Check-in", version="1.0.0", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "invalid-unconfigured-session-secret"),
    session_cookie="confluence_session",
    max_age=60 * 60 * 10,
    same_site="strict",
    https_only=os.getenv("COOKIE_SECURE", "true").lower() == "true",
)

# Basic per-process login throttling. Keep one API worker unless this is moved to shared infrastructure.
_login_failures: dict[str, list[float]] = defaultdict(list)
_scan_requests: dict[str, list[float]] = defaultdict(list)
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_ATTEMPTS = 10
SCAN_WINDOW_SECONDS = 60
SCAN_MAX_ATTEMPTS_PER_USER = 600
MAX_EXCEL_BYTES = 15 * 1024 * 1024
MAX_EXCEL_ROWS = 10000
MAX_TEMPLATE_BYTES = 12 * 1024 * 1024


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
    if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/health/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def new_id() -> str:
    return str(uuid.uuid4())


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_email(email: str | None) -> str | None:
    value = (email or "").strip().casefold()
    return value or None


def clean_cell(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value.isoformat()
    elif isinstance(value, float) and value.is_integer():
        result = str(int(value))
    else:
        result = str(value)
    result = result.strip()
    return result or None


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())

HEADER_ALIASES = {
    "name": {"name", "fullname", "studentname", "attendeename", "attendee", "participantname"},
    "year": {"year", "studyyear", "academicyear", "class", "courseyear", "studentyear", "yeardept", "yeardeptegtyce"},
    "email": {"email", "emailaddress", "mailid", "emailid"},
    "phone": {"phone", "phonenumber", "mobile", "mobilenumber", "contactnumber", "contact", "whatsapp"},
}


def infer_mapping(headers: list[str]) -> dict[str, str | None]:
    normalized = {h: normalize_header(h) for h in headers}
    result = {field: None for field in HEADER_ALIASES}
    for field, aliases in HEADER_ALIASES.items():
        for header, value in normalized.items():
            if value in aliases:
                result[field] = header
                break
    return result


def parse_workbook(data: bytes) -> tuple[list[str], list[dict]]:
    # XLSX is a ZIP container. Bound expanded size and entry count before handing it to openpyxl.
    try:
        with ZipFile(io.BytesIO(data)) as container:
            members = container.infolist()
            if len(members) > 1000 or sum(item.file_size for item in members) > 100 * 1024 * 1024:
                raise HTTPException(413, "Workbook expands beyond the 100 MB processing limit.")
            if not any(item.filename == "xl/workbook.xml" for item in members):
                raise HTTPException(400, "File is not a valid .xlsx workbook.")
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(400, "Cannot read this workbook. Upload a valid .xlsx file (not .xls or a password-protected file).") from exc
    sheet = workbook.worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    first = None
    header_row_index = None
    for ix, row in enumerate(rows, start=1):
        if ix > MAX_EXCEL_ROWS + 1:
            workbook.close()
            raise HTTPException(413, f"Worksheet exceeds the {MAX_EXCEL_ROWS:,}-row safety limit.")
        if len(row) > 100:
            workbook.close()
            raise HTTPException(413, "Worksheet has more than 100 columns; remove unused columns and upload again.")
        values = [clean_cell(v) for v in row]
        if any(values):
            first = values
            header_row_index = ix
            break
    if first is None:
        workbook.close()
        raise HTTPException(400, "The first worksheet is empty.")
    headers = []
    for i, value in enumerate(first):
        label = value or f"Column {i + 1}"
        headers.append(label[:100])
    records = []
    for row_index, row in enumerate(rows, start=header_row_index + 1):
        if row_index > MAX_EXCEL_ROWS + 1:
            workbook.close()
            raise HTTPException(413, f"Worksheet exceeds the {MAX_EXCEL_ROWS:,}-row safety limit.")
        if len(row) > 100:
            workbook.close()
            raise HTTPException(413, "Worksheet has more than 100 columns; remove unused columns and upload again.")
        values = [clean_cell(v) for v in row]
        if not any(values):
            continue
        if len(records) >= MAX_EXCEL_ROWS:
            workbook.close()
            raise HTTPException(413, f"Workbook exceeds the {MAX_EXCEL_ROWS:,}-row safety limit.")
        if len(values) < len(headers):
            values.extend([None] * (len(headers) - len(values)))
        values = values[:len(headers)]
        records.append({"row_number": row_index, "values": {headers[i]: values[i] for i in range(len(headers))}})
    workbook.close()
    return headers, records


def attendee_fingerprint(name: str, year: str | None, email: str | None, phone: str | None) -> str:
    parts = [name.strip().casefold(), (year or "").strip().casefold(), (email or "").strip().casefold(), re.sub(r"\s+", "", phone or "")]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def audit(db: Session, event_id: str, actor_id: str | None, action: str, entity_type: str, entity_id: str | None = None, details: dict | None = None):
    db.add(AuditLog(id=new_id(), event_id=event_id, actor_user_id=actor_id, action=action, entity_type=entity_type,
                    entity_id=entity_id, details=details or {}))


def event_for(db: Session) -> Event:
    event = db.query(Event).filter(Event.slug == "confluence").first()
    if not event:
        raise HTTPException(503, "Confluence event is not initialized. Check startup configuration.")
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


def roles_allowed(*roles: str):
    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, "Your account does not have permission for this action.")
        return user
    return dependency


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


def is_admin(user: User) -> bool:
    return user.role in {"OWNER", "ADMIN"}


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


def default_placements(width: int, height: int) -> dict:
    return {
        "qr": {"x": 0.73, "y": 0.22, "size": 0.18},
        "name": {"x": 0.08, "y": 0.64, "scale": 0.035, "color": "#172554"},
        "year": {"x": 0.08, "y": 0.75, "scale": 0.025, "color": "#334155"},
        "phone": {"x": 0.08, "y": 0.83, "scale": 0.025, "color": "#334155"},
    }


def validate_placements(value: dict) -> dict:
    if not isinstance(value, dict) or any(k not in value for k in ("qr", "name", "year", "phone")):
        raise HTTPException(422, "Placements must include qr, name, year, and phone.")
    normalized = {}
    for key in ("qr", "name", "year", "phone"):
        item = value[key]
        if not isinstance(item, dict):
            raise HTTPException(422, f"Invalid placement for {key}.")
        try:
            x, y = float(item["x"]), float(item["y"])
            if not (0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError
            if key == "qr":
                size = float(item["size"])
                if not 0.06 <= size <= 0.6 or x + size > 1.001 or y + size > 1.001:
                    raise ValueError
                normalized[key] = {"x": x, "y": y, "size": size}
            else:
                scale = float(item.get("scale", 0.03))
                color = str(item.get("color", "#172554"))
                if not 0.008 <= scale <= 0.12 or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
                    raise ValueError
                normalized[key] = {"x": x, "y": y, "scale": scale, "color": color}
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, f"Invalid coordinates, size, font scale, or colour for {key}.")
    return normalized


def safe_filename(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_")
    return (result[:55] or "Attendee")


def make_ticket_image(template_image: Image.Image, attendee: Attendee, ticket_number: str, raw_token: str, placements: dict) -> Image.Image:
    image = template_image.copy().convert("RGB")
    width, height = image.size
    draw = ImageDraw.Draw(image)

    qr_cfg = placements["qr"]
    qr_size = max(120, int(float(qr_cfg["size"]) * width))
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_Q, box_size=8, border=4)
    qr.add_data("CFL1." + raw_token)
    qr.make(fit=True)
    qr_image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_image = qr_image.resize((qr_size, qr_size), Image.Resampling.NEAREST)
    qx, qy = int(float(qr_cfg["x"]) * width), int(float(qr_cfg["y"]) * height)
    if qx + qr_size > width or qy + qr_size > height:
        raise RuntimeError("QR position extends beyond the ticket canvas. Adjust the template placement.")
    image.paste(qr_image, (qx, qy))

    values = {"name": attendee.full_name, "year": attendee.year or "", "phone": attendee.phone or ""}
    for key, value in values.items():
        cfg = placements[key]
        x, y = int(float(cfg["x"]) * width), int(float(cfg["y"]) * height)
        font_px = max(12, min(240, int(float(cfg["scale"]) * width)))
        try:
            font = ImageFont.load_default(size=font_px)
        except TypeError:
            font = ImageFont.load_default()
        # Wrap long text based on available width; keep a small right margin.
        text = str(value)
        max_width = max(80, width - x - int(width * 0.06))
        if draw.textlength(text, font=font) > max_width:
            while text and draw.textlength(text + "…", font=font) > max_width:
                text = text[:-1]
            text = (text + "…") if text != str(value) else text
        draw.text((x, y), text, fill=cfg["color"], font=font, anchor="lt")
    return image


def delete_temp(zip_path: str, directory: str):
    try:
        os.unlink(zip_path)
    except OSError:
        pass
    shutil.rmtree(directory, ignore_errors=True)


def startup():
    validate_security_configuration()
    with SessionLocal() as db:
        event = db.query(Event).filter(Event.slug == "confluence").first()
        if event is None:
            event = Event(id=new_id(), name="Confluence", slug="confluence", timezone="Asia/Kolkata", status="active")
            db.add(event)
            db.flush()
        owner_count = db.query(User).count()
        if owner_count == 0:
            email = normalize_email(os.getenv("BOOTSTRAP_ADMIN_EMAIL"))
            password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
            if not email or len(password) < 12:
                raise RuntimeError("First start requires BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD (12+ characters).")
            db.add(User(id=new_id(), email=email, password_hash=hash_password(password), role="OWNER", active=True))
        db.commit()


@app.get("/api/health/live")
def live():
    return {"status": "ok"}


@app.get("/api/health/ready")
def ready(db: Session = Depends(get_db)):
    try:
        db.execute(select(Event.id).limit(1)).first()
    except Exception as exc:
        raise HTTPException(503, "Database not ready.") from exc
    return {"status": "ready"}


@app.get("/api/auth/me")
def auth_me(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("uid")
    if not uid:
        return {"authenticated": False}
    user = db.get(User, uid)
    if not user or not user.active or request.session.get("sv") != user.session_version:
        request.session.clear()
        return {"authenticated": False}
    csrf = request.session.get("csrf_token")
    if not csrf:
        csrf = secrets.token_urlsafe(32)
        request.session["csrf_token"] = csrf
    event = event_for(db)
    return {"authenticated": True, "csrfToken": csrf, "user": {"id": user.id, "email": user.email, "role": user.role}, "event": {"id": event.id, "name": event.name, "timezone": event.timezone}}


@app.post("/api/auth/login")
def login(payload: LoginInput, request: Request, db: Session = Depends(get_db)):
    same_origin(request)
    ip = request.client.host if request.client else "unknown"
    now = time.time()
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
    return {"authenticated": True}


@app.post("/api/auth/logout")
def logout(request: Request, user: User = Depends(require_csrf)):
    request.session.clear()
    return {"ok": True}


@app.post("/api/auth/change-password")
def change_password(payload: PasswordChangeInput, request: Request, user: User = Depends(require_csrf), db: Session = Depends(get_db)):
    old_password = payload.oldPassword
    new_password = payload.newPassword
    if not verify_password(old_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect.")
    try:
        user.password_hash = hash_password(new_password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    user.session_version += 1
    next_session_version = user.session_version
    event = event_for(db)
    audit(db, event.id, user.id, "PASSWORD_CHANGED", "staff_user", user.id, {"sessionsRevoked": True})
    db.commit()
    # Invalidate other signed cookies for this user while keeping the current browser authenticated.
    request.session["sv"] = next_session_version
    return {"ok": True, "message": "Password updated. Other active sessions have been invalidated."}


@app.get("/api/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    event = event_for(db)
    attendee_count = db.query(func.count(Attendee.id)).filter(Attendee.event_id == event.id).scalar() or 0
    tickets_issued = db.query(func.count(Ticket.id)).filter(Ticket.event_id == event.id, Ticket.status == "ISSUED").scalar() or 0
    checked_in = db.query(func.count(CheckIn.id)).filter(CheckIn.event_id == event.id, CheckIn.status == "CHECKED_IN").scalar() or 0
    revoked = db.query(func.count(Ticket.id)).filter(Ticket.event_id == event.id, Ticket.status == "REVOKED").scalar() or 0
    failed_emails = None  # No email-sending workflow is included in this event-day scope.
    recent = db.query(ScanAttempt).filter(ScanAttempt.event_id == event.id).order_by(ScanAttempt.created_at.desc()).limit(8).all()
    return {"event": event.name, "timezone": event.timezone, "registered": attendee_count, "ticketsIssued": tickets_issued,
            "checkedIn": checked_in, "notCheckedIn": max(attendee_count - checked_in, 0), "revoked": revoked,
            "attendanceRate": round((checked_in / attendee_count * 100), 1) if attendee_count else 0,
            "recentScans": [{"result": a.result_code, "time": iso(a.created_at)} for a in recent]}


@app.post("/api/imports/inspect")
def inspect_import(file: UploadFile = File(...), user: User = Depends(roles_allowed("OWNER", "ADMIN")), _csrf: User = Depends(require_csrf)):
    if not file.filename or Path(file.filename).suffix.lower() != ".xlsx":
        raise HTTPException(400, "Upload an Excel .xlsx file. Legacy .xls files are not supported.")
    data = file.file.read(MAX_EXCEL_BYTES + 1)
    if len(data) > MAX_EXCEL_BYTES:
        raise HTTPException(413, "Excel file is larger than the 15 MB limit.")
    headers, records = parse_workbook(data)
    samples = [{"row_number": r["row_number"], **r["values"]} for r in records[:5]]
    return {"filename": Path(file.filename).name[:255], "headers": headers, "autoMapping": infer_mapping(headers), "sampleRows": samples, "totalRows": len(records)}


@app.post("/api/imports/preview")
def preview_import(file: UploadFile = File(...), mapping: str = Form(...), user: User = Depends(roles_allowed("OWNER", "ADMIN")), _csrf: User = Depends(require_csrf), db: Session = Depends(get_db)):
    if not file.filename or Path(file.filename).suffix.lower() != ".xlsx":
        raise HTTPException(400, "Upload an Excel .xlsx file.")
    data = file.file.read(MAX_EXCEL_BYTES + 1)
    if len(data) > MAX_EXCEL_BYTES:
        raise HTTPException(413, "Excel file is larger than the 15 MB limit.")
    try:
        column_map = json.loads(mapping)
    except Exception as exc:
        raise HTTPException(400, "Column mapping must be valid JSON.") from exc
    headers, records = parse_workbook(data)
    if not isinstance(column_map, dict) or not column_map.get("name"):
        raise HTTPException(422, "Map the Name column before previewing.")
    unknown = [v for v in column_map.values() if v and v not in headers]
    if unknown:
        raise HTTPException(422, "The selected column mapping no longer matches the uploaded workbook. Upload it again.")
    event = event_for(db)
    existing_emails = {x[0] for x in db.query(Attendee.normalized_email).filter(Attendee.event_id == event.id, Attendee.normalized_email.is_not(None)).all()}
    existing_fingerprints = {x[0] for x in db.query(Attendee.import_fingerprint).filter(Attendee.event_id == event.id).all()}
    seen_emails: set[str] = set()
    seen_fingerprints: set[str] = set()
    payload_rows = []
    for record in records:
        raw = record["values"]
        name = (clean_cell(raw.get(column_map.get("name"))) or "").strip()
        year = clean_cell(raw.get(column_map.get("year"))) if column_map.get("year") else None
        email = clean_cell(raw.get(column_map.get("email"))) if column_map.get("email") else None
        phone = clean_cell(raw.get(column_map.get("phone"))) if column_map.get("phone") else None
        email = email.strip() if email else None
        normalized = normalize_email(email)
        errors = []
        if not name:
            errors.append("Name is required")
        if len(name) > 200:
            errors.append("Name exceeds 200 characters")
        if year and len(year) > 80:
            errors.append("Year exceeds 80 characters")
        if email and (len(email) > 320 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
            errors.append("Email looks invalid")
        if phone and len(phone) > 40:
            errors.append("Phone exceeds 40 characters")
        fingerprint = attendee_fingerprint(name, year, normalized, phone)
        duplicate = False
        if normalized and (normalized in existing_emails or normalized in seen_emails):
            duplicate = True
        if fingerprint in existing_fingerprints or fingerprint in seen_fingerprints:
            duplicate = True
        if normalized:
            seen_emails.add(normalized)
        seen_fingerprints.add(fingerprint)
        status = "invalid" if errors else ("duplicate" if duplicate else "valid")
        payload_rows.append({"row_number": record["row_number"], "name": name, "year": year, "email": email, "normalized_email": normalized,
                             "phone": phone, "fingerprint": fingerprint, "errors": errors, "status": status})
    summary = {"total": len(payload_rows), "valid": sum(r["status"] == "valid" for r in payload_rows),
               "invalid": sum(r["status"] == "invalid" for r in payload_rows), "duplicates": sum(r["status"] == "duplicate" for r in payload_rows)}
    # Short-lived staging records are necessary so commit uses exactly the reviewed content.
    db.query(ImportPreview).filter(ImportPreview.expires_at < utcnow()).delete(synchronize_session=False)
    preview = ImportPreview(id=new_id(), event_id=event.id, created_by=user.id, filename=Path(file.filename).name[:255],
                            payload=payload_rows, summary=summary, expires_at=utcnow() + timedelta(hours=2))
    db.add(preview)
    audit(db, event.id, user.id, "IMPORT_PREVIEW_CREATED", "import_preview", preview.id, summary)
    db.commit()
    return {"previewId": preview.id, "filename": preview.filename, "summary": summary,
            "rows": [{"row_number": r["row_number"], "name": r["name"], "year": r["year"], "email": r["email"], "phone": r["phone"], "status": r["status"], "errors": r["errors"]} for r in payload_rows[:200]],
            "truncated": len(payload_rows) > 200}


@app.post("/api/imports/{preview_id}/commit")
def commit_import(preview_id: str, user: User = Depends(roles_allowed("OWNER", "ADMIN")), _csrf: User = Depends(require_csrf), db: Session = Depends(get_db)):
    event = event_for(db)
    preview = db.query(ImportPreview).filter(ImportPreview.id == preview_id, ImportPreview.event_id == event.id).with_for_update().first()
    if not preview:
        raise HTTPException(404, "Import preview not found.")
    if preview.committed_at:
        raise HTTPException(409, "This import preview has already been committed.")
    expires_at = preview.expires_at if preview.expires_at.tzinfo else preview.expires_at.replace(tzinfo=timezone.utc)
    if expires_at < utcnow():
        raise HTTPException(410, "This import preview expired. Upload and preview the workbook again.")
    added, skipped, raced_duplicates = 0, 0, 0
    # The reviewed payload is immutable; re-check uniqueness during the commit transaction.
    for row in preview.payload:
        if row["status"] != "valid":
            skipped += 1
            continue
        try:
            existing = None
            if row["normalized_email"]:
                existing = db.query(Attendee.id).filter(Attendee.event_id == event.id, Attendee.normalized_email == row["normalized_email"]).first()
            if not existing:
                existing = db.query(Attendee.id).filter(Attendee.event_id == event.id, Attendee.import_fingerprint == row["fingerprint"]).first()
            if existing:
                skipped += 1
                raced_duplicates += 1
                continue
            db.add(Attendee(id=new_id(), event_id=event.id, full_name=row["name"], year=row["year"], email=row["email"],
                            normalized_email=row["normalized_email"], phone=row["phone"], import_fingerprint=row["fingerprint"], source_row=row["row_number"]))
            db.flush()
            added += 1
        except IntegrityError:
            db.rollback()
            # Reload the preview to avoid committing a partially failed batch.
            raise HTTPException(409, "An attendee changed concurrently during import. Refresh the attendee list and create a new preview.")
    preview.committed_at = utcnow()
    audit(db, event.id, user.id, "ATTENDEES_IMPORTED", "import_preview", preview.id,
          {"added": added, "skipped": skipped, "concurrentDuplicates": raced_duplicates, "filename": preview.filename})
    db.commit()
    return {"ok": True, "added": added, "skipped": skipped, "concurrentDuplicates": raced_duplicates, "message": f"Imported {added} students; skipped {skipped} invalid or duplicate rows."}


@app.get("/api/attendees")
def list_attendees(q: str = "", status: str = "all", user: User = Depends(roles_allowed("OWNER", "ADMIN", "VIEWER")), db: Session = Depends(get_db)):
    event = event_for(db)
    query = db.query(Attendee, Ticket, CheckIn).outerjoin(Ticket, Ticket.attendee_id == Attendee.id).outerjoin(CheckIn, CheckIn.ticket_id == Ticket.id).filter(Attendee.event_id == event.id)
    if q.strip():
        term = "%" + q.strip().replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter((Attendee.full_name.ilike(term)) | (Attendee.email.ilike(term)) | (Attendee.phone.ilike(term)) | (Ticket.ticket_number.ilike(term)))
    rows = query.order_by(Attendee.full_name.asc()).limit(1000).all()
    output = []
    for attendee, ticket, checkin in rows:
        checked = bool(checkin and checkin.status == "CHECKED_IN")
        if status == "checked_in" and not checked:
            continue
        if status == "not_checked_in" and checked:
            continue
        output.append({"id": attendee.id, "name": attendee.full_name, "year": attendee.year, "email": attendee.email, "phone": attendee.phone,
                       "ticketId": ticket.id if ticket else None, "ticketNumber": ticket.ticket_number if ticket else None, "ticketStatus": ticket.status if ticket else "NOT_ISSUED",
                       "attendanceStatus": "CHECKED IN" if checked else "NOT CHECKED IN", "checkedInAt": iso(checkin.checked_in_at) if checked else None})
    return {"rows": output, "count": len(output), "limit": 1000}


@app.get("/api/template")
def get_template(user: User = Depends(current_user), db: Session = Depends(get_db)):
    event = event_for(db)
    template = db.query(TicketTemplate).filter(TicketTemplate.event_id == event.id).first()
    if not template:
        return {"exists": False, "placements": None}
    return {"exists": True, "id": template.id, "width": template.image_width, "height": template.image_height,
            "placements": template.placements, "updatedAt": iso(template.updated_at), "imageUrl": "/api/template/image?v=" + template.id}


@app.post("/api/template/image")
def upload_template_image(file: UploadFile = File(...), user: User = Depends(roles_allowed("OWNER", "ADMIN")), _csrf: User = Depends(require_csrf), db: Session = Depends(get_db)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(400, "Template must be a PNG, JPG, or WebP image. SVG is not accepted.")
    data = file.file.read(MAX_TEMPLATE_BYTES + 1)
    if len(data) > MAX_TEMPLATE_BYTES:
        raise HTTPException(413, "Ticket design is larger than 12 MB.")
    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(400, "The uploaded file is not a valid image.") from exc
    width, height = image.size
    if width < 400 or height < 250 or width > 10000 or height > 10000 or width * height > 24000000:
        raise HTTPException(422, "Template image must be at least 400×250 pixels and no larger than 24 megapixels.")
    event = event_for(db)
    template = db.query(TicketTemplate).filter(TicketTemplate.event_id == event.id).first()
    old_template_path = None
    filename = f"{event.id}-ticket-template-{new_id()}.png"
    path = TEMPLATE_DIR / filename
    image.save(path, format="PNG", optimize=True)
    if template:
        old_template_path = Path(template.image_path)
        template.image_path = str(path)
        template.image_width = width
        template.image_height = height
        template.placements = default_placements(width, height)
        template.updated_by = user.id
        template.updated_at = utcnow()
        template.id = new_id()
    else:
        template = TicketTemplate(id=new_id(), event_id=event.id, image_path=str(path), image_width=width, image_height=height,
                                  placements=default_placements(width, height), updated_by=user.id)
        db.add(template)
    audit(db, event.id, user.id, "TICKET_TEMPLATE_UPLOADED", "ticket_template", template.id, {"width": width, "height": height})
    db.commit()
    if old_template_path:
        try:
            old_template_path.unlink(missing_ok=True)
        except OSError:
            pass
    return {"exists": True, "id": template.id, "width": width, "height": height, "placements": template.placements, "imageUrl": "/api/template/image?v=" + template.id}


@app.put("/api/template/placements")
def save_template_placements(placements: dict = Body(...), user: User = Depends(roles_allowed("OWNER", "ADMIN")), _csrf: User = Depends(require_csrf), db: Session = Depends(get_db)):
    event = event_for(db)
    template = db.query(TicketTemplate).filter(TicketTemplate.event_id == event.id).first()
    if not template:
        raise HTTPException(409, "Upload a ticket design first.")
    normalized = validate_placements(placements)
    qr_height_fraction = normalized["qr"]["size"] * template.image_width / template.image_height
    if normalized["qr"]["y"] + qr_height_fraction > 1.001:
        raise HTTPException(422, "The QR position extends below the artwork. Move it up or reduce its size.")
    template.placements = normalized
    template.updated_by = user.id
    template.updated_at = utcnow()
    audit(db, event.id, user.id, "TICKET_LAYOUT_SAVED", "ticket_template", template.id, {"placementKeys": list(normalized.keys())})
    db.commit()
    return {"ok": True, "placements": normalized}


@app.get("/api/template/image")
def template_image(user: User = Depends(current_user), db: Session = Depends(get_db)):
    event = event_for(db)
    template = db.query(TicketTemplate).filter(TicketTemplate.event_id == event.id).first()
    if not template or not Path(template.image_path).is_file():
        raise HTTPException(404, "No ticket template uploaded.")
    return FileResponse(template.image_path, media_type="image/png", headers={"Cache-Control": "private, max-age=60"})


@app.post("/api/tickets/generate")
def generate_ticket_zip(user: User = Depends(roles_allowed("OWNER", "ADMIN")), _csrf: User = Depends(require_csrf), db: Session = Depends(get_db)):
    event = event_for(db)
    template = db.query(TicketTemplate).filter(TicketTemplate.event_id == event.id).first()
    if not template or not Path(template.image_path).is_file():
        raise HTTPException(409, "Upload a ticket design and save the placement layout before generating tickets.")
    attendee_rows = db.query(Attendee).filter(Attendee.event_id == event.id).order_by(Attendee.full_name.asc()).all()
    if not attendee_rows:
        raise HTTPException(409, "Import students before generating tickets.")

    # Allocate credentials once. Re-generating the ZIP reuses existing ticket credentials.
    created_count = 0
    with SessionLocal() as work:
        with work.begin():
            locked_event = work.query(Event).filter(Event.id == event.id).with_for_update().one()
            existing_tickets = {t.attendee_id: t for t in work.query(Ticket).filter(Ticket.event_id == locked_event.id).all()}
            max_seq = 0
            for ticket in existing_tickets.values():
                match = re.search(r"-(\d+)$", ticket.ticket_number)
                if match:
                    max_seq = max(max_seq, int(match.group(1)))
            attendees = work.query(Attendee).filter(Attendee.event_id == locked_event.id).order_by(Attendee.full_name.asc()).all()
            for attendee in attendees:
                if attendee.id in existing_tickets:
                    continue
                max_seq += 1
                token = secrets.token_urlsafe(32)
                while work.query(Ticket.id).filter(Ticket.qr_token_hash == qr_token_hash(token)).first():
                    token = secrets.token_urlsafe(32)
                ticket = Ticket(id=new_id(), event_id=locked_event.id, attendee_id=attendee.id,
                                ticket_number=f"CONF-{max_seq:05d}", status="ISSUED", qr_token_hash=qr_token_hash(token),
                                qr_token_encrypted=encrypt_qr_token(token))
                work.add(ticket)
                created_count += 1
            work.flush()
            audit(work, locked_event.id, user.id, "TICKETS_GENERATED", "ticket_batch", None,
                  {"attendeeCount": len(attendees), "newTickets": created_count})

    # Re-read durable state after allocation; a failed render can be retried without changing credentials.
    with SessionLocal() as read_db:
        saved_template = read_db.query(TicketTemplate).filter(TicketTemplate.event_id == event.id).first()
        image = Image.open(saved_template.image_path).convert("RGB")
        placements = validate_placements(saved_template.placements)
        rows = read_db.query(Attendee, Ticket).join(Ticket, Ticket.attendee_id == Attendee.id).filter(
            Attendee.event_id == event.id, Ticket.event_id == event.id, Ticket.status == "ISSUED").order_by(Attendee.full_name.asc()).all()
        if len(rows) == 0:
            raise HTTPException(409, "No active tickets are available to generate.")
        temp_dir = tempfile.mkdtemp(prefix="confluence-tickets-")
        fd, zip_path = tempfile.mkstemp(prefix="confluence-tickets-", suffix=".zip")
        os.close(fd)
        manifest = io.StringIO(newline="")
        writer = csv.writer(manifest)
        writer.writerow(["Ticket ID", "Name", "Year", "Filename"])
        try:
            with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
                for attendee, ticket in rows:
                    token = decrypt_qr_token(ticket.qr_token_encrypted)
                    result = make_ticket_image(image, attendee, ticket.ticket_number, token, placements)
                    filename = f"{ticket.ticket_number}_{safe_filename(attendee.full_name)}.png"
                    path = Path(temp_dir) / filename
                    result.save(path, format="PNG", optimize=True)
                    zf.write(path, arcname=filename)
                    writer.writerow([ticket.ticket_number, attendee.full_name, attendee.year or "", filename])
                zf.writestr("ticket_manifest.csv", manifest.getvalue().encode("utf-8-sig"))
        except Exception:
            delete_temp(zip_path, temp_dir)
            raise
        count = len(rows)
    return FileResponse(zip_path, media_type="application/zip", filename="Confluence-tickets.zip",
                        background=BackgroundTask(delete_temp, zip_path, temp_dir), headers={"X-Tickets-Included": str(count)})


@app.post("/api/tickets/{ticket_id}/revoke")
def revoke_ticket(ticket_id: str, user: User = Depends(roles_allowed("OWNER", "ADMIN")), _csrf: User = Depends(require_csrf), db: Session = Depends(get_db)):
    event = event_for(db)
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id, Ticket.event_id == event.id).with_for_update().first()
    if not ticket:
        raise HTTPException(404, "Ticket not found.")
    if ticket.status == "REVOKED":
        return {"ok": True, "status": "REVOKED"}
    ticket.status = "REVOKED"
    ticket.revoked_at = utcnow()
    ticket.revoked_by = user.id
    audit(db, event.id, user.id, "TICKET_REVOKED", "ticket", ticket.id, {"ticketNumber": ticket.ticket_number})
    db.commit()
    return {"ok": True, "status": "REVOKED"}


@app.post("/api/check-ins")
def scan_checkin(payload: ScanInput, request: Request, user: User = Depends(roles_allowed("OWNER", "ADMIN", "CHECKIN_STAFF")), _csrf: User = Depends(require_csrf), db: Session = Depends(get_db)):
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
        raise HTTPException(503, "Ticket validation is not configured.") from exc

    # Idempotency is scoped to staff identity. Reusing an ID for a different QR token is rejected.
    previous = db.query(ScanAttempt).filter(ScanAttempt.scanner_user_id == user.id, ScanAttempt.request_id == payload.clientScanId).first()
    if previous:
        if not secrets.compare_digest(previous.request_fingerprint, fingerprint):
            raise HTTPException(409, "This scan request ID was already used for a different ticket. Scan again.")
        return JSONResponse(status_code=200 if previous.result_code == "CHECKED_IN" else (409 if previous.result_code == "ALREADY_CHECKED_IN" else 404), content=previous.response_json)

    ticket = db.query(Ticket).filter(Ticket.qr_token_hash == fingerprint).with_for_update().first()
    now = utcnow()
    if not ticket or ticket.event_id != event.id:
        body = {"result": "INVALID_TICKET", "message": "This QR code is not valid for Confluence."}
        return persist_scan_result(db, event.id, user.id, payload.clientScanId, fingerprint, None, "INVALID_TICKET", body)

    # A same-operation retry may have committed while this request waited for the ticket row lock.
    previous_after_lock = db.query(ScanAttempt).filter(ScanAttempt.scanner_user_id == user.id, ScanAttempt.request_id == payload.clientScanId).first()
    if previous_after_lock:
        if not secrets.compare_digest(previous_after_lock.request_fingerprint, fingerprint):
            raise HTTPException(409, "This scan request ID was already used for a different ticket. Scan again.")
        return JSONResponse(status_code=scan_status_http(previous_after_lock.result_code), content=previous_after_lock.response_json)

    if ticket.status != "ISSUED":
        body = {"result": "TICKET_REVOKED", "message": "This ticket has been revoked. Please direct the attendee to the help desk."}
        return persist_scan_result(db, event.id, user.id, payload.clientScanId, fingerprint, ticket.id, "TICKET_REVOKED", body)

    checkin = db.query(CheckIn).filter(CheckIn.ticket_id == ticket.id).with_for_update().first()
    attendee = db.get(Attendee, ticket.attendee_id)
    if checkin and checkin.status == "CHECKED_IN":
        body = {"result": "ALREADY_CHECKED_IN", "message": "This ticket has already been used.", "ticketNumber": ticket.ticket_number,
                "attendee": {"name": attendee.full_name, "year": attendee.year}, "checkedInAt": iso(checkin.checked_in_at)}
        return persist_scan_result(db, event.id, user.id, payload.clientScanId, fingerprint, ticket.id, "ALREADY_CHECKED_IN", body)

    if checkin:
        checkin.status = "CHECKED_IN"
        checkin.checked_in_at = now
        checkin.scanner_user_id = user.id
        checkin.reversed_at = None
        checkin.reversed_by = None
        checkin.reversal_reason = None
        action = "CHECKIN_RESTORED"
    else:
        checkin = CheckIn(id=new_id(), event_id=event.id, ticket_id=ticket.id, status="CHECKED_IN", checked_in_at=now, scanner_user_id=user.id)
        db.add(checkin)
        action = "CHECKED_IN"
    body = {"result": "CHECKED_IN", "message": "Entry approved.", "ticketNumber": ticket.ticket_number,
            "attendee": {"name": attendee.full_name, "year": attendee.year}, "checkedInAt": iso(now)}
    db.add(ScanAttempt(id=new_id(), event_id=event.id, scanner_user_id=user.id, request_id=payload.clientScanId,
                       request_fingerprint=fingerprint, ticket_id=ticket.id, result_code="CHECKED_IN", response_json=body, created_at=now))
    audit(db, event.id, user.id, action, "check_in", checkin.id, {"ticketNumber": ticket.ticket_number})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Resolve idempotency conflicts first; then inspect the durable check-in state.
        previous = db.query(ScanAttempt).filter(ScanAttempt.scanner_user_id == user.id, ScanAttempt.request_id == payload.clientScanId).first()
        if previous:
            if not secrets.compare_digest(previous.request_fingerprint, fingerprint):
                raise HTTPException(409, "This scan request ID was already used for a different ticket. Scan again.")
            return JSONResponse(status_code=scan_status_http(previous.result_code), content=previous.response_json)
        checkin_after = db.query(CheckIn).filter(CheckIn.ticket_id == ticket.id).first()
        if checkin_after and checkin_after.status == "CHECKED_IN":
            attendee_after = db.get(Attendee, ticket.attendee_id)
            ticket_after = db.get(Ticket, ticket.id)
            duplicate_body = {"result": "ALREADY_CHECKED_IN", "message": "This ticket has already been used.", "ticketNumber": ticket_after.ticket_number,
                              "attendee": {"name": attendee_after.full_name, "year": attendee_after.year}, "checkedInAt": iso(checkin_after.checked_in_at)}
            return persist_scan_result(db, event.id, user.id, payload.clientScanId, fingerprint, ticket.id, "ALREADY_CHECKED_IN", duplicate_body)
        raise HTTPException(409, "Check-in conflicted with another scan. Scan again to verify the saved state.")
    return JSONResponse(status_code=200, content=body)


@app.post("/api/check-ins/{checkin_id}/reverse")
def reverse_checkin(checkin_id: str, payload: ReverseInput, user: User = Depends(roles_allowed("OWNER", "ADMIN")), _csrf: User = Depends(require_csrf), db: Session = Depends(get_db)):
    event = event_for(db)
    checkin = db.query(CheckIn).filter(CheckIn.id == checkin_id, CheckIn.event_id == event.id).with_for_update().first()
    if not checkin:
        raise HTTPException(404, "Check-in record not found.")
    if checkin.status == "REVERSED":
        return {"ok": True, "status": "REVERSED"}
    checkin.status = "REVERSED"
    checkin.reversed_at = utcnow()
    checkin.reversed_by = user.id
    checkin.reversal_reason = payload.reason.strip()
    audit(db, event.id, user.id, "CHECKIN_REVERSED", "check_in", checkin.id,
          {"reason": payload.reason.strip(), "ticketId": checkin.ticket_id})
    db.commit()
    return {"ok": True, "status": "REVERSED"}


@app.get("/api/check-ins/recent")
def recent_checkins(user: User = Depends(current_user), db: Session = Depends(get_db)):
    event = event_for(db)
    rows = db.query(CheckIn, Ticket, Attendee, User).join(Ticket, CheckIn.ticket_id == Ticket.id).join(Attendee, Ticket.attendee_id == Attendee.id).join(User, CheckIn.scanner_user_id == User.id).filter(
        CheckIn.event_id == event.id, CheckIn.status == "CHECKED_IN").order_by(CheckIn.checked_in_at.desc()).limit(100).all()
    return {"rows": [{"checkinId": c.id, "ticketNumber": t.ticket_number, "name": a.full_name, "year": a.year,
                      "checkedInAt": iso(c.checked_in_at), "scanner": u.email} for c, t, a, u in rows]}


@app.get("/api/reports/attendance.xlsx")
def attendance_xlsx(user: User = Depends(roles_allowed("OWNER", "ADMIN", "VIEWER")), db: Session = Depends(get_db)):
    event = event_for(db)
    rows = db.query(Attendee, Ticket, CheckIn).outerjoin(Ticket, Ticket.attendee_id == Attendee.id).outerjoin(CheckIn, CheckIn.ticket_id == Ticket.id).filter(
        Attendee.event_id == event.id).order_by(Attendee.full_name.asc()).all()
    wb = Workbook()
    ws = wb.active
    ws.title = "Confluence Attendance"
    headers = ["Name", "Year", "Email", "Phone", "Attendance Status"]
    ws.append(headers)
    for cell in ws[1]:
        header_font = copy_style(cell.font)
        header_font.bold = True
        header_font.color = "FFFFFF"
        cell.font = header_font
        cell.fill = __import__("openpyxl").styles.PatternFill("solid", fgColor="183B56")
    for attendee, ticket, checkin in rows:
        checked = bool(checkin and checkin.status == "CHECKED_IN")
        ws.append([attendee.full_name, attendee.year or "", attendee.email or "", attendee.phone or "", "CHECKED IN" if checked else "NOT CHECKED IN"])
    widths = [32, 18, 36, 22, 22]
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + idx)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    audit(db, event.id, user.id, "ATTENDANCE_EXPORT_CREATED", "attendance_report", None, {"rows": len(rows), "format": "xlsx"})
    db.commit()
    return StreamingResponse(stream, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": 'attachment; filename="Confluence-attendance.xlsx"', "Cache-Control": "no-store"})


@app.get("/api/staff")
def list_staff(user: User = Depends(roles_allowed("OWNER", "ADMIN")), db: Session = Depends(get_db)):
    rows = db.query(User).order_by(User.created_at.asc()).all()
    return {"rows": [{"id": row.id, "email": row.email, "role": row.role, "active": row.active, "createdAt": iso(row.created_at)} for row in rows]}


@app.post("/api/staff")
def create_staff(payload: StaffCreateInput, user: User = Depends(roles_allowed("OWNER", "ADMIN")), _csrf: User = Depends(require_csrf), db: Session = Depends(get_db)):
    email = normalize_email(payload.email)
    if not email or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(422, "Enter a valid staff email address.")
    if user.role != "OWNER" and payload.role == "ADMIN":
        raise HTTPException(403, "Only the owner can create another administrator.")
    if db.query(User.id).filter(User.email == email).first():
        raise HTTPException(409, "A staff account with this email already exists.")
    row = User(id=new_id(), email=email, password_hash=hash_password(payload.password), role=payload.role, active=True)
    db.add(row)
    event = event_for(db)
    audit(db, event.id, user.id, "STAFF_CREATED", "staff_user", row.id, {"email": email, "role": payload.role})
    db.commit()
    return {"id": row.id, "email": row.email, "role": row.role, "active": row.active}


@app.post("/api/staff/{staff_id}/deactivate")
def deactivate_staff(staff_id: str, user: User = Depends(roles_allowed("OWNER", "ADMIN")), _csrf: User = Depends(require_csrf), db: Session = Depends(get_db)):
    if staff_id == user.id:
        raise HTTPException(400, "You cannot deactivate the account you are currently using.")
    target = db.get(User, staff_id)
    if not target:
        raise HTTPException(404, "Staff account not found.")
    if target.role == "OWNER" and user.role != "OWNER":
        raise HTTPException(403, "Only the owner can change owner accounts.")
    target.active = False
    event = event_for(db)
    audit(db, event.id, user.id, "STAFF_DEACTIVATED", "staff_user", target.id, {"email": target.email})
    db.commit()
    return {"ok": True}


# Serve the built frontend from the same origin so session cookies and CSRF protection apply.
if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="frontend-assets")

@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(404, "API route not found.")
    index = FRONTEND_DIST / "index.html"
    if index.is_file():
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})
    return Response("Frontend is not built yet. See README.md and run the frontend build.", media_type="text/plain", status_code=503)
