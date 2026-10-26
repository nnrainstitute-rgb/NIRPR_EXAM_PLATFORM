"""Batch membership controls both notifications and admission."""
from datetime import datetime, timedelta
from unittest.mock import Mock
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from models import (User, UserRole, CandidateProfile, TrainingBatch, CandidateBatchAssignment,
                    ExamBatchAssignment, ExamAttempt, AttemptStatus, TrainingProgram, Exam, QuestionBank, Question)
from exam_eligibility import eligible_candidate_ids
import main
import schemas
from test_payment_requirements import setup


async def make_batch(db, admin, exam, name="Second batch"):
    batch=TrainingBatch(training_program_id=exam.training_program_id,name=name,created_by=admin.id)
    db.add(batch)
    await db.commit()
    return batch


@pytest.mark.asyncio
async def test_batch_reassignment_changes_visibility_and_email_audience(setup,monkeypatch):
    db,admin,candidate,exam,other=setup
    sender=Mock()
    monkeypatch.setattr(main.email_utils,"send_exam_schedule_email",sender)
    second=await make_batch(db,admin,exam)
    assignment=(await db.execute(select(ExamBatchAssignment).where(ExamBatchAssignment.exam_id==other.id))).scalar_one()
    assignment.batch_id=second.id
    await db.commit()
    assert candidate.id in await eligible_candidate_ids(db,exam)
    assert candidate.id not in await eligible_candidate_ids(db,other)
    await main.notify_exam_schedule(db,other)
    sender.assert_not_called()
    await main.assign_training_batch(schemas.CandidateBatchUpdate(batch_id=second.id,user_ids=[candidate.id]),admin,db)
    assert candidate.id not in await eligible_candidate_ids(db,exam)
    assert candidate.id in await eligible_candidate_ids(db,other)
    assert sender.call_count==1
    assert sender.call_args.args[2]==other.title
    sender.reset_mock()
    await main.notify_exam_schedule(db,exam,is_update=True)
    sender.assert_not_called()
    result=await main.assign_training_batch(schemas.CandidateBatchUpdate(batch_id=second.id,user_ids=[candidate.id]),admin,db)
    assert result["changed"]==0
    sender.assert_not_called()
    rows=await main.list_batch_candidates(exam.training_program_id,admin,db)
    assert rows[0]["batch_id"]==second.id


@pytest.mark.asyncio
async def test_previously_passed_candidate_needs_renewal_even_in_new_programme(setup,monkeypatch):
    db,admin,candidate,exam,other=setup
    monkeypatch.setattr(main.email_utils,"send_exam_schedule_email",Mock())
    programme=TrainingProgram(code="PRIOR",name="Previous course",practice_area="Prior")
    db.add(programme)
    await db.flush()
    prior=Exam(training_program_id=programme.id,title="Passed exam",start_time=datetime.utcnow()-timedelta(days=10),
               end_time=datetime.utcnow()-timedelta(days=9),status=exam.status)
    db.add(prior)
    await db.flush()
    db.add(ExamAttempt(exam_id=prior.id,user_id=candidate.id,status=AttemptStatus.APPROVED,passed=True))
    await db.commit()
    assert candidate.id not in await eligible_candidate_ids(db,exam)
    await main.set_renewal_approval(exam.id,candidate.id,
        schemas.ExamRenewalApprovalUpdate(approved=True,reason="Annual renewal approved"),admin,db)
    assert candidate.id in await eligible_candidate_ids(db,exam)
    assert candidate.id not in await eligible_candidate_ids(db,other)


@pytest.mark.asyncio
async def test_existing_attempt_survives_reassignment_but_receives_no_old_batch_mail(setup,monkeypatch):
    db,admin,candidate,exam,other=setup
    db.add(ExamAttempt(exam_id=exam.id,user_id=candidate.id,status=AttemptStatus.IN_PROGRESS))
    await db.commit()
    second=await make_batch(db,admin,exam)
    sender=Mock()
    monkeypatch.setattr(main.email_utils,"send_exam_schedule_email",sender)
    await main.assign_training_batch(schemas.CandidateBatchUpdate(batch_id=second.id,user_ids=[candidate.id]),admin,db)
    assert candidate.id in await eligible_candidate_ids(db,exam)
    await main.notify_exam_schedule(db,exam,is_update=True)
    sender.assert_not_called()


@pytest.mark.asyncio
async def test_assignment_rejects_other_programme_atomically(setup):
    db,admin,candidate,exam,other=setup
    programme=TrainingProgram(code="FOREIGN",name="Foreign",practice_area="Other")
    db.add(programme)
    await db.flush()
    batch=TrainingBatch(training_program_id=programme.id,name="Foreign batch",created_by=admin.id)
    db.add(batch)
    await db.commit()
    before=(await db.execute(select(CandidateBatchAssignment.batch_id).where(CandidateBatchAssignment.user_id==candidate.id))).scalar_one()
    with pytest.raises(HTTPException) as error:
        await main.assign_training_batch(schemas.CandidateBatchUpdate(batch_id=batch.id,user_ids=[candidate.id]),admin,db)
    assert error.value.status_code==400
    assert (await db.execute(select(CandidateBatchAssignment.batch_id).where(CandidateBatchAssignment.user_id==candidate.id))).scalar_one()==before


@pytest.mark.asyncio
async def test_unassigned_legacy_exam_requires_batch_and_batch_cannot_change_after_attempt(setup,monkeypatch):
    db,admin,candidate,exam,other=setup
    assignment=(await db.execute(select(ExamBatchAssignment).where(ExamBatchAssignment.exam_id==exam.id))).scalar_one()
    batch=assignment.batch_id
    await db.delete(assignment)
    await db.commit()
    assert candidate.id not in await eligible_candidate_ids(db,exam)
    # Use an awaitable notification stub for this update.
    from unittest.mock import AsyncMock
    monkeypatch.setattr(main,"notify_exam_schedule",AsyncMock())
    await main.update_exam(exam.id,schemas.ExamUpdate(batch_id=batch),admin,db)
    assert candidate.id in await eligible_candidate_ids(db,exam)
    db.add(ExamAttempt(exam_id=exam.id,user_id=candidate.id))
    await db.commit()
    with pytest.raises(HTTPException) as error:
        await main.update_exam(exam.id,schemas.ExamUpdate(batch_id=batch),admin,db)
    assert error.value.status_code==400


@pytest.mark.asyncio
async def test_create_exam_sends_only_to_selected_batch(setup,monkeypatch):
    db,admin,candidate,exam,other=setup
    sender=Mock()
    monkeypatch.setattr(main.email_utils,"send_exam_schedule_email",sender)
    batch=await make_batch(db,admin,exam)
    second_user=User(email="second@example.test",first_name="Second",surname="Candidate",hashed_password="unused",role=UserRole.CANDIDATE)
    db.add(second_user)
    bank=QuestionBank(training_program_id=exam.training_program_id,name="Bank")
    db.add(bank)
    await db.flush()
    db.add(CandidateProfile(user_id=second_user.id,training_program_id=exam.training_program_id))
    db.add(CandidateBatchAssignment(user_id=second_user.id,batch_id=batch.id,assigned_by=admin.id))
    db.add(Question(question_bank_id=bank.id,question_text="Question",options={"A":"Answer"},correct_answer="A"))
    await db.commit()
    result=await main.create_exam(schemas.ExamCreate(training_program_id=exam.training_program_id,batch_id=batch.id,
        title="Batch exam",questions_per_exam=1,question_bank_ids=[bank.id],start_time=exam.start_time,end_time=exam.end_time),admin,db)
    assert sender.call_count==1
    assert sender.call_args.args[0]==second_user.email
    assert candidate.id not in await eligible_candidate_ids(db,await db.get(Exam,result.id))


@pytest.mark.asyncio
async def test_programme_with_batches_is_deactivated_not_deleted(setup):
    db,admin,*_=setup
    programme=TrainingProgram(code="EMPTY",name="Batch only",practice_area="Other")
    db.add(programme)
    await db.flush()
    db.add(TrainingBatch(training_program_id=programme.id,name="Unused batch",created_by=admin.id))
    await db.commit()
    result=await main.delete_training_program(programme.id,admin,db)
    assert "deactivated" in result.detail
    await db.refresh(programme)
    assert programme.is_active is False
