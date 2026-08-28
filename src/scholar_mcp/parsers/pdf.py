from collections import Counter
import io
import re
from pypdf import PdfReader


def _strip_repeated_lines(pages: list[str]) -> str:
    """Drop lines appearing on a majority of pages (running headers/footers)."""
    if not pages:
        return ""
    if len(pages) == 1:
        return pages[0]

    num_pages = len(pages)
    line_page_counts: Counter[str] = Counter()

    for page in pages:
        unique_lines = set(line.strip() for line in page.splitlines() if line.strip())
        for line in unique_lines:
            line_page_counts[line] += 1

    majority = num_pages / 2.0
    filtered_pages: list[str] = []

    for page in pages:
        kept_lines = [
            line
            for line in page.splitlines()
            if not (line.strip() and line_page_counts[line.strip()] > majority)
        ]
        filtered_pages.append("\n".join(kept_lines))

    return "\n\n".join(filtered_pages)


def _postprocess(text: str) -> str:
    """Rejoin hyphenated words across line breaks and collapse whitespace."""
    if not text:
        return ""

    # Rejoin hyphenated line breaks: "infor-\nmation" -> "information"
    text = re.sub(r"(\b\w+)-\n+(\w+\b)", r"\1\2", text)

    # Collapse horizontal spaces/tabs
    text = re.sub(r"[ \t]+", " ", text)

    # Clean lines
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(lines)

    # Collapse 3+ newlines to 2 newlines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def pdf_bytes_to_text(pdf_bytes: bytes) -> str:
    """Extract clean text from PDF bytes in memory."""
    if not pdf_bytes or not isinstance(pdf_bytes, (bytes, bytearray)):
        return ""

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return ""

        pages_text: list[str] = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                pages_text.append(t)

        if not pages_text:
            return ""

        combined = _strip_repeated_lines(pages_text)
        return _postprocess(combined)
    except Exception:
        return ""
