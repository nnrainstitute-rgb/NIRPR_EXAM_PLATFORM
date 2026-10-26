"""Generate approved NIRPR/NNRA candidate certificates as PDF documents."""

from datetime import datetime
from io import BytesIO
from pathlib import Path
import re
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph
from reportlab.lib.utils import ImageReader
import qrcode
from signature_utils import clean_signature_image


NAVY = colors.HexColor("#102A43")
TEAL = colors.HexColor("#168F8B")
GOLD = colors.HexColor("#D9A441")
MUTED = colors.HexColor("#526577")


def certificate_number(course_code: str, issued_on: datetime,
                       certification_id: int, examination_number: int) -> str:
    """Return the public, deterministic identifier printed on a certificate."""
    normalized_code = re.sub(r"[^A-Z0-9]+", "-", course_code.upper()).strip("-")
    course_parts = [part for part in normalized_code.split("-") if part != "RSO"]
    normalized_code = course_parts[0] if course_parts else ""
    if not normalized_code:
        raise ValueError("Course code must contain a value other than RSO")
    if examination_number < 0 or examination_number > 9999:
        raise ValueError("Examination number must contain no more than four digits")
    return (
        f"NIRPR/NTC/RSO/{normalized_code}/{issued_on.year}/"
        f"{examination_number:04d}"
    )



def legacy_certificate_number(course_code, issued_on, certification_id, examination_number):
    return f"{certificate_number(course_code,issued_on,certification_id,examination_number)}/{certification_id:04d}"


def _centered_paragraph(pdf, text, style, y, width, height=30 * mm):
    paragraph = Paragraph(text, style)
    _, rendered_height = paragraph.wrap(width, height)
    paragraph.drawOn(pdf, (landscape(A4)[0] - width) / 2, y - rendered_height)
    return y - rendered_height


def _fit_centered_text(pdf, text: str, y: float, max_width: float,
                       preferred_size: float = 27, minimum_size: float = 16,
                       font: str = "Helvetica-Bold", color=TEAL):
    size = preferred_size
    while size > minimum_size and pdf.stringWidth(text, font, size) > max_width:
        size -= 0.5
    pdf.setFillColor(color)
    pdf.setFont(font, size)
    pdf.drawCentredString(landscape(A4)[0] / 2, y, text)


def _draw_signature(pdf, signature: dict | None, center_x: float, line_y: float,
                    default_title: str):
    if signature and signature.get("image_path"):
        try:
            pdf.drawImage(
                ImageReader(clean_signature_image(signature["image_path"])), center_x - 28 * mm, line_y + 3 * mm,
                56 * mm, 15 * mm, preserveAspectRatio=True, anchor="c", mask="auto",
            )
        except Exception:
            pass
    pdf.setStrokeColor(colors.HexColor("#687889"))
    pdf.setLineWidth(0.7)
    pdf.line(center_x - 29 * mm, line_y, center_x + 29 * mm, line_y)
    text_y = line_y - 4 * mm
    if signature and signature.get("name"):
        pdf.setFillColor(NAVY); pdf.setFont("Helvetica-Bold", 7.8)
        pdf.drawCentredString(center_x, text_y, signature["name"])
        text_y -= 4 * mm
    pdf.setFillColor(MUTED); pdf.setFont("Helvetica", 6.4)
    for line in (
        default_title,
        "National Institute of Radiation Protection and Research, Ibadan",
        "Nigerian Nuclear Regulatory Authority",
    ):
        pdf.drawCentredString(center_x, text_y, line)
        text_y -= 3.1 * mm


def build_certificate_pdf(*, candidate_name: str, programme_name: str,
                          practice_area: str, exam_title: str, score: float,
                          issued_on: datetime, certificate_no: str,
                          institution: str | None = None,
                          course_start: datetime | None = None,
                          course_end: datetime | None = None,
                          verification_url: str | None = None,
                          signatures: list[dict] | None = None) -> bytes:
    """Create a clean certificate derived from the institute's supplied sample."""
    buffer = BytesIO()
    page_width, page_height = landscape(A4)
    pdf = canvas.Canvas(buffer, pagesize=(page_width, page_height), pageCompression=1)
    pdf.setTitle(f"NIRPR Certificate - {candidate_name}")
    pdf.setAuthor("National Institute of Radiation Protection and Research (NIRPR)")
    pdf.setSubject("Radiation Safety Officer Certification")

    # Crisp institutional frame inspired by the supplied paper certificate.
    pdf.setFillColor(colors.HexColor("#FFFEFB")); pdf.rect(0, 0, page_width, page_height, fill=1, stroke=0)
    pdf.setStrokeColor(colors.HexColor("#971D2B")); pdf.setLineWidth(3)
    pdf.rect(9 * mm, 9 * mm, page_width - 18 * mm, page_height - 18 * mm)
    pdf.setStrokeColor(GOLD); pdf.setLineWidth(1)
    pdf.rect(14 * mm, 14 * mm, page_width - 28 * mm, page_height - 28 * mm)
    pdf.setStrokeColor(colors.HexColor("#D9E1DC")); pdf.setLineWidth(0.6)
    pdf.rect(18 * mm, 18 * mm, page_width - 36 * mm, page_height - 36 * mm)

    # Official NNRA emblem watermark, sourced from NNRA_Official and converted
    # to a transparent asset so it remains subtle behind certificate text.
    watermark_path = Path(__file__).resolve().parent / "assets" / "nnra-watermark.png"
    if watermark_path.is_file():
        pdf.drawImage(str(watermark_path), page_width / 2 - 52 * mm,
                      page_height / 2 - 58 * mm, 104 * mm, 104 * mm,
                      preserveAspectRatio=True, anchor="c", mask="auto")

    logo_path = Path(__file__).resolve().parent.parent / "frontend" / "images" / "nnra-logo.jpg"
    if logo_path.is_file():
        pdf.drawImage(str(logo_path), page_width / 2 - 42 * mm, page_height - 34 * mm,
                      84 * mm, 25 * mm, preserveAspectRatio=True, anchor="c", mask="auto")

    pdf.setFillColor(MUTED); pdf.setFont("Helvetica-Bold", 6.8)
    pdf.drawRightString(page_width - 20 * mm, page_height - 18 * mm, certificate_no)

    # Statutory institute heading and hierarchy from the supplied certificate.
    pdf.setFillColor(colors.HexColor("#B0182B")); pdf.setFont("Helvetica-Bold", 16.5)
    pdf.drawCentredString(page_width / 2, page_height - 43 * mm,
                          "NATIONAL INSTITUTE OF RADIATION PROTECTION AND RESEARCH")
    pdf.setFillColor(MUTED); pdf.setFont("Helvetica", 7.8)
    pdf.drawCentredString(page_width / 2, page_height - 49 * mm,
                          "(Established by S.11(5) of Nuclear Safety and Radiation Protection Act 19 of 1995)")
    pdf.drawCentredString(page_width / 2, page_height - 54 * mm,
                          "as Technical Support Organization to the")
    pdf.setFillColor(colors.HexColor("#3D7D42")); pdf.setFont("Helvetica-Bold", 16)
    pdf.drawCentredString(page_width / 2, page_height - 63 * mm,
                          "NIGERIAN NUCLEAR REGULATORY AUTHORITY")

    pdf.setFillColor(NAVY); pdf.setFont("Helvetica-Oblique", 11)
    pdf.drawCentredString(page_width / 2, page_height - 74 * mm, "Certifies that")
    name_y = page_height - 88 * mm
    _fit_centered_text(pdf, candidate_name.strip().upper(), name_y, page_width - 100 * mm,
                       preferred_size=24, minimum_size=14, color=NAVY)
    pdf.setStrokeColor(GOLD); pdf.setLineWidth(1.2)
    pdf.line(70 * mm, name_y - 4 * mm, page_width - 70 * mm, name_y - 4 * mm)

    body_style = ParagraphStyle("certificate-body", fontName="Helvetica", fontSize=8.4,
                                leading=11, textColor=MUTED, alignment=TA_CENTER)
    emphasis_style = ParagraphStyle("certificate-emphasis", fontName="Helvetica-Bold", fontSize=9.5,
                                    leading=12, textColor=NAVY, alignment=TA_CENTER)
    y = page_height - 101 * mm
    y = _centered_paragraph(pdf, "Successfully completed the", body_style, y, page_width - 85 * mm)
    y -= 2 * mm
    y = _centered_paragraph(pdf,
        "NATIONAL TRAINING COURSE FOR RADIATION SAFETY OFFICERS IN",
        emphasis_style, y, page_width - 75 * mm)
    y -= 1.5 * mm
    course_text = escape(practice_area or programme_name).upper()
    y = _centered_paragraph(pdf, course_text, emphasis_style, y, page_width - 65 * mm, 22 * mm)
    y -= 2 * mm
    y = _centered_paragraph(pdf, "Held at", body_style, y, page_width - 80 * mm)
    y -= 1.5 * mm
    if course_start and course_end:
        date_range = f"{course_start.strftime('%d')} - {course_end.strftime('%d %B %Y')}"
    else:
        date_range = issued_on.strftime("%d %B %Y")
    venue = (f"NATIONAL INSTITUTE OF RADIATION PROTECTION AND RESEARCH, IBADAN, "
             f"OYO STATE. {date_range.upper()}")
    y = _centered_paragraph(pdf, venue, emphasis_style, y, page_width - 65 * mm, 18 * mm)
    y -= 2 * mm
    syllabus = ("The RSO Training Course was based on the modules and syllabus developed by the "
                "International Atomic Energy Agency (IAEA), Vienna, Austria")
    _centered_paragraph(pdf, syllabus, body_style, y, page_width - 70 * mm, 18 * mm)

    try:
        valid_until = issued_on.replace(year=issued_on.year + 3)
    except ValueError:
        valid_until = issued_on.replace(year=issued_on.year + 3, day=28)
    pdf.setFillColor(MUTED); pdf.setFont("Helvetica", 7.3)
    pdf.drawString(22 * mm, 22 * mm, f"Valid till {valid_until.strftime('%B %Y')}")
    pdf.drawRightString(page_width - 22 * mm, 22 * mm,
                        f"Issued {issued_on.strftime('%d %B %Y')}")

    by_role = {s.get("role_key"): s for s in (signatures or [])}
    manager = by_role.get("general_manager")
    coordinator = by_role.get("course_coordinator")
    if not by_role and signatures:
        manager = signatures[0] if len(signatures) > 0 else None
        coordinator = signatures[1] if len(signatures) > 1 else None
    _draw_signature(pdf, manager, 67 * mm, 42 * mm, "General Manager")
    _draw_signature(pdf, coordinator, page_width - 67 * mm, 42 * mm, "Course Coordinator")

    if verification_url:
        qr = qrcode.make(verification_url)
        qr_buffer = BytesIO(); qr.save(qr_buffer, format="PNG"); qr_buffer.seek(0)
        pdf.drawImage(ImageReader(qr_buffer), page_width / 2 - 10 * mm, 27 * mm, 20 * mm, 20 * mm,
                      preserveAspectRatio=True, mask="auto")
        pdf.setFillColor(MUTED); pdf.setFont("Helvetica", 6.5)
        pdf.drawCentredString(page_width / 2, 24 * mm, "Scan to verify authenticity")

    pdf.showPage(); pdf.save()
    return buffer.getvalue()
