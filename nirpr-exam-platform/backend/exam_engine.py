"""
NIRPR RSO Examination Platform - Examination Engine
"""

import random
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from fastapi import HTTPException, status

from models import (
    Exam, Question, ExamAttempt, Answer, QuestionBank,
    AttemptStatus, ExamStatus, QuestionType, User,
    RetakeAuthorization, ExamScheduleOverride,
)
from schemas import AnswerSubmit
from exam_eligibility import require_exam_eligibility


class ExamEngine:
    @staticmethod
    async def generate_question_set(
        db: AsyncSession,
        exam: Exam,
        candidate_id: int
    ) -> List[int]:
        result = await db.execute(
            select(Question)
            .join(QuestionBank)
            .where(
                and_(
                    Question.question_bank_id.in_(
                        select(QuestionBank.id).where(
                            QuestionBank.training_program_id == exam.training_program_id
                        )
                    ),
                    Question.is_active == True
                )
            )
        )
        available_questions = result.scalars().all()

        if len(available_questions) < exam.questions_per_exam:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Not enough questions available. Required: {exam.questions_per_exam}, Available: {len(available_questions)}"
            )

        random.seed(datetime.utcnow().timestamp() + candidate_id)

        questions_by_difficulty = {"easy": [], "medium": [], "hard": []}
        for q in available_questions:
            questions_by_difficulty.get(q.difficulty, questions_by_difficulty["medium"]).append(q.id)

        total = exam.questions_per_exam
        easy_count = int(total * 0.3)
        hard_count = int(total * 0.2)
        medium_count = total - easy_count - hard_count

        selected = []
        for diff, count in [("easy", easy_count), ("medium", medium_count), ("hard", hard_count)]:
            # A question borrowed to cover an earlier difficulty shortage must
            # never be sampled again by its own difficulty group.
            pool = [qid for qid in questions_by_difficulty.get(diff, [])
                    if qid not in selected]
            selected.extend(random.sample(pool, min(count, len(pool))))

        if len(selected) < total:
            all_ids = [q.id for q in available_questions if q.id not in selected]
            needed = total - len(selected)
            if len(all_ids) >= needed:
                selected.extend(random.sample(all_ids, needed))

        if len(selected) != total or len(set(selected)) != total:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not generate a complete set of unique exam questions",
            )

        random.shuffle(selected)
        selected = selected[:total]

        return selected

    @staticmethod
    async def start_exam(
        db: AsyncSession,
        exam_id: int,
        candidate: User,
        ip_address: str,
        user_agent: str
    ) -> ExamAttempt:
        result = await db.execute(select(Exam).where(Exam.id == exam_id))
        exam = result.scalar_one_or_none()

        if not exam:
            raise HTTPException(status_code=404, detail="Exam not found")

        await require_exam_eligibility(db, exam, candidate)
        now = datetime.utcnow()

        # A per-candidate schedule override (set by an admin) replaces the
        # exam's normal window for this candidate only.
        window_start, window_end = exam.start_time, exam.end_time
        override_result = await db.execute(
            select(ExamScheduleOverride).where(
                ExamScheduleOverride.exam_id == exam_id,
                ExamScheduleOverride.user_id == candidate.id,
            )
        )
        override = override_result.scalar_one_or_none()
        if override:
            window_start, window_end = override.start_time, override.end_time

        if now < window_start:
            raise HTTPException(status_code=400, detail="This exam has not started yet")
        if now > window_end:
            if not override and exam.status not in (ExamStatus.COMPLETED, ExamStatus.ARCHIVED):
                exam.status = ExamStatus.COMPLETED
                await db.commit()
            raise HTTPException(status_code=400, detail="This exam window has already ended")
        if exam.status == ExamStatus.SCHEDULED:
            # Auto-activate: the scheduled start time has arrived, so there is no
            # need for an admin to manually flip a status switch.
            exam.status = ExamStatus.ACTIVE
            await db.commit()
        elif exam.status not in (ExamStatus.ACTIVE,):
            if not override:
                raise HTTPException(status_code=400, detail="Exam is not currently available")

        result = await db.execute(
            select(ExamAttempt).where(
                and_(
                    ExamAttempt.exam_id == exam_id,
                    ExamAttempt.user_id == candidate.id
                )
            ).order_by(ExamAttempt.attempt_number.desc())
        )
        attempts_so_far = result.scalars().all()
        latest = attempts_so_far[0] if attempts_so_far else None

        if latest and latest.status == AttemptStatus.IN_PROGRESS:
            return latest

        next_attempt_number = 1
        if latest:
            terminal = (AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED,
                        AttemptStatus.APPROVED, AttemptStatus.REJECTED)
            if latest.status in terminal:
                if len(attempts_so_far) < exam.max_attempts:
                    next_attempt_number = latest.attempt_number + 1
                else:
                    # Out of standard attempts — check for an admin-granted retake.
                    grant_result = await db.execute(
                        select(RetakeAuthorization).where(
                            RetakeAuthorization.exam_id == exam_id,
                            RetakeAuthorization.user_id == candidate.id,
                            RetakeAuthorization.used == False,
                        ).order_by(RetakeAuthorization.created_at.desc())
                    )
                    grant = grant_result.scalars().first()
                    if not grant:
                        raise HTTPException(
                            status_code=400,
                            detail="You have already completed this exam and used all your attempts. "
                                   "Contact NIRPR if you believe you should be allowed to retake it.",
                        )
                    grant.used = True
                    grant.used_at = datetime.utcnow()
                    next_attempt_number = latest.attempt_number + 1

        question_set = await ExamEngine.generate_question_set(db, exam, candidate.id)

        attempt = ExamAttempt(
            exam_id=exam_id,
            user_id=candidate.id,
            attempt_number=next_attempt_number,
            status=AttemptStatus.IN_PROGRESS,
            started_at=now,
            ip_address=ip_address,
            user_agent=user_agent,
            question_set=question_set,
            tab_switch_count=0,
            fullscreen_exit_count=0,
            suspicious_activity=[]
        )

        db.add(attempt)
        await db.commit()
        await db.refresh(attempt)

        return attempt

    @staticmethod
    async def get_exam_questions_for_candidate(
        db: AsyncSession,
        attempt: ExamAttempt,
        exam: Exam
    ) -> List[Dict[str, Any]]:
        if not attempt.question_set:
            raise HTTPException(status_code=400, detail="No question set assigned")

        # Repair question sets created by older selection logic, which could
        # contain a duplicate. Preserve the first occurrence (and its answer)
        # and replace only repeated positions with unused active questions.
        question_set = list(attempt.question_set)
        seen = set()
        duplicate_indexes = []
        for index, question_id in enumerate(question_set):
            if question_id in seen:
                duplicate_indexes.append(index)
            else:
                seen.add(question_id)

        if duplicate_indexes:
            replacement_result = await db.execute(
                select(Question.id)
                .join(QuestionBank)
                .where(
                    QuestionBank.training_program_id == exam.training_program_id,
                    Question.is_active == True,
                    Question.id.notin_(seen),
                )
            )
            replacements = list(replacement_result.scalars().all())
            random.shuffle(replacements)
            if len(replacements) < len(duplicate_indexes):
                raise HTTPException(
                    status_code=400,
                    detail="The exam question set contains duplicates and cannot be repaired because the question bank has too few unique questions",
                )
            for index, replacement_id in zip(duplicate_indexes, replacements):
                question_set[index] = replacement_id
                seen.add(replacement_id)
            attempt.question_set = question_set
            await db.commit()

        questions = []
        for idx, qid in enumerate(question_set):
            result = await db.execute(select(Question).where(Question.id == qid))
            question = result.scalar_one_or_none()
            if not question:
                continue

            options = (question.options or {}).copy()

            if exam.randomize_options and question.question_type in (QuestionType.MULTIPLE_CHOICE,):
                option_items = list(options.items())
                random.shuffle(option_items)
                options = dict(option_items)

            questions.append({
                "id": question.id,
                "question_text": question.question_text,
                "question_type": question.question_type.value,
                "options": options,
                "marks": question.marks,
                "difficulty": question.difficulty,
                "topic": question.topic,
                "order_index": idx + 1
            })

        return questions

    @staticmethod
    def _grade_text_answer(selected: List[str], correct_answer: List[str]) -> bool:
        """Grades short-answer / fill-in-the-gap questions. `correct_answer` has
        one entry per blank; each entry may contain several acceptable
        alternatives separated by '/'. All blanks must match (case-insensitive,
        whitespace-trimmed) for the question to be marked correct."""
        if not correct_answer:
            return False
        if len(selected) < len(correct_answer):
            selected = selected + [""] * (len(correct_answer) - len(selected))
        for i, expected in enumerate(correct_answer):
            given = (selected[i] if i < len(selected) else "").strip().lower()
            alternatives = {alt.strip().lower() for alt in expected.split("/") if alt.strip()}
            if not alternatives or given not in alternatives:
                return False
        return True

    @staticmethod
    async def submit_answer(
        db: AsyncSession,
        attempt: ExamAttempt,
        answer_data: AnswerSubmit
    ) -> Optional[Answer]:
        if attempt.status != AttemptStatus.IN_PROGRESS:
            raise HTTPException(status_code=400, detail="Exam is not in progress")

        if answer_data.question_id not in attempt.question_set:
            raise HTTPException(status_code=400, detail="Invalid question for this exam attempt")

        result = await db.execute(select(Question).where(Question.id == answer_data.question_id))
        question = result.scalar_one_or_none()
        if not question:
            raise HTTPException(status_code=404, detail="Question not found")

        result = await db.execute(
            select(Answer).where(
                and_(
                    Answer.attempt_id == attempt.id,
                    Answer.question_id == answer_data.question_id
                )
            )
        )
        existing = result.scalar_one_or_none()

        # Clearing a response must remove it from persisted answered counts.
        selected_raw = [str(a).strip() for a in (answer_data.selected_answer or [])]
        if not any(selected_raw):
            if existing:
                await db.delete(existing)
                await db.commit()
            return None

        if question.question_type in (QuestionType.SHORT_ANSWER, QuestionType.FILL_IN_GAP):
            # Free-text grading: compare each submitted blank/answer against the
            # acceptable answer(s) for that position. Each correct_answer entry
            # may itself list alternatives separated by "/" (e.g. "Shielding/Shield").
            selected = selected_raw
            is_correct = ExamEngine._grade_text_answer(selected, question.correct_answer)
            marks = question.marks if is_correct else 0.0
        else:
            selected = sorted([a.upper() for a in selected_raw if a])
            correct = sorted([a.upper().strip() for a in question.correct_answer])
            is_correct = selected == correct
            marks = question.marks if is_correct else 0.0

        if existing:
            existing.selected_answer = selected
            existing.is_correct = is_correct
            existing.marks_obtained = marks
            existing.time_spent_seconds = answer_data.time_spent_seconds
            existing.answered_at = datetime.utcnow()
            await db.commit()
            return existing
        else:
            answer = Answer(
                attempt_id=attempt.id,
                question_id=answer_data.question_id,
                selected_answer=selected,
                is_correct=is_correct,
                marks_obtained=marks,
                time_spent_seconds=answer_data.time_spent_seconds
            )
            db.add(answer)
            await db.commit()
            await db.refresh(answer)
            return answer

    @staticmethod
    async def finalize_exam(
        db: AsyncSession,
        attempt: ExamAttempt,
        time_spent_seconds: int,
        auto_submit: bool = False
    ) -> ExamAttempt:
        if attempt.status not in [AttemptStatus.IN_PROGRESS, AttemptStatus.NOT_STARTED]:
            raise HTTPException(status_code=400, detail="Exam already finalized")

        result = await db.execute(select(Answer).where(Answer.attempt_id == attempt.id))
        answers = result.scalars().all()

        result = await db.execute(select(Exam).where(Exam.id == attempt.exam_id))
        exam = result.scalar_one()

        # IMPORTANT: percentage must be computed against the marks actually
        # achievable on THIS candidate's question set, not exam.total_marks.
        # exam.total_marks is an admin-entered configuration value (defaults
        # to 100) that can drift out of sync with questions_per_exam and the
        # real marks of the selected questions (e.g. a 5-question exam whose
        # questions are worth 1 mark each has 5 achievable marks, not 100) —
        # using the configured value there produced wildly wrong percentages.
        question_ids = attempt.question_set or []
        achievable_marks = 0.0
        if question_ids:
            result = await db.execute(
                select(func.sum(Question.marks)).where(Question.id.in_(question_ids))
            )
            achievable_marks = result.scalar() or 0.0
        if achievable_marks <= 0:
            # Fallback for any attempt with no question_set on record.
            achievable_marks = exam.total_marks or 0.0

        total_score = sum(a.marks_obtained for a in answers)
        percentage = (total_score / achievable_marks * 100) if achievable_marks > 0 else 0.0
        passed = percentage >= exam.passing_score

        attempt.total_score = round(total_score, 2)
        attempt.total_possible_marks = round(achievable_marks, 2)
        attempt.percentage = round(percentage, 2)
        attempt.passed = passed
        attempt.time_spent_seconds = time_spent_seconds
        attempt.submitted_at = datetime.utcnow()

        if auto_submit:
            attempt.status = AttemptStatus.AUTO_SUBMITTED
        else:
            attempt.status = AttemptStatus.SUBMITTED

        await db.commit()
        await db.refresh(attempt)

        return attempt

    @staticmethod
    async def approve_result(
        db: AsyncSession,
        attempt: ExamAttempt,
        approver: User,
        approved: bool,
        notes: Optional[str] = None
    ) -> ExamAttempt:
        if attempt.status not in [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED]:
            raise HTTPException(status_code=400, detail="Attempt is not ready for approval")

        attempt.approved_by = approver.id
        attempt.approved_at = datetime.utcnow()
        attempt.approval_notes = notes
        attempt.result_released = approved

        if approved:
            attempt.status = AttemptStatus.APPROVED
        else:
            attempt.status = AttemptStatus.REJECTED

        await db.commit()
        await db.refresh(attempt)
        return attempt

    @staticmethod
    async def record_suspicious_activity(
        db: AsyncSession,
        attempt: ExamAttempt,
        activity_type: str,
        details: Dict[str, Any]
    ):
        if attempt.suspicious_activity is None:
            attempt.suspicious_activity = []

        attempt.suspicious_activity.append({
            "type": activity_type,
            "timestamp": datetime.utcnow().isoformat(),
            "details": details
        })

        if activity_type == "tab_switch":
            attempt.tab_switch_count += 1
        elif activity_type == "fullscreen_exit":
            attempt.fullscreen_exit_count += 1

        await db.commit()

    @staticmethod
    async def get_exam_analytics(db: AsyncSession, exam_id: int) -> Dict[str, Any]:
        result = await db.execute(select(Exam).where(Exam.id == exam_id))
        exam = result.scalar_one_or_none()
        if not exam:
            raise HTTPException(status_code=404, detail="Exam not found")

        completed_statuses = [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED, AttemptStatus.APPROVED, AttemptStatus.REJECTED]

        result = await db.execute(
            select(func.count(ExamAttempt.id)).where(ExamAttempt.exam_id == exam_id)
        )
        total_attempts = result.scalar() or 0

        result = await db.execute(
            select(ExamAttempt).where(
                and_(ExamAttempt.exam_id == exam_id, ExamAttempt.status.in_(completed_statuses))
            )
        )
        completed_attempts = result.scalars().all()
        completed = len(completed_attempts)

        percentages = [a.percentage or 0.0 for a in completed_attempts]
        avg_score = (sum(percentages) / completed) if completed else 0.0
        highest_score = max(percentages) if percentages else 0.0
        lowest_score = min(percentages) if percentages else 0.0
        passed_count = sum(1 for a in completed_attempts if a.passed)
        failed_count = completed - passed_count
        pass_rate = (passed_count / completed * 100) if completed > 0 else 0.0
        flagged_attempts = sum(
            1 for a in completed_attempts if (a.tab_switch_count or 0) > 0 or (a.fullscreen_exit_count or 0) > 0
        )

        # Score distribution in 10-point buckets (0-9, 10-19, ... 90-100)
        buckets = {f"{i}-{i+9}": 0 for i in range(0, 100, 10)}
        for p in percentages:
            idx = min(int(p // 10) * 10, 90)
            buckets[f"{idx}-{idx+9}"] += 1
        score_distribution = [{"range": k, "count": v} for k, v in buckets.items()]

        # Topic-level performance across every answer submitted for this exam's attempts
        topic_stats: Dict[str, Dict[str, int]] = {}
        difficulty_stats: Dict[str, Dict[str, int]] = {}
        if completed_attempts:
            attempt_ids = [a.id for a in completed_attempts]
            result = await db.execute(
                select(Answer, Question)
                .join(Question, Answer.question_id == Question.id)
                .where(Answer.attempt_id.in_(attempt_ids))
            )
            for answer, question in result.all():
                topic = question.topic or "Uncategorized"
                t = topic_stats.setdefault(topic, {"asked": 0, "correct": 0})
                t["asked"] += 1
                if answer.is_correct:
                    t["correct"] += 1

                diff = question.difficulty or "medium"
                d = difficulty_stats.setdefault(diff, {"asked": 0, "correct": 0})
                d["asked"] += 1
                if answer.is_correct:
                    d["correct"] += 1

        topic_performance = [
            {
                "topic": topic,
                "times_asked": stats["asked"],
                "times_correct": stats["correct"],
                "accuracy": round(stats["correct"] / stats["asked"] * 100, 1) if stats["asked"] else 0.0,
            }
            for topic, stats in sorted(topic_stats.items())
        ]
        difficulty_breakdown = {
            diff: {
                "times_asked": stats["asked"],
                "times_correct": stats["correct"],
                "accuracy": round(stats["correct"] / stats["asked"] * 100, 1) if stats["asked"] else 0.0,
            }
            for diff, stats in difficulty_stats.items()
        }

        return {
            "exam_id": exam_id,
            "exam_title": exam.title,
            "total_attempts": total_attempts,
            "completed_attempts": completed,
            "average_score": round(avg_score, 2),
            "pass_rate": round(pass_rate, 2),
            "highest_score": round(highest_score, 2),
            "lowest_score": round(lowest_score, 2),
            "passed_count": passed_count,
            "failed_count": failed_count,
            "score_distribution": score_distribution,
            "difficulty_breakdown": difficulty_breakdown,
            "topic_performance": topic_performance,
            "flagged_attempts": flagged_attempts,
        }

    @staticmethod
    async def get_attempt_script(db: AsyncSession, attempt: ExamAttempt) -> List[Dict[str, Any]]:
        """Full answer sheet for admin/examiner review: every question on this
        candidate's paper alongside what they selected, the correct answer,
        and marks awarded. Never exposed to the candidate themselves."""
        result = await db.execute(select(Answer).where(Answer.attempt_id == attempt.id))
        answers_by_qid = {a.question_id: a for a in result.scalars().all()}

        script = []
        for qid in (attempt.question_set or []):
            result = await db.execute(select(Question).where(Question.id == qid))
            question = result.scalar_one_or_none()
            if not question:
                continue
            answer = answers_by_qid.get(qid)
            script.append({
                "question_id": question.id,
                "question_text": question.question_text,
                "question_type": question.question_type.value,
                "topic": question.topic,
                "difficulty": question.difficulty,
                "options": question.options or {},
                "correct_answer": question.correct_answer or [],
                "selected_answer": (answer.selected_answer if answer else []) or [],
                "is_correct": answer.is_correct if answer else None,
                "marks_obtained": answer.marks_obtained if answer else 0.0,
                "marks_available": question.marks,
                "explanation": question.explanation,
            })
        return script

    @staticmethod
    def compute_risk_score(attempt: ExamAttempt) -> Dict[str, Any]:
        """A simple, transparent heuristic risk score from the signals we
        already record. This flags attempts for human review — it never
        fails a candidate automatically."""
        tab = attempt.tab_switch_count or 0
        fs = attempt.fullscreen_exit_count or 0
        events = attempt.suspicious_activity or []

        score = 0
        score += min(tab * 8, 40)
        score += min(fs * 12, 40)
        # Rapid-fire back-to-back events (within 5s) read as more suspicious
        # than isolated ones spread across the exam.
        timestamps = []
        for e in events:
            try:
                timestamps.append(datetime.fromisoformat(e["timestamp"]))
            except Exception:
                continue
        timestamps.sort()
        rapid = sum(
            1 for a, b in zip(timestamps, timestamps[1:])
            if (b - a).total_seconds() < 5
        )
        score += min(rapid * 5, 20)
        score = min(score, 100)

        if score >= 60:
            level = "high"
        elif score >= 25:
            level = "moderate"
        else:
            level = "low"

        return {"risk_score": score, "risk_level": level}

    @staticmethod
    async def get_attempt_timeline(db: AsyncSession, attempt: ExamAttempt) -> List[Dict[str, Any]]:
        events = list(attempt.suspicious_activity or [])
        timeline = []
        if attempt.started_at:
            timeline.append({"type": "exam_started", "timestamp": attempt.started_at.isoformat(), "details": {}})
        for e in events:
            timeline.append({
                "type": e.get("type", "unknown"),
                "timestamp": e.get("timestamp"),
                "details": e.get("details", {}),
            })
        if attempt.submitted_at:
            event_type = "exam_auto_submitted" if attempt.status == AttemptStatus.AUTO_SUBMITTED else "exam_submitted"
            timeline.append({"type": event_type, "timestamp": attempt.submitted_at.isoformat(), "details": {}})
        timeline.sort(key=lambda e: e["timestamp"] or "")
        return timeline
