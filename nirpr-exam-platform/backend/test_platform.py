from datetime import datetime
from fastapi.testclient import TestClient

from certificate_utils import build_certificate_pdf, certificate_number
from main import app
from reporting_utils import official_exam_report_pdf


def test_public_health_captcha_and_verification_page():
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        challenge = client.get("/api/auth/captcha")
        assert challenge.status_code == 200
        assert challenge.json()["question"] and challenge.json()["token"]
        assert client.get("/verify-certificate").status_code == 200


def test_certificate_number_is_stable():
    assert certificate_number("RSO-DR", datetime(2026, 8, 23), 12, 3) == "NIRPR/NTC/RSO/DR/2026/0003"
    assert certificate_number("RSO-DIAG-RAD", datetime(2026, 8, 23), 12, 3) == "NIRPR/NTC/RSO/DIAG/2026/0003"


def test_certificate_qr_pdf_generation():
    pdf = build_certificate_pdf(candidate_name="Test Candidate", programme_name="RSO",
        practice_area="Diagnostic Radiology", exam_title="Final Examination", score=82.5,
        issued_on=datetime.utcnow(), certificate_no=certificate_number("RSO-DR", datetime(2026, 8, 23), 12, 3),
        verification_url="https://example.test/verify-certificate?number=NIRPR%2FNTC%2FRSO%2FDR%2F2026%2F0003")
    assert pdf.startswith(b"%PDF") and len(pdf) > 5_000


def test_official_report_pdf_generation():
    pdf = official_exam_report_pdf({"examination":"Final", "date":"23 August 2026", "programme":"RSO",
        "number_registered":10, "number_present":9, "number_absent":1, "number_passed":8,
        "number_failed":1, "average_score":75.2, "pass_rate":88.9, "examiner":"Examiner",
        "approval_date":"23 August 2026"})
    assert pdf.startswith(b"%PDF") and len(pdf) > 1_000
