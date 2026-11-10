"""Programme registration, exam audience and renewal govern admission separately from payment."""
from sqlalchemy import select
from fastapi import HTTPException
from models import (CandidateProfile, User, UserRole, ExamAttempt, ExamRenewalApproval,
                    CandidateBatchAssignment, ExamBatchAssignment)


async def candidate_history(db, programme_id=None, exclude_exam_id=None):
    # New means never attempted any exam, even in another programme.
    query = select(ExamAttempt.user_id)
    if exclude_exam_id is not None:
        query = query.where(ExamAttempt.exam_id != exclude_exam_id)
    return set((await db.execute(query)).scalars().all())


async def batch_candidate_ids(db, exam):
    return set((await db.execute(select(User.id).join(
        CandidateProfile, CandidateProfile.user_id == User.id
    ).join(CandidateBatchAssignment, CandidateBatchAssignment.user_id == User.id).join(
        ExamBatchAssignment, ExamBatchAssignment.batch_id == CandidateBatchAssignment.batch_id
    ).where(ExamBatchAssignment.exam_id == exam.id,
            CandidateProfile.training_program_id == exam.training_program_id,
            User.role == UserRole.CANDIDATE, User.is_active == True))).scalars().all())


async def programme_candidate_ids(db, exam):
    return set((await db.execute(select(User.id).join(
        CandidateProfile, CandidateProfile.user_id == User.id
    ).where(CandidateProfile.training_program_id == exam.training_program_id,
            User.is_active == True, User.role == UserRole.CANDIDATE))).scalars().all())


async def exam_audience_ids(db, exam):
    if exam.programme_wide:
        return await programme_candidate_ids(db, exam)
    return await batch_candidate_ids(db, exam)


async def eligible_candidate_ids(db, exam):
    candidates = await exam_audience_ids(db, exam)
    returning = await candidate_history(db)
    current = set((await db.execute(select(ExamAttempt.user_id).where(
        ExamAttempt.exam_id == exam.id))).scalars().all())
    approved = set((await db.execute(select(ExamRenewalApproval.user_id).where(
        ExamRenewalApproval.exam_id == exam.id))).scalars().all())
    # Reassignment must not strand an existing attempt or erase its history.
    active_programme = candidates if exam.programme_wide else await programme_candidate_ids(db, exam)
    return (candidates & ((candidates - returning) | approved)) | (current & active_programme)


async def require_exam_eligibility(db, exam, candidate):
    if candidate.id not in await eligible_candidate_ids(db, exam):
        raise HTTPException(status_code=403, detail=(
            "You are not eligible for this examination. Check your programme registration, "
            "training batch if required, and renewal approval if you have previously attempted an exam."
        ))
