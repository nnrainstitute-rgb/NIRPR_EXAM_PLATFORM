from datetime import datetime
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw
from pypdf import PdfReader
import pytest
from fastapi import HTTPException
from sqlalchemy import select

import main
from models import ExamAttempt, AttemptStatus
from certificate_utils import certificate_number, legacy_certificate_number, build_certificate_pdf
from signature_utils import clean_signature_image, signature_png
from test_payment_requirements import setup


def test_single_candidate_number_and_legacy_format():
    assert certificate_number("RSO-NWLST",datetime(2026,10,24),5,6)=="NIRPR/NTC/RSO/NWLST/2026/0006"
    assert legacy_certificate_number("RSO-NWLST",datetime(2026,10,24),5,6)=="NIRPR/NTC/RSO/NWLST/2026/0006/0005"


def signature_source(background=(180,180,180),ink=(25,30,90)):
    source=Image.new("RGB",(500,180),background)
    draw=ImageDraw.Draw(source)
    draw.line([(70,120),(100,40),(130,125),(160,55),(200,105),(350,80)],fill=ink,width=4)
    output=BytesIO();source.save(output,format="PNG");output.seek(0)
    return output


def test_signature_cleanup_removes_gray_paper_and_crops():
    cleaned=clean_signature_image(signature_source())
    assert cleaned.mode=="RGBA"
    assert cleaned.width<500 and cleaned.height<180
    alpha=cleaned.getchannel("A")
    assert alpha.getextrema()==(0,255)
    assert sum(value==0 for value in alpha.getdata())>cleaned.width*cleaned.height*.6
    assert Image.open(BytesIO(signature_png(signature_source().getvalue()))).mode=="RGBA"


def test_blank_signature_rejected():
    content=BytesIO();Image.new("RGB",(100,50),"white").save(content,format="PNG");content.seek(0)
    with pytest.raises(ValueError,match="No signature ink"):
        clean_signature_image(content)


def test_pdf_has_short_number_and_restored_original_text():
    number=certificate_number("RSO-NWLST",datetime(2026,10,24),5,6)
    pdf=build_certificate_pdf(candidate_name="Sample Candidate",programme_name="RSO",practice_area="Nuclear Well Logging",exam_title="Final",score=85,issued_on=datetime(2026,10,24),certificate_no=number)
    text=PdfReader(BytesIO(pdf)).pages[0].extract_text().upper()
    assert number in text
    assert "NATIONAL INSTITUTE OF RADIATION PROTECTION AND RESEARCH" in text
    assert "NIGERIAN NUCLEAR REGULATORY AUTHORITY" in text
    assert "GENERAL MANAGER" in text and "COURSE COORDINATOR" in text
    assert "AS TECHNICAL SUPPORT ORGANIZATION TO THE" in text
    assert "ESTABLISHED BY S.11(5)" in text
    assert "SCORE" not in text
    assert "85.0%" not in text
    assert "ISSUED 24 OCTOBER 2026" in text


@pytest.mark.asyncio
async def test_short_legacy_and_exact_attempt_verification(setup):
    db,admin,candidate,exam,other=setup
    attempt=ExamAttempt(exam_id=exam.id,user_id=candidate.id,status=AttemptStatus.APPROVED,
        result_released=True,passed=True,percentage=88,approved_at=datetime(2026,10,24))
    db.add(attempt);await db.commit()
    programme=(await db.execute(select(main.TrainingProgram).where(main.TrainingProgram.id==exam.training_program_id))).scalar_one()
    short=certificate_number(programme.code,attempt.approved_at,attempt.id,candidate.id)
    old=legacy_certificate_number(programme.code,attempt.approved_at,attempt.id,candidate.id)
    assert (await main.verify_certificate(short,db))["valid"] is True
    assert (await main.verify_certificate(old,db))["valid"] is True
    assert (await main.verify_certificate(short,db,attempt_id=attempt.id))["valid"] is True
    with pytest.raises(HTTPException):await main.verify_certificate(short,db,attempt_id=999999)
    attempt.result_released=False;await db.commit()
    with pytest.raises(HTTPException):await main.verify_certificate(short,db)
