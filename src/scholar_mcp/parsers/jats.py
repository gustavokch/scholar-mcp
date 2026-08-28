from collections import deque
import re
from typing import Any
from bs4 import BeautifulSoup, NavigableString, Tag

DECOMPOSE_TAGS = [
    "ref-list",
    "ref",
    "fn-group",
    "fn",
    "permissions",
    "license",
    "copyright-holder",
    "supplementary-material",
    "related-article",
    "tex-math",
    "mml:math",
    "math",
]


def _format_table(table_tag: Tag) -> str:
    rows = table_tag.find_all("tr")
    if not rows:
        return ""

    table_data: list[list[str]] = []
    has_header = False

    for row in rows:
        th_cells = row.find_all("th")
        td_cells = row.find_all("td")
        if th_cells:
            has_header = True
            table_data.append([c.get_text(" ", strip=True) for c in th_cells])
        elif td_cells:
            table_data.append([c.get_text(" ", strip=True) for c in td_cells])

    if not table_data:
        return ""

    num_cols = max(len(r) for r in table_data)
    # Pad rows
    padded = [r + [""] * (num_cols - len(r)) for r in table_data]

    lines: list[str] = []
    if has_header:
        headers = padded[0]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join(["---"] * num_cols) + "|")
        for row in padded[1:]:
            lines.append("| " + " | ".join(row) + " |")
    else:
        for row in padded:
            lines.append("| " + " | ".join(row) + " |")

    return "\n" + "\n".join(lines) + "\n"


def _format_list(list_tag: Tag) -> str:
    list_type = list_tag.get("list-type", "bullet")
    items = list_tag.find_all("list-item", recursive=False)
    lines: list[str] = []
    for idx, item in enumerate(items, 1):
        item_text = item.get_text(" ", strip=True)
        if list_type == "order":
            lines.append(f"{idx}. {item_text}")
        else:
            lines.append(f"- {item_text}")
    return "\n" + "\n".join(lines) + "\n"


def _format_figure(fig_tag: Tag) -> str:
    label = fig_tag.find("label")
    caption = fig_tag.find("caption")
    label_str = label.get_text(" ", strip=True) if label else "Figure"
    caption_str = caption.get_text(" ", strip=True) if caption else ""
    if caption_str:
        return f"\n[{label_str}] {caption_str}\n"
    return f"\n[{label_str}]\n"


def _format_boxed_text(boxed_tag: Tag) -> str:
    text = boxed_tag.get_text("\n", strip=True)
    lines = [f"> {line}" for line in text.splitlines() if line.strip()]
    return "\n" + "\n".join(lines) + "\n"


def _sec_depth(tag: Tag) -> int:
    depth = 2
    parent = tag.parent
    while parent and parent.name != "body" and parent.name != "article":
        if parent.name == "sec":
            depth += 1
        parent = parent.parent
    return min(depth, 6)


def _render_node(node: Tag | NavigableString, sec_level: int = 2) -> str:
    if isinstance(node, NavigableString):
        return str(node)

    if not isinstance(node, Tag):
        return ""

    tag_name = node.name.lower() if node.name else ""

    if tag_name in DECOMPOSE_TAGS or "mml:" in tag_name:
        return ""

    if tag_name == "sec":
        depth = _sec_depth(node)
        parts: list[str] = []
        title = node.find("title", recursive=False)
        if title:
            title_text = title.get_text(" ", strip=True)
            parts.append(f"\n{'#' * depth} {title_text}\n")
        for child in node.children:
            if child != title:
                parts.append(_render_node(child, depth))
        return "".join(parts)

    if tag_name == "title":
        # Handled in sec
        return ""

    if tag_name == "p":
        inner = "".join(_render_node(c, sec_level) for c in node.children).strip()
        return f"\n\n{inner}\n\n" if inner else ""

    if tag_name == "xref":
        return node.get_text(" ", strip=True)

    if tag_name == "boxed-text":
        return _format_boxed_text(node)

    if tag_name == "fig":
        return _format_figure(node)

    if tag_name == "table-wrap":
        tbl = node.find("table")
        if tbl:
            return _format_table(tbl)
        return ""

    if tag_name == "table":
        return _format_table(node)

    if tag_name == "list":
        return _format_list(node)

    if tag_name in ("inline-formula", "disp-formula"):
        return node.get_text(" ", strip=True)

    # General container
    return "".join(_render_node(c, sec_level) for c in node.children)


def jats_to_markdown(xml_content: str | bytes) -> str:
    if not xml_content:
        return ""

    if isinstance(xml_content, bytes):
        xml_content = xml_content.decode("utf-8", errors="ignore")

    if not xml_content.strip():
        return ""

    try:
        soup = BeautifulSoup(xml_content, "lxml-xml")
    except Exception:
        try:
            soup = BeautifulSoup(xml_content, "html.parser")
        except Exception:
            return ""

    if not soup:
        return ""

    # Decompose unwanted elements
    for tag_name in DECOMPOSE_TAGS:
        for match in soup.find_all(tag_name):
            match.decompose()

    # Also decompose namespace tags
    for tag in soup.find_all(True):
        if tag.name and ("mml:" in tag.name or "math" in tag.name):
            tag.decompose()

    parts: list[str] = []

    # Title
    title_tag = soup.find("article-title")
    if title_tag:
        title_text = title_tag.get_text(" ", strip=True)
        if title_text:
            parts.append(f"# {title_text}\n")

    # Authors
    contrib_tags = soup.find_all("contrib", attrs={"contrib-type": "author"})
    author_names: list[str] = []
    for contrib in contrib_tags:
        name_tag = contrib.find("name")
        if name_tag:
            given = name_tag.find("given-names")
            surname = name_tag.find("surname")
            given_str = given.get_text(" ", strip=True) if given else ""
            surname_str = surname.get_text(" ", strip=True) if surname else ""
            full = f"{given_str} {surname_str}".strip()
            if full:
                author_names.append(full)
        else:
            txt = contrib.get_text(" ", strip=True)
            if txt:
                author_names.append(txt)

    if author_names:
        parts.append(f"**Authors:** {', '.join(author_names)}\n\n")

    # Abstract
    abstract_tag = soup.find("abstract")
    if abstract_tag:
        abstract_p = abstract_tag.find_all("p")
        if abstract_p:
            p_texts = [p.get_text(" ", strip=True) for p in abstract_p]
            parts.append("## Abstract\n\n" + "\n\n".join(p_texts) + "\n\n")
        else:
            abs_text = abstract_tag.get_text(" ", strip=True)
            if abs_text:
                parts.append(f"## Abstract\n\n{abs_text}\n\n")

    # Body
    body_tag = soup.find("body")
    if body_tag:
        body_text = _render_node(body_tag, sec_level=2)
        parts.append(body_text)

    full_md = "".join(parts)
    # Clean up whitespace
    full_md = re.sub(r"\n{3,}", "\n\n", full_md).strip()
    return full_md


def list_sections(markdown: str) -> list[str]:
    matches = re.findall(r"^#{1,6}\s+(.+)$", markdown, re.MULTILINE)
    # Exclude title line if it was level 1? Actually keep all section headers
    # (Level 1 in our output is the article title, level 2+ are sections like Abstract, Introduction)
    # But let's check: if "Abstract" is in sections, "Introduction" is in sections, etc.
    # Level 2+ matches
    sections: list[str] = []
    for line in markdown.splitlines():
        m = re.match(r"^#{2,6}\s+(.+)$", line)
        if m:
            sections.append(m.group(1).strip())
    return sections


def select_sections(markdown: str, wanted: list[str]) -> str:
    if not wanted:
        return markdown

    wanted_lower = [w.lower().strip() for w in wanted if w.strip()]
    if not wanted_lower:
        return markdown

    # Split markdown by headings (##, ###, etc.)
    heading_pattern = re.compile(r"^(#{2,6}\s+.+)$", re.MULTILINE)
    splits = heading_pattern.split(markdown)

    # splits will be: [preamble, heading1, content1, heading2, content2, ...]
    selected_parts: list[str] = []

    i = 1
    while i < len(splits):
        heading_line = splits[i]
        content = splits[i + 1] if i + 1 < len(splits) else ""

        # Extract heading title
        heading_match = re.match(r"^#{2,6}\s+(.+)$", heading_line)
        if heading_match:
            heading_title = heading_match.group(1).strip().lower()
            if any(w in heading_title for w in wanted_lower):
                selected_parts.append(f"{heading_line}\n{content}".strip())
        i += 2

    return "\n\n".join(selected_parts).strip()
