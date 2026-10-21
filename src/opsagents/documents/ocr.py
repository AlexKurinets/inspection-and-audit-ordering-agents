"""PDF to text: the PDF's own text layer when it has one, Tesseract OCR when it doesn't."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pypdfium2 as pdfium

# A page whose text layer has fewer characters than this is treated as a scan.
MIN_TEXT_CHARS = 40
OCR_DPI = 300


@dataclass(frozen=True)
class DocText:
    text: str
    pages: int
    ocr_pages: int
    sha256: str


def ocr_image(image) -> str:
    import pytesseract

    return pytesseract.image_to_string(image, config="--psm 6")


def read_pdf(data: bytes) -> DocText:
    pdf = pdfium.PdfDocument(data)
    parts: list[str] = []
    ocr_pages = 0
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            text = page.get_textpage().get_text_range()
            if len(text.strip()) < MIN_TEXT_CHARS:
                image = page.render(scale=OCR_DPI / 72).to_pil().convert("L")
                text = ocr_image(image)
                ocr_pages += 1
            parts.append(text.replace("\r\n", "\n"))
        pages = len(pdf)
    finally:
        pdf.close()
    return DocText("\n".join(parts), pages, ocr_pages, hashlib.sha256(data).hexdigest())
