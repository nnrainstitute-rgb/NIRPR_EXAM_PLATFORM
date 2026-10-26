from datetime import datetime
import pytest
import main
from certificate_utils import certificate_number
from models import CandidateProfile, ExamAttempt, AttemptStatus
from sqlalchemy import select
from test_payment_requirements import setup


@pytest.mark.asyncio
async def test_dashboard_number_matches_existing_certificate_year(setup):
    db,admin,candidate,exam,other=setup
    candidate.email="candidate@example.org"
    attempt=ExamAttempt(exam_id=exam.id,user_id=candidate.id,status=AttemptStatus.APPROVED,
        result_released=True,passed=True,approved_at=datetime(2025,12,31))
    db.add(attempt);await db.commit()
    detail=await main.me(candidate,db)
    assert detail.dashboard_candidate_number==certificate_number("TEST",attempt.approved_at,attempt.id,candidate.id)
    assert "/2025/" in detail.dashboard_candidate_number


@pytest.mark.asyncio
async def test_candidate_without_certificate_uses_same_format(setup):
    db,admin,candidate,exam,other=setup
    candidate.email="candidate@example.org"
    detail=await main.me(candidate,db)
    assert detail.dashboard_candidate_number==certificate_number("TEST",datetime.utcnow(),0,candidate.id)
    assert len(detail.dashboard_candidate_number.split("/"))==6


@pytest.mark.asyncio
async def test_unreleased_result_does_not_change_display_number(setup):
    db,admin,candidate,exam,other=setup
    candidate.email="candidate@example.org"
    db.add(ExamAttempt(exam_id=exam.id,user_id=candidate.id,status=AttemptStatus.APPROVED,
        result_released=False,passed=True,approved_at=datetime(2023,1,1)))
    await db.commit()
    assert (await main.me(candidate,db)).dashboard_candidate_number==certificate_number("TEST",datetime.utcnow(),0,candidate.id)


@pytest.mark.asyncio
async def test_staff_has_no_dashboard_candidate_number(setup):
    db,admin,*_=setup
    admin.email="admin@example.org"
    assert (await main.me(admin,db)).dashboard_candidate_number is None
