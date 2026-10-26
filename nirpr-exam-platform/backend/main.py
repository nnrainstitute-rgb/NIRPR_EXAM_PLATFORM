"""
NIRPR RSO Examination Platform - Main Application
National Institute of Radiation Protection and Research (NIRPR)
Technical arm of the Nigerian Nuclear Regulatory Authority (NNRA)
"""

import os
from dotenv import load_dotenv
load_dotenv()  # must run before database.py / auth.py / email_utils.py read env vars at import time

from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, status, Request, UploadFile, File, Query, Form
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, delete as sa_delete, update as sa_update
import io
import json
import secrets
import random
import hashlib
import base64
import qrcode
from pathlib import Path
from PIL import Image, UnidentifiedImageError

from database import init_db, get_db, get_db_context
from models import (
    User, UserRole, CandidateProfile, TrainingProgram, QuestionBank,
    Question, Exam, ExamAttempt, Answer, AuditLog, AttemptStatus, ExamStatus,
    QuestionType as ModelQuestionType, RetakeAuthorization, ExamScheduleOverride,
    ExamPolicy, CandidateIdentity, QuestionRevision, ResultAppeal, PaymentReceipt,
    Notification, StaffSignature, ProgrammeSignature, CandidateTagSignature, SavedReport, AuthSession, StaffMFA, StaffLoginApproval,
    ExamAccessGrant, ExamRenewalApproval, TrainingBatch, CandidateBatchAssignment, ExamBatchAssignment,
)
import schemas
from question_options import balanced_correct_labels, redistribute_options
from exam_eligibility import batch_candidate_ids, candidate_history, eligible_candidate_ids, require_exam_eligibility
from auth import (
    verify_password, get_password_hash, create_access_token, get_current_user,
    require_admin, require_examiner, require_candidate, require_any, require_role,
    require_real_candidate,
    log_audit, get_client_ip, ACCESS_TOKEN_EXPIRE_MINUTES, generate_secure_token,
    VERIFICATION_TOKEN_EXPIRE_HOURS, PASSWORD_RESET_TOKEN_EXPIRE_MINUTES, decode_token,
)
import pyotp
import httpx
from exam_engine import ExamEngine
import csv_utils
import email_utils
from certificate_utils import build_certificate_pdf, certificate_number, legacy_certificate_number
from signature_utils import signature_png
from name_utils import split_full_name
from candidate_tag_utils import build_candidate_tag_pdf
from candidate_number_utils import candidate_number as format_candidate_number
from infrastructure import rate_limit, heartbeat
from security_config import recaptcha_settings, is_production, RequestBodyLimitMiddleware
from reporting_utils import official_exam_report_pdf

# Kept as aliases so the rest of this file (and anyone grepping for these
# names) doesn't need to change — the real, configurable values now live in
# auth.py and are read from the VERIFICATION_TOKEN_EXPIRE_HOURS /
# PASSWORD_RESET_TOKEN_EXPIRE_MINUTES environment variables.
VERIFICATION_TOKEN_HOURS = VERIFICATION_TOKEN_EXPIRE_HOURS
RESET_TOKEN_MINUTES = PASSWORD_RESET_TOKEN_EXPIRE_MINUTES

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "frontend"))
RECAPTCHA_SITE_KEY, RECAPTCHA_SECRET_KEY, RECAPTCHA_ALLOWED_HOSTNAMES = recaptcha_settings()


async def verify_recaptcha(token: str, remote_ip: str) -> bool:
    if not token or len(token) > 4096 or not RECAPTCHA_SECRET_KEY or not RECAPTCHA_ALLOWED_HOSTNAMES:
        return False
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post("https://www.google.com/recaptcha/api/siteverify", data={
                "secret": RECAPTCHA_SECRET_KEY, "response": token, "remoteip": remote_ip,
            })
        response.raise_for_status()
        result = response.json()
        return (result.get("success") is True
                and result.get("hostname", "").lower() in RECAPTCHA_ALLOWED_HOSTNAMES)
    except Exception:
        return False


def build_user_detail(user: User, candidate_profile: Optional[CandidateProfile] = None) -> schemas.UserDetailResponse:
    """Build a UserDetailResponse without letting pydantic lazily touch the
    SQLAlchemy `candidate_profile` relationship (which would need an active
    async greenlet). We build from the flat UserResponse fields instead and
    attach the profile explicitly."""
    base = schemas.UserResponse.model_validate(user)
    resp = schemas.UserDetailResponse(**base.model_dump(), candidate_profile=None,
                                       is_impersonated=bool(getattr(user, "impersonated_by", None)))
    if candidate_profile:
        resp.candidate_profile = schemas.CandidateProfileResponse.model_validate(candidate_profile)
    return resp


async def allocate_candidate_identity(db: AsyncSession, user_id: int) -> CandidateIdentity:
    """Allocate monotonic public numbers independently of SQLite's reusable user IDs."""
    # Sessions disable autoflush; newly registered/imported profiles must be
    # visible before looking up their programme.
    await db.flush()
    course_code = (await db.execute(
        select(TrainingProgram.code).join(
            CandidateProfile, CandidateProfile.training_program_id == TrainingProgram.id
        ).where(CandidateProfile.user_id == user_id)
    )).scalar_one_or_none()
    if not course_code:
        raise ValueError("A candidate must have a training programme before allocating a number")
    rows = (await db.execute(select(CandidateIdentity.candidate_number,
                                    CandidateIdentity.examination_number))).all()
    highest = 0
    for candidate_number, examination_number in rows:
        for value in (candidate_number, examination_number):
            try:
                highest = max(highest, int((value or "").rsplit("-", 1)[-1]))
            except (TypeError, ValueError):
                continue
    sequence = max(user_id, highest + 1)
    return CandidateIdentity(user_id=user_id, candidate_number=format_candidate_number(course_code, sequence),
                             examination_number=f"NIRPR-EXM-{sequence:06d}")


async def notify_exam_schedule(db: AsyncSession, exam: Exam, is_update: bool = False, user_ids=None):
    candidates = (await db.execute(select(User).join(CandidateProfile, CandidateProfile.user_id == User.id).where(
        CandidateProfile.training_program_id == exam.training_program_id, User.role == UserRole.CANDIDATE,
        User.is_active == True))).scalars().all()
    eligible = (await eligible_candidate_ids(db, exam)) & (await batch_candidate_ids(db, exam))
    if user_ids is not None:
        eligible &= set(user_ids)
    candidates = [candidate for candidate in candidates if candidate.id in eligible]
    title = "Exam schedule updated" if is_update else "New examination scheduled"
    message = (f"{exam.title} will take place from {exam.start_time.strftime('%d %b %Y, %I:%M %p')} "
               f"to {exam.end_time.strftime('%d %b %Y, %I:%M %p')}. Duration: {exam.duration_minutes} minutes.")
    for candidate in candidates:
        db.add(Notification(user_id=candidate.id, title=title, message=message, notification_type="exam_reminder"))
        email_utils.send_exam_schedule_email(candidate.email, candidate.full_name, exam.title, exam.start_time,
                                             exam.end_time, exam.duration_minutes, is_update=is_update)
    await db.commit()

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with get_db_context() as db:
        # Older versions could leave an identity behind when a candidate was
        # deleted while SQLite foreign-key checks were disabled. Remove only
        # identities whose user record no longer exists before allocating any
        # missing numbers. This prevents a reused user ID from colliding with
        # the orphaned public candidate/examination number.
        await db.execute(sa_delete(CandidateIdentity).where(
            ~CandidateIdentity.user_id.in_(select(User.id))
        ))
        candidate_ids = (await db.execute(select(User.id).where(User.role == UserRole.CANDIDATE))).scalars().all()
        existing = set((await db.execute(select(CandidateIdentity.user_id))).scalars().all())
        for user_id in candidate_ids:
            if user_id not in existing:
                db.add(await allocate_candidate_identity(db, user_id))
    yield


app = FastAPI(
    title="NIRPR RSO Examination Platform",
    description="Certification examination platform for Radiation Safety Officers (RSO) in Nigeria",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestBodyLimitMiddleware)

@app.middleware("http")
async def security_headers_and_rate_limits(request: Request, call_next):
    ip = get_client_ip(request) or "unknown"
    if request.url.path.startswith("/api/auth/") and not await rate_limit(f"auth:{ip}", 30, 60):
        return JSONResponse({"detail": "Too many authentication requests. Please try again shortly."}, status_code=429)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' https://www.google.com/recaptcha/ https://www.gstatic.com/recaptcha/; frame-src blob: https://www.google.com/recaptcha/ https://recaptcha.google.com/recaptcha/; img-src 'self' data: blob: https://www.gstatic.com/recaptcha/; connect-src 'self' https://www.google.com/recaptcha/"
    if is_production():
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
    return response


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/", include_in_schema=False)
async def serve_login():
    return FileResponse(os.path.join(FRONTEND_DIR, "login.html"))


@app.get("/candidate", include_in_schema=False)
async def serve_candidate():
    return FileResponse(os.path.join(FRONTEND_DIR, "candidate.html"))


@app.get("/admin-login", include_in_schema=False)
async def serve_admin_login():
    return FileResponse(os.path.join(FRONTEND_DIR, "admin-login.html"))


@app.get("/admin", include_in_schema=False)
async def serve_admin():
    return FileResponse(os.path.join(FRONTEND_DIR, "admin.html"))


@app.get("/change-password", include_in_schema=False)
async def serve_change_password():
    return FileResponse(os.path.join(FRONTEND_DIR, "change-password.html"))


@app.get("/register", include_in_schema=False)
async def serve_register():
    return FileResponse(os.path.join(FRONTEND_DIR, "register.html"))


@app.get("/verify-email", include_in_schema=False)
async def serve_verify_email():
    return FileResponse(os.path.join(FRONTEND_DIR, "verify-email.html"))


@app.get("/forgot-password", include_in_schema=False)
async def serve_forgot_password():
    return FileResponse(os.path.join(FRONTEND_DIR, "forgot-password.html"))


@app.get("/reset-password", include_in_schema=False)
async def serve_reset_password():
    return FileResponse(os.path.join(FRONTEND_DIR, "reset-password.html"))


@app.get("/verify-certificate", include_in_schema=False)
async def serve_certificate_verification():
    return FileResponse(os.path.join(FRONTEND_DIR, "verify-certificate.html"))


@app.get("/api/health", include_in_schema=False)
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.post("/api/auth/register", response_model=schemas.RegisterResponse, tags=["Auth"])
async def register(data: schemas.UserCreate, request: Request, db: AsyncSession = Depends(get_db)):
    if not await verify_recaptcha(data.recaptcha_token or "", get_client_ip(request) or ""):
        raise HTTPException(status_code=400, detail="CAPTCHA_FAILED: Complete the human verification challenge again.")
    result = await db.execute(select(User).where(User.email == data.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="An account with this email already exists")

    if data.role != schemas.UserRole.CANDIDATE:
        # Public self-registration is only for candidates. Staff accounts are
        # created by an admin via /api/admin/users or CSV import.
        raise HTTPException(status_code=403, detail="Only candidate self-registration is allowed here")

    if not data.training_program_id:
        raise HTTPException(status_code=400, detail="Please select the RSO training programme you attended")

    result = await db.execute(select(TrainingProgram).where(TrainingProgram.id == data.training_program_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Selected training programme was not found")

    surname, first_name, other_name = (data.surname, data.first_name, data.other_name) if data.surname and data.first_name else split_full_name(data.full_name)
    user = User(
        email=data.email,
        hashed_password=get_password_hash(data.password),
        surname=surname, first_name=first_name, other_name=other_name,
        phone=data.phone,
        role=UserRole.CANDIDATE,
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    await db.flush()

    profile = CandidateProfile(
        user_id=user.id,
        training_program_id=data.training_program_id,
        institution=data.institution,
        qualification=data.qualification,
        practice_type=data.practice_type,
    )
    db.add(profile)
    db.add(await allocate_candidate_identity(db, user.id))
    await db.commit()
    await db.refresh(user)

    await log_audit(db, "user_register", user_id=user.id, entity_type="user", entity_id=user.id,
                     ip_address=get_client_ip(request), user_agent=request.headers.get("user-agent"))

    return schemas.RegisterResponse(
        detail="Registration successful. Your account is active and you can log in now.",
        email=user.email,
        email_sent=False,
    )


@app.get("/api/admin/email-delivery/status", tags=["Admin"])
async def email_delivery_status(current_user: User = Depends(require_admin)):
    return {"configured": bool(email_utils.SMTP_HOST), "smtp_host": email_utils.SMTP_HOST or None,
            "smtp_port": email_utils.SMTP_PORT, "from_address": email_utils.SMTP_FROM,
            "app_base_url": email_utils.APP_BASE_URL,
            "mode": "smtp" if email_utils.SMTP_HOST else "local_outbox",
            "detail": ("Verification messages are sent through SMTP." if email_utils.SMTP_HOST else
                       "SMTP_HOST is empty. Messages are saved in backend/outbox and are not delivered to recipients.")}


@app.post("/api/auth/login", tags=["Auth"])
async def login(data: schemas.LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    if not await verify_recaptcha(data.recaptcha_token or "", get_client_ip(request) or ""):
        raise HTTPException(status_code=400, detail="CAPTCHA_FAILED: Complete the photographic verification challenge again.")
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()

    if user and user.locked_until and user.locked_until > datetime.utcnow():
        raise HTTPException(status_code=423, detail=f"Account temporarily locked until {user.locked_until.isoformat()}Z")

    if not user or not verify_password(data.password, user.hashed_password):
        if user:
            user.login_attempts = (user.login_attempts or 0) + 1
            if user.login_attempts >= 5:
                user.locked_until = datetime.utcnow() + timedelta(minutes=15)
            await db.commit()
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account has been deactivated. Contact NIRPR administration.")

    if data.portal == "candidate" and user.role != UserRole.CANDIDATE:
        raise HTTPException(status_code=403, detail="Staff accounts must sign in through the administration login page.")
    if data.portal == "staff" and user.role == UserRole.CANDIDATE:
        raise HTTPException(status_code=403, detail="Candidate accounts must sign in through the candidate login page.")

    if user.role in (UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.EXAMINER):
        mfa = (await db.execute(select(StaffMFA).where(StaffMFA.user_id == user.id))).scalar_one_or_none()
        if mfa and mfa.enabled and not pyotp.TOTP(mfa.secret).verify(data.otp or "", valid_window=1):
            raise HTTPException(status_code=401, detail="OTP_REQUIRED: Enter the current authenticator code.")

    if data.portal == "staff" and user.role in (UserRole.ADMIN, UserRole.EXAMINER):
        poll_token = secrets.token_urlsafe(32); request_id = secrets.token_hex(24)
        db.add(StaffLoginApproval(id=request_id, user_id=user.id,
            poll_token_hash=hashlib.sha256(poll_token.encode()).hexdigest(), status="pending",
            ip_address=get_client_ip(request), user_agent=request.headers.get("user-agent", ""),
            expires_at=datetime.utcnow() + timedelta(minutes=15)))
        superiors = (await db.execute(select(User).where(User.role == UserRole.SUPER_ADMIN,
                                                         User.is_active == True))).scalars().all()
        for superior in superiors:
            db.add(Notification(user_id=superior.id, title="Staff login approval required",
                message=f"{user.full_name} ({user.email}) is requesting administration access from {get_client_ip(request)}. Open Governance to approve or reject it.",
                notification_type="security"))
            email_utils.send_general_notification_email(superior.email, superior.full_name,
                "Staff login approval required", f"{user.full_name} ({user.email}) requested administration access. The request expires in 15 minutes. Open Governance to decide it.")
        await db.commit()
        await log_audit(db, "staff_login_approval_requested", user_id=user.id,
                        entity_type="staff_login_approval", details={"request_id": request_id})
        return {"approval_required": True, "request_id": request_id, "poll_token": poll_token,
                "expires_in": 900, "detail": "Waiting for approval from the Head of ICT or General Manager."}

    user.last_login = datetime.utcnow()
    user.login_attempts = 0
    user.locked_until = None
    await db.commit()

    session_id = secrets.token_hex(32)
    user_agent = request.headers.get("user-agent", "")
    known_device = (await db.execute(select(AuthSession.id).where(AuthSession.user_id == user.id,
        AuthSession.user_agent == user_agent).limit(1))).scalar_one_or_none()
    db.add(AuthSession(id=session_id, user_id=user.id, ip_address=get_client_ip(request), user_agent=user_agent,
                       device_label=user_agent[:180] or "Unknown browser",
                       expires_at=datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)))
    if not known_device:
        db.add(Notification(user_id=user.id, title="New device login",
                            message=f"A new login was detected from {get_client_ip(request)}. If this was not you, open Settings → Logged-in devices and select Revoke for that session. When running locally, 127.0.0.1 means this computer.",
                            notification_type="security"))
    await db.commit()
    token = create_access_token({"sub": str(user.id), "role": user.role.value, "sid": session_id})
    await log_audit(db, "user_login", user_id=user.id, entity_type="user", entity_id=user.id,
                     ip_address=get_client_ip(request), user_agent=request.headers.get("user-agent"))

    return schemas.Token(
        access_token=token,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user_role=user.role.value,
        must_change_password=user.must_change_password,
    )


@app.post("/api/auth/change-temporary-password", response_model=schemas.MessageResponse, tags=["Auth"])
async def change_temporary_password(data: schemas.PasswordChange,
                                    current_user: User = Depends(require_any),
                                    db: AsyncSession = Depends(get_db)):
    if not current_user.must_change_password:
        raise HTTPException(status_code=400, detail="This account is not using a temporary password.")
    if not verify_password(data.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="The current temporary password is incorrect.")
    if verify_password(data.new_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Choose a new password different from the temporary password.")
    current_user.hashed_password = get_password_hash(data.new_password)
    current_user.must_change_password = False
    await db.commit()
    await log_audit(db, "temporary_password_changed", user_id=current_user.id,
                    entity_type="user", entity_id=current_user.id)
    return schemas.MessageResponse(detail="Password updated successfully. You can now use the portal.")


@app.get("/api/auth/captcha", tags=["Auth"])
async def captcha():
    groups = {
        "vehicles": ["🚗", "🚌", "🚚", "🚕", "🚙"],
        "bicycles": ["🚲", "🚴", "🚵", "🚴‍♀️"],
        "animals": ["🐕", "🐈", "🐘", "🦒", "🐎", "🐇"],
    }
    target = random.choice(list(groups))
    target_tiles = random.sample(groups[target], 3)
    distractor_pool = [icon for name, icons in groups.items() if name != target for icon in icons]
    tiles = [(icon, True) for icon in target_tiles] + [(icon, False) for icon in random.sample(distractor_pool, 6)]
    random.shuffle(tiles)
    selection = [index for index, (_, correct) in enumerate(tiles) if correct]
    token = create_access_token({"purpose": "captcha", "selection": selection}, expires_delta=timedelta(minutes=5))
    return {"question": f"Select all images containing {target}", "tiles": [icon for icon, _ in tiles],
            "token": token, "challenge_type": "image_grid"}


@app.get("/api/auth/recaptcha-config", tags=["Auth"])
async def recaptcha_config():
    if not RECAPTCHA_SITE_KEY or not RECAPTCHA_SECRET_KEY or not RECAPTCHA_ALLOWED_HOSTNAMES:
        raise HTTPException(status_code=503, detail="Human verification is not configured. Contact the administrator.")
    return {"site_key": RECAPTCHA_SITE_KEY, "provider": "google_recaptcha_v2"}


@app.post("/api/auth/mfa/setup", tags=["Auth"])
async def setup_mfa(current_user: User = Depends(require_examiner), db: AsyncSession = Depends(get_db)):
    secret = pyotp.random_base32()
    record = (await db.execute(select(StaffMFA).where(StaffMFA.user_id == current_user.id))).scalar_one_or_none()
    if record and record.enabled:
        raise HTTPException(status_code=409, detail="Disable existing two-factor authentication with a valid code before replacing it.")
    if record:
        record.secret, record.enabled = secret, False
    else:
        db.add(StaffMFA(user_id=current_user.id, secret=secret, enabled=False))
    await db.commit()
    uri = pyotp.TOTP(secret).provisioning_uri(current_user.email, issuer_name="NIRPR Exams")
    qr_buffer = io.BytesIO(); qrcode.make(uri).save(qr_buffer, format="PNG")
    qr_data = "data:image/png;base64," + base64.b64encode(qr_buffer.getvalue()).decode()
    return {"secret": secret, "otpauth_uri": uri, "qr_code_data_url": qr_data}


@app.post("/api/auth/mfa/enable", tags=["Auth"])
async def enable_mfa(code: str, current_user: User = Depends(require_examiner), db: AsyncSession = Depends(get_db)):
    record = (await db.execute(select(StaffMFA).where(StaffMFA.user_id == current_user.id))).scalar_one_or_none()
    if not record or not pyotp.TOTP(record.secret).verify(code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid authentication code")
    record.enabled = True
    await db.commit()
    return {"detail": "Two-factor authentication enabled"}


@app.get("/api/auth/mfa/status", tags=["Auth"])
async def mfa_status(current_user: User = Depends(require_examiner), db: AsyncSession = Depends(get_db)):
    record = (await db.execute(select(StaffMFA).where(
        StaffMFA.user_id == current_user.id))).scalar_one_or_none()
    return {"enabled": bool(record and record.enabled)}


@app.post("/api/auth/mfa/disable", tags=["Auth"])
async def disable_mfa(code: str, request: Request,
                      current_user: User = Depends(require_examiner),
                      db: AsyncSession = Depends(get_db)):
    record = (await db.execute(select(StaffMFA).where(
        StaffMFA.user_id == current_user.id))).scalar_one_or_none()
    if not record or not record.enabled:
        raise HTTPException(status_code=400, detail="Two-factor authentication is not enabled")
    if not pyotp.TOTP(record.secret).verify(code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid authentication code")
    await db.delete(record)
    await db.commit()
    await log_audit(db, "staff_mfa_disabled", user_id=current_user.id,
                    entity_type="user", entity_id=current_user.id,
                    ip_address=get_client_ip(request),
                    user_agent=request.headers.get("user-agent"))
    return {"detail": "Two-factor authentication disabled"}


@app.get("/api/auth/sessions", tags=["Auth"])
async def list_sessions(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AuthSession).where(AuthSession.user_id == current_user.id)
                             .order_by(AuthSession.last_seen_at.desc()))).scalars().all()
    return [{"id": s.id, "device": s.device_label, "ip_address": s.ip_address, "created_at": s.created_at,
             "last_seen_at": s.last_seen_at, "expires_at": s.expires_at, "revoked": bool(s.revoked_at)} for s in rows]


@app.post("/api/auth/logout", tags=["Auth"])
async def logout(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await db.execute(sa_update(AuthSession).where(
        AuthSession.id == current_user.authenticated_session_id
    ).values(revoked_at=datetime.utcnow()))
    await db.commit()
    return {"detail": "Signed out"}


@app.delete("/api/auth/sessions/{session_id}", tags=["Auth"])
async def revoke_session(session_id: str, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    record = (await db.execute(select(AuthSession).where(AuthSession.id == session_id,
                                                         AuthSession.user_id == current_user.id))).scalar_one_or_none()
    if not record: raise HTTPException(status_code=404, detail="Session not found")
    record.revoked_at = datetime.utcnow(); await db.commit(); return {"detail": "Session revoked"}


@app.post("/api/auth/staff-approval/{request_id}/status", tags=["Auth"])
async def staff_approval_status(request_id: str, poll_token: str, request: Request,
                                db: AsyncSession = Depends(get_db)):
    approval = (await db.execute(select(StaffLoginApproval).where(
        StaffLoginApproval.id == request_id))).scalar_one_or_none()
    supplied_hash = hashlib.sha256(poll_token.encode()).hexdigest()
    if not approval or not secrets.compare_digest(approval.poll_token_hash, supplied_hash):
        raise HTTPException(status_code=404, detail="Login approval request not found")
    if approval.expires_at < datetime.utcnow():
        approval.status = "expired"; await db.commit()
        raise HTTPException(status_code=410, detail="Approval request expired. Sign in again.")
    if approval.status == "rejected":
        raise HTTPException(status_code=403, detail=approval.decision_notes or "The superior rejected this login request.")
    if approval.status != "approved":
        return {"status": "pending", "detail": "Waiting for Head of ICT or General Manager approval."}
    if approval.consumed_at:
        raise HTTPException(status_code=409, detail="This approval has already been used")
    user = (await db.execute(select(User).where(User.id == approval.user_id))).scalar_one_or_none()
    if not user or not user.is_active: raise HTTPException(status_code=403, detail="Staff account is unavailable")
    session_id = secrets.token_hex(32); user_agent = request.headers.get("user-agent", "")
    db.add(AuthSession(id=session_id, user_id=user.id, ip_address=get_client_ip(request), user_agent=user_agent,
        device_label=user_agent[:180] or "Unknown browser",
        expires_at=datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)))
    approval.consumed_at = datetime.utcnow(); user.last_login = datetime.utcnow()
    await db.commit()
    token = create_access_token({"sub": str(user.id), "role": user.role.value, "sid": session_id})
    await log_audit(db, "staff_login_approved_and_issued", user_id=user.id,
                    entity_type="staff_login_approval", details={"request_id": request_id,
                    "authority": approval.approver_authority})
    return {"status": "approved", "access_token": token, "token_type": "bearer",
            "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60, "user_role": user.role.value,
            "must_change_password": user.must_change_password}


@app.get("/api/admin/staff-login-approvals", tags=["Admin Security"])
async def staff_login_approvals(current_user: User = Depends(require_role(UserRole.SUPER_ADMIN)),
                                db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(StaffLoginApproval, User.full_name, User.email)
        .join(User, StaffLoginApproval.user_id == User.id)
        .order_by(StaffLoginApproval.requested_at.desc()).limit(100))).all()
    return [{"id": a.id, "staff_name": name, "email": email, "status": a.status,
             "ip_address": a.ip_address, "user_agent": a.user_agent, "requested_at": a.requested_at,
             "expires_at": a.expires_at, "authority": a.approver_authority,
             "notes": a.decision_notes} for a, name, email in rows]


@app.patch("/api/admin/staff-login-approvals/{request_id}", tags=["Admin Security"])
async def decide_staff_login(request_id: str, data: schemas.StaffLoginDecision,
                             current_user: User = Depends(require_role(UserRole.SUPER_ADMIN)),
                             db: AsyncSession = Depends(get_db)):
    approval = (await db.execute(select(StaffLoginApproval).where(
        StaffLoginApproval.id == request_id))).scalar_one_or_none()
    if not approval: raise HTTPException(status_code=404, detail="Approval request not found")
    if approval.status != "pending": raise HTTPException(status_code=409, detail="This request has already been decided")
    if approval.expires_at < datetime.utcnow(): raise HTTPException(status_code=410, detail="This request has expired")
    approval.status, approval.approved_by = data.decision, current_user.id
    approval.approver_authority, approval.decision_notes = data.authority, data.notes
    approval.decided_at = datetime.utcnow(); await db.commit()
    await log_audit(db, f"staff_login_{data.decision}", user_id=current_user.id,
                    entity_type="staff_login_approval", details={"request_id": request_id,
                    "authority": data.authority, "staff_user_id": approval.user_id})
    return {"detail": f"Staff login {data.decision}"}


async def dashboard_candidate_number(db, user, profile):
    if profile is None:
        return None
    # Reuse the actual issued certificate's course and year when available.
    row = (await db.execute(select(ExamAttempt, TrainingProgram.code)
        .join(Exam, ExamAttempt.exam_id == Exam.id)
        .join(TrainingProgram, Exam.training_program_id == TrainingProgram.id)
        .where(ExamAttempt.user_id == user.id,
               Exam.training_program_id == profile.training_program_id,
               ExamAttempt.status == AttemptStatus.APPROVED,
               ExamAttempt.result_released == True, ExamAttempt.passed == True)
        .order_by(func.coalesce(ExamAttempt.approved_at, ExamAttempt.submitted_at,
                                ExamAttempt.created_at).desc(), ExamAttempt.id.desc()).limit(1))).first()
    if row:
        attempt, code = row
        issued_on = attempt.approved_at or attempt.submitted_at or attempt.created_at
        return certificate_number(code, issued_on, attempt.id, user.id)
    code = (await db.execute(select(TrainingProgram.code).where(
        TrainingProgram.id == profile.training_program_id))).scalar_one_or_none()
    return certificate_number(code, datetime.utcnow(), 0, user.id) if code else None


@app.get("/api/auth/me", response_model=schemas.UserDetailResponse, tags=["Auth"])
async def me(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cp = None
    candidate_number = None
    if current_user.role == UserRole.CANDIDATE:
        result = await db.execute(select(CandidateProfile).where(CandidateProfile.user_id == current_user.id))
        cp = result.scalar_one_or_none()
        candidate_number = (await db.execute(
            select(CandidateIdentity.candidate_number).where(CandidateIdentity.user_id == current_user.id)
        )).scalar_one_or_none()
    detail = build_user_detail(current_user, cp)
    detail.candidate_number = candidate_number
    if current_user.role == UserRole.CANDIDATE:
        detail.dashboard_candidate_number = await dashboard_candidate_number(db, current_user, cp)
    return detail


@app.post("/api/admin/impersonate/{user_id}", response_model=schemas.Token, tags=["Admin"])
async def impersonate_candidate(
    user_id: int,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Issue a candidate-scoped session token so an admin can see exactly
    what that candidate's dashboard looks like. Fully audit-logged.
    Exam-taking actions (start / answer / submit) are blocked on this
    token — see require_real_candidate — so this can never be used to sit
    or complete an exam on a candidate's behalf."""
    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if target.role != UserRole.CANDIDATE:
        raise HTTPException(status_code=400, detail="You can only view the platform as a candidate account.")
    if not target.is_active:
        raise HTTPException(status_code=400, detail="This candidate account is deactivated.")

    token = create_access_token({
        "sub": str(target.id),
        "role": target.role.value,
        "impersonated_by": current_user.id,
        "sid": current_user.authenticated_session_id,
    }, expires_delta=timedelta(minutes=60))

    await log_audit(db, "impersonation_start", user_id=current_user.id, entity_type="user", entity_id=target.id,
                     details={"admin_email": current_user.email, "candidate_email": target.email},
                     ip_address=get_client_ip(request))

    return schemas.Token(access_token=token, expires_in=60 * 60, user_role=target.role.value)


@app.get("/api/auth/verify-email", response_model=schemas.MessageResponse, tags=["Auth"])
async def verify_email(token: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.verification_token == token))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or already-used verification link.")
    if user.verification_token_expires and user.verification_token_expires < datetime.utcnow():
        raise HTTPException(status_code=400, detail="This verification link has expired. Please request a new one.")

    user.email_verified = True
    user.verification_token = None
    user.verification_token_expires = None
    await db.commit()
    await log_audit(db, "email_verified", user_id=user.id, entity_type="user", entity_id=user.id)

    return schemas.MessageResponse(detail="Your email has been verified. You can now log in.")


@app.post("/api/auth/resend-verification", response_model=schemas.MessageResponse, tags=["Auth"])
async def resend_verification(data: schemas.ResendVerificationRequest, db: AsyncSession = Depends(get_db)):
    if not email_utils.SMTP_HOST:
        return schemas.MessageResponse(detail="Email delivery is not configured. The message cannot be sent until SMTP is configured.")
    generic = schemas.MessageResponse(
        detail="If an unverified account exists for that email address, a new verification link has been sent."
    )
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()
    if not user or user.email_verified:
        return generic

    token = generate_secure_token()
    user.verification_token = token
    user.verification_token_expires = datetime.utcnow() + timedelta(hours=VERIFICATION_TOKEN_HOURS)
    await db.commit()
    sent = email_utils.send_verification_email(user.email, user.full_name, token, expires_at=user.verification_token_expires)
    return generic if sent else schemas.MessageResponse(detail="The mail server did not accept the message. Please contact an administrator.")


@app.post("/api/auth/forgot-password", response_model=schemas.MessageResponse, tags=["Auth"])
async def forgot_password(data: schemas.ForgotPasswordRequest, request: Request,
                          db: AsyncSession = Depends(get_db)):
    if not await verify_recaptcha(data.recaptcha_token or "", get_client_ip(request) or ""):
        raise HTTPException(status_code=400, detail="CAPTCHA_FAILED: Complete the human verification challenge again.")
    generic = schemas.MessageResponse(
        detail="If an account exists for that email address, a password reset link has been sent."
    )
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()
    # This page belongs to candidate login. Keep the response deliberately
    # generic so it cannot be used to discover registered email addresses.
    if not user or user.role != UserRole.CANDIDATE or not user.is_active:
        return generic

    token = generate_secure_token()
    user.reset_token = token
    user.reset_token_expires = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_MINUTES)
    await db.commit()
    email_utils.send_password_reset_email(user.email, user.full_name, token, expires_at=user.reset_token_expires)
    await log_audit(db, "password_reset_requested", user_id=user.id,
                    entity_type="user", entity_id=user.id,
                    ip_address=get_client_ip(request), user_agent=request.headers.get("user-agent"))
    return generic


@app.get("/api/auth/reset-password/token-status", tags=["Auth"])
async def reset_password_token_status(token: str, db: AsyncSession = Depends(get_db)):
    """Lets the reset-password page show a live countdown of time remaining,
    without consuming the token or revealing which account it belongs to."""
    result = await db.execute(select(User).where(User.reset_token == token))
    user = result.scalar_one_or_none()
    if not user or not user.reset_token_expires:
        return {"valid": False, "seconds_remaining": 0}
    remaining = (user.reset_token_expires - datetime.utcnow()).total_seconds()
    if remaining <= 0:
        return {"valid": False, "seconds_remaining": 0}
    return {"valid": True, "seconds_remaining": int(remaining), "expires_at": user.reset_token_expires.isoformat()}


@app.post("/api/auth/reset-password", response_model=schemas.MessageResponse, tags=["Auth"])
async def reset_password(data: schemas.ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.reset_token == data.token))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or already-used reset link.")
    if not user.reset_token_expires or user.reset_token_expires <= datetime.utcnow():
        raise HTTPException(status_code=400, detail="This reset link has expired. Please request a new one.")

    user.hashed_password = get_password_hash(data.new_password)
    user.reset_token = None
    user.reset_token_expires = None
    user.login_attempts = 0
    user.locked_until = None
    user.must_change_password = False
    await db.execute(sa_update(AuthSession).where(
        AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)
    ).values(revoked_at=datetime.utcnow()))
    await db.commit()
    await log_audit(db, "password_reset", user_id=user.id, entity_type="user", entity_id=user.id)

    return schemas.MessageResponse(detail="Your password has been reset. You can now log in with your new password.")


# ---------------------------------------------------------------------------
# Training Programs (RSO practice-area tracks)
# ---------------------------------------------------------------------------
@app.get("/api/training-programs", response_model=List[schemas.TrainingProgramResponse], tags=["Training Programs"])
async def list_training_programs(active_only: bool = True, db: AsyncSession = Depends(get_db)):
    q = select(TrainingProgram)
    if active_only:
        q = q.where(TrainingProgram.is_active == True)
    result = await db.execute(q.order_by(TrainingProgram.name))
    programs = result.scalars().all()

    out = []
    for p in programs:
        qb_count = (await db.execute(
            select(func.count(QuestionBank.id)).where(QuestionBank.training_program_id == p.id)
        )).scalar()
        c_count = (await db.execute(
            select(func.count(CandidateProfile.id)).where(CandidateProfile.training_program_id == p.id)
        )).scalar()
        r = schemas.TrainingProgramResponse.model_validate(p)
        r.question_bank_count = qb_count or 0
        r.candidate_count = c_count or 0
        out.append(r)
    return out


@app.post("/api/training-programs", response_model=schemas.TrainingProgramResponse, tags=["Training Programs"])
async def create_training_program(
    data: schemas.TrainingProgramCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(TrainingProgram).where(TrainingProgram.code == data.code))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="A training program with this code already exists")
    program = TrainingProgram(**data.model_dump())
    db.add(program)
    await db.commit()
    await db.refresh(program)
    await log_audit(db, "training_program_create", user_id=current_user.id, entity_type="training_program", entity_id=program.id)
    resp = schemas.TrainingProgramResponse.model_validate(program)
    return resp


@app.post("/api/training-programs/{program_id}/generate-starter-questions", tags=["Training Programs"])
async def generate_starter_questions(program_id: int, count: int = Query(15, ge=5, le=100),
                                     current_user: User = Depends(require_admin),
                                     db: AsyncSession = Depends(get_db)):
    """Create an editable starter bank of radiation-protection questions for a new programme."""
    program = (await db.execute(select(TrainingProgram).where(TrainingProgram.id == program_id))).scalar_one_or_none()
    if not program: raise HTTPException(status_code=404, detail="Training programme not found")
    bank = (await db.execute(select(QuestionBank).where(
        QuestionBank.training_program_id == program_id, QuestionBank.name == "Generated Starter Question Bank"))).scalar_one_or_none()
    if not bank:
        bank = QuestionBank(training_program_id=program_id, name="Generated Starter Question Bank",
                            description=f"Editable starter questions for {program.name}. Review and approve before examination use.",
                            created_by=current_user.id)
        db.add(bank); await db.flush()
    area = program.practice_area
    templates = [
        ("Radiation protection principles", f"Which principle should guide every justified radiation activity in {area}?", {"A":"Keep exposure as low as reasonably achievable","B":"Maximize exposure time","C":"Remove all shielding","D":"Ignore dose monitoring"}, ["A"], "ALARA requires exposures to be kept as low as reasonably achievable."),
        ("Time, distance and shielding", "Which action generally reduces external radiation exposure to a worker?", {"A":"Increasing time near the source","B":"Increasing distance from the source","C":"Removing shielding","D":"Standing closer to the source"}, ["B"], "Reducing time, increasing distance and using shielding reduce external exposure."),
        ("Personnel monitoring", "What is the primary purpose of a personal dosimeter?", {"A":"To measure room temperature","B":"To estimate the radiation dose received by the wearer","C":"To identify the patient's name","D":"To control electrical voltage"}, ["B"], "A personal dosimeter records or estimates occupational radiation exposure."),
        ("Controlled areas", "Why should access to a controlled radiation area be restricted?", {"A":"To reduce unauthorized exposure and maintain safety controls","B":"To increase public occupancy","C":"To avoid keeping records","D":"To remove warning signs"}, ["A"], "Restricted access helps ensure that only authorized and protected persons enter."),
        ("Emergency response", "What should be the first priority after discovering a suspected radiation incident?", {"A":"Protect people and secure the area","B":"Continue normal work","C":"Delete monitoring records","D":"Move the source without assessment"}, ["A"], "Immediate protection of people and control of the area come before recovery actions."),
        ("Quality assurance", f"Why is routine quality assurance important in {area}?", {"A":"It confirms equipment and procedures continue to perform safely","B":"It replaces staff training","C":"It eliminates the need for records","D":"It increases unnecessary exposure"}, ["A"], "Quality assurance identifies performance problems and supports safe, consistent practice."),
        ("Regulatory compliance", "What is the best evidence that a required radiation-safety check was completed?", {"A":"A dated and authorized record","B":"An informal verbal statement only","C":"An unsigned blank form","D":"No documentation"}, ["A"], "Controlled, dated records provide traceable evidence of compliance."),
        ("Warning systems", "What is the purpose of radiation warning signs and labels?", {"A":"To identify hazards and required precautions","B":"To decorate the work area","C":"To replace dose limits","D":"To authorize every visitor"}, ["A"], "Signs and labels communicate the presence and nature of radiation hazards."),
        ("Source security", "When a radiation source is not in use, it should be:", {"A":"Secured against unauthorized access","B":"Left unattended in a public area","C":"Stored without identification","D":"Transferred without records"}, ["A"], "Sources must remain secured, identified and under appropriate control."),
        ("Pregnancy protection", "What is the appropriate response when a radiation worker declares pregnancy?", {"A":"Perform an individual risk and dose assessment and apply applicable controls","B":"Ignore the declaration","C":"Automatically increase permitted dose","D":"Stop all workplace monitoring"}, ["A"], "The employer should assess the work and apply appropriate protection under applicable requirements."),
        ("Contamination control", "Which practice helps prevent the spread of radioactive contamination?", {"A":"Monitoring and controlling movement from affected areas","B":"Removing protective clothing outside the area","C":"Eating in contamination areas","D":"Avoiding surveys"}, ["A"], "Monitoring, protective measures and controlled movement limit contamination spread."),
        ("Incident reporting", "Why should unusual radiation events be reported promptly?", {"A":"To enable assessment, corrective action and required notification","B":"To avoid investigation","C":"To conceal equipment faults","D":"To discontinue all records"}, ["A"], "Prompt reporting supports protection, investigation and regulatory compliance."),
        ("Optimization", "Which option best demonstrates optimization of protection?", {"A":"Choosing controls that reduce dose while achieving the required task","B":"Using the highest exposure available","C":"Ignoring workload and shielding","D":"Removing operating procedures"}, ["A"], "Optimization balances the required objective with practical dose reduction."),
        ("Workplace surveys", "A radiation workplace survey is mainly used to:", {"A":"Identify and evaluate radiation levels in the work environment","B":"Measure staff attendance","C":"Replace personal monitoring in all cases","D":"Calculate programme fees"}, ["A"], "Surveys assess workplace radiation conditions and help verify controls."),
        ("Training and competence", f"Before independently performing radiation work in {area}, a worker should:", {"A":"Be trained, assessed as competent and authorized","B":"Rely only on observation","C":"Ignore local rules","D":"Use equipment without instruction"}, ["A"], "Training, competence and authorization are fundamental administrative controls."),
    ]
    existing_count = (await db.execute(select(func.count(Question.id)).where(Question.question_bank_id == bank.id))).scalar() or 0
    created = 0
    targets = balanced_correct_labels(count)
    for index in range(count):
        topic, text, options, correct, explanation = templates[index % len(templates)]
        options, correct = redistribute_options(options, correct, targets[index])
        cycle = index // len(templates)
        if cycle: text = f"Scenario {cycle + 1}: {text}"
        db.add(Question(question_bank_id=bank.id, question_text=text, question_type=ModelQuestionType.MULTIPLE_CHOICE,
                        options=options, correct_answer=correct, explanation=explanation, marks=1.0,
                        difficulty="medium" if index % 3 else "easy", topic=topic, is_active=True))
        created += 1
    await db.commit()
    await log_audit(db, "starter_questions_generated", user_id=current_user.id, entity_type="question_bank",
                    entity_id=bank.id, details={"program_id": program_id, "created": created, "previous_count": existing_count})
    return {"detail": f"{created} starter questions generated. Review them before use.", "bank_id": bank.id, "created": created}


@app.patch("/api/training-programs/{program_id}", response_model=schemas.TrainingProgramResponse, tags=["Training Programs"])
async def update_training_program(
    program_id: int,
    data: schemas.TrainingProgramUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(TrainingProgram).where(TrainingProgram.id == program_id))
    program = result.scalar_one_or_none()
    if not program:
        raise HTTPException(status_code=404, detail="Training program not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(program, field, value)
    program.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(program)
    await log_audit(db, "training_program_update", user_id=current_user.id, entity_type="training_program", entity_id=program.id)
    return schemas.TrainingProgramResponse.model_validate(program)


@app.delete("/api/training-programs/{program_id}", response_model=schemas.MessageResponse, tags=["Training Programs"])
async def delete_training_program(
    program_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(TrainingProgram).where(TrainingProgram.id == program_id))
    program = result.scalar_one_or_none()
    if not program:
        raise HTTPException(status_code=404, detail="Training program not found")

    candidate_count = (await db.execute(
        select(func.count(CandidateProfile.id)).where(CandidateProfile.training_program_id == program_id)
    )).scalar() or 0
    exam_count = (await db.execute(
        select(func.count(Exam.id)).where(Exam.training_program_id == program_id)
    )).scalar() or 0

    batch_count = (await db.execute(select(func.count(TrainingBatch.id)).where(
        TrainingBatch.training_program_id == program_id))).scalar() or 0
    if candidate_count > 0 or exam_count > 0 or batch_count > 0:
        # Historical exam/candidate records must never silently disappear —
        # deactivate instead of a hard delete so past attempts still resolve.
        program.is_active = False
        program.updated_at = datetime.utcnow()
        await db.commit()
        await log_audit(db, "training_program_deactivate", user_id=current_user.id, entity_type="training_program", entity_id=program_id,
                         details={"reason": "has candidates or exams", "candidate_count": candidate_count, "exam_count": exam_count})
        return schemas.MessageResponse(
            detail=f"This programme has {candidate_count} candidate(s), {exam_count} exam(s) and {batch_count} batch(es) on record, "
                   f"so it has been deactivated rather than deleted to preserve those records."
        )

    await db.execute(sa_delete(QuestionBank).where(QuestionBank.training_program_id == program_id))
    await db.delete(program)
    await db.commit()
    await log_audit(db, "training_program_delete", user_id=current_user.id, entity_type="training_program", entity_id=program_id)
    return schemas.MessageResponse(detail="Training programme deleted.")


# ---------------------------------------------------------------------------
# Question Banks
# ---------------------------------------------------------------------------
@app.get("/api/question-banks", response_model=List[schemas.QuestionBankResponse], tags=["Question Banks"])
async def list_question_banks(
    training_program_id: Optional[int] = None,
    current_user: User = Depends(require_examiner),
    db: AsyncSession = Depends(get_db),
):
    q = select(QuestionBank)
    if training_program_id:
        q = q.where(QuestionBank.training_program_id == training_program_id)
    result = await db.execute(q.order_by(QuestionBank.name))
    banks = result.scalars().all()
    out = []
    for b in banks:
        count = (await db.execute(
            select(func.count(Question.id)).where(Question.question_bank_id == b.id, Question.is_active == True)
        )).scalar()
        r = schemas.QuestionBankResponse.model_validate(b)
        r.question_count = count or 0
        out.append(r)
    return out


@app.post("/api/question-banks", response_model=schemas.QuestionBankResponse, tags=["Question Banks"])
async def create_question_bank(
    data: schemas.QuestionBankCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(TrainingProgram).where(TrainingProgram.id == data.training_program_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Training program not found")
    bank = QuestionBank(**data.model_dump(), created_by=current_user.id)
    db.add(bank)
    await db.commit()
    await db.refresh(bank)
    await log_audit(db, "question_bank_create", user_id=current_user.id, entity_type="question_bank", entity_id=bank.id)
    return schemas.QuestionBankResponse.model_validate(bank)


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------
@app.get("/api/question-banks/{bank_id}/questions", response_model=List[schemas.QuestionResponse], tags=["Questions"])
async def list_questions(
    bank_id: int,
    current_user: User = Depends(require_examiner),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Question).where(Question.question_bank_id == bank_id).order_by(Question.id))
    return [schemas.QuestionResponse.model_validate(q) for q in result.scalars().all()]


@app.post("/api/questions", response_model=schemas.QuestionResponse, tags=["Questions"])
async def create_question(
    data: schemas.QuestionCreate,
    current_user: User = Depends(require_examiner),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(QuestionBank).where(QuestionBank.id == data.question_bank_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Question bank not found")
    payload = data.model_dump()
    payload["question_type"] = ModelQuestionType(payload["question_type"])
    question = Question(**payload)
    db.add(question)
    await db.flush()
    db.add(QuestionRevision(question_id=question.id, version=1, action="created",
                            changed_by=current_user.id, snapshot=data.model_dump(mode="json")))
    await db.commit()
    await db.refresh(question)
    return schemas.QuestionResponse.model_validate(question)


@app.post("/api/question-banks/{bank_id}/questions/bulk", tags=["Questions"])
async def bulk_upload_questions(
    bank_id: int,
    questions: List[schemas.QuestionBase],
    current_user: User = Depends(require_examiner),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(QuestionBank).where(QuestionBank.id == bank_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Question bank not found")

    created = 0
    for q in questions:
        payload = q.model_dump()
        payload["question_type"] = ModelQuestionType(payload["question_type"])
        db.add(Question(**payload, question_bank_id=bank_id))
        created += 1
    await db.commit()
    await log_audit(db, "questions_bulk_upload", user_id=current_user.id, entity_type="question_bank",
                     entity_id=bank_id, details={"count": created})
    return {"created": created}


@app.get("/api/question-banks/csv-template", tags=["Questions"])
async def questions_csv_template(current_user: User = Depends(require_examiner)):
    content = csv_utils.questions_csv_template()
    return StreamingResponse(
        io.StringIO(content),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=question_import_template.csv"},
    )


@app.post("/api/question-banks/{bank_id}/questions/import-csv", response_model=schemas.CSVImportResult, tags=["Questions"])
async def import_questions_csv(
    bank_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(require_examiner),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(QuestionBank).where(QuestionBank.id == bank_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Question bank not found")

    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a .csv file")

    raw = await file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="CSV exceeds 5 MB")
    rows, parse_errors = csv_utils.parse_questions_csv(raw)

    created = 0
    for row in rows:
        try:
            payload = schemas.QuestionBase(**row)
            question = Question(
                question_bank_id=bank_id,
                question_text=payload.question_text,
                question_type=ModelQuestionType(payload.question_type.value),
                options=payload.options,
                correct_answer=payload.correct_answer,
                explanation=payload.explanation,
                marks=payload.marks,
                difficulty=payload.difficulty,
                topic=payload.topic,
                is_active=True,
            )
            db.add(question)
            created += 1
        except Exception as e:
            parse_errors.append(f"'{row.get('question_text', '')[:50]}...': {e}")

    await db.commit()
    await log_audit(db, "questions_csv_import", user_id=current_user.id, entity_type="question_bank",
                     entity_id=bank_id, details={"created": created, "errors": len(parse_errors)})

    return schemas.CSVImportResult(created=created, skipped=len(parse_errors), errors=parse_errors[:50])


@app.patch("/api/questions/{question_id}", response_model=schemas.QuestionResponse, tags=["Questions"])
async def update_question(
    question_id: int,
    data: schemas.QuestionUpdate,
    current_user: User = Depends(require_examiner),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Question).where(Question.id == question_id))
    question = result.scalar_one_or_none()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        if field == "question_type" and value is not None:
            value = ModelQuestionType(value)
        setattr(question, field, value)
    version = (await db.execute(select(func.max(QuestionRevision.version)).where(
        QuestionRevision.question_id == question.id))).scalar() or 0
    snapshot = {"question_bank_id": question.question_bank_id, "question_text": question.question_text,
                "question_type": question.question_type.value, "options": question.options,
                "correct_answer": question.correct_answer, "explanation": question.explanation,
                "marks": question.marks, "difficulty": question.difficulty, "topic": question.topic,
                "is_active": question.is_active}
    db.add(QuestionRevision(question_id=question.id, version=version + 1, action="updated",
                            changed_by=current_user.id, snapshot=snapshot))
    await db.commit()
    await db.refresh(question)
    return schemas.QuestionResponse.model_validate(question)


@app.delete("/api/questions/{question_id}", tags=["Questions"])
async def delete_question(
    question_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Question).where(Question.id == question_id))
    question = result.scalar_one_or_none()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    question.is_active = False
    await db.commit()
    return {"detail": "Question deactivated"}


# ---------------------------------------------------------------------------
# Exams
# ---------------------------------------------------------------------------
@app.get("/api/admin/training-batches", tags=["Admin"])
async def list_training_batches(training_program_id: Optional[int]=None,
                                 current_user: User=Depends(require_examiner), db: AsyncSession=Depends(get_db)):
    query=select(TrainingBatch,TrainingProgram.name).join(TrainingProgram,TrainingProgram.id==TrainingBatch.training_program_id)
    if training_program_id:
        query=query.where(TrainingBatch.training_program_id==training_program_id)
    rows=(await db.execute(query.order_by(TrainingBatch.created_at.desc(),TrainingBatch.id.desc()))).all()
    return [{"id":batch.id,"name":batch.name,"training_program_id":batch.training_program_id,
             "programme":programme} for batch,programme in rows]


@app.post("/api/admin/training-batches", tags=["Admin"])
async def create_training_batch(data: schemas.TrainingBatchCreate,current_user: User=Depends(require_admin),
                                 db: AsyncSession=Depends(get_db)):
    name=data.name.strip()
    if len(name)<2:
        raise HTTPException(status_code=400,detail="Enter a batch name of at least two characters")
    if not (await db.execute(select(TrainingProgram.id).where(TrainingProgram.id==data.training_program_id))).scalar_one_or_none():
        raise HTTPException(status_code=404,detail="Training programme not found")
    if (await db.execute(select(TrainingBatch.id).where(TrainingBatch.training_program_id==data.training_program_id,TrainingBatch.name==name))).scalar_one_or_none():
        raise HTTPException(status_code=409,detail="A batch with this name already exists in this programme")
    batch=TrainingBatch(training_program_id=data.training_program_id,name=name,created_by=current_user.id)
    db.add(batch)
    await db.commit()
    await log_audit(db,"training_batch_create",user_id=current_user.id,entity_type="training_batch",entity_id=batch.id)
    return {"id":batch.id,"name":batch.name,"training_program_id":batch.training_program_id}


@app.get("/api/admin/training-batches/candidates", tags=["Admin"])
async def list_batch_candidates(training_program_id: int,current_user: User=Depends(require_examiner),
                                 db: AsyncSession=Depends(get_db)):
    rows=(await db.execute(select(User,CandidateBatchAssignment.batch_id,TrainingBatch.name).join(
        CandidateProfile,CandidateProfile.user_id==User.id).outerjoin(CandidateBatchAssignment,CandidateBatchAssignment.user_id==User.id
    ).outerjoin(TrainingBatch,TrainingBatch.id==CandidateBatchAssignment.batch_id).where(
        CandidateProfile.training_program_id==training_program_id,User.role==UserRole.CANDIDATE,User.is_active==True
    ).order_by(User.full_name))).all()
    returning=await candidate_history(db)
    return [{"user_id":user.id,"name":user.full_name,"email":user.email,"batch_id":batch_id,"batch_name":batch_name,
             "candidate_type":"returning" if user.id in returning else "new"} for user,batch_id,batch_name in rows]


@app.put("/api/admin/training-batches/assignments", tags=["Admin"])
async def assign_training_batch(data: schemas.CandidateBatchUpdate,current_user: User=Depends(require_admin),
                                 db: AsyncSession=Depends(get_db)):
    batch=(await db.execute(select(TrainingBatch).where(TrainingBatch.id==data.batch_id))).scalar_one_or_none()
    if batch is None:
        raise HTTPException(status_code=404,detail="Training batch not found")
    ids=set(data.user_ids)
    valid=set((await db.execute(select(User.id).join(CandidateProfile,CandidateProfile.user_id==User.id).where(
        User.id.in_(ids),User.role==UserRole.CANDIDATE,User.is_active==True,
        CandidateProfile.training_program_id==batch.training_program_id))).scalars().all())
    if ids!=valid:
        raise HTTPException(status_code=400,detail="All selected candidates must be active and registered in the batch's programme")
    changed=[]
    transitions=[]
    for uid in ids:
        assignment=(await db.execute(select(CandidateBatchAssignment).where(CandidateBatchAssignment.user_id==uid))).scalar_one_or_none()
        if assignment and assignment.batch_id==batch.id:
            continue
        changed.append(uid)
        transitions.append({"user_id":uid,"previous_batch_id":assignment.batch_id if assignment else None,"batch_id":batch.id})
        if assignment:
            assignment.batch_id=batch.id
            assignment.assigned_by=current_user.id
            assignment.assigned_at=datetime.utcnow()
        else:
            db.add(CandidateBatchAssignment(user_id=uid,batch_id=batch.id,assigned_by=current_user.id))
    await db.commit()
    await log_audit(db,"training_batch_assign",user_id=current_user.id,entity_type="training_batch",entity_id=batch.id,
                    details={"candidate_ids":changed,"transitions":transitions})
    # A late assignment should receive only that batch's upcoming schedules.
    if changed:
        exams=(await db.execute(select(Exam).join(ExamBatchAssignment,ExamBatchAssignment.exam_id==Exam.id).where(
            ExamBatchAssignment.batch_id==batch.id,Exam.status.in_([ExamStatus.SCHEDULED,ExamStatus.ACTIVE]),
            Exam.end_time>datetime.utcnow()))).scalars().all()
        for exam in exams:
            await notify_exam_schedule(db,exam,user_ids=changed)
    return {"detail":f"Assigned {len(changed)} candidate(s) to {batch.name}","changed":len(changed)}


@app.post("/api/exams", response_model=schemas.ExamResponse, tags=["Exams"])
async def create_exam(
    data: schemas.ExamCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(TrainingProgram).where(TrainingProgram.id == data.training_program_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Training program not found")

    available = (await db.execute(
        select(func.count(Question.id)).where(
            Question.question_bank_id.in_(
                select(QuestionBank.id).where(
                    QuestionBank.training_program_id == data.training_program_id,
                    QuestionBank.id.in_(data.question_bank_ids) if data.question_bank_ids else True,
                )
            ),
            Question.is_active == True,
        )
    )).scalar()

    if (available or 0) < data.questions_per_exam:
        raise HTTPException(
            status_code=400,
            detail=f"Not enough active questions for this training program. "
                   f"Need {data.questions_per_exam}, have {available or 0}. Add more questions first.",
        )

    batch = (await db.execute(select(TrainingBatch).where(TrainingBatch.id == data.batch_id,
        TrainingBatch.training_program_id == data.training_program_id))).scalar_one_or_none()
    if batch is None:
        raise HTTPException(status_code=400, detail="Select a training batch belonging to this programme")
    payload = data.model_dump(exclude={"question_bank_ids", "batch_id"})
    exam = Exam(**payload, created_by=current_user.id, status=ExamStatus.SCHEDULED)
    db.add(exam)
    await db.flush()
    db.add(ExamBatchAssignment(exam_id=exam.id,batch_id=data.batch_id))
    await db.commit()
    await db.refresh(exam)
    await notify_exam_schedule(db, exam)
    await log_audit(db, "exam_create", user_id=current_user.id, entity_type="exam", entity_id=exam.id)
    resp = schemas.ExamResponse.model_validate(exam)
    return resp


@app.get("/api/exams", response_model=List[schemas.ExamListResponse], tags=["Exams"])
async def list_exams(training_program_id: Optional[int] = None,
                     current_user: User = Depends(require_any), db: AsyncSession = Depends(get_db)):
    q = select(Exam, TrainingProgram).join(TrainingProgram, Exam.training_program_id == TrainingProgram.id)
    candidate_completed_exam_ids = None
    candidate_approved_exam_ids = set()
    candidate_granted_exam_ids = set()
    candidate_started_exam_ids = set()

    if training_program_id:
        q = q.where(Exam.training_program_id == training_program_id)

    if current_user.role == UserRole.CANDIDATE:
        result = await db.execute(select(CandidateProfile).where(CandidateProfile.user_id == current_user.id))
        profile = result.scalar_one_or_none()
        if not profile:
            return []
        q = q.where(
            Exam.training_program_id == profile.training_program_id,
            Exam.status.in_([ExamStatus.SCHEDULED, ExamStatus.ACTIVE, ExamStatus.COMPLETED]),
        )
        # A candidate may join a programme after earlier examinations have
        # finished.  Do not present those historical exams as if the newly
        # registered candidate completed them.  A completed exam remains on
        # the dashboard only when this candidate has actually completed an
        # attempt for it.
        completed_attempt_statuses = (
            AttemptStatus.SUBMITTED,
            AttemptStatus.AUTO_SUBMITTED,
            AttemptStatus.APPROVED,
            AttemptStatus.REJECTED,
        )
        candidate_completed_exam_ids = set((await db.execute(
            select(ExamAttempt.exam_id).where(
                ExamAttempt.user_id == current_user.id,
                ExamAttempt.status.in_(completed_attempt_statuses),
            )
        )).scalars().all())
        candidate_approved_exam_ids = set((await db.execute(
            select(PaymentReceipt.exam_id).where(
                PaymentReceipt.user_id == current_user.id,
                PaymentReceipt.status == "approved",
            )
        )).scalars().all())
        candidate_granted_exam_ids = set((await db.execute(
            select(ExamAccessGrant.exam_id).where(ExamAccessGrant.user_id == current_user.id)
        )).scalars().all())
        candidate_started_exam_ids = set((await db.execute(
            select(ExamAttempt.exam_id).where(ExamAttempt.user_id == current_user.id)
        )).scalars().all())

    result = await db.execute(q.order_by(Exam.start_time.desc()))
    rows = result.all()

    payment_flags = dict((await db.execute(select(ExamPolicy.exam_id, ExamPolicy.payment_required))).all())
    batch_rows = (await db.execute(select(ExamBatchAssignment.exam_id, TrainingBatch.id, TrainingBatch.name).join(
        TrainingBatch, TrainingBatch.id == ExamBatchAssignment.batch_id))).all()
    exam_batches = {eid: (bid, name) for eid, bid, name in batch_rows}
    out = []
    now = datetime.utcnow()
    for exam, program in rows:
        if current_user.role == UserRole.CANDIDATE and current_user.id not in await eligible_candidate_ids(db, exam):
            continue
        reg = (await db.execute(
            select(func.count(ExamAttempt.id)).where(ExamAttempt.exam_id == exam.id)
        )).scalar()
        display_status = exam.status
        if exam.status == ExamStatus.SCHEDULED and exam.start_time <= now <= exam.end_time:
            display_status = ExamStatus.ACTIVE
        elif exam.status in (ExamStatus.SCHEDULED, ExamStatus.ACTIVE) and now > exam.end_time:
            display_status = ExamStatus.COMPLETED
        if (current_user.role == UserRole.CANDIDATE
                and display_status == ExamStatus.COMPLETED
                and exam.id not in candidate_completed_exam_ids):
            continue
        payment_required = bool(payment_flags.get(exam.id, False))
        can_start = True
        access_status = None
        access_message = None
        if current_user.role == UserRole.CANDIDATE:
            if not payment_required:
                access_status = "payment_not_required"
            elif exam.id in candidate_started_exam_ids:
                access_status = "started"
            elif exam.id in candidate_granted_exam_ids:
                access_status = "admin_granted"
            elif exam.id in candidate_approved_exam_ids:
                access_status = "payment_approved"
            else:
                can_start = False
                access_status = "payment_required"
                access_message = "Upload a payment receipt and wait for admin approval before participating in this exam."
        out.append(schemas.ExamListResponse(
            id=exam.id,
            batch_id=exam_batches.get(exam.id, (None, None))[0],
            batch_name=exam_batches.get(exam.id, (None, None))[1],
            training_program_id=exam.training_program_id,
            title=exam.title,
            training_program_name=program.name,
            practice_area=program.practice_area,
            program_description=program.description,
            exam_description=exam.description,
            instructions=exam.instructions,
            duration_minutes=exam.duration_minutes,
            passing_score=exam.passing_score,
            total_marks=exam.total_marks,
            max_attempts=exam.max_attempts,
            start_time=exam.start_time,
            end_time=exam.end_time,
            status=display_status,
            questions_per_exam=exam.questions_per_exam,
            registered_candidates=reg or 0,
            assigned_candidates=len(await batch_candidate_ids(db, exam)),
            can_start=can_start,
            payment_required=payment_required,
            access_status=access_status,
            access_message=access_message,
        ))
    return out


@app.patch("/api/exams/{exam_id}", response_model=schemas.ExamResponse, tags=["Exams"])
async def update_exam(
    exam_id: int,
    data: schemas.ExamUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Exam).where(Exam.id == exam_id))
    exam = result.scalar_one_or_none()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")

    has_attempts = (await db.execute(
        select(func.count(ExamAttempt.id)).where(ExamAttempt.exam_id == exam_id)
    )).scalar() or 0
    locked_fields = {"questions_per_exam", "randomize_questions", "randomize_options"}
    incoming = data.model_dump(exclude_unset=True)
    batch_id = incoming.pop("batch_id", None)
    if batch_id is not None:
        if has_attempts:
            raise HTTPException(status_code=400, detail="Cannot change the batch after an exam attempt exists")
        batch = (await db.execute(select(TrainingBatch).where(TrainingBatch.id==batch_id,
            TrainingBatch.training_program_id==exam.training_program_id))).scalar_one_or_none()
        if batch is None:
            raise HTTPException(status_code=400, detail="Batch does not belong to this exam's programme")
        assignment = (await db.execute(select(ExamBatchAssignment).where(ExamBatchAssignment.exam_id==exam_id))).scalar_one_or_none()
        if assignment:
            assignment.batch_id = batch_id
        else:
            db.add(ExamBatchAssignment(exam_id=exam_id,batch_id=batch_id))
    if has_attempts and (locked_fields & incoming.keys()):
        raise HTTPException(
            status_code=400,
            detail="This exam already has candidate attempts, so its question-selection "
                   "configuration (questions per exam, randomization) can no longer be changed "
                   "— that would make existing attempts inconsistent with new ones. "
                   "You can still edit timing, marks, and instructions.",
        )

    for field, value in incoming.items():
        if field == "status" and value is not None:
            value = ExamStatus(value)
        setattr(exam, field, value)
    await db.commit()
    await db.refresh(exam)
    if batch_id is not None or {"title", "start_time", "end_time", "duration_minutes"} & incoming.keys():
        await notify_exam_schedule(db, exam, is_update=True)
    await log_audit(db, "exam_update", user_id=current_user.id, entity_type="exam", entity_id=exam.id)
    return schemas.ExamResponse.model_validate(exam)


@app.delete("/api/exams/{exam_id}", response_model=schemas.MessageResponse, tags=["Exams"])
async def delete_exam(
    exam_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Exam).where(Exam.id == exam_id))
    exam = result.scalar_one_or_none()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")

    attempt_count = (await db.execute(
        select(func.count(ExamAttempt.id)).where(ExamAttempt.exam_id == exam_id)
    )).scalar() or 0
    receipt_count = (await db.execute(
        select(func.count(PaymentReceipt.id)).where(PaymentReceipt.exam_id == exam_id)
    )).scalar() or 0
    report_count = (await db.execute(
        select(func.count(SavedReport.id)).where(SavedReport.exam_id == exam_id)
    )).scalar() or 0

    if attempt_count or receipt_count or report_count:
        # Never delete an exam candidates have actually sat — archive it so
        # historical results/attempts remain valid and visible.
        exam.status = ExamStatus.ARCHIVED
        await db.commit()
        await log_audit(db, "exam_archive", user_id=current_user.id, entity_type="exam", entity_id=exam_id,
                         details={"reason": "has historical records", "attempt_count": attempt_count,
                                  "receipt_count": receipt_count, "report_count": report_count})
        return schemas.MessageResponse(
            detail=f"This exam has {attempt_count} candidate attempt(s), {receipt_count} payment receipt(s), "
                   f"and {report_count} saved report(s), so it has been archived to preserve those records."
        )

    # These settings have no independent history once an unused exam is removed.
    # Delete children explicitly: existing PostgreSQL foreign keys do not cascade.
    for child in (ExamPolicy, ExamAccessGrant, ExamScheduleOverride, RetakeAuthorization, ExamRenewalApproval, ExamBatchAssignment):
        await db.execute(sa_delete(child).where(child.exam_id == exam_id))
    await db.delete(exam)
    await db.commit()
    await log_audit(db, "exam_delete", user_id=current_user.id, entity_type="exam", entity_id=exam_id)
    return schemas.MessageResponse(detail="Exam deleted.")


@app.get("/api/exams/{exam_id}/analytics", response_model=schemas.ExamAnalytics, tags=["Exams"])
async def exam_analytics(
    exam_id: int,
    current_user: User = Depends(require_examiner),
    db: AsyncSession = Depends(get_db),
):
    stats = await ExamEngine.get_exam_analytics(db, exam_id)
    return schemas.ExamAnalytics(**stats)


# ---------------------------------------------------------------------------
# Candidate exam-taking flow
# ---------------------------------------------------------------------------
async def _get_owned_attempt(attempt_id: int, current_user: User, db: AsyncSession) -> ExamAttempt:
    result = await db.execute(select(ExamAttempt).where(ExamAttempt.id == attempt_id))
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if attempt.user_id != current_user.id and current_user.role == UserRole.CANDIDATE:
        raise HTTPException(status_code=403, detail="This attempt does not belong to you")
    return attempt


@app.post("/api/exams/{exam_id}/start", response_model=schemas.StartExamResponse, tags=["Exam Taking"])
async def start_exam(
    exam_id: int,
    request: Request,
    current_user: User = Depends(require_real_candidate),
    db: AsyncSession = Depends(get_db),
):
    exam = (await db.execute(select(Exam).where(Exam.id == exam_id))).scalar_one_or_none()
    if exam is None:
        raise HTTPException(status_code=404, detail="Exam not found")
    await require_exam_eligibility(db, exam, current_user)
    existing_attempt = (await db.execute(select(ExamAttempt.id).where(
        ExamAttempt.exam_id == exam_id, ExamAttempt.user_id == current_user.id
    ).limit(1))).scalar_one_or_none()
    approved_receipt = (await db.execute(select(PaymentReceipt.id).where(
        PaymentReceipt.exam_id == exam_id, PaymentReceipt.user_id == current_user.id,
        PaymentReceipt.status == "approved"
    ))).scalar_one_or_none()
    admin_grant = (await db.execute(select(ExamAccessGrant.id).where(
        ExamAccessGrant.exam_id == exam_id, ExamAccessGrant.user_id == current_user.id
    ))).scalar_one_or_none()
    payment_required = (await db.execute(select(ExamPolicy.payment_required).where(
        ExamPolicy.exam_id == exam_id
    ))).scalar_one_or_none()
    if payment_required and not (existing_attempt or approved_receipt or admin_grant):
        raise HTTPException(status_code=403, detail=(
            "You cannot participate in this exam until your payment receipt is approved "
            "or an administrator grants you access."
        ))
    attempt = await ExamEngine.start_exam(
        db, exam_id, current_user,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    result = await db.execute(select(Exam).where(Exam.id == exam_id))
    exam = result.scalar_one()

    await log_audit(db, "exam_start", user_id=current_user.id, entity_type="exam_attempt", entity_id=attempt.id,
                     ip_address=get_client_ip(request))

    return schemas.StartExamResponse(
        attempt_id=attempt.id,
        exam_title=exam.title,
        duration_minutes=exam.duration_minutes,
        total_questions=len(attempt.question_set),
        total_marks=exam.total_marks,
        passing_score=exam.passing_score,
        instructions=exam.instructions,
        started_at=attempt.started_at,
        server_time=datetime.utcnow(),
    )


@app.get("/api/attempts/{attempt_id}/questions", response_model=List[schemas.QuestionForExam], tags=["Exam Taking"])
async def get_attempt_questions(
    attempt_id: int,
    current_user: User = Depends(require_real_candidate),
    db: AsyncSession = Depends(get_db),
):
    attempt = await _get_owned_attempt(attempt_id, current_user, db)
    if attempt.status != AttemptStatus.IN_PROGRESS:
        raise HTTPException(status_code=400, detail="This exam attempt is not in progress")
    result = await db.execute(select(Exam).where(Exam.id == attempt.exam_id))
    exam = result.scalar_one()
    questions = await ExamEngine.get_exam_questions_for_candidate(db, attempt, exam)
    return questions


@app.get("/api/attempts/{attempt_id}/answers", tags=["Exam Taking"])
async def get_attempt_answers(
    attempt_id: int,
    current_user: User = Depends(require_real_candidate),
    db: AsyncSession = Depends(get_db),
):
    """Previously-saved answers for an in-progress attempt, so the candidate
    portal can resume a session (e.g. after a page refresh or connection drop)
    without losing their progress."""
    attempt = await _get_owned_attempt(attempt_id, current_user, db)
    result = await db.execute(select(Answer).where(Answer.attempt_id == attempt.id))
    answers = result.scalars().all()
    return {str(a.question_id): a.selected_answer for a in answers
            if any(str(value).strip() for value in (a.selected_answer or []))}


@app.get("/api/attempts/{attempt_id}/status", tags=["Exam Taking"])
async def attempt_status(
    attempt_id: int,
    current_user: User = Depends(require_real_candidate),
    db: AsyncSession = Depends(get_db),
):
    attempt = await _get_owned_attempt(attempt_id, current_user, db)
    result = await db.execute(select(Exam).where(Exam.id == attempt.exam_id))
    exam = result.scalar_one()

    elapsed = 0
    remaining = exam.duration_minutes * 60
    if attempt.started_at:
        elapsed = int((datetime.utcnow() - attempt.started_at).total_seconds())
        remaining = max(0, exam.duration_minutes * 60 - elapsed)

    # The server is authoritative for expiry. This also finalizes attempts
    # when a browser tab was suspended, closed, offline, or its timer stopped.
    if remaining == 0 and attempt.status == AttemptStatus.IN_PROGRESS:
        attempt = await ExamEngine.finalize_exam(
            db, attempt, min(elapsed, exam.duration_minutes * 60), auto_submit=True
        )
        await log_audit(db, "exam_auto_submitted", user_id=current_user.id,
                        entity_type="exam_attempt", entity_id=attempt.id)

    result = await db.execute(select(Answer.selected_answer).where(Answer.attempt_id == attempt.id))
    answered = sum(1 for selected in result.scalars().all()
                   if any(str(value).strip() for value in (selected or [])))

    return {
        "status": attempt.status.value,
        "elapsed_seconds": elapsed,
        "remaining_seconds": remaining,
        "answered_count": answered,
        "total_questions": len(attempt.question_set or []),
    }


@app.post("/api/attempts/{attempt_id}/answer", response_model=schemas.AnswerResponse, tags=["Exam Taking"])
async def submit_answer(
    attempt_id: int,
    data: schemas.AnswerSubmit,
    current_user: User = Depends(require_real_candidate),
    db: AsyncSession = Depends(get_db),
):
    attempt = await _get_owned_attempt(attempt_id, current_user, db)
    answer = await ExamEngine.submit_answer(db, attempt, data)
    # Never leak correctness to the candidate mid-exam.
    return schemas.AnswerResponse(
        question_id=data.question_id,
        selected_answer=answer.selected_answer if answer else [],
        is_correct=None,
        marks_obtained=None,
    )


@app.post("/api/attempts/{attempt_id}/activity", tags=["Exam Taking"])
async def record_activity(
    attempt_id: int,
    activity_type: str,
    current_user: User = Depends(require_real_candidate),
    db: AsyncSession = Depends(get_db),
):
    attempt = await _get_owned_attempt(attempt_id, current_user, db)
    await ExamEngine.record_suspicious_activity(db, attempt, activity_type, {})
    return {"detail": "recorded"}


@app.post("/api/attempts/{attempt_id}/submit", tags=["Exam Taking"])
async def submit_exam(
    attempt_id: int,
    data: schemas.ExamSubmit,
    current_user: User = Depends(require_real_candidate),
    db: AsyncSession = Depends(get_db),
):
    attempt = await _get_owned_attempt(attempt_id, current_user, db)
    for a in data.answers:
        await ExamEngine.submit_answer(db, attempt, a)
    attempt = await ExamEngine.finalize_exam(db, attempt, data.time_spent_seconds, auto_submit=False)
    await log_audit(db, "exam_submit", user_id=current_user.id, entity_type="exam_attempt", entity_id=attempt.id)
    return {
        "detail": "Exam submitted successfully. Your result will be released after review and approval by NIRPR.",
        "attempt_id": attempt.id,
        "status": attempt.status.value,
    }


@app.post("/api/attempts/{attempt_id}/auto-submit", tags=["Exam Taking"])
async def auto_submit_exam(
    attempt_id: int,
    data: schemas.ExamSubmit,
    current_user: User = Depends(require_real_candidate),
    db: AsyncSession = Depends(get_db),
):
    attempt = await _get_owned_attempt(attempt_id, current_user, db)
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return {"detail": "Exam was already submitted.", "attempt_id": attempt.id,
                "status": attempt.status.value}
    for a in data.answers:
        await ExamEngine.submit_answer(db, attempt, a)
    exam = (await db.execute(select(Exam).where(Exam.id == attempt.exam_id))).scalar_one()
    elapsed = int((datetime.utcnow() - attempt.started_at).total_seconds()) if attempt.started_at else data.time_spent_seconds
    attempt = await ExamEngine.finalize_exam(
        db, attempt, min(elapsed, exam.duration_minutes * 60), auto_submit=True
    )
    await log_audit(db, "exam_auto_submitted", user_id=current_user.id,
                    entity_type="exam_attempt", entity_id=attempt.id)
    return {"detail": "Time expired. Exam auto-submitted.", "attempt_id": attempt.id, "status": attempt.status.value}


@app.get("/api/candidate/active-attempt", tags=["Exam Taking"])
async def get_active_attempt(current_user: User = Depends(require_candidate), db: AsyncSession = Depends(get_db)):
    """Lets the candidate dashboard offer to resume an in-progress exam after
    a page refresh, browser crash, or lost connection — the timer keeps
    running server-side regardless, so resuming just reconnects the UI."""
    result = await db.execute(
        select(ExamAttempt, Exam)
        .join(Exam, ExamAttempt.exam_id == Exam.id)
        .where(ExamAttempt.user_id == current_user.id, ExamAttempt.status == AttemptStatus.IN_PROGRESS)
        .order_by(ExamAttempt.started_at.desc())
    )
    row = result.first()
    if not row:
        return None
    attempt, exam = row
    elapsed = int((datetime.utcnow() - attempt.started_at).total_seconds()) if attempt.started_at else 0
    remaining = max(0, exam.duration_minutes * 60 - elapsed)

    if remaining == 0 and attempt.status == AttemptStatus.IN_PROGRESS:
        attempt = await ExamEngine.finalize_exam(
            db, attempt, min(elapsed, exam.duration_minutes * 60), auto_submit=True
        )
        await log_audit(db, "exam_auto_submitted", user_id=current_user.id,
                        entity_type="exam_attempt", entity_id=attempt.id)
        return None

    total_questions = len(attempt.question_set or [])
    saved = (await db.execute(
        select(Answer.selected_answer).where(Answer.attempt_id == attempt.id)
    )).scalars().all()
    answered_questions = sum(1 for selected in saved
                             if any(str(value).strip() for value in (selected or [])))

    return {
        "attempt_id": attempt.id,
        "exam_id": exam.id,
        "exam_title": exam.title,
        "remaining_seconds": remaining,
        "total_questions": total_questions,
        "answered_questions": answered_questions,
    }


# ---------------------------------------------------------------------------
# Candidate results
# ---------------------------------------------------------------------------
@app.get("/api/candidate/results", response_model=List[schemas.CandidateResultView], tags=["Results"])
async def my_results(current_user: User = Depends(require_candidate), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ExamAttempt, Exam, TrainingProgram)
        .join(Exam, ExamAttempt.exam_id == Exam.id)
        .join(TrainingProgram, Exam.training_program_id == TrainingProgram.id)
        .where(ExamAttempt.user_id == current_user.id)
        .order_by(ExamAttempt.created_at.desc())
    )
    out = []
    for attempt, exam, program in result.all():
        out.append(schemas.CandidateResultView(
            attempt_id=attempt.id,
            exam_title=exam.title,
            training_program=program.name,
            exam_date=attempt.submitted_at or attempt.created_at,
            attempt_number=attempt.attempt_number,
            score=attempt.total_score if attempt.result_released else 0.0,
            total_possible_marks=attempt.total_possible_marks if attempt.result_released else 0.0,
            percentage=attempt.percentage if attempt.result_released else 0.0,
            passed=bool(attempt.passed) if attempt.result_released else False,
            status=attempt.status.value,
            certificate_eligible=bool(attempt.result_released and attempt.passed),
            certificate_number=certificate_number(
                program.code, attempt.approved_at or attempt.submitted_at or attempt.created_at,
                attempt.id, current_user.id,
            )
                if attempt.result_released and attempt.passed else None,
            certificate_download_url=f"/api/candidate/certificates/{attempt.id}/download"
                if attempt.result_released and attempt.passed else None,
        ))
    return out


@app.get("/api/candidate/certificates/{attempt_id}/download", tags=["Results"])
async def download_certificate(
    attempt_id: int,
    current_user: User = Depends(require_real_candidate),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ExamAttempt, Exam, TrainingProgram, CandidateProfile)
        .join(Exam, ExamAttempt.exam_id == Exam.id)
        .join(TrainingProgram, Exam.training_program_id == TrainingProgram.id)
        .join(CandidateProfile, CandidateProfile.user_id == ExamAttempt.user_id)
        .where(ExamAttempt.id == attempt_id, ExamAttempt.user_id == current_user.id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Certificate record not found")
    attempt, exam, program, profile = row
    if not (attempt.status == AttemptStatus.APPROVED and attempt.result_released and attempt.passed):
        raise HTTPException(status_code=403, detail="A certificate is available only after a passing result is approved and released")

    issued_on = attempt.approved_at or attempt.submitted_at or attempt.created_at
    number = certificate_number(program.code, issued_on, attempt.id, current_user.id)
    signatures = (await db.execute(select(ProgrammeSignature).where(
        ProgrammeSignature.training_program_id == program.id).order_by(ProgrammeSignature.role_key))).scalars().all()
    if not signatures:  # Backward-compatible fallback for existing global signatures.
        signatures = (await db.execute(select(StaffSignature).order_by(StaffSignature.role_key))).scalars().all()
    verify_url = f"{os.getenv('APP_BASE_URL', 'http://localhost:8000')}/verify-certificate?number={number}&attempt_id={attempt.id}"
    pdf = build_certificate_pdf(
        candidate_name=current_user.full_name,
        programme_name=program.name,
        practice_area=program.practice_area,
        exam_title=exam.title,
        score=attempt.percentage,
        issued_on=issued_on,
        certificate_no=number,
        institution=profile.institution,
        course_start=exam.start_time,
        course_end=exam.end_time,
        verification_url=verify_url,
        signatures=[{"role_key": s.role_key, "image_path": s.image_path,
                     "name": s.signatory_name, "title": s.title} for s in signatures],
    )
    await log_audit(db, "certificate_download", user_id=current_user.id,
                    entity_type="exam_attempt", entity_id=attempt.id,
                    details={"certificate_number": number})
    filename = f"NIRPR-Certificate-{attempt.id}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Admin: approval queue, users, dashboard, audit log
# ---------------------------------------------------------------------------
@app.get("/api/admin/attempts", tags=["Admin"])
async def list_attempts(
    status_filter: Optional[str] = None,
    exam_id: Optional[int] = None,
    training_program_id: Optional[int] = None,
    current_user: User = Depends(require_examiner),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(ExamAttempt, User, Exam)
        .join(User, ExamAttempt.user_id == User.id)
        .join(Exam, ExamAttempt.exam_id == Exam.id)
    )
    if status_filter:
        q = q.where(ExamAttempt.status == AttemptStatus(status_filter))
    if exam_id:
        q = q.where(ExamAttempt.exam_id == exam_id)
    if training_program_id:
        q = q.where(Exam.training_program_id == training_program_id)

    result = await db.execute(q.order_by(ExamAttempt.submitted_at.desc().nullslast()))
    out = []
    for attempt, user, exam in result.all():
        answered = (await db.execute(
            select(func.count(Answer.id)).where(Answer.attempt_id == attempt.id, Answer.is_correct == True)
        )).scalar()
        risk = ExamEngine.compute_risk_score(attempt)
        out.append({
            "attempt_id": attempt.id,
            "candidate_id": user.id,
            "candidate_name": user.full_name,
            "candidate_email": user.email,
            "exam_id": exam.id,
            "exam_title": exam.title,
            "attempt_number": attempt.attempt_number,
            "status": attempt.status.value,
            "total_score": attempt.total_score,
            "total_possible_marks": attempt.total_possible_marks,
            "percentage": attempt.percentage,
            "passed": attempt.passed,
            "correct_answers": answered or 0,
            "total_questions": len(attempt.question_set or []),
            "tab_switch_count": attempt.tab_switch_count,
            "fullscreen_exit_count": attempt.fullscreen_exit_count,
            "risk_score": risk["risk_score"],
            "risk_level": risk["risk_level"],
            "submitted_at": attempt.submitted_at.isoformat() if attempt.submitted_at else None,
            "result_released": attempt.result_released,
        })
    return out


@app.post("/api/admin/attempts/approve", tags=["Admin"])
async def approve_attempt(
    data: schemas.ResultApproval,
    current_user: User = Depends(require_examiner),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ExamAttempt).where(ExamAttempt.id == data.attempt_id))
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")
    approved = data.action == "approve"
    attempt = await ExamEngine.approve_result(db, attempt, current_user, approved, data.notes)
    await log_audit(db, f"result_{data.action}", user_id=current_user.id, entity_type="exam_attempt",
                     entity_id=attempt.id, details={"notes": data.notes})

    if attempt.result_released:
        u_result = await db.execute(select(User).where(User.id == attempt.user_id))
        e_result = await db.execute(select(Exam).where(Exam.id == attempt.exam_id))
        candidate, exam = u_result.scalar_one_or_none(), e_result.scalar_one_or_none()
        if candidate and exam:
            email_utils.send_result_notification_email(candidate.email, candidate.full_name, exam.title, bool(attempt.passed))

    return {"detail": f"Result {data.action}d", "attempt_id": attempt.id, "status": attempt.status.value}


@app.post("/api/admin/attempts/{attempt_id}/grant-retake", response_model=schemas.MessageResponse, tags=["Admin"])
async def grant_retake(
    attempt_id: int,
    data: schemas.RetakeGrantRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Let a specific candidate sit a specific exam again, on top of the
    exam's normal max_attempts. One-time use, fully audited."""
    result = await db.execute(select(ExamAttempt).where(ExamAttempt.id == attempt_id))
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if attempt.status in (AttemptStatus.NOT_STARTED, AttemptStatus.IN_PROGRESS):
        raise HTTPException(status_code=400, detail="This attempt is still in progress — a retake can only be granted after it's finished.")

    existing = await db.execute(
        select(RetakeAuthorization).where(
            RetakeAuthorization.exam_id == attempt.exam_id,
            RetakeAuthorization.user_id == attempt.user_id,
            RetakeAuthorization.used == False,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="This candidate already has an unused retake authorization for this exam.")

    grant = RetakeAuthorization(
        exam_id=attempt.exam_id,
        user_id=attempt.user_id,
        authorized_by=current_user.id,
        reason=data.reason,
    )
    db.add(grant)
    await db.commit()
    await log_audit(db, "retake_granted", user_id=current_user.id, entity_type="exam_attempt", entity_id=attempt_id,
                     details={"exam_id": attempt.exam_id, "candidate_id": attempt.user_id, "reason": data.reason})
    return schemas.MessageResponse(detail="Retake granted. The candidate can now start this exam again from their dashboard.")


async def exam_roster_batch(db, exam_id):
    exam = (await db.execute(select(Exam).where(Exam.id==exam_id))).scalar_one_or_none()
    if exam is None:
        raise HTTPException(status_code=404,detail="Exam not found")
    batch_id = (await db.execute(select(ExamBatchAssignment.batch_id).where(ExamBatchAssignment.exam_id==exam_id))).scalar_one_or_none()
    if batch_id is None:
        raise HTTPException(status_code=400,detail="Edit this exam and select a training batch before importing candidates")
    if exam.status not in (ExamStatus.DRAFT,ExamStatus.SCHEDULED,ExamStatus.ACTIVE) or exam.end_time < datetime.utcnow():
        raise HTTPException(status_code=400,detail="Candidates can only be imported for an upcoming or active exam")
    return exam,batch_id


@app.get("/api/admin/exams/{exam_id}/candidates/import-template", tags=["Admin"])
async def exam_roster_template(exam_id: int,current_user: User=Depends(require_admin),db: AsyncSession=Depends(get_db)):
    await exam_roster_batch(db,exam_id)
    return StreamingResponse(io.BytesIO(b"email\ncandidate@example.com\n"),media_type="text/csv",
        headers={"Content-Disposition":"attachment; filename=exam-candidates-template.csv"})


@app.post("/api/admin/exams/{exam_id}/candidates/import-preview", tags=["Admin"])
async def preview_exam_roster(exam_id: int,file: UploadFile=File(...),current_user: User=Depends(require_admin),
                              db: AsyncSession=Depends(get_db)):
    exam,batch_id=await exam_roster_batch(db,exam_id)
    return await preview_candidate_file(file,db,exam.training_program_id,batch_id,exam_id)


async def preview_candidate_file(file,db,training_program_id,batch_id,exam_id=None):
    content=await file.read(5*1024*1024+1)
    if len(content)>5*1024*1024:
        raise HTTPException(status_code=413,detail="Candidate file must be 5 MB or smaller")
    from roster_import import parse_roster_file
    rows=parse_roster_file(content,file.filename)
    emails={row["email"] for row in rows if row["email"]}
    matched=(await db.execute(select(User,CandidateProfile.training_program_id).outerjoin(
        CandidateProfile,CandidateProfile.user_id==User.id).where(func.lower(User.email).in_(emails)))).all()
    accounts={user.email.lower():(user,programme_id) for user,programme_id in matched}
    returning=await candidate_history(db)
    approvals=set((await db.execute(select(ExamRenewalApproval.user_id).where(ExamRenewalApproval.exam_id==exam_id))).scalars().all()) if exam_id is not None else set()
    assignments=dict((await db.execute(select(CandidateBatchAssignment.user_id,CandidateBatchAssignment.batch_id))).all())
    seen=set()
    for row in rows:
        email=row["email"]
        match=accounts.get(email)
        row.update(selectable=False,user_id=None,name="",renewal_required=False)
        if not email or "@" not in email:
            row["status"]="Missing or invalid email"
        elif email in seen:
            row["status"]="Duplicate email in file"
        elif match is None:
            row["status"]="No account found — correct the email or register the candidate first"
        else:
            user,programme_id=match
            row.update(user_id=user.id,name=user.full_name)
            if user.role!=UserRole.CANDIDATE:
                row["status"]="Account is not a candidate"
            elif not user.is_active:
                row["status"]="Candidate account is inactive"
            elif programme_id!=training_program_id:
                row["status"]="Candidate is registered in a different training programme"
            else:
                row["selectable"]=True
                row["renewal_required"]=user.id in returning and user.id not in approvals
                old_batch=assignments.get(user.id)
                row["status"]=("Already in the selected batch" if old_batch==batch_id else
                               "Will be reassigned from another batch" if old_batch else "Ready to assign")
        seen.add(email)
    return {"batch_id":batch_id,"rows":rows,"matched":sum(row["selectable"] for row in rows)}


@app.post("/api/admin/exams/{exam_id}/candidates/import", tags=["Admin"])
async def apply_exam_roster(exam_id: int,data: schemas.ExamRosterApply,current_user: User=Depends(require_admin),
                            db: AsyncSession=Depends(get_db)):
    exam,batch_id=await exam_roster_batch(db,exam_id)
    if batch_id!=data.batch_id:
        raise HTTPException(status_code=409,detail="This exam's batch changed. Preview the file again before importing")
    result=await assign_training_batch(schemas.CandidateBatchUpdate(batch_id=batch_id,user_ids=data.user_ids),current_user,db)
    result["detail"]=f"Imported {len(set(data.user_ids))} selected candidate(s) into this exam's batch. Returning candidates still require renewal approval."
    return result


@app.get("/api/admin/training-batches/{batch_id}/import-template", tags=["Admin"])
async def batch_roster_template(batch_id: int,current_user: User=Depends(require_admin),db: AsyncSession=Depends(get_db)):
    if not (await db.execute(select(TrainingBatch.id).where(TrainingBatch.id==batch_id))).scalar_one_or_none():
        raise HTTPException(status_code=404,detail="Training batch not found")
    return StreamingResponse(io.BytesIO(b"email\ncandidate@example.com\n"),media_type="text/csv",
        headers={"Content-Disposition":"attachment; filename=batch-candidates-template.csv"})


@app.post("/api/admin/training-batches/{batch_id}/import-preview", tags=["Admin"])
async def preview_batch_roster(batch_id: int,file: UploadFile=File(...),current_user: User=Depends(require_admin),
                              db: AsyncSession=Depends(get_db)):
    batch=(await db.execute(select(TrainingBatch).where(TrainingBatch.id==batch_id))).scalar_one_or_none()
    if batch is None:
        raise HTTPException(status_code=404,detail="Training batch not found")
    return await preview_candidate_file(file,db,batch.training_program_id,batch_id)


@app.post("/api/admin/training-batches/{batch_id}/import", tags=["Admin"])
async def apply_batch_roster(batch_id: int,data: schemas.ExamRosterApply,current_user: User=Depends(require_admin),
                            db: AsyncSession=Depends(get_db)):
    if data.batch_id!=batch_id:
        raise HTTPException(status_code=409,detail="Batch selection changed. Preview the file again")
    return await assign_training_batch(schemas.CandidateBatchUpdate(batch_id=batch_id,user_ids=data.user_ids),current_user,db)


@app.get("/api/admin/exams/{exam_id}/candidates", tags=["Admin"])
async def list_exam_candidates(
    exam_id: int,
    current_user: User = Depends(require_examiner),
    db: AsyncSession = Depends(get_db),
):
    """Candidates eligible for this exam (registered under its training
    programme), with their attempt status and any active schedule
    override — used by the reschedule and retake admin UI."""
    result = await db.execute(select(Exam).where(Exam.id == exam_id))
    exam = result.scalar_one_or_none()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")

    result = await db.execute(
        select(User, CandidateProfile)
        .join(CandidateProfile, CandidateProfile.user_id == User.id)
        .where(CandidateProfile.training_program_id == exam.training_program_id, User.role == UserRole.CANDIDATE)
        .order_by(User.full_name)
    )
    rows = result.all()

    returning = await candidate_history(db, exam.training_program_id, exclude_exam_id=exam.id)
    eligible = await eligible_candidate_ids(db, exam)
    batch_members = await batch_candidate_ids(db, exam)
    existing_candidate_ids = set((await db.execute(select(ExamAttempt.user_id).where(ExamAttempt.exam_id==exam_id))).scalars().all())
    rows = [(user,profile) for user,profile in rows if user.id in batch_members or user.id in existing_candidate_ids]
    invitations = set((await db.execute(select(ExamRenewalApproval.user_id).where(
        ExamRenewalApproval.exam_id == exam.id))).scalars().all())
    out = []
    for user, profile in rows:
        att_result = await db.execute(
            select(ExamAttempt).where(ExamAttempt.exam_id == exam_id, ExamAttempt.user_id == user.id)
            .order_by(ExamAttempt.attempt_number.desc())
        )
        latest_attempt = att_result.scalars().first()
        ov_result = await db.execute(
            select(ExamScheduleOverride).where(ExamScheduleOverride.exam_id == exam_id, ExamScheduleOverride.user_id == user.id)
        )
        override = ov_result.scalar_one_or_none()
        out.append({
            "user_id": user.id,
            "full_name": user.full_name,
            "email": user.email,
            "institution": profile.institution,
            "candidate_type": "returning" if user.id in returning else "new",
            "renewal_approved": user.id in invitations,
            "in_exam_batch": user.id in batch_members,
            "eligible": user.id in eligible,
            "latest_status": latest_attempt.status.value if latest_attempt else "not_started",
            "attempts_used": latest_attempt.attempt_number if latest_attempt else 0,
            "max_attempts": exam.max_attempts,
            "has_schedule_override": bool(override),
            "override_start": override.start_time.isoformat() if override else None,
            "override_end": override.end_time.isoformat() if override else None,
        })
    return out


@app.put("/api/admin/exams/{exam_id}/renewal-approvals/{user_id}", tags=["Admin"])
async def set_renewal_approval(exam_id: int, user_id: int, data: schemas.ExamRenewalApprovalUpdate,
                               current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    exam = (await db.execute(select(Exam).where(Exam.id == exam_id))).scalar_one_or_none()
    candidate = (await db.execute(select(User).join(CandidateProfile, CandidateProfile.user_id == User.id).where(
        User.id == user_id, User.role == UserRole.CANDIDATE, User.is_active == True,
        CandidateProfile.training_program_id == exam.training_program_id if exam else False
    ))).scalar_one_or_none()
    if exam is None or candidate is None:
        raise HTTPException(status_code=404, detail="Exam or candidate in this programme not found")
    if data.approved and user_id not in await batch_candidate_ids(db, exam):
        raise HTTPException(status_code=400, detail="Assign this candidate to the exam's training batch before approving renewal")
    if exam.status not in (ExamStatus.SCHEDULED, ExamStatus.ACTIVE) or exam.end_time < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Choose an upcoming or active exam for a renewal approval")
    invitation = (await db.execute(select(ExamRenewalApproval).where(
        ExamRenewalApproval.exam_id == exam_id, ExamRenewalApproval.user_id == user_id))).scalar_one_or_none()
    changed = False
    if data.approved and invitation is None:
        db.add(ExamRenewalApproval(exam_id=exam_id,user_id=user_id,invited_by=current_user.id,reason=data.reason))
        changed = True
    elif not data.approved and invitation is not None:
        if (await db.execute(select(ExamAttempt.id).where(ExamAttempt.exam_id==exam_id,ExamAttempt.user_id==user_id).limit(1))).scalar_one_or_none():
            raise HTTPException(status_code=409, detail="An attempt already exists; renewal approval cannot be revoked")
        await db.delete(invitation)
        changed = True
    await db.commit()
    await log_audit(db,"exam_renewal_approved" if data.approved else "exam_renewal_revoked",
                    user_id=current_user.id,entity_type="exam",entity_id=exam_id,
                    details={"candidate_id":user_id,"reason":data.reason})
    if changed:
        message = (f"You have been approved for renewal in {exam.title}. Payment approval is still required if enabled."
                   if data.approved else f"Your renewal approval for {exam.title} was removed.")
        db.add(Notification(user_id=user_id,title="Renewal approval updated",message=message,notification_type="exam_reminder"))
        await db.commit()
        if data.approved:
            email_utils.send_exam_schedule_email(candidate.email,candidate.full_name,exam.title,
                                                 exam.start_time,exam.end_time,exam.duration_minutes)
    return {"detail":"Renewal approval saved", "approved":data.approved}


@app.post("/api/admin/exams/{exam_id}/reschedule", response_model=schemas.MessageResponse, tags=["Admin"])
async def reschedule_exam_for_candidates(
    exam_id: int,
    data: schemas.RescheduleRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Give one or more specific candidates a different exam window than
    everyone else — without touching the exam's normal schedule."""
    result = await db.execute(select(Exam).where(Exam.id == exam_id))
    exam = result.scalar_one_or_none()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    if data.end_time <= data.start_time:
        raise HTTPException(status_code=400, detail="End time must be after start time.")
    if not data.user_ids:
        raise HTTPException(status_code=400, detail="Select at least one candidate.")

    for uid in data.user_ids:
        result = await db.execute(
            select(ExamScheduleOverride).where(ExamScheduleOverride.exam_id == exam_id, ExamScheduleOverride.user_id == uid)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.start_time = data.start_time
            existing.end_time = data.end_time
            existing.reason = data.reason
            existing.created_by = current_user.id
            existing.created_at = datetime.utcnow()
        else:
            db.add(ExamScheduleOverride(
                exam_id=exam_id, user_id=uid, start_time=data.start_time, end_time=data.end_time,
                reason=data.reason, created_by=current_user.id,
            ))
    await db.commit()
    await log_audit(db, "exam_reschedule", user_id=current_user.id, entity_type="exam", entity_id=exam_id,
                     details={"candidate_ids": data.user_ids, "start_time": data.start_time.isoformat(), "end_time": data.end_time.isoformat()})
    return schemas.MessageResponse(detail=f"Rescheduled for {len(data.user_ids)} candidate(s).")


@app.delete("/api/admin/exams/{exam_id}/reschedule/{user_id}", response_model=schemas.MessageResponse, tags=["Admin"])
async def clear_exam_reschedule(
    exam_id: int,
    user_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ExamScheduleOverride).where(ExamScheduleOverride.exam_id == exam_id, ExamScheduleOverride.user_id == user_id)
    )
    override = result.scalar_one_or_none()
    if not override:
        raise HTTPException(status_code=404, detail="No schedule override found for this candidate.")
    await db.delete(override)
    await db.commit()
    await log_audit(db, "exam_reschedule_cleared", user_id=current_user.id, entity_type="exam", entity_id=exam_id,
                     details={"candidate_id": user_id})
    return schemas.MessageResponse(detail="Candidate reverted to the exam's normal schedule.")


@app.get("/api/admin/attempts/{attempt_id}/script", response_model=schemas.AttemptScriptResponse, tags=["Admin"])
async def get_attempt_script(
    attempt_id: int,
    current_user: User = Depends(require_examiner),
    db: AsyncSession = Depends(get_db),
):
    """The full answer script for a submitted attempt: every question the
    candidate saw, what they selected, and the correct answer — for
    examiner/admin review. Never exposed to the candidate."""
    result = await db.execute(select(ExamAttempt).where(ExamAttempt.id == attempt_id))
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if attempt.status in (AttemptStatus.NOT_STARTED, AttemptStatus.IN_PROGRESS):
        raise HTTPException(status_code=400, detail="This candidate has not submitted this exam yet")

    u_result = await db.execute(select(User).where(User.id == attempt.user_id))
    candidate = u_result.scalar_one()
    e_result = await db.execute(select(Exam).where(Exam.id == attempt.exam_id))
    exam = e_result.scalar_one()

    script = await ExamEngine.get_attempt_script(db, attempt)
    await log_audit(db, "attempt_script_view", user_id=current_user.id, entity_type="exam_attempt", entity_id=attempt.id)

    return schemas.AttemptScriptResponse(
        attempt_id=attempt.id,
        candidate_name=candidate.full_name,
        candidate_email=candidate.email,
        exam_title=exam.title,
        status=attempt.status.value,
        total_score=attempt.total_score or 0.0,
        total_possible_marks=attempt.total_possible_marks or 0.0,
        percentage=attempt.percentage or 0.0,
        passed=attempt.passed,
        started_at=attempt.started_at,
        submitted_at=attempt.submitted_at,
        time_spent_seconds=attempt.time_spent_seconds or 0,
        answers=[schemas.AttemptAnswerDetail(**a) for a in script],
    )


@app.get("/api/admin/attempts/{attempt_id}/timeline", response_model=schemas.AttemptTimelineResponse, tags=["Admin"])
async def get_attempt_timeline(
    attempt_id: int,
    current_user: User = Depends(require_examiner),
    db: AsyncSession = Depends(get_db),
):
    """Chronological activity/incident log for one exam attempt — tab
    switches, fullscreen exits, start/submit — with a risk score, so an
    examiner can monitor for exam malpractice. A flagged attempt is never
    auto-failed; a human always reviews it."""
    result = await db.execute(select(ExamAttempt).where(ExamAttempt.id == attempt_id))
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")

    u_result = await db.execute(select(User).where(User.id == attempt.user_id))
    candidate = u_result.scalar_one()
    e_result = await db.execute(select(Exam).where(Exam.id == attempt.exam_id))
    exam = e_result.scalar_one()

    events = await ExamEngine.get_attempt_timeline(db, attempt)
    risk = ExamEngine.compute_risk_score(attempt)

    return schemas.AttemptTimelineResponse(
        attempt_id=attempt.id,
        candidate_name=candidate.full_name,
        exam_title=exam.title,
        started_at=attempt.started_at,
        submitted_at=attempt.submitted_at,
        ip_address=attempt.ip_address,
        user_agent=attempt.user_agent,
        tab_switch_count=attempt.tab_switch_count or 0,
        fullscreen_exit_count=attempt.fullscreen_exit_count or 0,
        risk_score=risk["risk_score"],
        risk_level=risk["risk_level"],
        events=[schemas.ActivityTimelineEntry(**e) for e in events if e.get("timestamp")],
    )


@app.get("/api/admin/monitoring/live", tags=["Admin"])
async def live_monitoring(
    training_program_id: Optional[int] = None,
    current_user: User = Depends(require_examiner),
    db: AsyncSession = Depends(get_db),
):
    """All currently in-progress attempts with their activity signals, for a
    live 'candidates currently sitting exams' monitoring view."""
    result = await db.execute(
        select(ExamAttempt, User, Exam)
        .join(User, ExamAttempt.user_id == User.id)
        .join(Exam, ExamAttempt.exam_id == Exam.id)
        .where(ExamAttempt.status == AttemptStatus.IN_PROGRESS,
               *([Exam.training_program_id == training_program_id] if training_program_id else []))
        .order_by(ExamAttempt.started_at.desc())
    )
    out = []
    for attempt, user, exam in result.all():
        live_answers = (await db.execute(
            select(Answer.selected_answer).where(Answer.attempt_id == attempt.id)
        )).scalars().all()
        answered = sum(1 for selected in live_answers
                       if any(str(value).strip() for value in (selected or [])))
        risk = ExamEngine.compute_risk_score(attempt)
        elapsed = int((datetime.utcnow() - attempt.started_at).total_seconds()) if attempt.started_at else 0
        remaining = max(0, exam.duration_minutes * 60 - elapsed)
        out.append({
            "attempt_id": attempt.id,
            "candidate_name": user.full_name,
            "candidate_email": user.email,
            "exam_title": exam.title,
            "training_program_id": exam.training_program_id,
            "answered_questions": answered,
            "total_questions": len(attempt.question_set or []),
            "remaining_seconds": remaining,
            "tab_switch_count": attempt.tab_switch_count or 0,
            "fullscreen_exit_count": attempt.fullscreen_exit_count or 0,
            "risk_score": risk["risk_score"],
            "risk_level": risk["risk_level"],
            "ip_address": attempt.ip_address,
        })
    return out


@app.get("/api/admin/reports/performance", response_model=schemas.PerformanceReport, tags=["Admin"])
async def performance_report(
    training_program_id: Optional[int] = None,
    exam_id: Optional[int] = None,
    schedule_key: Optional[str] = None,
    current_user: User = Depends(require_examiner),
    db: AsyncSession = Depends(get_db),
):
    """Statistical analysis of candidate performance: per-candidate summary,
    topic-level accuracy, and overall score distribution — optionally
    filtered to one training programme or one exam."""
    completed_statuses = [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED, AttemptStatus.APPROVED, AttemptStatus.REJECTED]

    q = (
        select(ExamAttempt, User, Exam, TrainingProgram, CandidateProfile)
        .join(User, ExamAttempt.user_id == User.id)
        .join(Exam, ExamAttempt.exam_id == Exam.id)
        .join(TrainingProgram, Exam.training_program_id == TrainingProgram.id)
        .outerjoin(CandidateProfile, CandidateProfile.user_id == User.id)
        .outerjoin(ExamScheduleOverride, and_(ExamScheduleOverride.exam_id == Exam.id,
                                             ExamScheduleOverride.user_id == User.id))
        .where(ExamAttempt.status.in_(completed_statuses))
    )
    if training_program_id:
        q = q.where(Exam.training_program_id == training_program_id)
    if exam_id:
        q = q.where(ExamAttempt.exam_id == exam_id)
    if schedule_key == "general":
        q = q.where(ExamScheduleOverride.id.is_(None))
    elif schedule_key and schedule_key.startswith("custom:"):
        try:
            start_iso, end_iso = schedule_key[7:].split("|", 1)
            q = q.where(ExamScheduleOverride.start_time == datetime.fromisoformat(start_iso),
                        ExamScheduleOverride.end_time == datetime.fromisoformat(end_iso))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid schedule selection")

    rows = (await db.execute(q)).all()

    by_candidate: Dict[int, Dict[str, Any]] = {}
    all_percentages: List[float] = []
    passed_total = 0

    for attempt, user, exam, program, profile in rows:
        pct = attempt.percentage or 0.0
        all_percentages.append(pct)
        if attempt.passed:
            passed_total += 1

        c = by_candidate.setdefault(user.id, {
            "user_id": user.id,
            "full_name": user.full_name,
            "email": user.email,
            "training_program": program.name,
            "institution": profile.institution if profile else None,
            "percentages": [],
            "passed": 0,
            "latest_date": None,
        })
        c["percentages"].append(pct)
        if attempt.passed:
            c["passed"] += 1
        d = attempt.submitted_at or attempt.created_at
        if d and (c["latest_date"] is None or d > c["latest_date"]):
            c["latest_date"] = d

    candidates = [
        schemas.CandidatePerformanceRow(
            user_id=c["user_id"],
            full_name=c["full_name"],
            email=c["email"],
            training_program=c["training_program"],
            institution=c["institution"],
            exams_taken=len(c["percentages"]),
            exams_passed=c["passed"],
            average_percentage=round(sum(c["percentages"]) / len(c["percentages"]), 2) if c["percentages"] else 0.0,
            best_percentage=round(max(c["percentages"]), 2) if c["percentages"] else 0.0,
            latest_attempt_date=c["latest_date"],
        )
        for c in by_candidate.values()
    ]
    candidates.sort(key=lambda c: c.average_percentage, reverse=True)

    # Topic-level accuracy across every answer in the filtered attempt set
    attempt_ids = [a.id for a, *_ in rows]
    topic_stats: Dict[str, Dict[str, int]] = {}
    if attempt_ids:
        ans_result = await db.execute(
            select(Answer, Question)
            .join(Question, Answer.question_id == Question.id)
            .where(Answer.attempt_id.in_(attempt_ids))
        )
        for answer, question in ans_result.all():
            topic = question.topic or "Uncategorized"
            t = topic_stats.setdefault(topic, {"asked": 0, "correct": 0})
            t["asked"] += 1
            if answer.is_correct:
                t["correct"] += 1

    topic_breakdown = [
        schemas.TopicPerformanceRow(
            topic=topic,
            times_asked=stats["asked"],
            times_correct=stats["correct"],
            accuracy=round(stats["correct"] / stats["asked"] * 100, 1) if stats["asked"] else 0.0,
        )
        for topic, stats in sorted(topic_stats.items())
    ]

    buckets = {f"{i}-{i+9}": 0 for i in range(0, 100, 10)}
    for p in all_percentages:
        idx = min(int(p // 10) * 10, 90)
        buckets[f"{idx}-{idx+9}"] += 1
    score_distribution = [{"range": k, "count": v} for k, v in buckets.items()]

    overall_average = round(sum(all_percentages) / len(all_percentages), 2) if all_percentages else 0.0
    overall_pass_rate = round(passed_total / len(all_percentages) * 100, 2) if all_percentages else 0.0
    ordered_scores = sorted(all_percentages)
    midpoint = len(ordered_scores) // 2
    median_score = ((ordered_scores[midpoint] if len(ordered_scores) % 2 else
                     (ordered_scores[midpoint - 1] + ordered_scores[midpoint]) / 2)
                    if ordered_scores else 0.0)
    variance = sum((score - overall_average) ** 2 for score in all_percentages) / len(all_percentages) if all_percentages else 0.0

    return schemas.PerformanceReport(
        candidates=candidates,
        topic_breakdown=topic_breakdown,
        score_distribution=score_distribution,
        overall_average=overall_average,
        overall_pass_rate=overall_pass_rate,
        total_attempts_considered=len(all_percentages),
        median_score=round(median_score, 2),
        score_standard_deviation=round(variance ** 0.5, 2),
        highest_score=round(max(all_percentages), 2) if all_percentages else 0.0,
        lowest_score=round(min(all_percentages), 2) if all_percentages else 0.0,
    )


@app.get("/api/admin/reports/schedules", tags=["Reports"])
async def report_schedules(exam_id: int, current_user: User = Depends(require_examiner),
                           db: AsyncSession = Depends(get_db)):
    exam = (await db.execute(select(Exam).where(Exam.id == exam_id))).scalar_one_or_none()
    if not exam: raise HTTPException(status_code=404, detail="Exam not found")
    rows = (await db.execute(select(ExamScheduleOverride.start_time, ExamScheduleOverride.end_time)
                            .where(ExamScheduleOverride.exam_id == exam_id).distinct()
                            .order_by(ExamScheduleOverride.start_time))).all()
    scopes = [{"key": "all", "label": "Complete exam — all schedules"},
              {"key": "general", "label": f"General schedule — {exam.start_time:%d %b %Y %H:%M} to {exam.end_time:%d %b %Y %H:%M}"}]
    scopes += [{"key": f"custom:{start.isoformat()}|{end.isoformat()}",
                "label": f"Custom schedule — {start:%d %b %Y %H:%M} to {end:%d %b %Y %H:%M}"}
               for start, end in rows]
    return scopes


@app.get("/api/admin/reports/saved", tags=["Reports"])
async def list_saved_reports(current_user: User = Depends(require_examiner), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(SavedReport, User.full_name).join(User, SavedReport.created_by == User.id)
                            .order_by(SavedReport.created_at.desc()))).all()
    return [{"id": report.id, "title": report.title, "exam_id": report.exam_id,
             "training_program_id": report.training_program_id, "schedule_key": report.schedule_key,
             "created_by": creator, "created_at": report.created_at} for report, creator in rows]


@app.post("/api/admin/reports/saved", tags=["Reports"])
async def save_statistical_report(payload: Dict[str, Any], current_user: User = Depends(require_admin),
                                  db: AsyncSession = Depends(get_db)):
    exam_id = payload.get("exam_id"); programme_id = payload.get("training_program_id")
    schedule_key = payload.get("schedule_key") or "all"
    report_data = payload.get("report_data")
    if not isinstance(report_data, dict): raise HTTPException(status_code=400, detail="Report data is required")
    title = str(payload.get("title") or "Statistical report").strip()[:250]
    report = SavedReport(training_program_id=programme_id, exam_id=exam_id, schedule_key=schedule_key,
                         title=title, report_data=report_data, created_by=current_user.id)
    db.add(report); await db.commit(); await db.refresh(report)
    await log_audit(db, "report_created", user_id=current_user.id, entity_type="saved_report", entity_id=report.id,
                    details={"exam_id": exam_id, "schedule_key": schedule_key})
    return {"id": report.id, "detail": "Statistical report saved"}


@app.delete("/api/admin/reports/saved/{report_id}", tags=["Reports"])
async def delete_saved_report(report_id: int, current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    report = (await db.execute(select(SavedReport).where(SavedReport.id == report_id))).scalar_one_or_none()
    if not report: raise HTTPException(status_code=404, detail="Saved report not found")
    title = report.title; await db.delete(report); await db.commit()
    await log_audit(db, "report_deleted", user_id=current_user.id, entity_type="saved_report", entity_id=report_id,
                    details={"title": title})
    return {"detail": "Report deleted. Exam and candidate records were not affected."}


@app.get("/api/admin/users/csv-template", tags=["Admin"])
async def users_csv_template(current_user: User = Depends(require_admin)):
    content = csv_utils.users_csv_template()
    return StreamingResponse(
        io.StringIO(content),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=user_import_template.csv"},
    )


@app.post("/api/admin/users/import-csv", response_model=schemas.CSVImportResult, tags=["Admin"])
async def import_users_csv(
    file: UploadFile = File(...),
    send_email: bool = Query(True, description="Email each new user their login credentials"),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a .csv file")

    raw = await file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="CSV exceeds 5 MB")
    rows, parse_errors = csv_utils.parse_users_csv(raw)
    if current_user.role != UserRole.SUPER_ADMIN and any(row['role'] == 'super_admin' for row in rows):
        raise HTTPException(status_code=403, detail="Only a Super Admin can create Super Admin accounts.")

    # Preload training programs by code for candidate rows
    programs_result = await db.execute(select(TrainingProgram))
    programs_by_code = {p.code: p for p in programs_result.scalars().all()}

    created = 0
    for row in rows:
        try:
            existing = await db.execute(select(User).where(User.email == row["email"]))
            if existing.scalar_one_or_none():
                parse_errors.append(f"{row['email']}: an account with this email already exists — skipped.")
                continue

            surname, first_name, other_name = split_full_name(row["full_name"])
            user = User(
                email=row["email"],
                hashed_password=get_password_hash(row["password"]),
                surname=surname, first_name=first_name, other_name=other_name,
                phone=row["phone"],
                role=UserRole(row["role"]),
                is_active=True,
                email_verified=True,       # admin-imported accounts are trusted / pre-verified
                created_by_admin=True,
                must_change_password=True,
            )
            db.add(user)
            await db.flush()

            if user.role == UserRole.CANDIDATE:
                program = programs_by_code.get(row.get("training_program_code") or "")
                profile = CandidateProfile(
                    user_id=user.id,
                    training_program_id=program.id if program else None,
                    institution=row.get("institution"),
                    qualification=row.get("qualification"),
                    practice_type=row.get("practice_type"),
                )
                if not program and row.get("training_program_code"):
                    parse_errors.append(
                        f"{row['email']}: training programme code "
                        f"'{row['training_program_code']}' not found — candidate created without a programme."
                    )
                db.add(profile)
                db.add(await allocate_candidate_identity(db, user.id))

            created += 1

            if send_email:
                email_utils.send_credentials_email(user.email, user.full_name, row["password"], user.role.value)
        except Exception as e:
            parse_errors.append(f"{row.get('email', 'unknown')}: {e}")

    await db.commit()
    await log_audit(db, "users_csv_import", user_id=current_user.id, entity_type="user",
                     details={"created": created, "errors": len(parse_errors)})

    return schemas.CSVImportResult(created=created, skipped=len(parse_errors), errors=parse_errors[:50])


@app.get("/api/admin/users", response_model=List[schemas.UserDetailResponse], tags=["Admin"])
async def list_users(
    role: Optional[str] = None,
    training_program_id: Optional[int] = None,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    q = select(User)
    if role:
        q = q.where(User.role == UserRole(role))
    if training_program_id:
        q = q.join(CandidateProfile, CandidateProfile.user_id == User.id).where(
            CandidateProfile.training_program_id == training_program_id)
    result = await db.execute(q.order_by(User.created_at.desc()))
    users = result.scalars().all()
    out = []
    for u in users:
        cp = None
        candidate_number = None
        if u.role == UserRole.CANDIDATE:
            r2 = await db.execute(select(CandidateProfile).where(CandidateProfile.user_id == u.id))
            cp = r2.scalar_one_or_none()
            candidate_number = (await db.execute(select(CandidateIdentity.candidate_number).where(
                CandidateIdentity.user_id == u.id))).scalar_one_or_none()
        detail = build_user_detail(u, cp)
        detail.candidate_number = candidate_number
        out.append(detail)
    return out


@app.get("/api/admin/candidates/{user_id}/tag.pdf", tags=["Admin"])
async def download_candidate_tag(user_id: int, current_user: User = Depends(require_admin),
                                 db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(User, CandidateProfile, TrainingProgram, CandidateIdentity)
        .join(CandidateProfile, CandidateProfile.user_id == User.id)
        .join(TrainingProgram, CandidateProfile.training_program_id == TrainingProgram.id)
        .join(CandidateIdentity, CandidateIdentity.user_id == User.id)
        .where(User.id == user_id, User.role == UserRole.CANDIDATE))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Candidate, training course or candidate number not found")
    candidate, profile, programme, identity = row
    signature = (await db.execute(select(CandidateTagSignature).where(
        CandidateTagSignature.id == 1))).scalar_one_or_none()
    if not signature or not os.path.isfile(signature.image_path):
        raise HTTPException(status_code=409, detail="Upload the candidate tag signature under Governance > Candidate tag signature before downloading tags")
    try:
        with Image.open(signature.image_path) as signature_image:
            signature_image.verify()
    except (UnidentifiedImageError, OSError):
        raise HTTPException(status_code=409, detail="The candidate tag signature image cannot be read. Upload it again under Governance > Candidate tag signature.")
    pdf = build_candidate_tag_pdf(
        full_name=candidate.full_name, candidate_number=identity.candidate_number,
        course_name=programme.name, institution=profile.institution,
        general_manager={"name": signature.signatory_name, "title": signature.title,
                         "image_path": signature.image_path} if signature else None,
        logo_path=os.path.join(FRONTEND_DIR, "images", "nirpr_logo.jpg"),
    )
    await log_audit(db, "candidate_tag_download", user_id=current_user.id,
                    entity_type="user", entity_id=user_id)
    return StreamingResponse(io.BytesIO(pdf), media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="NIRPR-Candidate-Tag-{identity.candidate_number}.pdf"'
    })


@app.post("/api/admin/users", response_model=schemas.UserDetailResponse, tags=["Admin"])
async def create_staff_user(
    data: schemas.UserCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == data.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="An account with this email already exists")

    role = UserRole(data.role.value)
    if role == UserRole.SUPER_ADMIN and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Only a Super Admin can create Super Admin accounts.")
    if role == UserRole.CANDIDATE and not data.training_program_id:
        raise HTTPException(status_code=400, detail="Select a training programme for this candidate")
    if data.training_program_id:
        result = await db.execute(select(TrainingProgram).where(TrainingProgram.id == data.training_program_id))
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Selected training programme was not found")

    surname, first_name, other_name = (data.surname, data.first_name, data.other_name) if data.surname and data.first_name else split_full_name(data.full_name)
    user = User(
        email=data.email,
        hashed_password=get_password_hash(data.password),
        surname=surname, first_name=first_name, other_name=other_name,
        phone=data.phone,
        role=role,
        is_active=True,
        email_verified=True,
        created_by_admin=True,
        must_change_password=True,
    )
    db.add(user)
    await db.flush()

    if role == UserRole.CANDIDATE:
        db.add(CandidateProfile(
            user_id=user.id,
            training_program_id=data.training_program_id,
            institution=data.institution,
            qualification=data.qualification,
            practice_type=data.practice_type,
        ))
        db.add(await allocate_candidate_identity(db, user.id))

    await db.commit()
    await db.refresh(user)

    await log_audit(db, "user_create_by_admin", user_id=current_user.id, entity_type="user", entity_id=user.id,
                     details={"role": role.value, "email": user.email})

    email_utils.send_credentials_email(user.email, user.full_name, data.password, role.value)

    cp = None
    if role == UserRole.CANDIDATE:
        result = await db.execute(select(CandidateProfile).where(CandidateProfile.user_id == user.id))
        cp = result.scalar_one_or_none()
    return build_user_detail(user, cp)


@app.patch("/api/admin/users/{user_id}", response_model=schemas.UserDetailResponse, tags=["Admin"])
async def update_user(
    user_id: int,
    data: schemas.UserUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == UserRole.SUPER_ADMIN and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Only a Super Admin can manage Super Admin accounts.")
    changes = data.model_dump(exclude_unset=True)
    if any(key in changes for key in ("surname", "first_name", "other_name")):
        surname = (changes.get("surname", user.surname) or "").strip()
        first_name = (changes.get("first_name", user.first_name) or "").strip()
        other_name = (changes.get("other_name", user.other_name) or "").strip() or None
        if not surname or not first_name:
            raise HTTPException(status_code=400, detail="Surname and first name are required")
        user.surname, user.first_name, user.other_name = surname, first_name, other_name
    elif "full_name" in changes:
        user.full_name = changes["full_name"].strip()
    for field in ("phone", "is_active"):
        if field in changes:
            setattr(user, field, changes[field])
    await db.commit()
    await db.refresh(user)
    return build_user_detail(user)


@app.post("/api/admin/users/{user_id}/reset-password", response_model=schemas.MessageResponse, tags=["Admin"])
async def admin_reset_password(user_id: int, data: schemas.AdminPasswordReset,
                               current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == UserRole.SUPER_ADMIN and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Only a Super Admin can manage Super Admin accounts.")
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Use the account password change flow for your own account")
    user.hashed_password = get_password_hash(data.new_password)
    user.must_change_password = True
    await db.execute(sa_update(AuthSession).where(AuthSession.user_id == user_id,
        AuthSession.revoked_at.is_(None)).values(revoked_at=datetime.utcnow()))
    await db.commit()
    await log_audit(db, "admin_password_reset", user_id=current_user.id, entity_type="user", entity_id=user_id)
    return schemas.MessageResponse(detail="Password reset. Existing sessions were revoked; the user must change this temporary password at next login.")


@app.delete("/api/admin/users/{user_id}", response_model=schemas.MessageResponse, tags=["Admin"])
async def delete_user(
    user_id: int,
    force: bool = Query(False, description="Also delete this user's exam attempts, answers and candidate profile"),
    current_user: User = Depends(require_role(UserRole.SUPER_ADMIN, UserRole.ADMIN)),
    db: AsyncSession = Depends(get_db),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account while logged in.")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == UserRole.SUPER_ADMIN and current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Only a Super Admin can manage Super Admin accounts.")

    if user.role == UserRole.SUPER_ADMIN:
        count_result = await db.execute(select(func.count(User.id)).where(User.role == UserRole.SUPER_ADMIN))
        if (count_result.scalar() or 0) <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last remaining Super Admin account.")

    attempts_result = await db.execute(select(func.count(ExamAttempt.id)).where(ExamAttempt.user_id == user_id))
    attempt_count = attempts_result.scalar() or 0

    if attempt_count > 0 and not force:
        raise HTTPException(
            status_code=400,
            detail=f"This user has {attempt_count} exam attempt(s) on record. "
                   f"Retry with force=true to permanently delete the user along with their exam history.",
        )

    if attempt_count > 0 and force:
        attempt_ids_result = await db.execute(select(ExamAttempt.id).where(ExamAttempt.user_id == user_id))
        attempt_ids = [row[0] for row in attempt_ids_result.all()]
        if attempt_ids:
            await db.execute(sa_delete(ResultAppeal).where(ResultAppeal.attempt_id.in_(attempt_ids)))
            await db.execute(sa_delete(Answer).where(Answer.attempt_id.in_(attempt_ids)))
        await db.execute(sa_delete(ExamAttempt).where(ExamAttempt.user_id == user_id))

    # Remove candidate/service records that otherwise become orphans and can
    # block a future registration when SQLite reuses a deleted numeric user ID.
    await db.execute(sa_delete(CandidateIdentity).where(CandidateIdentity.user_id == user_id))
    await db.execute(sa_delete(ResultAppeal).where(ResultAppeal.user_id == user_id))
    await db.execute(sa_delete(CandidateBatchAssignment).where(CandidateBatchAssignment.user_id == user_id))
    await db.execute(sa_update(CandidateBatchAssignment).where(
        CandidateBatchAssignment.assigned_by == user_id).values(assigned_by=current_user.id))
    # Keep batch ownership records when a staff account is removed.
    await db.execute(sa_update(TrainingBatch).where(TrainingBatch.created_by==user_id).values(created_by=current_user.id))
    await db.execute(sa_delete(ExamRenewalApproval).where(or_(
        ExamRenewalApproval.user_id == user_id, ExamRenewalApproval.invited_by == user_id)))
    await db.execute(sa_delete(PaymentReceipt).where(PaymentReceipt.user_id == user_id))
    await db.execute(sa_delete(ExamAccessGrant).where(
        or_(ExamAccessGrant.user_id == user_id, ExamAccessGrant.granted_by == user_id)))
    await db.execute(sa_delete(Notification).where(Notification.user_id == user_id))
    await db.execute(sa_delete(ExamScheduleOverride).where(ExamScheduleOverride.user_id == user_id))
    await db.execute(sa_delete(RetakeAuthorization).where(RetakeAuthorization.user_id == user_id))
    await db.execute(sa_delete(AuthSession).where(AuthSession.user_id == user_id))
    await db.execute(sa_delete(StaffMFA).where(StaffMFA.user_id == user_id))
    await db.execute(sa_delete(StaffLoginApproval).where(StaffLoginApproval.user_id == user_id))
    await db.execute(sa_delete(CandidateProfile).where(CandidateProfile.user_id == user_id))
    # Preserve audit history but detach it from the deleted user record.
    await db.execute(sa_update(AuditLog).where(AuditLog.user_id == user_id).values(user_id=None))

    deleted_email = user.email
    await db.delete(user)
    await db.commit()

    await log_audit(db, "user_delete", user_id=current_user.id, entity_type="user", entity_id=user_id,
                     details={"deleted_email": deleted_email, "cascaded_attempts": attempt_count})

    return schemas.MessageResponse(detail=f"User {deleted_email} has been permanently deleted.")


@app.get("/api/admin/dashboard", response_model=schemas.DashboardStats, tags=["Admin"])
async def dashboard(training_program_id: Optional[int] = None, current_user: User = Depends(require_examiner), db: AsyncSession = Depends(get_db)):
    total_candidates = (await db.execute(
        select(func.count(User.id)).outerjoin(CandidateProfile, CandidateProfile.user_id == User.id).where(
            User.role == UserRole.CANDIDATE,
            *([CandidateProfile.training_program_id == training_program_id] if training_program_id else []))
    )).scalar() or 0
    total_exams = (await db.execute(select(func.count(Exam.id)).where(
        *([Exam.training_program_id == training_program_id] if training_program_id else [])))).scalar() or 0
    total_questions = (await db.execute(
        select(func.count(Question.id)).join(QuestionBank, Question.question_bank_id == QuestionBank.id).where(
            Question.is_active == True,
            *([QuestionBank.training_program_id == training_program_id] if training_program_id else []))
    )).scalar() or 0
    active_exams = (await db.execute(
        select(func.count(Exam.id)).where(Exam.status == ExamStatus.ACTIVE,
            *([Exam.training_program_id == training_program_id] if training_program_id else []))
    )).scalar() or 0
    pending_approvals = (await db.execute(
        select(func.count(ExamAttempt.id)).join(Exam, ExamAttempt.exam_id == Exam.id).where(
            ExamAttempt.status.in_([AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED]),
            *([Exam.training_program_id == training_program_id] if training_program_id else [])
        )
    )).scalar() or 0
    pending_payments = (await db.execute(select(func.count(PaymentReceipt.id)).join(Exam, PaymentReceipt.exam_id == Exam.id).where(
        PaymentReceipt.status == "pending",
        *([Exam.training_program_id == training_program_id] if training_program_id else [])))).scalar() or 0

    result = await db.execute(
        select(ExamAttempt, User, Exam)
        .join(User, ExamAttempt.user_id == User.id)
        .join(Exam, ExamAttempt.exam_id == Exam.id)
        .where(*([Exam.training_program_id == training_program_id] if training_program_id else []))
        .order_by(ExamAttempt.created_at.desc())
        .limit(10)
    )
    recent = [
        {
            "candidate_name": u.full_name,
            "exam_title": e.title,
            "status": a.status.value,
            "percentage": a.percentage,
        }
        for a, u, e in result.all()
    ]

    return schemas.DashboardStats(
        total_candidates=total_candidates,
        total_exams=total_exams,
        total_questions=total_questions,
        active_exams=active_exams,
        pending_approvals=pending_approvals,
        pending_payments=pending_payments,
        recent_attempts=recent,
    )


@app.get("/api/admin/audit-log", response_model=List[schemas.AuditLogResponse], tags=["Admin"])
async def audit_log(current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(AuditLog, User.email, User.role, CandidateProfile.training_program_id)
        .outerjoin(User, AuditLog.user_id == User.id)
        .outerjoin(CandidateProfile, CandidateProfile.user_id == User.id)
        .order_by(AuditLog.created_at.desc())
        .limit(200)
    )
    out = []
    for log, email, user_role, training_program_id in result.all():
        out.append(schemas.AuditLogResponse(
            id=log.id,
            user_email=email,
            action=log.action,
            entity_type=log.entity_type,
            entity_id=log.entity_id,
            details=log.details,
            ip_address=log.ip_address,
            created_at=log.created_at,
            user_role=user_role.value if user_role else None,
            training_program_id=training_program_id,
        ))
    return out


# ---------------------------------------------------------------------------
# Extended governance, payments, notifications and verification
# ---------------------------------------------------------------------------
@app.get("/api/exams/{exam_id}/policy", tags=["Exams"])
async def get_exam_policy(exam_id: int, current_user: User = Depends(require_any), db: AsyncSession = Depends(get_db)):
    policy = (await db.execute(select(ExamPolicy).where(ExamPolicy.exam_id == exam_id))).scalar_one_or_none()
    return {column.name: getattr(policy, column.name) for column in ExamPolicy.__table__.columns} if policy else schemas.ExamPolicyUpdate().model_dump()


@app.put("/api/exams/{exam_id}/policy", tags=["Exams"])
async def update_exam_policy(exam_id: int, data: schemas.ExamPolicyUpdate,
                             current_user: User = Depends(require_examiner), db: AsyncSession = Depends(get_db)):
    if not (await db.execute(select(Exam.id).where(Exam.id == exam_id))).scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Exam not found")
    policy = (await db.execute(select(ExamPolicy).where(ExamPolicy.exam_id == exam_id))).scalar_one_or_none()
    if not policy:
        policy = ExamPolicy(exam_id=exam_id); db.add(policy)
    for key, value in data.model_dump(exclude_unset=True).items(): setattr(policy, key, value)
    await db.commit()
    await log_audit(db, "exam_policy_update", current_user.id, "exam", exam_id, data.model_dump(mode="json", exclude_unset=True))
    return {column.name: getattr(policy, column.name) for column in ExamPolicy.__table__.columns}


@app.patch("/api/exams/{exam_id}/payment-requirement", tags=["Payments"])
async def update_payment_requirement(exam_id: int, data: schemas.ExamPaymentRequirement,
                                     current_user: User = Depends(require_admin),
                                     db: AsyncSession = Depends(get_db)):
    if not (await db.execute(select(Exam.id).where(Exam.id == exam_id))).scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Exam not found")
    policy = (await db.execute(select(ExamPolicy).where(ExamPolicy.exam_id == exam_id))).scalar_one_or_none()
    if policy is None:
        policy = ExamPolicy(exam_id=exam_id)
        db.add(policy)
    policy.payment_required = data.payment_required
    await db.commit()
    await log_audit(db, "exam_payment_requirement_update", current_user.id, "exam", exam_id,
                    {"payment_required": data.payment_required})
    return {"exam_id": exam_id, "payment_required": policy.payment_required}


@app.get("/api/questions/{question_id}/versions", tags=["Questions"])
async def question_versions(question_id: int, current_user: User = Depends(require_examiner), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(QuestionRevision, User.full_name).join(User, QuestionRevision.changed_by == User.id)
                             .where(QuestionRevision.question_id == question_id)
                             .order_by(QuestionRevision.version.desc()))).all()
    return [{"id": r.id, "version": r.version, "action": r.action, "snapshot": r.snapshot,
             "review_status": r.review_status, "changed_by": name, "changed_at": r.changed_at} for r, name in rows]


@app.post("/api/questions/versions/{revision_id}/review", tags=["Questions"])
async def review_question_revision(revision_id: int, decision: str, notes: Optional[str] = None,
                                   current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    if decision not in ("approved", "rejected"): raise HTTPException(status_code=400, detail="Decision must be approved or rejected")
    revision = (await db.execute(select(QuestionRevision).where(QuestionRevision.id == revision_id))).scalar_one_or_none()
    if not revision: raise HTTPException(status_code=404, detail="Question revision not found")
    revision.review_status, revision.reviewed_by, revision.reviewed_at, revision.review_notes = decision, current_user.id, datetime.utcnow(), notes
    await db.commit(); return {"detail": f"Question version {decision}"}


@app.post("/api/candidate/appeals", tags=["Appeals"])
async def create_appeal(data: schemas.AppealCreate, current_user: User = Depends(require_real_candidate), db: AsyncSession = Depends(get_db)):
    attempt = (await db.execute(select(ExamAttempt).where(ExamAttempt.id == data.attempt_id,
                                                           ExamAttempt.user_id == current_user.id))).scalar_one_or_none()
    if not attempt or not attempt.result_released: raise HTTPException(status_code=400, detail="Only a released result can be appealed")
    existing = (await db.execute(select(ResultAppeal).where(ResultAppeal.attempt_id == attempt.id,
                                                             ResultAppeal.user_id == current_user.id))).scalar_one_or_none()
    if existing: raise HTTPException(status_code=409, detail="An appeal already exists for this result")
    appeal = ResultAppeal(**data.model_dump(), user_id=current_user.id); db.add(appeal); await db.commit(); await db.refresh(appeal)
    return {"id": appeal.id, "status": appeal.status, "submitted_at": appeal.submitted_at}


@app.get("/api/candidate/appeals", tags=["Appeals"])
async def my_appeals(current_user: User = Depends(require_candidate), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ResultAppeal).where(ResultAppeal.user_id == current_user.id)
                             .order_by(ResultAppeal.submitted_at.desc()))).scalars().all()
    return [{c.name: getattr(a, c.name) for c in ResultAppeal.__table__.columns if c.name not in ("user_id",)} for a in rows]


@app.get("/api/admin/appeals", tags=["Appeals"])
async def all_appeals(current_user: User = Depends(require_examiner), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ResultAppeal, User.full_name, Exam.title).join(User, ResultAppeal.user_id == User.id)
                             .join(ExamAttempt, ResultAppeal.attempt_id == ExamAttempt.id).join(Exam, ExamAttempt.exam_id == Exam.id)
                             .order_by(ResultAppeal.submitted_at.desc()))).all()
    return [{"id": a.id, "attempt_id": a.attempt_id, "candidate": name, "exam": title, "reason": a.reason,
             "status": a.status, "resolution": a.resolution, "submitted_at": a.submitted_at} for a, name, title in rows]


@app.patch("/api/admin/appeals/{appeal_id}", tags=["Appeals"])
async def decide_appeal(appeal_id: int, data: schemas.AppealReview, current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    appeal = (await db.execute(select(ResultAppeal).where(ResultAppeal.id == appeal_id))).scalar_one_or_none()
    if not appeal: raise HTTPException(status_code=404, detail="Appeal not found")
    retake_granted = False
    if data.status in ("approved", "upheld"):
        attempt = (await db.execute(select(ExamAttempt).where(
            ExamAttempt.id == appeal.attempt_id,
            ExamAttempt.user_id == appeal.user_id,
        ))).scalar_one_or_none()
        if not attempt:
            raise HTTPException(status_code=409, detail="The examination attempt linked to this appeal no longer exists")
        existing_grant = (await db.execute(select(RetakeAuthorization).where(
            RetakeAuthorization.exam_id == attempt.exam_id,
            RetakeAuthorization.user_id == appeal.user_id,
            RetakeAuthorization.used == False,
        ))).scalars().first()
        if not existing_grant:
            db.add(RetakeAuthorization(
                exam_id=attempt.exam_id,
                user_id=appeal.user_id,
                authorized_by=current_user.id,
                reason=f"Appeal #{appeal.id} upheld: {data.resolution}",
            ))
            retake_granted = True
    appeal.status, appeal.resolution, appeal.reviewed_by, appeal.reviewed_at = data.status, data.resolution, current_user.id, datetime.utcnow()
    await db.commit()
    await log_audit(db, "appeal_reviewed", user_id=current_user.id, entity_type="result_appeal", entity_id=appeal.id,
                    details={"status": data.status, "resolution": data.resolution, "retake_granted": retake_granted})
    detail = "Appeal updated"
    if data.status in ("approved", "upheld"):
        detail = ("Appeal approved and rewrite access granted." if retake_granted
                  else "Appeal approved. The candidate already has unused rewrite access.")
    return {"detail": detail, "retake_granted": retake_granted}


UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(Path(BASE_DIR) / "uploads"))).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@app.post("/api/candidate/payments/{exam_id}", tags=["Payments"])
async def upload_payment(exam_id: int, file: UploadFile = File(...), amount: Optional[float] = Form(None),
                         reference: Optional[str] = Form(None), current_user: User = Depends(require_real_candidate),
                         db: AsyncSession = Depends(get_db)):
    allowed = {"application/pdf", "image/png", "image/jpeg"}
    if file.content_type not in allowed: raise HTTPException(status_code=400, detail="Receipt must be PDF, PNG or JPEG")
    content = await file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024: raise HTTPException(status_code=413, detail="Receipt exceeds 5 MB")
    if file.content_type == "application/pdf":
        if not content.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="Receipt is not a PDF")
    else:
        try:
            with Image.open(io.BytesIO(content)) as image:
                expected = "PNG" if file.content_type == "image/png" else "JPEG"
                if image.format != expected or image.width * image.height > 20_000_000:
                    raise ValueError("Invalid receipt image")
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            raise HTTPException(status_code=400, detail="Receipt image could not be read")
    exam = (await db.execute(select(Exam).where(Exam.id == exam_id))).scalar_one_or_none()
    if exam is None:
        raise HTTPException(status_code=404, detail="Exam not found")
    await require_exam_eligibility(db, exam, current_user)
    suffix = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}[file.content_type]
    destination = UPLOAD_DIR / f"receipt-{current_user.id}-{exam_id}-{secrets.token_hex(8)}{suffix}"
    receipt = (await db.execute(select(PaymentReceipt).where(PaymentReceipt.user_id == current_user.id,
                                                              PaymentReceipt.exam_id == exam_id))).scalar_one_or_none()
    if receipt and receipt.status == "approved": raise HTTPException(status_code=409, detail="This payment is already approved")
    destination.write_bytes(content)
    if receipt:
        receipt.file_name, receipt.stored_path, receipt.content_type = file.filename or destination.name, str(destination), file.content_type
        receipt.amount, receipt.reference, receipt.status, receipt.uploaded_at = amount, reference, "pending", datetime.utcnow()
    else:
        receipt = PaymentReceipt(user_id=current_user.id, exam_id=exam_id, file_name=file.filename or destination.name,
                                 stored_path=str(destination), content_type=file.content_type, amount=amount, reference=reference)
        db.add(receipt)
    await db.commit(); return {"detail": "Receipt uploaded for verification", "status": "pending"}


@app.get("/api/candidate/payments", tags=["Payments"])
async def my_payments(current_user: User = Depends(require_candidate), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(PaymentReceipt, Exam.title).join(Exam, PaymentReceipt.exam_id == Exam.id)
                             .where(PaymentReceipt.user_id == current_user.id))).all()
    return [{"id": p.id, "exam_id": p.exam_id, "exam": title, "amount": p.amount, "reference": p.reference,
             "status": p.status, "review_notes": p.review_notes, "uploaded_at": p.uploaded_at} for p, title in rows]


@app.get("/api/admin/payments", tags=["Payments"])
async def pending_payments(training_program_id: Optional[int] = None,
                           current_user: User = Depends(require_examiner), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(PaymentReceipt, User.full_name, Exam.title).join(User, PaymentReceipt.user_id == User.id)
                             .join(Exam, PaymentReceipt.exam_id == Exam.id)
                             .where(*([Exam.training_program_id == training_program_id] if training_program_id else []))
                             .order_by(PaymentReceipt.uploaded_at.desc()))).all()
    return [{"id": p.id, "candidate": name, "exam": title, "amount": p.amount, "reference": p.reference,
             "file_name": p.file_name, "content_type": p.content_type,
             "file_url": f"/api/admin/payments/{p.id}/file", "status": p.status,
             "review_notes": p.review_notes, "uploaded_at": p.uploaded_at} for p, name, title in rows]


@app.get("/api/admin/exam-access", tags=["Payments"])
async def list_exam_access(training_program_id: Optional[int] = None,
                           current_user: User = Depends(require_examiner),
                           db: AsyncSession = Depends(get_db)):
    candidate_query = (select(User, CandidateProfile, TrainingProgram)
                       .join(CandidateProfile, CandidateProfile.user_id == User.id)
                       .join(TrainingProgram, CandidateProfile.training_program_id == TrainingProgram.id)
                       .where(User.role == UserRole.CANDIDATE, User.is_active == True))
    exam_query = select(Exam).where(Exam.status.in_([ExamStatus.SCHEDULED, ExamStatus.ACTIVE]))
    if training_program_id:
        candidate_query = candidate_query.where(CandidateProfile.training_program_id == training_program_id)
        exam_query = exam_query.where(Exam.training_program_id == training_program_id)
    candidates = (await db.execute(candidate_query.order_by(User.full_name))).all()
    exams = (await db.execute(exam_query.order_by(Exam.start_time.desc()))).scalars().all()
    payment_flags = dict((await db.execute(select(ExamPolicy.exam_id, ExamPolicy.payment_required))).all())
    rows = []
    eligibility = {exam.id: await eligible_candidate_ids(db, exam) for exam in exams}
    for user, profile, program in candidates:
        for exam in exams:
            if exam.training_program_id != profile.training_program_id:
                continue
            receipt = (await db.execute(select(PaymentReceipt.status).where(
                PaymentReceipt.user_id == user.id, PaymentReceipt.exam_id == exam.id
            ))).scalar_one_or_none()
            grant = (await db.execute(select(ExamAccessGrant.id).where(
                ExamAccessGrant.user_id == user.id, ExamAccessGrant.exam_id == exam.id
            ))).scalar_one_or_none()
            started = (await db.execute(select(ExamAttempt.id).where(
                ExamAttempt.user_id == user.id, ExamAttempt.exam_id == exam.id
            ).limit(1))).scalar_one_or_none()
            rows.append({
                "user_id": user.id, "candidate": user.full_name, "email": user.email,
                "programme": program.name, "exam_id": exam.id, "exam": exam.title,
                "receipt_status": receipt or "not_uploaded", "admin_granted": bool(grant),
                "has_started": bool(started),
                "eligible": user.id in eligibility[exam.id],
                "payment_required": bool(payment_flags.get(exam.id, False)),
                "can_start": bool(user.id in eligibility[exam.id] and (not payment_flags.get(exam.id, False) or started or grant or receipt == "approved")),
            })
    return rows


@app.put("/api/admin/exam-access", tags=["Payments"])
async def update_exam_access(data: schemas.ExamAccessUpdate,
                             current_user: User = Depends(require_examiner),
                             db: AsyncSession = Depends(get_db)):
    candidate = (await db.execute(select(User, CandidateProfile)
        .join(CandidateProfile, CandidateProfile.user_id == User.id)
        .where(User.id == data.user_id, User.role == UserRole.CANDIDATE))).first()
    exam = (await db.execute(select(Exam).where(Exam.id == data.exam_id))).scalar_one_or_none()
    if not candidate or not exam:
        raise HTTPException(status_code=404, detail="Candidate or exam not found")
    if candidate[1].training_program_id != exam.training_program_id:
        raise HTTPException(status_code=400, detail="Candidate is not registered for this exam's training programme")
    grant = (await db.execute(select(ExamAccessGrant).where(
        ExamAccessGrant.user_id == data.user_id, ExamAccessGrant.exam_id == data.exam_id
    ))).scalar_one_or_none()
    if data.granted and not grant:
        db.add(ExamAccessGrant(user_id=data.user_id, exam_id=data.exam_id, granted_by=current_user.id))
    elif not data.granted and grant:
        await db.delete(grant)
    db.add(Notification(
        user_id=data.user_id,
        title="Examination access updated",
        message=(f"An administrator granted you access to {exam.title}."
                 if data.granted else f"Your administrative access to {exam.title} was removed."),
        notification_type="exam_access",
    ))
    await db.commit()
    await log_audit(db, "exam_access_grant" if data.granted else "exam_access_revoke",
                    user_id=current_user.id, entity_type="user", entity_id=data.user_id,
                    details={"exam_id": data.exam_id})
    return {"detail": "Exam access granted" if data.granted else "Exam access revoked",
            "granted": data.granted}


@app.get("/api/admin/payments/{receipt_id}/file", tags=["Payments"])
async def view_payment_receipt(receipt_id: int, current_user: User = Depends(require_examiner),
                               db: AsyncSession = Depends(get_db)):
    receipt = (await db.execute(select(PaymentReceipt).where(PaymentReceipt.id == receipt_id))).scalar_one_or_none()
    if not receipt: raise HTTPException(status_code=404, detail="Receipt not found")
    stored = Path(receipt.stored_path).resolve(); upload_root = UPLOAD_DIR.resolve()
    if upload_root not in stored.parents or not stored.is_file():
        raise HTTPException(status_code=404, detail="The uploaded receipt file is unavailable")
    # Download untrusted receipts rather than execute active PDF content in our origin.
    return FileResponse(stored, media_type=receipt.content_type or "application/octet-stream",
                        filename=Path(receipt.file_name).name,
                        content_disposition_type="attachment",
                        headers={"X-Content-Type-Options": "nosniff"})


@app.patch("/api/admin/payments/{receipt_id}", tags=["Payments"])
async def review_payment(receipt_id: int, data: schemas.ReceiptReview, current_user: User = Depends(require_examiner), db: AsyncSession = Depends(get_db)):
    receipt = (await db.execute(select(PaymentReceipt).where(PaymentReceipt.id == receipt_id))).scalar_one_or_none()
    if not receipt: raise HTTPException(status_code=404, detail="Receipt not found")
    receipt.status, receipt.review_notes, receipt.reviewed_by, receipt.reviewed_at = data.status, data.notes, current_user.id, datetime.utcnow()
    db.add(Notification(user_id=receipt.user_id, title="Payment receipt reviewed",
                        message=f"Your payment receipt was {data.status}.", notification_type="payment"))
    candidate = (await db.execute(select(User).where(User.id == receipt.user_id))).scalar_one()
    email_utils.send_general_notification_email(candidate.email, candidate.full_name, "Payment receipt reviewed",
        f"Your uploaded payment receipt has been {data.status}." + (f" Review note: {data.notes}" if data.notes else ""))
    await db.commit(); return {"detail": f"Receipt {data.status}"}


def notification_visibility(current_user, announcements_only=False):
    conditions = [Notification.user_id == current_user.id,
                  or_(Notification.scheduled_for.is_(None), Notification.scheduled_for <= datetime.utcnow())]
    if announcements_only or current_user.role == UserRole.CANDIDATE:
        conditions.append(Notification.notification_type == "announcement")
    return conditions


async def read_notification_items(current_user, db, announcements_only=False):
    rows = (await db.execute(select(Notification).where(*notification_visibility(current_user, announcements_only))
                             .order_by(Notification.created_at.desc(), Notification.id.desc()).limit(100))).scalars().all()
    return [{c.name: getattr(n, c.name) for c in Notification.__table__.columns} for n in rows]


@app.get("/api/notifications", tags=["Notifications"])
async def notifications(current_user: User = Depends(require_any), db: AsyncSession = Depends(get_db)):
    return await read_notification_items(current_user, db)


@app.get("/api/announcements", tags=["Announcements"])
async def announcements(current_user: User = Depends(require_any), db: AsyncSession = Depends(get_db)):
    return await read_notification_items(current_user, db, announcements_only=True)


@app.get("/api/notifications/unread-count", tags=["Notifications"])
async def unread_notification_count(current_user: User = Depends(require_any), db: AsyncSession = Depends(get_db)):
    count = (await db.execute(select(func.count(Notification.id)).where(
        *notification_visibility(current_user), Notification.read_at.is_(None)))).scalar() or 0
    return {"unread_count": count}


@app.get("/api/announcements/unread-count", tags=["Announcements"])
async def unread_announcement_count(current_user: User = Depends(require_any), db: AsyncSession = Depends(get_db)):
    count = (await db.execute(select(func.count(Notification.id)).where(
        *notification_visibility(current_user, True), Notification.read_at.is_(None)))).scalar() or 0
    return {"unread_count": count}


async def mark_visible_notifications_read(current_user, db, announcements_only=False):
    await db.execute(sa_update(Notification).where(*notification_visibility(current_user, announcements_only),
        Notification.read_at.is_(None)).values(read_at=datetime.utcnow()))
    await db.commit()
    return {"detail": "Announcements marked as read" if announcements_only else "Notifications marked as read", "unread_count": 0}


@app.post("/api/notifications/mark-all-read", tags=["Notifications"])
async def mark_notifications_read(current_user: User = Depends(require_any), db: AsyncSession = Depends(get_db)):
    return await mark_visible_notifications_read(current_user, db)


@app.post("/api/announcements/mark-all-read", tags=["Announcements"])
async def mark_announcements_read(current_user: User = Depends(require_any), db: AsyncSession = Depends(get_db)):
    return await mark_visible_notifications_read(current_user, db, True)


@app.get("/api/admin/announcements", tags=["Announcements"])
async def announcement_history(current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    rows=(await db.execute(select(Notification.title, Notification.message, Notification.created_at,
        func.count(Notification.id).label("recipients"), func.count(Notification.read_at).label("read_count"))
        .where(Notification.notification_type=="announcement")
        .group_by(Notification.title,Notification.message,Notification.created_at)
        .order_by(Notification.created_at.desc()).limit(50))).all()
    return [{"title":title,"message":message,"created_at":created,"recipients":recipients,"read_count":read_count}
            for title,message,created,recipients,read_count in rows]


@app.post("/api/admin/announcements", tags=["Announcements"])
@app.post("/api/admin/notifications", tags=["Announcements"])
async def create_notifications(data: schemas.NotificationCreate, current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    if not data.title.strip() or not data.message.strip():
        raise HTTPException(status_code=400, detail="Enter an announcement title and message")
    if data.scheduled_for is not None:
        raise HTTPException(status_code=400, detail="Announcements are published immediately; do not provide a scheduled time")
    query=select(User).join(CandidateProfile,CandidateProfile.user_id==User.id).where(
        User.role==UserRole.CANDIDATE,User.is_active==True)
    if data.user_ids:
        query=query.where(User.id.in_(set(data.user_ids)))
    if data.training_program_id:
        query=query.where(CandidateProfile.training_program_id==data.training_program_id)
    if data.batch_id:
        batch=(await db.execute(select(TrainingBatch).where(TrainingBatch.id==data.batch_id))).scalar_one_or_none()
        if batch is None or (data.training_program_id and batch.training_program_id!=data.training_program_id):
            raise HTTPException(status_code=400,detail="Select a valid batch for the training programme")
        query=query.join(CandidateBatchAssignment,CandidateBatchAssignment.user_id==User.id).where(
            CandidateBatchAssignment.batch_id==data.batch_id,CandidateProfile.training_program_id==batch.training_program_id)
    users=(await db.execute(query)).scalars().all()
    if not users:
        raise HTTPException(status_code=400,detail="No active candidates match this announcement's audience")
    published_at=datetime.utcnow()
    for user in users:
        db.add(Notification(user_id=user.id,title=data.title.strip(),message=data.message.strip(),
                            notification_type="announcement",created_at=published_at))
    await db.commit()
    await log_audit(db,"announcement_publish",user_id=current_user.id,entity_type="announcement",
                    details={"title":data.title.strip(),"recipient_count":len(users),"batch_id":data.batch_id,
                             "training_program_id":data.training_program_id,"email_requested":data.send_email})
    if data.send_email:
        for user in users:
            email_utils.send_general_notification_email(user.email,user.full_name,data.title.strip(),data.message.strip())
    return {"detail":f"Announcement published to {len(users)} candidate(s)","created":len(users),"email_requested":data.send_email}


@app.post("/api/attempts/{attempt_id}/heartbeat", tags=["Exam Taking"])
async def exam_heartbeat(attempt_id: int, request: Request, current_user: User = Depends(require_real_candidate), db: AsyncSession = Depends(get_db)):
    attempt = await _get_owned_attempt(attempt_id, current_user, db)
    if attempt.status != AttemptStatus.IN_PROGRESS: raise HTTPException(status_code=400, detail="Attempt is not active")
    payload = {"user_id": current_user.id, "time": datetime.utcnow().isoformat(), "ip": get_client_ip(request)}
    await heartbeat(attempt_id, payload); return {"ok": True, "server_time": payload["time"]}


@app.get("/api/certificates/verify/{number:path}", tags=["Certificates"])
async def verify_certificate(number: str, db: AsyncSession = Depends(get_db), attempt_id: Optional[int] = None):
    value=number.strip().upper()
    try:
        parts=value.split("/")
        if len(parts) not in (6,7) or parts[:3]!=["NIRPR","NTC","RSO"]:
            raise ValueError
        candidate_id=int(parts[5])
        int(parts[4])
        legacy=len(parts)==7
        record_id=int(parts[6]) if legacy else attempt_id
    except Exception:
        raise HTTPException(status_code=404,detail="Certificate number is invalid")
    query=(select(ExamAttempt,User.full_name,Exam.title,TrainingProgram)
        .join(User,ExamAttempt.user_id==User.id).join(Exam,ExamAttempt.exam_id==Exam.id)
        .join(TrainingProgram,Exam.training_program_id==TrainingProgram.id)
        .where(ExamAttempt.user_id==candidate_id,ExamAttempt.status==AttemptStatus.APPROVED,
               ExamAttempt.result_released==True,ExamAttempt.passed==True))
    if record_id is not None:
        query=query.where(ExamAttempt.id==record_id)
    rows=(await db.execute(query.order_by(ExamAttempt.approved_at.desc(),ExamAttempt.id.desc()))).all()
    for attempt,candidate,exam,programme in rows:
        issued_on=attempt.approved_at or attempt.submitted_at or attempt.created_at
        formatter=legacy_certificate_number if legacy else certificate_number
        expected=formatter(programme.code,issued_on,attempt.id,candidate_id)
        if value==expected:
            return {"valid":True,"certificate_number":expected,"candidate":candidate,"exam":exam,
                    "programme":programme.name,"score":attempt.percentage,"issued_at":issued_on}
    raise HTTPException(status_code=404,detail="No valid released certificate matches this number")


@app.post("/api/admin/signatures/{role_key}", tags=["Certificates"])
async def upload_signature(role_key: str, signatory_name: str = Form(...), title: str = Form(...),
                           file: UploadFile = File(...), current_user: User = Depends(require_admin),
                           db: AsyncSession = Depends(get_db)):
    if role_key not in ("general_manager", "course_coordinator"):
        raise HTTPException(status_code=400, detail="Signature role must be general_manager or course_coordinator")
    if file.content_type not in ("image/png", "image/jpeg"):
        raise HTTPException(status_code=400, detail="Signature must be PNG or JPEG")
    content = await file.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024: raise HTTPException(status_code=413, detail="Signature exceeds 2 MB")
    try:
        with Image.open(io.BytesIO(content)) as signature_image:
            signature_image.verify()
    except (UnidentifiedImageError, OSError):
        raise HTTPException(status_code=400, detail="Signature image could not be read")
    try:
        content = signature_png(content)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error))
    suffix = ".png"
    destination = UPLOAD_DIR / f"signature-{role_key}-{secrets.token_hex(6)}{suffix}"; destination.write_bytes(content)
    record = (await db.execute(select(StaffSignature).where(StaffSignature.role_key == role_key))).scalar_one_or_none()
    if record:
        record.signatory_name, record.title, record.image_path = signatory_name, title, str(destination)
        record.uploaded_by, record.uploaded_at = current_user.id, datetime.utcnow()
    else:
        db.add(StaffSignature(role_key=role_key, signatory_name=signatory_name, title=title,
                              image_path=str(destination), uploaded_by=current_user.id))
    await db.commit(); return {"detail": "Certificate signature saved"}


@app.get("/api/admin/programmes/{programme_id}/signatures", tags=["Certificates"])
async def list_programme_signatures(programme_id: int, current_user: User = Depends(require_examiner),
                                    db: AsyncSession = Depends(get_db)):
    programme = (await db.execute(select(TrainingProgram).where(
        TrainingProgram.id == programme_id))).scalar_one_or_none()
    if not programme:
        raise HTTPException(status_code=404, detail="Training programme not found")
    rows = (await db.execute(select(ProgrammeSignature).where(
        ProgrammeSignature.training_program_id == programme_id).order_by(ProgrammeSignature.role_key))).scalars().all()
    return [{"id": s.id, "programme_name": programme.name, "programme_code": programme.code,
             "role_key": s.role_key, "signatory_name": s.signatory_name,
             "title": s.title, "uploaded_at": s.uploaded_at} for s in rows]


async def _programme_signature_record(db: AsyncSession, programme_id: int, signature_id: int) -> ProgrammeSignature:
    record = (await db.execute(select(ProgrammeSignature).where(
        ProgrammeSignature.id == signature_id,
        ProgrammeSignature.training_program_id == programme_id))).scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Programme signature not found")
    return record


def _stored_signature_file(record: ProgrammeSignature) -> Path:
    stored = Path(record.image_path).resolve()
    if not stored.is_relative_to(UPLOAD_DIR.resolve()) or not stored.is_file():
        raise HTTPException(status_code=404, detail="Signature image file not found")
    return stored


@app.get("/api/admin/candidate-tag-signature", tags=["Certificates"])
async def get_candidate_tag_signature(current_user: User = Depends(require_admin),
                                      db: AsyncSession = Depends(get_db)):
    record = (await db.execute(select(CandidateTagSignature).where(
        CandidateTagSignature.id == 1))).scalar_one_or_none()
    if not record:
        return None
    return {"id": record.id, "signatory_name": record.signatory_name,
            "title": record.title, "uploaded_at": record.uploaded_at}


@app.get("/api/admin/candidate-tag-signature/image", tags=["Certificates"])
async def view_candidate_tag_signature(current_user: User = Depends(require_admin),
                                       db: AsyncSession = Depends(get_db)):
    record = (await db.execute(select(CandidateTagSignature).where(
        CandidateTagSignature.id == 1))).scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Candidate tag signature not found")
    stored = _stored_signature_file(record)
    return FileResponse(stored, media_type="image/png" if stored.suffix.lower() == ".png" else "image/jpeg",
                        headers={"Cache-Control": "no-store"})


@app.put("/api/admin/candidate-tag-signature", tags=["Certificates"])
async def save_candidate_tag_signature(signatory_name: str = Form(...), title: str = Form(...),
                                       file: UploadFile | None = File(None),
                                       current_user: User = Depends(require_admin),
                                       db: AsyncSession = Depends(get_db)):
    if not signatory_name.strip() or not title.strip():
        raise HTTPException(status_code=400, detail="Signatory name and official title are required")
    record = (await db.execute(select(CandidateTagSignature).where(
        CandidateTagSignature.id == 1))).scalar_one_or_none()
    if not record and (not file or not file.filename):
        raise HTTPException(status_code=400, detail="Select a signature image")
    replacement = None
    previous = Path(record.image_path) if record else None
    if file and file.filename:
        if file.content_type not in ("image/png", "image/jpeg"):
            raise HTTPException(status_code=400, detail="Signature must be PNG or JPEG")
        content = await file.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Signature exceeds 2 MB")
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
        except (UnidentifiedImageError, OSError):
            raise HTTPException(status_code=400, detail="Signature image could not be read")
        suffix = ".png" if file.content_type == "image/png" else ".jpg"
        replacement = UPLOAD_DIR / f"candidate-tag-signature-{secrets.token_hex(8)}{suffix}"
        replacement.write_bytes(content)
    if record:
        record.signatory_name = signatory_name.strip()
        record.title = title.strip()
        record.uploaded_by = current_user.id
        record.uploaded_at = datetime.utcnow()
        if replacement:
            record.image_path = str(replacement)
    else:
        db.add(CandidateTagSignature(id=1, signatory_name=signatory_name.strip(), title=title.strip(),
                                     image_path=str(replacement), uploaded_by=current_user.id))
    try:
        await db.commit()
    except Exception:
        if replacement:
            replacement.unlink(missing_ok=True)
        raise
    if replacement and previous and previous.resolve().is_relative_to(UPLOAD_DIR.resolve()):
        try:
            previous.unlink(missing_ok=True)
        except OSError:
            pass
    return {"detail": "Candidate tag signature saved"}


@app.delete("/api/admin/candidate-tag-signature", tags=["Certificates"])
async def delete_candidate_tag_signature(current_user: User = Depends(require_admin),
                                         db: AsyncSession = Depends(get_db)):
    record = (await db.execute(select(CandidateTagSignature).where(
        CandidateTagSignature.id == 1))).scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Candidate tag signature not found")
    stored = Path(record.image_path).resolve()
    await db.delete(record)
    await db.commit()
    if stored.is_relative_to(UPLOAD_DIR.resolve()):
        try:
            stored.unlink(missing_ok=True)
        except OSError:
            pass
    return {"detail": "Candidate tag signature deleted"}


@app.get("/api/admin/programmes/{programme_id}/signatures/{signature_id}/image", tags=["Certificates"])
async def view_programme_signature_image(programme_id: int, signature_id: int,
                                         current_user: User = Depends(require_examiner),
                                         db: AsyncSession = Depends(get_db)):
    record = await _programme_signature_record(db, programme_id, signature_id)
    stored = _stored_signature_file(record)
    media_type = "image/png" if stored.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(stored, media_type=media_type, headers={"Cache-Control": "no-store"})


@app.patch("/api/admin/programmes/{programme_id}/signatures/{signature_id}", tags=["Certificates"])
async def edit_programme_signature(programme_id: int, signature_id: int,
                                   signatory_name: str = Form(...), title: str = Form(...),
                                   file: UploadFile | None = File(None),
                                   current_user: User = Depends(require_admin),
                                   db: AsyncSession = Depends(get_db)):
    record = await _programme_signature_record(db, programme_id, signature_id)
    if not signatory_name.strip() or not title.strip():
        raise HTTPException(status_code=400, detail="Signatory name and official title are required")
    previous = Path(record.image_path)
    replacement = None
    if file and file.filename:
        if file.content_type not in ("image/png", "image/jpeg"):
            raise HTTPException(status_code=400, detail="Signature must be PNG or JPEG")
        content = await file.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Signature exceeds 2 MB")
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
        except (UnidentifiedImageError, OSError):
            raise HTTPException(status_code=400, detail="Signature image could not be read")
        try:
            content = signature_png(content)
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error))
        suffix = ".png"
        replacement = UPLOAD_DIR / f"signature-programme-{programme_id}-{record.role_key}-{secrets.token_hex(6)}{suffix}"
        replacement.write_bytes(content)
        record.image_path = str(replacement)
    record.signatory_name = signatory_name.strip()
    record.title = title.strip()
    record.uploaded_by = current_user.id
    record.uploaded_at = datetime.utcnow()
    try:
        await db.commit()
    except Exception:
        if replacement:
            replacement.unlink(missing_ok=True)
        raise
    if replacement and previous.resolve().is_relative_to(UPLOAD_DIR.resolve()):
        try:
            previous.unlink(missing_ok=True)
        except OSError:
            pass
    return {"detail": "Programme signature updated"}


@app.delete("/api/admin/programmes/{programme_id}/signatures/{signature_id}", tags=["Certificates"])
async def delete_programme_signature(programme_id: int, signature_id: int,
                                     current_user: User = Depends(require_admin),
                                     db: AsyncSession = Depends(get_db)):
    record = await _programme_signature_record(db, programme_id, signature_id)
    stored = Path(record.image_path).resolve()
    await db.delete(record)
    await db.commit()
    if stored.is_relative_to(UPLOAD_DIR.resolve()):
        try:
            stored.unlink(missing_ok=True)
        except OSError:
            pass
    return {"detail": "Programme signature deleted"}


@app.post("/api/admin/programmes/{programme_id}/signatures/{role_key}", tags=["Certificates"])
async def upload_programme_signature(programme_id: int, role_key: str, signatory_name: str = Form(...),
                                     title: str = Form(...), file: UploadFile = File(...),
                                     current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    if role_key not in ("general_manager", "course_coordinator"):
        raise HTTPException(status_code=400, detail="Select General Manager or Course Coordinator")
    if not (await db.execute(select(TrainingProgram.id).where(TrainingProgram.id == programme_id))).scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Training programme not found")
    if file.content_type not in ("image/png", "image/jpeg"):
        raise HTTPException(status_code=400, detail="Signature must be PNG or JPEG")
    content = await file.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024: raise HTTPException(status_code=413, detail="Signature exceeds 2 MB")
    try:
        with Image.open(io.BytesIO(content)) as signature_image:
            signature_image.verify()
    except (UnidentifiedImageError, OSError):
        raise HTTPException(status_code=400, detail="Signature image could not be read")
    try:
        content = signature_png(content)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error))
    suffix = ".png"
    destination = UPLOAD_DIR / f"signature-programme-{programme_id}-{role_key}-{secrets.token_hex(6)}{suffix}"
    destination.write_bytes(content)
    record = (await db.execute(select(ProgrammeSignature).where(
        ProgrammeSignature.training_program_id == programme_id,
        ProgrammeSignature.role_key == role_key))).scalar_one_or_none()
    if record:
        record.signatory_name, record.title, record.image_path = signatory_name, title, str(destination)
        record.uploaded_by, record.uploaded_at = current_user.id, datetime.utcnow()
    else:
        db.add(ProgrammeSignature(training_program_id=programme_id, role_key=role_key,
                                  signatory_name=signatory_name, title=title, image_path=str(destination),
                                  uploaded_by=current_user.id))
    await db.commit()
    return {"detail": "Programme certificate signature saved"}


@app.get("/api/admin/reports/exams/{exam_id}/official.pdf", tags=["Reports"])
async def official_exam_report(exam_id: int, current_user: User = Depends(require_examiner), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(Exam, TrainingProgram, User.full_name).join(TrainingProgram, Exam.training_program_id == TrainingProgram.id)
                            .outerjoin(User, Exam.created_by == User.id).where(Exam.id == exam_id))).first()
    if not row: raise HTTPException(status_code=404, detail="Exam not found")
    exam, programme, examiner = row
    registered = (await db.execute(select(func.count(CandidateProfile.id)).where(CandidateProfile.training_program_id == exam.training_program_id))).scalar() or 0
    attempts = (await db.execute(select(ExamAttempt).where(ExamAttempt.exam_id == exam_id))).scalars().all()
    completed = [a for a in attempts if a.status in (AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED, AttemptStatus.APPROVED, AttemptStatus.REJECTED)]
    passed = sum(1 for a in completed if a.passed); failed = sum(1 for a in completed if a.passed is False)
    average = sum(a.percentage or 0 for a in completed) / len(completed) if completed else 0
    approval_dates = [a.approved_at for a in attempts if a.approved_at]
    data = {"examination": exam.title, "date": exam.start_time.strftime("%d %B %Y"), "programme": programme.name,
            "number_registered": registered, "number_present": len(completed), "number_absent": max(0, registered-len(completed)),
            "number_passed": passed, "number_failed": failed, "average_score": average,
            "pass_rate": passed/len(completed)*100 if completed else 0, "examiner": examiner or "Not assigned",
            "approval_date": max(approval_dates).strftime("%d %B %Y") if approval_dates else None}
    pdf = official_exam_report_pdf(data)
    return StreamingResponse(io.BytesIO(pdf), media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="official-exam-report-{exam_id}.pdf"'})


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("RELOAD", "false").lower() in ("1", "true", "yes")
    uvicorn.run("main:app", host=host, port=port, reload=reload)
