"""Eligibility and notification regression tests on disposable databases."""
from unittest.mock import Mock, AsyncMock
from datetime import datetime, timedelta
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import select, func

import main
import schemas
from exam_engine import ExamEngine
from exam_eligibility import eligible_candidate_ids
from models import (User, UserRole, CandidateProfile, ExamAttempt, AttemptStatus,
                    ExamPolicy, ExamRenewalApproval, Notification, TrainingProgram, Exam, CandidateBatchAssignment, ExamBatchAssignment)
from test_payment_requirements import setup


async def add_returning(db, exam):
    user=User(email="returning@example.test",first_name="Returning",surname="Candidate",hashed_password="unused",role=UserRole.CANDIDATE)
    db.add(user)
    await db.flush()
    db.add(CandidateProfile(user_id=user.id,training_program_id=exam.training_program_id))
    batch=(await db.execute(select(ExamBatchAssignment.batch_id).where(ExamBatchAssignment.exam_id==exam.id))).scalar_one()
    db.add(CandidateBatchAssignment(user_id=user.id,batch_id=batch,assigned_by=exam.created_by))
    db.add(ExamAttempt(exam_id=exam.id,user_id=user.id,status=AttemptStatus.APPROVED))
    await db.commit()
    return user


@pytest.mark.asyncio
async def test_only_new_and_invited_receive_schedule_email(setup,monkeypatch):
    db,admin,new,old_exam,new_exam=setup
    returning=await add_returning(db,old_exam)
    sender=Mock()
    monkeypatch.setattr(main.email_utils,"send_exam_schedule_email",sender)
    await main.notify_exam_schedule(db,new_exam)
    assert [call.args[0] for call in sender.call_args_list]==[new.email]
    assert (await db.execute(select(Notification.user_id))).scalars().all()==[new.id]
    data=schemas.ExamRenewalApprovalUpdate(approved=True,reason="Approved renewal application")
    await main.set_renewal_approval(new_exam.id,returning.id,data,admin,db)
    assert sender.call_args_list[-1].args[0]==returning.email
    count=sender.call_count
    await main.set_renewal_approval(new_exam.id,returning.id,data,admin,db)
    assert sender.call_count==count
    assert (await db.execute(select(func.count()).select_from(ExamRenewalApproval))).scalar_one()==1
    sender.reset_mock()
    await main.notify_exam_schedule(db,new_exam,is_update=True)
    assert {call.args[0] for call in sender.call_args_list}=={new.email,returning.email}


@pytest.mark.asyncio
async def test_returning_hidden_and_blocked_until_invited_payment_still_applies(setup,monkeypatch):
    db,admin,new,old_exam,new_exam=setup
    returning=await add_returning(db,old_exam)
    monkeypatch.setattr(main.email_utils,"send_exam_schedule_email",Mock())
    assert new_exam.id not in {row.id for row in await main.list_exams(None,returning,db)}
    assert old_exam.id in {row.id for row in await main.list_exams(None,returning,db)}
    request=Request({"type":"http","headers":[],"client":("127.0.0.1",1234)})
    with pytest.raises(HTTPException) as error:
        await main.start_exam(new_exam.id,request,returning,db)
    assert error.value.status_code==403
    # Direct engine calls also enforce programme eligibility.
    with pytest.raises(HTTPException) as error:
        await ExamEngine.start_exam(db,new_exam.id,returning,"127.0.0.1","")
    assert error.value.status_code==403
    await main.set_renewal_approval(new_exam.id,returning.id,
        schemas.ExamRenewalApprovalUpdate(approved=True,reason="Approved renewal"),admin,db)
    db.add(ExamPolicy(exam_id=new_exam.id,payment_required=True))
    await db.commit()
    rows={row.id:row for row in await main.list_exams(None,returning,db)}
    assert rows[new_exam.id].can_start is False
    with pytest.raises(HTTPException) as error:
        await main.start_exam(new_exam.id,request,returning,db)
    assert "payment receipt" in error.value.detail
    await main.update_payment_requirement(new_exam.id,schemas.ExamPaymentRequirement(payment_required=False),admin,db)
    engine=AsyncMock(side_effect=RuntimeError("eligible engine reached"))
    monkeypatch.setattr(ExamEngine,"start_exam",engine)
    with pytest.raises(RuntimeError,match="eligible engine reached"):
        await main.start_exam(new_exam.id,request,returning,db)
    await main.set_renewal_approval(new_exam.id,returning.id,
        schemas.ExamRenewalApprovalUpdate(approved=False,reason="Withdrawn application"),admin,db)
    assert returning.id not in await eligible_candidate_ids(db,new_exam)


@pytest.mark.asyncio
async def test_invitation_cannot_be_revoked_after_attempt(setup,monkeypatch):
    db,admin,new,old_exam,new_exam=setup
    returning=await add_returning(db,old_exam)
    monkeypatch.setattr(main.email_utils,"send_exam_schedule_email",Mock())
    await main.set_renewal_approval(new_exam.id,returning.id,
        schemas.ExamRenewalApprovalUpdate(approved=True,reason="Approved renewal"),admin,db)
    db.add(ExamAttempt(user_id=returning.id,exam_id=new_exam.id,status=AttemptStatus.IN_PROGRESS))
    await db.commit()
    with pytest.raises(HTTPException) as error:
        await main.set_renewal_approval(new_exam.id,returning.id,
            schemas.ExamRenewalApprovalUpdate(approved=False,reason="Withdraw request"),admin,db)
    assert error.value.status_code==409
    assert returning.id in await eligible_candidate_ids(db,new_exam)


@pytest.mark.asyncio
async def test_cross_programme_not_eligible_or_invitable(setup):
    db,admin,candidate,exam,other=setup
    programme=TrainingProgram(code="OTHER",name="Other programme",practice_area="Other")
    db.add(programme)
    await db.flush()
    foreign_exam=Exam(training_program_id=programme.id,title="Foreign exam",start_time=datetime.utcnow(),
                     end_time=datetime.utcnow()+timedelta(hours=3),status=exam.status)
    db.add(foreign_exam)
    await db.commit()
    assert candidate.id not in await eligible_candidate_ids(db,foreign_exam)
    with pytest.raises(HTTPException) as error:
        await main.set_renewal_approval(foreign_exam.id,candidate.id,
            schemas.ExamRenewalApprovalUpdate(approved=True,reason="Should be blocked"),admin,db)
    assert error.value.status_code==404
