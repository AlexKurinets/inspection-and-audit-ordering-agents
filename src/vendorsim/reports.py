"""Renders the simulated vendors' reports as PDFs, and some of them as scans with no text layer."""

from __future__ import annotations

import io
import random

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

VENDOR_NAMES = {
    "LFS": "Larkspur Field Surveys",
    "QRS": "Quillmate Risk Services",
    "HAP": "Harrowgate Audit Partners",
}


def _money(x: float) -> str:
    return f"${x:,.0f}"


def _draw(lines: list[tuple[str, str | list[tuple[float, str, str]]]]) -> bytes:
    """lines: (style, text) or ("row", [(x, align, text), ...])."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)
    y = 740
    for style, content in lines:
        if style == "gap":
            y -= 8
            continue
        if style == "row":
            c.setFont("Helvetica", 10)
            for x, align, text in content:
                (c.drawRightString if align == "r" else c.drawString)(x, y, text)
            y -= 15
            continue
        font, size = {"title": ("Helvetica-Bold", 15), "h": ("Helvetica-Bold", 11)}.get(style, ("Helvetica", 10))
        c.setFont(font, size)
        c.drawString(72, y, content)
        y -= 22 if style == "title" else 15
    c.showPage()
    c.save()
    return buf.getvalue()


def inspection_pdf(spec: dict) -> bytes:
    lines: list = [
        ("title", VENDOR_NAMES[spec["vendor"]]),
        ("h", "Loss Control Survey Report"),
        ("gap", ""),
        ("p", f"Report number: {spec['report_id']}"),
        ("p", f"Policy number: {spec['policy_number']}"),
        ("p", f"Named insured: {spec['insured_name']}"),
        ("p", f"Survey date: {spec['survey_date']}"),
        ("p", f"Survey type: {spec['survey_type']}"),
        ("p", f"Operations: {spec['operations']}"),
        ("p", f"Overall risk rating: {spec['rating']}"),
        ("gap", ""),
        ("h", "Recommendations"),
    ]
    if not spec["recommendations"]:
        lines.append(("p", "No recommendations."))
    for i, r in enumerate(spec["recommendations"], start=1):
        lines.append(("p", f"R{i} ({r['priority']}) {r['text']}"))
    lines += [("gap", ""), ("p", f"Surveyor: {spec['surveyor']}")]
    return _finish(_draw(lines), spec)


def audit_pdf(spec: dict) -> bytes:
    title = "Self-Audit Payroll Report" if spec["vendor"] == "SELF" else "Premium Audit Worksheet"
    header = spec["insured_name"] if spec["vendor"] == "SELF" else VENDOR_NAMES[spec["vendor"]]
    lines: list = [
        ("title", header),
        ("h", title),
        ("gap", ""),
        ("p", f"Audit number: {spec['report_id']}"),
        ("p", f"Policy number: {spec['policy_number']}"),
        ("p", f"Named insured: {spec['insured_name']}"),
        ("p", f"Audit period: {spec['period_start']} to {spec['period_end']}"),
        ("p", f"Audit type: {spec['audit_type']}"),
        ("gap", ""),
        ("row", [(72, "l", "Class"), (140, "l", "Description"), (520, "r", "Payroll")]),
    ]
    for row in spec["rows"]:
        lines.append(("row", [(72, "l", row["class_code"]), (140, "l", row["description"]),
                              (520, "r", _money(row["payroll"]))]))
    lines += [
        ("gap", ""),
        ("p", f"Total payroll: {_money(spec['stated_total'])}"),
        ("p", f"Records reviewed: {spec['records']}"),
        ("p", f"{'Completed by' if spec['vendor'] == 'SELF' else 'Auditor'}: {spec['signed_by']}"),
    ]
    return _finish(_draw(lines), spec)


def _finish(pdf: bytes, spec: dict) -> bytes:
    return scan(pdf, spec["report_id"]) if spec.get("scanned") else pdf


def scan(pdf: bytes, seed: str) -> bytes:
    """Re-render the page as a grey 300 DPI image with light noise and no text layer."""
    import pypdfium2 as pdfium
    from PIL import Image

    doc = pdfium.PdfDocument(pdf)
    try:
        img = doc[0].render(scale=300 / 72).to_pil().convert("L")
    finally:
        doc.close()
    rng = random.Random(seed)
    px = img.load()
    w, h = img.size
    for _ in range(w * h // 400):
        x, y = rng.randrange(w), rng.randrange(h)
        px[x, y] = min(255, max(0, px[x, y] + rng.choice((-60, 60))))
    out = io.BytesIO()
    Image.Image.save(img, out, "PDF", resolution=300)
    return out.getvalue()
