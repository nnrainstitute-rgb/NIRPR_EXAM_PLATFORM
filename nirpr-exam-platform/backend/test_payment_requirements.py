"""Per-exam receipt gates use disposable databases only."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import text, select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

import main
import schemas
from database import Base
from models import User, UserRole, TrainingProgram, CandidateProfile, Exam, ExamStatus, ExamPolicy, PaymentReceipt, ExamAccessGrant, ExamAttempt, TrainingBatch, CandidateBatchAssignment, ExamBatchAssignment


@pytest_asyncio.fixture
async def setup(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        admin = User(email="admin@example.test", hashed_password="unused", surname="Admin", first_name="Test", role=UserRole.ADMIN)
        candidate = User(email="candidate@example.test", hashed_password="unused", surname="Candidate", first_name="Test", role=UserRole.CANDIDATE)
        programme = TrainingProgram(code="TEST", name="Test programme", practice_area="Test")
        db.add_all([admin, candidate, programme])
        await db.flush()
        db.add(CandidateProfile(user_id=candidate.id, training_program_id=programme.id))
        exam = Exam(training_program_id=programme.id, title="Test exam", start_time=datetime.utcnow()-timedelta(minutes=5), end_time=datetime.utcnow()+timedelta(hours=3), status=ExamStatus.ACTIVE, created_by=admin.id)
        other = Exam(training_program_id=programme.id, title="Other exam", start_time=exam.start_time, end_time=exam.end_time, status=ExamStatus.ACTIVE, created_by=admin.id)
        db.add_all([exam, other])
        batch=TrainingBatch(training_program_id=programme.id,name="Test batch",created_by=admin.id)
        db.add(batch)
        await db.flush()
        db.add_all([CandidateBatchAssignment(user_id=candidate.id,batch_id=batch.id,assigned_by=admin.id),
                    ExamBatchAssignment(exam_id=exam.id,batch_id=batch.id),
                    ExamBatchAssignment(exam_id=other.id,batch_id=batch.id)])
        await db.commit()
        monkeypatch.setattr(main, "log_audit", AsyncMock())
        yield db, admin, candidate, exam, other
    await engine.dispose()


@pytest.mark.asyncio
async def test_toggle_updates_lists_preserves_policy_and_other_exam(setup):
    db, admin, candidate, exam, other = setup
    db.add(ExamPolicy(exam_id=exam.id, payment_required=False, allow_resume=False))
    await db.commit()
    for required in (True, False):
        await main.update_payment_requirement(exam.id, schemas.ExamPaymentRequirement(payment_required=required), admin, db)
        listed = {row.id: row for row in await main.list_exams(None, candidate, db)}
        assert listed[exam.id].payment_required is required
        assert listed[exam.id].can_start is not required
        assert listed[other.id].can_start is True
        access = await main.list_exam_access(None, admin, db)
        assert next(row for row in access if row["exam_id"] == exam.id)["can_start"] is not required
    await main.update_payment_requirement(exam.id, schemas.ExamPaymentRequirement(payment_required=True), admin, db)
    policy = await main.update_exam_policy(exam.id, schemas.ExamPolicyUpdate(block_printing=False), admin, db)
    assert policy["payment_required"] is True
    assert policy["allow_resume"] is False
    assert policy["block_printing"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("required,access", [(None,None),(False,None),(True,"started"),(True,None),(True,"pending"),(True,"rejected"),(True,"approved"),(True,"grant")])
async def test_start_gate_matches_candidate_list(setup,monkeypatch,required,access):
    db,admin,candidate,exam,other=setup
    if required is not None:
        await main.update_payment_requirement(exam.id, schemas.ExamPaymentRequirement(payment_required=required),admin,db)
    if access == "started":
        db.add(ExamAttempt(user_id=candidate.id, exam_id=exam.id))
    elif access == "grant":
        db.add(ExamAccessGrant(user_id=candidate.id,exam_id=exam.id,granted_by=admin.id))
    elif access:
        db.add(PaymentReceipt(user_id=candidate.id,exam_id=exam.id,status=access,file_name="test.pdf",stored_path="unused"))
    await db.commit()
    allowed = not required or access in ("approved","grant","started")
    listed={row.id:row for row in await main.list_exams(None,candidate,db)}
    assert listed[exam.id].can_start is allowed
    # Stop at the engine boundary: verify the payment gate without sitting an exam.
    engine_start=AsyncMock(side_effect=RuntimeError("engine reached"))
    monkeypatch.setattr(main.ExamEngine,"start_exam",engine_start)
    request=Request({"type":"http","headers":[],"client":("127.0.0.1",1234)})
    if allowed:
        with pytest.raises(RuntimeError,match="engine reached"):
            await main.start_exam(exam.id,request,candidate,db)
        engine_start.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as error:
            await main.start_exam(exam.id,request,candidate,db)
        assert error.value.status_code == 403
        engine_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_exam_is_rejected(setup):
    db,admin,*_=setup
    with pytest.raises(HTTPException) as error:
        await main.update_payment_requirement(99999,schemas.ExamPaymentRequirement(payment_required=True),admin,db)
    assert error.value.status_code == 404



@pytest.mark.asyncio
async def test_delete_unused_exam_cleans_dependents_with_foreign_keys(setup):
    from models import ExamScheduleOverride, RetakeAuthorization, QuestionBank, Question, exam_question_association
    db,admin,candidate,exam,other=setup
    db.add_all([
        ExamPolicy(exam_id=exam.id, payment_required=True),
        ExamPolicy(exam_id=other.id, payment_required=True),
        ExamAccessGrant(exam_id=exam.id, user_id=candidate.id, granted_by=admin.id),
        ExamScheduleOverride(exam_id=exam.id, user_id=candidate.id, created_by=admin.id,
                             start_time=exam.start_time,end_time=exam.end_time),
        RetakeAuthorization(exam_id=exam.id,user_id=candidate.id,authorized_by=admin.id),
    ])
    bank=QuestionBank(training_program_id=exam.training_program_id,name="Test bank")
    db.add(bank)
    await db.flush()
    question=Question(question_bank_id=bank.id,question_text="Question?",options={"A":"Yes"},correct_answer="A")
    db.add(question)
    await db.flush()
    await db.execute(exam_question_association.insert().values(exam_id=exam.id,question_id=question.id,order_index=0))
    await db.commit()
    result=await main.delete_exam(exam.id,admin,db)
    assert result.detail == "Exam deleted."
    assert (await db.execute(select(Exam.id).where(Exam.id==exam.id))).scalar_one_or_none() is None
    assert (await db.execute(select(ExamPolicy.exam_id))).scalars().all() == [other.id]
    for model in (ExamAccessGrant,ExamScheduleOverride,RetakeAuthorization):
        assert (await db.execute(select(func.count()).select_from(model))).scalar_one() == 0
    assert (await db.execute(select(Question.id))).scalar_one() == question.id
    assert (await db.execute(select(func.count()).select_from(exam_question_association))).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ["attempt","receipt","report"])
async def test_delete_exam_with_history_archives_without_losing_records(setup,history):
    from models import SavedReport
    db,admin,candidate,exam,other=setup
    policy=ExamPolicy(exam_id=exam.id,payment_required=True)
    if history=="attempt":
        record=ExamAttempt(exam_id=exam.id,user_id=candidate.id)
    elif history=="receipt":
        record=PaymentReceipt(exam_id=exam.id,user_id=candidate.id,file_name="receipt.pdf",stored_path="unused")
    else:
        record=SavedReport(exam_id=exam.id,title="Saved report",report_data={},created_by=admin.id)
    db.add_all([policy,record])
    await db.commit()
    response=await main.delete_exam(exam.id,admin,db)
    assert "archived" in response.detail
    await db.refresh(exam)
    assert exam.status == ExamStatus.ARCHIVED
    await db.refresh(record)
    await db.refresh(policy)
    assert record.exam_id == exam.id
    assert policy.payment_required is True
