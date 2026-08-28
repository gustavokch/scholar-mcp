import io

import pytest
from pypdf import PdfWriter

from scholar_mcp.parsers.pdf import pdf_bytes_to_text


def make_blank_pdf(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_pdf_bytes_to_text_returns_string():
    assert isinstance(pdf_bytes_to_text(make_blank_pdf()), str)


def test_pdf_bytes_to_text_multipage_does_not_crash():
    assert isinstance(pdf_bytes_to_text(make_blank_pdf(3)), str)


def test_pdf_bytes_to_text_corrupt_returns_empty():
    assert pdf_bytes_to_text(b"not-a-valid-pdf") == ""


def test_pdf_bytes_to_text_empty_input_returns_empty():
    assert pdf_bytes_to_text(b"") == ""


def test_dehyphenation_and_whitespace(monkeypatch):
    """Post-processing stitches hyphenated line breaks and collapses whitespace."""
    from scholar_mcp.parsers import pdf as pdf_mod

    raw = "This paper presents infor-\nmation about   spacing\n\n\n\nand   breaks."
    cleaned = pdf_mod._postprocess(raw)
    assert "information" in cleaned
    assert "infor-" not in cleaned
    assert "   " not in cleaned


def test_repeated_header_footer_removed():
    from scholar_mcp.parsers import pdf as pdf_mod

    pages = [
        "Journal of Testing\nReal content one\nPage 1",
        "Journal of Testing\nReal content two\nPage 2",
        "Journal of Testing\nReal content three\nPage 3",
    ]
    out = pdf_mod._strip_repeated_lines(pages)
    assert "Journal of Testing" not in out
    assert "Real content two" in out
