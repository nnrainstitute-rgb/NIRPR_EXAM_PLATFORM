"""Balance generated admin question banks without rewriting candidate history.

Run without arguments to inspect; pass --apply to save changes and a JSON backup.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import select, func, text

load_dotenv(Path(__file__).with_name(".env"))
from database import AsyncSessionLocal, engine
from models import (QuestionBank, Question, QuestionType, ExamAttempt, Answer, User, UserRole,
                    QuestionRevision, AuditLog, exam_question_association)
from question_options import balanced_correct_labels, redistribute_options

GENERATED_NAMES = ("NWLST Comprehensive Question Bank (100 Questions)", "Generated Starter Question Bank")


def snapshot(question):
    return {key: getattr(question,key) for key in (
        "question_bank_id","question_text","options","correct_answer","explanation",
        "marks","difficulty","topic","is_active")} | {"question_type":question.question_type.value}


async def candidate_fingerprint(db):
    digest=hashlib.sha256()
    for model in (ExamAttempt,Answer):
        rows=(await db.execute(select(model).order_by(model.id))).scalars().all()
        for row in rows:
            digest.update(json.dumps({column.name:getattr(row,column.name) for column in model.__table__.columns},
                                     default=str,sort_keys=True).encode())
    return digest.hexdigest()


async def rebalance(db, apply=False, backup_dir=None):
    if apply and db.bind.dialect.name=="postgresql":
        await db.execute(text("LOCK TABLE questions, exam_attempts, answers IN SHARE ROW EXCLUSIVE MODE"))
    banks=(await db.execute(select(QuestionBank).where(QuestionBank.name.in_(GENERATED_NAMES)))).scalars().all()
    attempts=(await db.execute(select(ExamAttempt))).scalars().all()
    if any(attempt.status.value=="in_progress" for attempt in attempts):
        raise RuntimeError("Wait until all in-progress attempts finish before rebalancing generated banks")
    used={qid for attempt in attempts for qid in (attempt.question_set or [])}
    used.update((await db.execute(select(Answer.question_id))).scalars().all())
    used.update((await db.execute(select(exam_question_association.c.question_id))).scalars().all())
    plans=[]
    summary=[]
    for bank in banks:
        rows=(await db.execute(select(Question).where(Question.question_bank_id==bank.id,Question.is_active==True)
                               .order_by(Question.id))).scalars().all()
        rows=[q for q in rows if q.question_type==QuestionType.MULTIPLE_CHOICE and
              set(q.options or {})==set("ABCD") and len(q.correct_answer or [])==1 and q.correct_answer[0] in q.options]
        distribution=Counter(q.correct_answer[0] for q in rows)
        balanced=bool(rows) and max(distribution.get(key,0) for key in "ABCD")-min(distribution.get(key,0) for key in "ABCD")<=1
        summary.append({"bank_id":bank.id,"question_count":len(rows),"before":dict(distribution),"already_balanced":balanced})
        if balanced:continue
        for question,target in zip(rows,balanced_correct_labels(len(rows))):
            options,correct=redistribute_options(question.options,question.correct_answer,target)
            plans.append((question,options,correct))
    if not apply or not plans:
        return {"banks":summary,"planned":len(plans),"applied":False}
    actor=(await db.execute(select(User).where(User.role.in_([UserRole.SUPER_ADMIN,UserRole.ADMIN]),User.is_active==True)
                           .order_by(User.id))).scalars().first()
    if actor is None:raise RuntimeError("An active administrator is required for revision audit records")
    before=await candidate_fingerprint(db)
    directory=Path(backup_dir or Path(__file__).parent/"backups")
    directory.mkdir(parents=True,exist_ok=True)
    backup=directory/f"generated-question-options-{datetime.utcnow().strftime('%Y%m%dT%H%M%S%f')}.json"
    backup.write_text(json.dumps({"questions":[{"id":q.id,**snapshot(q)} for q,_,_ in plans],
                                 "candidate_history_sha256":before},indent=2),encoding="utf-8")
    archived=0
    for question,options,correct in plans:
        previous=snapshot(question)
        if question.id in used:
            revised=Question(**{key:value for key,value in previous.items() if key not in ("question_type","options","correct_answer")},
                             question_type=question.question_type,options=options,correct_answer=correct)
            db.add(revised);await db.flush()
            question.is_active=False
            previous["replacement_question_id"]=revised.id
            archived+=1
        else:
            question.options=options;question.correct_answer=correct
        version=(await db.execute(select(func.max(QuestionRevision.version)).where(QuestionRevision.question_id==question.id))).scalar() or 0
        db.add(QuestionRevision(question_id=question.id,version=version+1,action="options_rebalanced",
                                changed_by=actor.id,snapshot=previous))
    await db.flush()
    if await candidate_fingerprint(db)!=before:
        raise RuntimeError("Candidate history changed during rebalance; transaction will be rolled back")
    db.add(AuditLog(action="generated_options_rebalanced",user_id=actor.id,entity_type="question_bank",
                    details={"questions":len(plans),"historical_versions_preserved":archived,"backup":backup.name,
                             "source":"local maintenance requested by owner"}))
    await db.commit()
    after=[]
    for bank in banks:
        answers=(await db.execute(select(Question.correct_answer).where(Question.question_bank_id==bank.id,
                                   Question.is_active==True,Question.question_type==QuestionType.MULTIPLE_CHOICE))).scalars().all()
        after.append({"bank_id":bank.id,"distribution":dict(Counter(answer[0] for answer in answers if len(answer)==1))})
    return {"banks":summary,"applied":True,"updated":len(plans),"historical_versions_preserved":archived,
            "candidate_history_unchanged":True,"after":after,"backup":str(backup)}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument("--apply",action="store_true");args=parser.parse_args()
    try:
        async with AsyncSessionLocal() as db:
            print(json.dumps(await rebalance(db,apply=args.apply),indent=2))
    finally:await engine.dispose()


if __name__=="__main__":asyncio.run(main())
