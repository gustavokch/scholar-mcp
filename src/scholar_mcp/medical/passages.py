"""Passage-targeted full-text serving (ENAMED misses plan B3).

Storing only the first 50k characters hides the discriminating section of
a long manual (the PCDT PrEP body is 129k chars; section 7.1 sits at offset
47.8k). Engines now store up to ``ceiling`` characters and serve them
through this shared helper, which supports two targeted modes besides the
legacy head cut:

* ``offset`` paging: ``body[offset:offset+limit]`` for neighbouring reads.
* ``query`` passages: the 2k head plus the top-scoring ~1500-char
  paragraph-aligned windows within ``limit``, with their offsets and
  scores in ``passages`` so the caller can page around a hit.

Window scoring reuses the B1 content-token pipeline (accent-folded,
stopword-stripped overlap), so passage matching agrees with ranking and
query relaxation on what a term means.
"""

from scholar_mcp.query_relax import content_overlap_count
from scholar_mcp.utils.text import truncate_content

# Default serving budget when the caller passes ``max_chars=None``. Engines
# store up to their own MAX_FULL_TEXT_CHARS ceiling (600k) so passage and
# offset reads can reach deep sections, but a plain call serves only this
# head budget -- the pre-PR default. Storage and serving are deliberately
# different numbers; see _serve_full_text in brazil_moh.py and who_iris.py.
DEFAULT_SERVING_CHARS = 50_000

# Verbatim head always returned in query mode: the title, abstract, and
# front matter carry the document's identity even when the answer sits far
# below. Windows are drawn from the body past this point, so head text is
# never duplicated into a window.
HEAD_CHARS = 2000

# Passage window size: roughly a long section (~250 tokens), small enough
# that several fit beside the head inside a 50k serving budget.
WINDOW_CHARS = 1500


def split_windows(
    body: str, *, base_offset: int = 0, window_chars: int = WINDOW_CHARS
) -> list[tuple[int, str]]:
    """Split ``body`` into ~``window_chars`` paragraph-aligned windows.

    Returns ``(offset, text)`` pairs with ``offset`` relative to the
    enclosing document (``base_offset`` added). Windows are contiguous --
    one window's end is the next window's start -- so offsets stay exact
    for ``offset`` paging. Boundaries prefer paragraph breaks, then line
    breaks, then spaces, each accepted only past the window's midpoint so
    a pathological gap cannot shrink a window to nothing.
    """
    windows: list[tuple[int, str]] = []
    n = len(body)
    start = 0
    half = int(window_chars * 0.5)
    while start < n:
        end = min(start + window_chars, n)
        if end < n:
            for sep in ("\n\n", "\n", " "):
                cut = body.rfind(sep, start, end)
                if cut > start + half:
                    end = cut
                    break
        windows.append((base_offset + start, body[start:end]))
        if end <= start:
            break
        start = end
    return windows


def _rank_windows(
    body: str, query: str, *, head_len: int, window_chars: int
) -> list[tuple[int, int, str]]:
    """Windows past the head as ``(score, offset, text)``, best first.

    Score is the accent-folded content-token overlap between the query and
    the window (B1 helper). Only windows sharing at least one query token
    are returned; ties break toward the earlier offset.
    """
    tail = body[head_len:]
    scored = [
        (content_overlap_count(query, text), offset, text)
        for offset, text in split_windows(
            tail, base_offset=head_len, window_chars=window_chars
        )
    ]
    scored = [item for item in scored if item[0] >= 1]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored


def serve_body(
    body: str,
    total_chars: int,
    limit: int,
    *,
    query: str | None = None,
    offset: int = 0,
    head_chars: int = HEAD_CHARS,
    window_chars: int = WINDOW_CHARS,
) -> dict[str, object]:
    """Serve ``content``/``truncated``/``passages`` for a stored body.

    ``limit`` is the caller's serving budget (already clamped to the
    engine ceiling). ``total_chars`` is the pre-storage document length.
    When both ``query`` and ``offset`` are given the query wins; an
    ``offset`` past the end serves an empty page.
    """
    body = body or ""
    total = total_chars if total_chars >= 0 else len(body)
    start = max(0, offset)

    if query:
        head_len = min(head_chars, len(body), limit)
        head = body[:head_len]
        parts = [head]
        used = len(head)
        # Tracked separately from ``used``: ``used`` bounds the serving
        # budget and must include marker bytes, but ``truncated`` compares
        # against real document length -- marker bytes are not body text
        # and must not mask a body that was actually cut short (finding 10).
        body_chars_served = len(head)
        passages: list[dict[str, int]] = []
        if used < limit:
            for score, win_offset, text in _rank_windows(
                body, query, head_len=head_len, window_chars=window_chars
            ):
                marker = f"\n\n[... passage at offset {win_offset} ...]\n\n"
                piece = marker + text
                if used + len(piece) > limit:
                    break
                parts.append(piece)
                used += len(piece)
                body_chars_served += len(text)
                passages.append({"offset": win_offset, "score": score})
        content = "".join(parts)
        return {
            "content": content,
            "truncated": body_chars_served < total,
            "passages": passages,
        }

    if start > 0:
        content = body[start : start + limit]
        return {
            "content": content,
            "truncated": start + len(content) < total,
            "passages": [],
        }

    content, truncated = truncate_content(body, limit)
    return {
        "content": content,
        "truncated": truncated or (len(content) < total),
        "passages": [],
    }
