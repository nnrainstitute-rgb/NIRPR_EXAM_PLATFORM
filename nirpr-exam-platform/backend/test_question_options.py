"""Admin option balancing preserves answer meaning and candidate history."""
import random
from collections import Counter
import pytest
from sqlalchemy import select
from models import Question, QuestionBank, QuestionType, ExamAttempt, AttemptStatus, Answer
from question_options import balanced_correct_labels, redistribute_options
from rebalance_generated_questions import rebalance, candidate_fingerprint
from test_payment_requirements import setup


@pytest.mark.parametrize("count",[4,15,100])
def test_balanced_random_labels(count):
    labels=balanced_correct_labels(count,rng=random.Random(7))
    counts=Counter(labels)
    assert max(counts.values())-min(counts.values())<=1
    assert set(counts)==set("ABCD")


@pytest.mark.parametrize("old",list("ABCD"))
@pytest.mark.parametrize("target",list("ABCD"))
def test_reposition_preserves_correct_text_and_labels(old,target):
    options={key:"Choice "+key for key in "ABCD"}
    updated,correct=redistribute_options(options,[old],target,rng=random.Random(9))
    assert list(updated)==list("ABCD")
    assert sorted(updated.values())==sorted(options.values())
    assert updated[correct[0]]==options[old]
    assert options=={key:"Choice "+key for key in "ABCD"}


@pytest.mark.asyncio
async def test_rebalance_preserves_used_question_and_candidates(setup,tmp_path):
    db,admin,candidate,exam,other=setup
    bank=QuestionBank(training_program_id=exam.training_program_id,name="Generated Starter Question Bank",created_by=admin.id)
    db.add(bank);await db.flush()
    questions=[Question(question_bank_id=bank.id,question_text=f"Question {i}",question_type=QuestionType.MULTIPLE_CHOICE,
                        options={"A":f"Correct {i}","B":"Wrong 1","C":"Wrong 2","D":"Wrong 3"},correct_answer=["A"])
               for i in range(8)]
    db.add_all(questions);await db.flush()
    attempt=ExamAttempt(user_id=candidate.id,exam_id=exam.id,status=AttemptStatus.APPROVED,question_set=[questions[0].id])
    db.add(attempt);await db.flush()
    db.add(Answer(attempt_id=attempt.id,question_id=questions[0].id,selected_answer=["A"],is_correct=True,marks_obtained=1))
    await db.commit()
    initial=await candidate_fingerprint(db)
    preview=await rebalance(db,apply=False)
    assert preview["planned"]==8
    assert questions[0].is_active is True
    result=await rebalance(db,apply=True,backup_dir=tmp_path)
    assert result["updated"]==8
    assert result["historical_versions_preserved"]==1
    assert questions[0].correct_answer==["A"]
    assert questions[0].options["A"]=="Correct 0"
    assert questions[0].is_active is False
    assert await candidate_fingerprint(db)==initial
    active=(await db.execute(select(Question).where(Question.question_bank_id==bank.id,Question.is_active==True))).scalars().all()
    assert len(active)==8
    assert Counter(q.correct_answer[0] for q in active)==dict.fromkeys("ABCD",2)
    for question in active:
        assert list(question.options)==list("ABCD")
        assert question.options[question.correct_answer[0]].startswith("Correct ")
    assert (await rebalance(db,apply=True,backup_dir=tmp_path))["planned"]==0


@pytest.mark.asyncio
async def test_live_attempt_prevents_rebalance(setup,tmp_path):
    db,admin,candidate,exam,other=setup
    db.add(ExamAttempt(exam_id=exam.id,user_id=candidate.id,status=AttemptStatus.IN_PROGRESS))
    await db.commit()
    with pytest.raises(RuntimeError,match="in-progress"):
        await rebalance(db,apply=True,backup_dir=tmp_path)
