"""Staff login works without CAPTCHA while preserving authentication checks."""
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

import main
import schemas
from models import AuthSession, StaffLoginApproval, StaffMFA, UserRole
from test_payment_requirements import setup


def staff_request():
    return Request({
        "type": "http", "method": "POST", "path": "/api/auth/login",
        "headers": [], "client": ("127.0.0.1", 1234),
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.EXAMINER])
async def test_staff_login_without_captcha(setup, monkeypatch, role):
    db, staff, *_ = setup
    staff.email = "staff@example.com"
    staff.role = role
    staff.hashed_password = main.get_password_hash("ValidPassword123!")
    await db.commit()
    monkeypatch.setattr(main.email_utils, "send_general_notification_email", Mock())

    result = await main.login(schemas.LoginRequest(
        email=staff.email, password="ValidPassword123!", portal="staff"
    ), staff_request(), db)

    if role == UserRole.SUPER_ADMIN:
        assert result.access_token
        assert result.user_role == role.value
        assert (await db.execute(select(AuthSession).where(
            AuthSession.user_id == staff.id
        ))).scalar_one()
    else:
        assert result["approval_required"] is True
        approval = await db.get(StaffLoginApproval, result["request_id"])
        assert approval.user_id == staff.id
        assert approval.status == "pending"


@pytest.mark.asyncio
async def test_staff_login_without_captcha_still_checks_password_and_mfa(setup):
    db, staff, *_ = setup
    staff.email = "staff@example.com"
    staff.hashed_password = main.get_password_hash("ValidPassword123!")
    db.add(StaffMFA(user_id=staff.id, secret="JBSWY3DPEHPK3PXP", enabled=True))
    await db.commit()

    for password, expected in [
        ("WrongPassword123!", "Incorrect email or password"),
        ("ValidPassword123!", "OTP_REQUIRED"),
    ]:
        with pytest.raises(HTTPException) as error:
            await main.login(schemas.LoginRequest(
                email=staff.email, password=password, portal="staff"
            ), staff_request(), db)
        assert error.value.status_code == 401
        assert expected in error.value.detail
