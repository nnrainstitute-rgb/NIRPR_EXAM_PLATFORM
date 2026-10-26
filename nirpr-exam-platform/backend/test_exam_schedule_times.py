from datetime import datetime

import pytest

from schemas import ExamCreate, ExamUpdate, RescheduleRequest


@pytest.mark.parametrize("start,expected", [
    ("2026-10-24T14:30:00Z", datetime(2026, 10, 24, 14, 30)),
    ("2026-10-24T15:30:00+01:00", datetime(2026, 10, 24, 14, 30)),
    ("2026-10-24T09:30:00-05:00", datetime(2026, 10, 24, 14, 30)),
    ("2026-10-24T00:30:00+01:00", datetime(2026, 10, 23, 23, 30)),
    ("2026-10-24T14:30:00", datetime(2026, 10, 24, 14, 30)),
])
@pytest.mark.parametrize("kind", [ExamCreate, ExamUpdate, RescheduleRequest])
def test_schedule_inputs_normalize_to_database_utc(kind, start, expected):
    payload = {"start_time": start, "end_time": start}
    if kind is ExamCreate:
        payload.update(batch_id=1, training_program_id=1, title="Test exam", question_bank_ids=[1])
    if kind is RescheduleRequest:
        payload["user_ids"] = [1]
    data = kind(**payload)
    assert data.start_time == expected
    assert data.end_time == expected
    assert data.model_dump()["start_time"].tzinfo is None
    assert data.model_dump()["end_time"].tzinfo is None


def test_partial_exam_update_preserves_omitted_times():
    data = ExamUpdate(title="Changed title")
    assert "start_time" not in data.model_dump(exclude_unset=True)
    assert "end_time" not in data.model_dump(exclude_unset=True)
