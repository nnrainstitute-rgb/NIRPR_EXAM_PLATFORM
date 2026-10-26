"""
NIRPR RSO Examination Platform - Email Utility

Sends transactional email (registration verification links, password reset
links, and generated credentials for CSV-imported users) over Brevo HTTPS or SMTP.

DEV MODE: if neither BREVO_API_KEY nor SMTP_HOST is configured, emails are not sent over the wire.
Instead they are written to the `outbox/` folder as .html files and logged
to stdout, so the whole registration / verification / reset flow can be
exercised end-to-end on a laptop with no mail server configured. Set the
SMTP_* environment variables (see README) to send real email in production.
"""

import os
import smtplib
import ssl
import logging
import httpx
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

logger = logging.getLogger("nirpr.email")
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
EMAIL_FROM = os.getenv("EMAIL_FROM", "").strip()
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "NIRPR RSO Examination Platform")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
# Google displays app passwords in four-character groups separated by spaces.
# SMTP authentication expects the same 16 characters without that formatting.
SMTP_PASSWORD = "".join(os.getenv("SMTP_PASSWORD", "").split())
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() != "false"
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "noreply@nirpr.gov.ng")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "NIRPR RSO Examination Platform")

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")
APP_NAME = os.getenv("APP_NAME", "NIRPR RSO Examination Platform")

OUTBOX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outbox")


def _dev_dump(to_email: str, subject: str, html_body: str):
    """Write the email to disk so it can be inspected when no SMTP server
    is configured. Also prints the plaintext-ish content to the console."""
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
    safe_to = "".join(c for c in to_email if c.isalnum() or c in "@._-")
    path = os.path.join(OUTBOX_DIR, f"{stamp}_{safe_to}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<!-- To: {to_email} | Subject: {subject} -->\n{html_body}")
    logger.warning(
        "SMTP not configured — email NOT sent. Saved to %s (To: %s, Subject: %s)",
        path, to_email, subject,
    )
    print(f"\n[DEV EMAIL] To: {to_email}\nSubject: {subject}\nSaved: {path}\n")


def send_email(to_email: str, subject: str, html_body: str, text_body: str = None) -> bool:
    """Send an email. Returns True if accepted by Brevo or dispatched over SMTP,
    False if it fell back to dev-mode file output (still 'succeeds' from
    the caller's point of view — registration/reset flows should not fail
    just because SMTP isn't configured on a dev machine)."""
    if BREVO_API_KEY:
        if not EMAIL_FROM:
            logger.error("Brevo requires EMAIL_FROM to match a verified sender.")
            return False
        payload = {"sender": {"email": EMAIL_FROM, "name": EMAIL_FROM_NAME},
                   "to": [{"email": to_email}], "subject": subject, "htmlContent": html_body}
        if text_body:
            payload["textContent"] = text_body
        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.post("https://api.brevo.com/v3/smtp/email",
                    headers={"api-key": BREVO_API_KEY, "accept": "application/json"}, json=payload)
            response.raise_for_status()
            return True
        except httpx.HTTPError:
            # Do not log provider bodies or dump private links on delivery errors.
            # Avoid automatic retries: a timed-out request may already be accepted.
            logger.error("Brevo email delivery failed; check provider logs, quota and sender verification.")
            return False
    if not SMTP_HOST:
        _dev_dump(to_email, subject, html_body)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM}>"
    msg["To"] = to_email

    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        if SMTP_USE_TLS:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.starttls(context=ssl.create_default_context())
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        else:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        return True
    except Exception as e:
        logger.error("Failed to send email to %s: %s", to_email, e)
        _dev_dump(to_email, subject, html_body)
        return False


def _wrapper(inner_html: str) -> str:
    return f"""
    <div style="font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; max-width:560px; margin:0 auto; padding:24px; color:#1c2530;">
      <div style="background:#0b2545; padding:18px 24px; border-radius:8px 8px 0 0;">
        <span style="color:#c8971f; font-weight:800; font-size:15px;">{APP_NAME}</span>
      </div>
      <div style="border:1px solid #dfe3e6; border-top:none; padding:26px 24px; border-radius:0 0 8px 8px;">
        {inner_html}
      </div>
      <p style="font-size:11.5px; color:#8a96a3; text-align:center; margin-top:18px;">
        This is an automated message from {APP_NAME}. Please do not reply directly to this email.
      </p>
    </div>
    """


def _format_expiry(expires_at: datetime) -> str:
    """Human-readable 'expires in X' + an exact timestamp, so the reader
    knows precisely how long they have without needing a live countdown
    (email clients can't run JavaScript)."""
    remaining = expires_at - datetime.utcnow()
    total_minutes = max(0, int(remaining.total_seconds() // 60))
    if total_minutes >= 120:
        duration = f"{total_minutes // 60} hours"
    elif total_minutes >= 60:
        duration = "1 hour"
    else:
        duration = f"{total_minutes} minutes"
    exact = expires_at.strftime("%H:%M UTC on %d %B %Y")
    return f"{duration} (by {exact})"


def send_verification_email(to_email: str, full_name: str, token: str, expires_at: datetime = None):
    link = f"{APP_BASE_URL}/verify-email?token={token}"
    expiry_text = _format_expiry(expires_at) if expires_at else "24 hours"
    html = _wrapper(f"""
      <h2 style="margin-top:0;">Confirm your email address</h2>
      <p>Hello {full_name},</p>
      <p>Thank you for registering on the {APP_NAME}. Please confirm your email address to
      activate your account and gain access to your certification exams.</p>
      <p style="text-align:center; margin:26px 0;">
        <a href="{link}" style="background:#046a38; color:#fff; padding:12px 22px; border-radius:7px;
           text-decoration:none; font-weight:700; display:inline-block;">Verify my email</a>
      </p>
      <p style="font-size:12.5px; color:#5c6b78;">Or copy this link into your browser:<br>
      <a href="{link}">{link}</a></p>
      <p style="font-size:12.5px; color:#5c6b78; font-weight:600;">&#9203; This link expires in {expiry_text}.</p>
      <p style="font-size:12.5px; color:#5c6b78;">If you did not
      create this account, you can safely ignore this email.</p>
    """)
    return send_email(to_email, f"Verify your email — {APP_NAME}", html)


def send_password_reset_email(to_email: str, full_name: str, token: str, expires_at: datetime = None):
    link = f"{APP_BASE_URL}/reset-password?token={token}"
    expiry_text = _format_expiry(expires_at) if expires_at else "60 minutes"
    html = _wrapper(f"""
      <h2 style="margin-top:0;">Reset your password</h2>
      <p>Hello {full_name},</p>
      <p>We received a request to reset the password for your account. Click the button below
      to choose a new password.</p>
      <p style="text-align:center; margin:26px 0;">
        <a href="{link}" style="background:#046a38; color:#fff; padding:12px 22px; border-radius:7px;
           text-decoration:none; font-weight:700; display:inline-block;">Reset my password</a>
      </p>
      <p style="font-size:12.5px; color:#5c6b78;">Or copy this link into your browser:<br>
      <a href="{link}">{link}</a></p>
      <p style="font-size:12.5px; color:#5c6b78; font-weight:600;">&#9203; This link expires in {expiry_text}.
      Once you open it, the page will show a live countdown of the time you have left.</p>
      <p style="font-size:12.5px; color:#5c6b78;">If you did not request this, you can safely
      ignore this email — your password will not change.</p>
    """)
    return send_email(to_email, f"Reset your password — {APP_NAME}", html)


def send_credentials_email(to_email: str, full_name: str, password: str, role: str):
    link = f"{APP_BASE_URL}/"
    html = _wrapper(f"""
      <h2 style="margin-top:0;">Your account has been created</h2>
      <p>Hello {full_name},</p>
      <p>An administrator has created an account for you on the {APP_NAME} as
      <strong>{role.replace('_',' ')}</strong>. Your login details are below:</p>
      <table style="width:100%; margin:18px 0; font-size:14px;">
        <tr><td style="padding:6px 0; color:#5c6b78;">Email</td><td style="padding:6px 0; font-weight:700;">{to_email}</td></tr>
        <tr><td style="padding:6px 0; color:#5c6b78;">Temporary password</td><td style="padding:6px 0; font-weight:700;">{password}</td></tr>
      </table>
      <p style="text-align:center; margin:26px 0;">
        <a href="{link}" style="background:#046a38; color:#fff; padding:12px 22px; border-radius:7px;
           text-decoration:none; font-weight:700; display:inline-block;">Log in now</a>
      </p>
      <p style="font-size:12.5px; color:#5c6b78;">For your security, please log in and change this
      password as soon as possible.</p>
    """)
    send_email(to_email, f"Your account has been created — {APP_NAME}", html)


def send_result_notification_email(to_email: str, full_name: str, exam_title: str, passed: bool):
    outcome = "Congratulations — you have passed" if passed else "Your result has been released"
    html = _wrapper(f"""
      <h2 style="margin-top:0;">{outcome}</h2>
      <p>Hello {full_name},</p>
      <p>Your result for <strong>{exam_title}</strong> has been reviewed and released by NIRPR.
      Log in to your candidate portal to view your full result.</p>
      <p style="text-align:center; margin:26px 0;">
        <a href="{APP_BASE_URL}/" style="background:#046a38; color:#fff; padding:12px 22px; border-radius:7px;
           text-decoration:none; font-weight:700; display:inline-block;">View my result</a>
      </p>
    """)
    send_email(to_email, f"Your exam result is available — {APP_NAME}", html)


def send_exam_schedule_email(to_email: str, full_name: str, exam_title: str,
                             start_time: datetime, end_time: datetime, duration_minutes: int,
                             is_update: bool = False):
    action = "has been updated" if is_update else "has been scheduled"
    login_url = f"{APP_BASE_URL.rstrip('/')}/"
    html = _wrapper(f"""
      <h2 style="margin-top:0;">Examination schedule notification</h2>
      <p>Hello {full_name},</p><p>Your examination <strong>{exam_title}</strong> {action}.</p>
      <table style="width:100%;margin:18px 0;font-size:14px;">
        <tr><td style="padding:7px;color:#5c6b78;">Starts</td><td><strong>{start_time.strftime('%A, %d %B %Y at %I:%M %p')}</strong></td></tr>
        <tr><td style="padding:7px;color:#5c6b78;">Closes</td><td><strong>{end_time.strftime('%A, %d %B %Y at %I:%M %p')}</strong></td></tr>
        <tr><td style="padding:7px;color:#5c6b78;">Duration</td><td><strong>{duration_minutes} minutes</strong></td></tr>
      </table><p>Please sign in before the scheduled start time and confirm that your payment approval and device are ready.</p>
      <p style="text-align:center;margin:26px 0;"><a href="{login_url}" style="background:#046a38;color:#fff;padding:12px 22px;border-radius:7px;text-decoration:none;font-weight:700;">Log in to your examination portal</a></p>
      <p>If the button does not open, click or copy this login link into your browser:<br>
        <a href="{login_url}">{login_url}</a></p>
      <p>Sign in with your registered email address and password.</p>
    """)
    text = (f"Hello {full_name},\n\nYour examination {exam_title} {action}.\n"
            f"Starts: {start_time.strftime('%A, %d %B %Y at %I:%M %p')}\n"
            f"Closes: {end_time.strftime('%A, %d %B %Y at %I:%M %p')}\n"
            f"Duration: {duration_minutes} minutes\n\n"
            f"Log in to your examination portal: {login_url}\n"
            "Sign in with your registered email address and password.")
    send_email(to_email, f"Exam schedule: {exam_title} — {APP_NAME}", html, text_body=text)


def send_general_notification_email(to_email: str, full_name: str, title: str, message: str):
    html = _wrapper(f"""<h2 style="margin-top:0;">{title}</h2><p>Hello {full_name},</p><p>{message}</p>
      <p style="text-align:center;margin:26px 0;"><a href="{APP_BASE_URL}/" style="background:#046a38;color:#fff;padding:12px 22px;border-radius:7px;text-decoration:none;font-weight:700;">Open candidate portal</a></p>""")
    send_email(to_email, f"{title} — {APP_NAME}", html)
