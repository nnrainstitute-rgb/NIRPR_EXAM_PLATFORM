"""Candidate roster previews are read-only; apply rechecks membership rules."""
import io
from datetime import datetime
from unittest.mock import Mock
import pytest
from fastapi import HTTPException, UploadFile
from openpyxl import Workbook
from sqlalchemy import select
import main
import schemas
from roster_import import parse_roster_file
from models import (User, UserRole, CandidateProfile, CandidateBatchAssignment,
                    ExamBatchAssignment, TrainingBatch)
from test_payment_requirements import setup


@pytest.mark.parametrize("filename,content",[
    ("candidates.csv",b"email\ncandidate@example.test\n"),
    ("candidates.csv",b"\xef\xbb\xbfEmail Address,name\nCANDIDATE@example.test,Test\n"),
])
def test_csv_parser(filename,content):
    assert parse_roster_file(content,filename)==[{"row":2,"email":"candidate@example.test"}]


def test_excel_parser():
    book=Workbook();sheet=book.active;sheet.append(["email","name"]);sheet.append(["CANDIDATE@example.test","Test"])
    output=io.BytesIO();book.save(output);book.close()
    assert parse_roster_file(output.getvalue(),"candidates.xlsx")==[{"row":2,"email":"candidate@example.test"}]


@pytest.mark.parametrize("filename,content",[("candidates.xls",b"invalid"),("candidates.xlsx",b"invalid"),("candidates.csv",b"name\nTest\n"),("candidates.csv",b"email\n")])
def test_invalid_files_rejected(filename,content):
    with pytest.raises(HTTPException):parse_roster_file(content,filename)


def test_over_500_candidates_rejected():
    with pytest.raises(HTTPException) as error:
        parse_roster_file(b"email\n"+b"candidate@example.test\n"*501,"candidates.csv")
    assert error.value.status_code==413


@pytest.mark.asyncio
async def test_preview_flags_rows_without_changing_assignments(setup):
    db,admin,candidate,exam,other=setup
    before=(await db.execute(select(CandidateBatchAssignment.batch_id))).scalars().all()
    content=("email\nCANDIDATE@example.test\ncandidate@example.test\nunknown@example.test\nadmin@example.test\n").encode()
    result=await main.preview_exam_roster(exam.id,UploadFile(filename="roster.csv",file=io.BytesIO(content)),admin,db)
    assert result["matched"]==1
    assert result["rows"][0]["user_id"]==candidate.id
    assert result["rows"][0]["selectable"] is True
    assert "Duplicate" in result["rows"][1]["status"]
    assert "No account" in result["rows"][2]["status"]
    assert "not a candidate" in result["rows"][3]["status"]
    assert (await db.execute(select(CandidateBatchAssignment.batch_id))).scalars().all()==before


@pytest.mark.asyncio
async def test_selected_import_updates_batch_roster_and_assigned_count(setup,monkeypatch):
    db,admin,candidate,exam,other=setup
    sender=Mock();monkeypatch.setattr(main.email_utils,"send_exam_schedule_email",sender)
    batch=TrainingBatch(training_program_id=exam.training_program_id,name="Imported cohort",created_by=admin.id)
    db.add(batch);await db.commit()
    # Editing the exam switches its candidate list to the newly selected batch.
    await main.update_exam(exam.id,schemas.ExamUpdate(batch_id=batch.id),admin,db)
    assert await main.list_exam_candidates(exam.id,admin,db)==[]
    new=User(email="selected@example.test",first_name="Selected",surname="Candidate",hashed_password="unused",role=UserRole.CANDIDATE)
    unselected=User(email="unselected@example.test",first_name="Unselected",surname="Candidate",hashed_password="unused",role=UserRole.CANDIDATE)
    db.add_all([new,unselected]);await db.flush()
    db.add_all([CandidateProfile(user_id=new.id,training_program_id=exam.training_program_id),CandidateProfile(user_id=unselected.id,training_program_id=exam.training_program_id)])
    await db.commit()
    preview=await main.preview_exam_roster(exam.id,UploadFile(filename="roster.csv",file=io.BytesIO(b"email\nselected@example.test\nunselected@example.test\n")),admin,db)
    assert preview["matched"]==2
    sender.reset_mock()
    await main.apply_exam_roster(exam.id,schemas.ExamRosterApply(batch_id=batch.id,user_ids=[new.id]),admin,db)
    rows=await main.list_exam_candidates(exam.id,admin,db)
    assert [row["user_id"] for row in rows]==[new.id]
    assert sender.call_count==1
    assert sender.call_args.args[0]==new.email
    listing={row.id:row for row in await main.list_exams(None,admin,db)}
    assert listing[exam.id].assigned_candidates==1
    assert listing[exam.id].registered_candidates==0
    assert (await db.execute(select(CandidateBatchAssignment).where(CandidateBatchAssignment.user_id==unselected.id))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_changed_exam_batch_rejects_old_preview(setup,monkeypatch):
    db,admin,candidate,exam,other=setup
    monkeypatch.setattr(main.email_utils,"send_exam_schedule_email",Mock())
    old_batch=(await db.execute(select(ExamBatchAssignment.batch_id).where(ExamBatchAssignment.exam_id==exam.id))).scalar_one()
    batch=TrainingBatch(training_program_id=exam.training_program_id,name="Different batch",created_by=admin.id)
    db.add(batch);await db.commit()
    await main.update_exam(exam.id,schemas.ExamUpdate(batch_id=batch.id),admin,db)
    with pytest.raises(HTTPException) as error:
        await main.apply_exam_roster(exam.id,schemas.ExamRosterApply(batch_id=old_batch,user_ids=[candidate.id]),admin,db)
    assert error.value.status_code==409


@pytest.mark.parametrize("filename,content",[
    ("candidates.tsv",b"email\tname\ncandidate@example.test\tTest\n"),
    ("candidates.txt",b"email;name\ncandidate@example.test;Test\n"),
    ("candidates.tsv","email\tname\ncandidate@example.test\tTest\n".encode("utf-16")),
])
def test_additional_delimited_formats(filename,content):
    assert parse_roster_file(content,filename)==[{"row":2,"email":"candidate@example.test"}]


def test_xlsm_reader_uses_first_worksheet():
    book=Workbook();book.active.append(["email"]);book.active.append(["candidate@example.test"])
    output=io.BytesIO();book.save(output);book.close()
    assert parse_roster_file(output.getvalue(),"candidates.xlsm")==[{"row":2,"email":"candidate@example.test"}]


def test_legacy_excel_reader_and_resource_cleanup(monkeypatch):
    import xlrd
    from types import SimpleNamespace
    from unittest.mock import Mock
    release=Mock()
    sheet=SimpleNamespace(nrows=2,row_values=lambda index:[["email"],["candidate@example.test"]][index])
    book=SimpleNamespace(sheet_by_index=lambda index:sheet,release_resources=release)
    opener=Mock(return_value=book)
    monkeypatch.setattr(xlrd,"open_workbook",opener)
    assert parse_roster_file(b"legacy workbook","candidates.xls")==[{"row":2,"email":"candidate@example.test"}]
    opener.assert_called_once_with(file_contents=b"legacy workbook",on_demand=True)
    release.assert_called_once()


@pytest.mark.asyncio
async def test_batch_import_without_scheduled_exam_preview_then_select(setup,monkeypatch):
    db,admin,candidate,exam,other=setup
    sender=Mock();monkeypatch.setattr(main.email_utils,"send_exam_schedule_email",sender)
    batch=TrainingBatch(training_program_id=exam.training_program_id,name="File batch",created_by=admin.id)
    db.add(batch);await db.commit()
    original=(await db.execute(select(CandidateBatchAssignment.batch_id).where(CandidateBatchAssignment.user_id==candidate.id))).scalar_one()
    preview=await main.preview_batch_roster(batch.id,UploadFile(filename="candidates.tsv",file=io.BytesIO(b"email\nCANDIDATE@example.test\nunknown@example.test\n")),admin,db)
    assert preview["matched"]==1
    assert (await db.execute(select(CandidateBatchAssignment.batch_id).where(CandidateBatchAssignment.user_id==candidate.id))).scalar_one()==original
    response=await main.apply_batch_roster(batch.id,schemas.ExamRosterApply(batch_id=batch.id,user_ids=[candidate.id]),admin,db)
    assert response["changed"]==1
    assert (await db.execute(select(CandidateBatchAssignment.batch_id).where(CandidateBatchAssignment.user_id==candidate.id))).scalar_one()==batch.id
    sender.assert_not_called()
    with pytest.raises(HTTPException) as error:
        await main.apply_batch_roster(batch.id,schemas.ExamRosterApply(batch_id=original,user_ids=[candidate.id]),admin,db)
    assert error.value.status_code==409


@pytest.mark.asyncio
async def test_batch_preview_rejects_missing_batch(setup):
    db,admin,*_=setup
    with pytest.raises(HTTPException) as error:
        await main.preview_batch_roster(999999,UploadFile(filename="candidates.csv",file=io.BytesIO(b"email\ncandidate@example.test\n")),admin,db)
    assert error.value.status_code==404
