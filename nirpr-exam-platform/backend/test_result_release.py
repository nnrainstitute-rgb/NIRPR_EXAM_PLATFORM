from datetime import datetime, timedelta
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import main
from database import Base
from models import AuditLog, AttemptStatus, Exam, ExamAttempt, ExamScheduleOverride, TrainingProgram, User, UserRole


@pytest.mark.asyncio
async def test_release_schedule_and_individual_results(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await check_schedule_release(db, monkeypatch)
    finally:
        await engine.dispose()


async def check_schedule_release(db, monkeypatch):
    admin = User(email="staff@example.test", hashed_password="unused", surname="Staff",
                 first_name="Test", role=UserRole.ADMIN)
    general = User(email="general@example.test", hashed_password="unused", surname="General",
                   first_name="Test", role=UserRole.CANDIDATE)
    custom = User(email="custom@example.test", hashed_password="unused", surname="Custom",
                  first_name="Test", role=UserRole.CANDIDATE)
    programme = TrainingProgram(code="TEST", name="Test programme", practice_area="Test")
    db.add_all([admin, general, custom, programme])
    await db.flush()
    start = datetime(2026, 11, 1, 9)
    end = start + timedelta(hours=2)
    exam = Exam(training_program_id=programme.id, title="Exam", start_time=start, end_time=end)
    other_exam = Exam(training_program_id=programme.id, title="Other exam", start_time=start, end_time=end)
    db.add_all([exam, other_exam])
    await db.flush()

    custom_start = start + timedelta(days=1)
    custom_end = end + timedelta(days=1)
    db.add(ExamScheduleOverride(exam_id=exam.id, user_id=custom.id, start_time=custom_start,
                               end_time=custom_end, created_by=admin.id))
    cases = [
        (exam, general, AttemptStatus.SUBMITTED),
        (exam, general, AttemptStatus.AUTO_SUBMITTED),
        (exam, general, AttemptStatus.IN_PROGRESS),
        (exam, general, AttemptStatus.REJECTED),
        (exam, custom, AttemptStatus.SUBMITTED),
        (exam, custom, AttemptStatus.AUTO_SUBMITTED),
        (other_exam, general, AttemptStatus.SUBMITTED),
    ]
    attempts = [
        ExamAttempt(exam_id=scheduled_exam.id, user_id=candidate.id, status=status, attempt_number=index)
        for index, (scheduled_exam, candidate, status) in enumerate(cases, 1)
    ]
    db.add_all(attempts)
    await db.commit()

    notify = Mock()
    monkeypatch.setattr(main.email_utils, "send_result_notification_email", notify)
    monkeypatch.setitem(main.app.dependency_overrides, main.get_db, lambda: db)
    monkeypatch.setitem(main.app.dependency_overrides, main.require_examiner, lambda: admin)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        path = f"/api/admin/exams/{exam.id}/release-results"
        assert (await client.post(path, params={"schedule_key": "all"})).status_code == 400
        queue = await client.get("/api/admin/attempts", params={
            "exam_id": exam.id, "schedule_key": "general", "status_filter": "pending",
        })
        assert {row["attempt_id"] for row in queue.json()} == {attempts[0].id, attempts[1].id}
        released = await client.post(path, params={"schedule_key": "general"})
        assert released.json()["released_count"] == 2
        assert (await client.post(path, params={"schedule_key": "general"})).json()["released_count"] == 0
        assert notify.call_count == 2

        rejected = await client.post("/api/admin/attempts/approve", json={
            "attempt_id": attempts[5].id, "action": "reject", "notes": "Reviewed separately",
        })
        assert rejected.json()["status"] == "rejected"
        custom_key = f"custom:{custom_start.isoformat()}|{custom_end.isoformat()}"
        assert (await client.post(path, params={"schedule_key": custom_key})).json()["released_count"] == 1

    rows = (await db.scalars(select(ExamAttempt).order_by(ExamAttempt.id))).all()
    assert [row.result_released for row in rows] == [True, True, False, False, True, False, False]
    assert rows[0].approved_by == admin.id and rows[0].approved_at is not None
    assert rows[2].status == AttemptStatus.IN_PROGRESS
    assert rows[5].approval_notes == "Reviewed separately"
    assert rows[6].status == AttemptStatus.SUBMITTED
    assert notify.call_count == 3
    audit = (await db.scalars(select(AuditLog).where(AuditLog.action == "schedule_results_released")
                              .order_by(AuditLog.id))).all()
    assert [row.details for row in audit] == [
        {"schedule_key": "general", "released_count": 2},
        {"schedule_key": custom_key, "released_count": 1},
    ]
