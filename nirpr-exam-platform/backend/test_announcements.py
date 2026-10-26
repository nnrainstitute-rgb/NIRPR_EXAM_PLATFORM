"""Announcements do not expose or mark security/system activity as read."""
from datetime import datetime, timedelta
from unittest.mock import Mock
import pytest
from fastapi import HTTPException
from sqlalchemy import select
import main
import schemas
from models import Notification, TrainingBatch, CandidateBatchAssignment, User, UserRole, CandidateProfile
from test_payment_requirements import setup


@pytest.mark.asyncio
async def test_candidate_feed_and_badge_only_show_announcements(setup):
    db,admin,candidate,*_=setup
    items=[Notification(user_id=candidate.id,title=kind,message="Test message",notification_type=kind)
           for kind in ("security","payment","exam_reminder","exam_access","announcement")]
    future=Notification(user_id=candidate.id,title="Future announcement",message="Not yet",notification_type="announcement",scheduled_for=datetime.utcnow()+timedelta(days=1))
    other=Notification(user_id=admin.id,title="Other user",message="Private",notification_type="announcement")
    db.add_all(items+[future,other])
    await db.commit()
    assert [row["title"] for row in await main.announcements(candidate,db)]==["announcement"]
    assert [row["title"] for row in await main.notifications(candidate,db)]==["announcement"]
    assert (await main.unread_announcement_count(candidate,db))["unread_count"]==1
    assert (await main.unread_notification_count(candidate,db))["unread_count"]==1
    await main.mark_announcements_read(candidate,db)
    for item in items+[future,other]:
        await db.refresh(item)
        assert (item.read_at is not None) == (item in items and item.notification_type=="announcement")
    assert (await main.unread_announcement_count(candidate,db))["unread_count"]==0


@pytest.mark.asyncio
async def test_staff_security_feed_is_preserved(setup):
    db,admin,*_=setup
    db.add(Notification(user_id=admin.id,title="Security",message="Security event",notification_type="security"))
    await db.commit()
    assert len(await main.notifications(admin,db))==1
    assert await main.announcements(admin,db)==[]
    await main.mark_announcements_read(admin,db)
    assert (await main.unread_notification_count(admin,db))["unread_count"]==1


@pytest.mark.asyncio
async def test_admin_publishes_to_batch_with_optional_email_and_history(setup,monkeypatch):
    db,admin,candidate,exam,other=setup
    batch=(await db.execute(select(CandidateBatchAssignment.batch_id).where(CandidateBatchAssignment.user_id==candidate.id))).scalar_one()
    outsider=User(email="outsider@example.test",first_name="Outside",surname="Batch",hashed_password="unused",role=UserRole.CANDIDATE)
    db.add(outsider)
    await db.flush()
    db.add(CandidateProfile(user_id=outsider.id,training_program_id=exam.training_program_id))
    await db.commit()
    sender=Mock()
    monkeypatch.setattr(main.email_utils,"send_general_notification_email",sender)
    result=await main.create_notifications(schemas.NotificationCreate(title="Admin announcement",message="Message to the selected batch",batch_id=batch,send_email=True,notification_type="security"),admin,db)
    assert result["created"]==1
    sender.assert_called_once()
    assert sender.call_args.args[0]==candidate.email
    rows=await main.announcements(candidate,db)
    assert rows[0]["notification_type"]=="announcement"
    assert await main.announcements(outsider,db)==[]
    history=await main.announcement_history(admin,db)
    assert history[0]["recipients"]==1
    assert history[0]["read_count"]==0
    await main.mark_announcements_read(candidate,db)
    assert (await main.announcement_history(admin,db))[0]["read_count"]==1


@pytest.mark.asyncio
async def test_all_candidate_audience_excludes_staff_and_email_is_opt_in(setup,monkeypatch):
    db,admin,candidate,*_=setup
    sender=Mock()
    monkeypatch.setattr(main.email_utils,"send_general_notification_email",sender)
    result=await main.create_notifications(schemas.NotificationCreate(title="Official notice",message="All active candidates"),admin,db)
    assert result["created"]==1
    sender.assert_not_called()
    assert await main.announcements(admin,db)==[]
    assert len(await main.announcements(candidate,db))==1


@pytest.mark.asyncio
async def test_invalid_audience_has_no_broadcast_fallback(setup):
    db,admin,*_=setup
    with pytest.raises(HTTPException) as error:
        await main.create_notifications(schemas.NotificationCreate(title="Official notice",message="Targeted message",batch_id=999999),admin,db)
    assert error.value.status_code==400
    assert await main.announcement_history(admin,db)==[]
