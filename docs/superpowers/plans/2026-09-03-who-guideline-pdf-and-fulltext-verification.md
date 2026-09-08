# Plan: Verify WHO Guideline Tool PDF Links and Full-Text Retrieval

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify and ensure that WHO guideline tool responses (`search_who_iris_guidelines`) return the direct PDF link within each IRIS entry as `pdf_url`, and that full text requests (`get_who_iris_full_text`) return the full contents of the PDF along with the resolved PDF link.

**Architecture:** 
1. **Model & Formatter (`models.py`, `formatters.py`)**: Add `pdf_url: str = ""` to `WHOGuideline` and render `- **PDF:** <pdf_url>` in `format_who_iris_guidelines`.
2. **Bitstream Resolution (`who_iris.py`)**: Extract shared helper `_select_pdf_bitstream(item_uuid) -> tuple[str, str, bool]` that queries the `ORIGINAL` bundle and selects the largest PDF bitstream, returning `(pdf_url, bitstream_uuid, errored)`.
3. **Search Enrichment (`who_iris.py`)**: In `search_guidelines`, extract embedded bitstream if available or resolve bitstream URLs for retrieved items using bounded concurrency (`asyncio.gather`), ensuring fast responses without failing if a single bitstream lookup fails.
4. **Full-Text Retrieval (`who_iris.py`)**: In `get_full_text`, retrieve the primary PDF bitstream, download the bytes, extract full text across all pages using `pdf_bytes_to_text`, and return the full content along with `pdf_url`, `content_type="pdf"`, and accurate truncation status.
5. **Verification Suite (`tests/medical/test_who_iris.py`)**: Unit and integration test suite asserting PDF link surfacing in search responses, multi-page PDF extraction, fallback paths, and end-to-end MCP tool invocations.

**Tech Stack:** Python 3.10+, httpx, respx, pypdf, pytest, pytest-asyncio, FastMCP.

---

## Global Constraints

- Never break backward compatibility for existing `WHOGuideline` fields (`handle`, `url`, `title`, `year`, `authors`, etc.).
- `WHOGuideline.pdf_url` must be a direct download link (e.g. `https://iris.who.int/server/api/core/bitstreams/{uuid}/content`), defaulting to `""` if no PDF bitstream exists or lookup fails.
- In `format_who_iris_guidelines`, display `- **PDF:** <pdf_url>` in the formatted Markdown output whenever `pdf_url` is non-empty.
- Per-item bitstream lookup failures in `search_guidelines` must degrade gracefully (`pdf_url=""`) without flipping `meta.error=True` for the overall search.
- `get_who_iris_full_text` must extract all pages of the PDF without truncation up to `max_chars` (default 50,000 chars), setting `truncated=True` only when content length exceeds `max_chars`.
- Follow strict TDD: failing test -> run to fail -> minimal implementation -> run to pass -> commit.

---

## Key Files Modified or Tested

- `src/scholar_mcp/medical/models.py`: Update `WHOGuideline` dataclass to include `pdf_url: str = ""`.
- `src/scholar_mcp/medical/formatters.py`: Update `format_who_iris_guidelines` to display `- **PDF:** <pdf_url>` in Markdown.
- `src/scholar_mcp/medical/who_iris.py`:
  - Extract helper `_select_pdf_bitstream(item_uuid: str) -> tuple[str, str, bool]`
  - Enrich `search_guidelines` to attach `pdf_url` to returned records.
  - Update `_extract_pdf_text` and `get_full_text` to return `pdf_url` alongside the extracted PDF content.
- `tests/medical/test_who_iris.py`: Add unit and integration tests covering PDF link extraction, search enrichment, multi-page PDF full text extraction, and MCP tool handlers.

---

### Task 1: Add `pdf_url` to `WHOGuideline` Model and Markdown Formatter

**Files:**
- Modify: `src/scholar_mcp/medical/models.py:210-237`
- Modify: `src/scholar_mcp/medical/formatters.py:259-291`
- Test: `tests/medical/test_who_iris.py`

**Interfaces:**
- `WHOGuideline`: dataclass field `pdf_url: str = ""`
- `format_who_iris_guidelines(guidelines: list[WHOGuideline], query: str, meta: CacheMetadata) -> dict[str, Any]`

- [ ] **Step 1: Write the failing test for `WHOGuideline.pdf_url` and markdown formatter**

Add to `tests/medical/test_who_iris.py`:

```python
def test_who_guideline_model_and_formatter_include_pdf_url():
    from scholar_mcp.medical.models import WHOGuideline
    from scholar_mcp.medical.formatters import format_who_iris_guidelines
    from scholar_mcp.utils.sqlite_cache import CacheMetadata

    guideline = WHOGuideline(
        title="WHO Malaria Guidelines",
        handle="10665/311551",
        url="https://iris.who.int/handle/10665/311551",
        pdf_url="https://iris.who.int/server/api/core/bitstreams/bit-uuid-1/content",
        year="2023",
        authors=["World Health Organization"],
    )

    data_dict = guideline.to_dict()
    assert data_dict["pdf_url"] == "https://iris.who.int/server/api/core/bitstreams/bit-uuid-1/content"

    restored = WHOGuideline.from_dict(data_dict)
    assert restored.pdf_url == "https://iris.who.int/server/api/core/bitstreams/bit-uuid-1/content"

    formatted = format_who_iris_guidelines(
        [guideline], query="malaria", meta=CacheMetadata(cached=False, cache_age=0, error=False)
    )
    assert "- **PDF:** https://iris.who.int/server/api/core/bitstreams/bit-uuid-1/content" in formatted["markdown"]
    assert "- **URL:** https://iris.who.int/handle/10665/311551" in formatted["markdown"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/medical/test_who_iris.py::test_who_guideline_model_and_formatter_include_pdf_url -v`
Expected: FAIL (`TypeError` or `AssertionError` for missing `pdf_url`).

- [ ] **Step 3: Update `WHOGuideline` model and `format_who_iris_guidelines`**

In `src/scholar_mcp/medical/models.py`:
```python
@dataclass
class WHOGuideline:
    title: str
    handle: str
    url: str
    organization: str = "World Health Organization"
    source: str = "who-iris"
    year: str = ""
    description: str = ""
    authors: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    mesh_subjects: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    spatial_coverage: list[str] = field(default_factory=list)
    isbn: str = ""
    publisher: str = ""
    item_type: str = ""
    pdf_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WHOGuideline":
        if not data or not isinstance(data, dict):
            return cls(title="", handle="", url="")
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**fields)
```

In `src/scholar_mcp/medical/formatters.py`:
```python
def format_who_iris_guidelines(
    guidelines: list[WHOGuideline],
    query: str,
    meta: CacheMetadata,
) -> dict[str, Any]:
    lines = [f"## WHO IRIS Guidelines: {query}", ""]
    if not guidelines:
        lines.append(_empty_state(f"No WHO IRIS guidelines found for: {query}", meta))
    else:
        for g in guidelines:
            lines.append(f"### {g.title}")
            lines.append(f"- **Organization:** {g.organization}")
            lines.append(f"- **Source:** {g.source}")
            if g.year:
                lines.append(f"- **Year:** {g.year}")
            if g.authors:
                lines.append(f"- **Authors:** {', '.join(g.authors)}")
            if g.languages:
                lines.append(f"- **Languages:** {', '.join(g.languages)}")
            if g.mesh_subjects:
                lines.append(f"- **MeSH:** {', '.join(g.mesh_subjects)}")
            if g.spatial_coverage:
                lines.append(f"- **Coverage:** {', '.join(g.spatial_coverage)}")
            if g.url:
                lines.append(f"- **URL:** {g.url}")
            if g.pdf_url:
                lines.append(f"- **PDF:** {g.pdf_url}")
            if g.description:
                lines.append("")
                lines.append(g.description)
            lines.append("")

    markdown = append_cache_info("\n".join(lines).strip(), meta)
    return {"data": [g.to_dict() for g in guidelines], "markdown": markdown}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/medical/test_who_iris.py::test_who_guideline_model_and_formatter_include_pdf_url -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/medical/models.py src/scholar_mcp/medical/formatters.py tests/medical/test_who_iris.py
git commit -m "feat(who-iris): add pdf_url to WHOGuideline model and markdown formatter"
```

---

### Task 2: Shared PDF Bitstream Resolver and Search Enrichment

**Files:**
- Modify: `src/scholar_mcp/medical/who_iris.py:65-191`
- Test: `tests/medical/test_who_iris.py`

**Interfaces:**
- `_extract_pdf_link_from_item(item: dict[str, Any]) -> str`: Extracts PDF URL from embedded DSpace bundles/bitstreams if present.
- `WHOIRISEngine._resolve_pdf_bitstream(item_uuid: str) -> tuple[str, str, bool]`: Fetches bundles and bitstreams for an item UUID, returning `(pdf_url, bitstream_uuid, errored)`.
- `search_guidelines`: Resolves `pdf_url` for returned guidelines with bounded concurrency.

- [ ] **Step 1: Write failing tests for search PDF link resolution**

Add to `tests/medical/test_who_iris.py`:

```python
@respx.mock
async def test_search_guidelines_resolves_pdf_links_for_items(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        item = _iris_item(handle="10665/44626", title="Guideline A")
        respx.get(IRIS_BROWSE_TITLE_URL).respond(json=_browse_page([item]))
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/{item['uuid']}/bundles").respond(
            json=_bundles_page([_bundle(uuid="bundle-1", name="ORIGINAL")])
        )
        respx.get(f"{IRIS_BUNDLE_BITSTREAMS_URL}/bundle-1/bitstreams").respond(
            json=_bitstreams_page([
                _bitstream(uuid="bit-guideline-pdf", name="guideline.pdf", size=5000)
            ])
        )

        guidelines, meta = await engine.search_guidelines("guideline", limit=1, mode="prefix")
        assert len(guidelines) == 1
        assert guidelines[0].pdf_url == f"{IRIS_BITSTREAM_CONTENT_URL}/bit-guideline-pdf/content"
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()

@respx.mock
async def test_search_guidelines_item_bitstream_error_does_not_fail_search(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        item = _iris_item(handle="10665/44626", title="Guideline A")
        respx.get(IRIS_BROWSE_TITLE_URL).respond(json=_browse_page([item]))
        # Bitstream bundle fetch fails with 500
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/{item['uuid']}/bundles").respond(status_code=500)

        guidelines, meta = await engine.search_guidelines("guideline", limit=1, mode="prefix")
        assert len(guidelines) == 1
        assert guidelines[0].pdf_url == ""  # Graceful fallback
        assert meta.error is False  # Search overall succeeded
    finally:
        await cache.close()
        await http_client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/medical/test_who_iris.py::test_search_guidelines_resolves_pdf_links_for_items -v`
Expected: FAIL (`assert "" == 'https://iris.who.int/server/api/core/bitstreams/bit-guideline-pdf/content'`).

- [ ] **Step 3: Implement `_resolve_pdf_bitstream` and integrate into `search_guidelines`**

In `src/scholar_mcp/medical/who_iris.py`:

```python
def _extract_pdf_link_from_item(item: dict[str, Any]) -> str:
    """Extract primary PDF bitstream content URL from an item dict if embedded."""
    embedded = item.get("_embedded") or {}
    bundles_container = embedded.get("bundles") or {}
    bundles = (bundles_container.get("_embedded") or {}).get("bundles") or []
    if not isinstance(bundles, list):
        return ""

    original = next((b for b in bundles if isinstance(b, dict) and b.get("name") == "ORIGINAL"), None)
    if not original:
        return ""

    bitstreams = ((original.get("_embedded") or {}).get("bitstreams")) or []
    if not isinstance(bitstreams, list):
        return ""

    pdfs = [
        b for b in bitstreams
        if isinstance(b, dict)
        and (
            (b.get("mimeType") or "").startswith("application/pdf")
            or (b.get("name") or "").lower().endswith(".pdf")
        )
    ]
    if not pdfs:
        return ""

    best_pdf = max(pdfs, key=lambda b: b.get("sizeBytes") or 0)
    content_href = (best_pdf.get("_links") or {}).get("content", {}).get("href")
    if content_href:
        return content_href
    uuid = best_pdf.get("uuid")
    if uuid:
        return f"{IRIS_BITSTREAM_CONTENT_URL}/{uuid}/content"
    return ""

class WHOIRISEngine:
    ...

    async def _resolve_pdf_bitstream(self, item_uuid: str) -> tuple[str, str, bool]:
        """Discover the primary PDF bitstream. Returns (pdf_url, bitstream_uuid, errored)."""
        if not item_uuid:
            return "", "", False
        try:
            bundles_resp = await self.http_client.get(
                f"{IRIS_ITEM_BUNDLES_URL}/{item_uuid}/bundles",
                params={"size": str(MAX_PAGE_SIZE)},
                headers={"Accept": "application/json"},
            )
            if bundles_resp is None:
                return "", "", True
            bundles = (bundles_resp.json().get("_embedded") or {}).get("bundles") or []
            original = next((b for b in bundles if b.get("name") == "ORIGINAL"), None)
            if original is None:
                return "", "", False

            bits_resp = await self.http_client.get(
                f"{IRIS_BUNDLE_BITSTREAMS_URL}/{original.get('uuid')}/bitstreams",
                params={"size": str(MAX_PAGE_SIZE)},
                headers={"Accept": "application/json"},
            )
            if bits_resp is None:
                return "", "", True
            bitstreams = (bits_resp.json().get("_embedded") or {}).get("bitstreams") or []
            pdfs = [
                b for b in bitstreams
                if (b.get("mimeType") or "").startswith("application/pdf")
                or (b.get("name") or "").lower().endswith(".pdf")
            ]
            if not pdfs:
                return "", "", False

            best = max(pdfs, key=lambda b: b.get("sizeBytes") or 0)
            best_uuid = best.get("uuid") or ""
            pdf_url = f"{IRIS_BITSTREAM_CONTENT_URL}/{best_uuid}/content" if best_uuid else ""
            return pdf_url, best_uuid, False
        except Exception:
            logger.warning("WHO IRIS bitstream resolution failed for item %s", item_uuid, exc_info=True)
            return "", "", True
```

In `search_guidelines`:
```python
        raw_items, errored = await self._fetch_paginated(url, params, extract, limit)
        if errored:
            records = [_build_record(item) for item in raw_items if item]
            return records, CacheMetadata(cached=False, cache_age=0, error=True)

        # Resolve PDF links concurrently for items that do not have embedded bitstreams
        async def _enrich_item(item: dict[str, Any]) -> WHOGuideline:
            rec = _build_record(item)
            if not rec.pdf_url and item.get("uuid"):
                pdf_url, _, _ = await self._resolve_pdf_bitstream(item.get("uuid") or "")
                rec.pdf_url = pdf_url
            return rec

        import asyncio
        records = await asyncio.gather(*[_enrich_item(item) for item in raw_items if item])
        records = list(records)

        await self.cache.set(cache_key, [r.to_dict() for r in records], source="who_iris")
        return records, CacheMetadata(cached=False, cache_age=0, error=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/medical/test_who_iris.py::test_search_guidelines_resolves_pdf_links_for_items tests/medical/test_who_iris.py::test_search_guidelines_item_bitstream_error_does_not_fail_search -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/medical/who_iris.py tests/medical/test_who_iris.py
git commit -m "feat(who-iris): resolve and attach direct PDF links in search results"
```

---

### Task 3: Complete Multi-Page Full-Text PDF Extraction and `pdf_url` Payload

**Files:**
- Modify: `src/scholar_mcp/medical/who_iris.py:192-310`
- Test: `tests/medical/test_who_iris.py`

**Interfaces:**
- `get_full_text(handle: str, max_chars: int | None = None) -> tuple[dict[str, Any], CacheMetadata]`
  - Returns `{status, handle, url, pdf_url, title, content_type: "pdf" | "abstract", content, truncated}`.

- [ ] **Step 1: Write test for multi-page full-text extraction and `pdf_url` field in `get_full_text`**

Add to `tests/medical/test_who_iris.py`:

```python
def make_multipage_pdf_bytes(pages_text: list[str]) -> bytes:
    """Helper to build real multi-page PDF bytes with extractable text using pypdf."""
    from pypdf import PdfWriter
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    import io

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for text in pages_text:
        c.drawString(100, 700, text)
        c.showPage()
    c.save()
    return buf.getvalue()

@respx.mock
async def test_get_full_text_returns_complete_multipage_pdf_and_pdf_url(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        page1 = "World Health Organization Clinical Management of Malaria 2023."
        page2 = "Section 2: Recommended artemisinin-based combination therapies (ACTs)."
        page3 = "Section 3: Special considerations for pregnant women and infants."
        real_pdf_bytes = make_multipage_pdf_bytes([page1, page2, page3])

        respx.get(IRIS_PID_FIND_URL).respond(json=_pid_find_item(handle="10665/311551"))
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-uuid-1/bundles").respond(
            json=_bundles_page([_bundle(uuid="bundle-uuid-1", name="ORIGINAL")])
        )
        respx.get(f"{IRIS_BUNDLE_BITSTREAMS_URL}/bundle-uuid-1/bitstreams").respond(
            json=_bitstreams_page([
                _bitstream(uuid="bit-malaria-pdf", name="who-malaria-guideline.pdf", size=len(real_pdf_bytes))
            ])
        )
        content_route = respx.get(f"{IRIS_BITSTREAM_CONTENT_URL}/bit-malaria-pdf/content").respond(
            content=real_pdf_bytes, headers={"Content-Type": "application/pdf"}
        )

        payload, meta = await engine.get_full_text("10665/311551")

        assert payload["status"] == "success"
        assert payload["content_type"] == "pdf"
        assert payload["pdf_url"] == f"{IRIS_BITSTREAM_CONTENT_URL}/bit-malaria-pdf/content"
        # Verify text from all 3 pages is extracted into content
        assert "Clinical Management of Malaria" in payload["content"]
        assert "artemisinin-based combination therapies" in payload["content"]
        assert "Special considerations for pregnant women" in payload["content"]
        assert payload["truncated"] is False
        assert meta.error is False
        assert content_route.call_count == 1
    finally:
        await cache.close()
        await http_client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/medical/test_who_iris.py::test_get_full_text_returns_complete_multipage_pdf_and_pdf_url -v`
Expected: FAIL (`KeyError: 'pdf_url'` or missing field in payload).

- [ ] **Step 3: Update `_extract_pdf_text` and `get_full_text` in `who_iris.py`**

In `src/scholar_mcp/medical/who_iris.py`:

```python
    async def _extract_pdf_text(self, item_uuid: str) -> tuple[str, str, bool]:
        """Extract the primary PDF's text and URL. Returns (text, pdf_url, errored)."""
        pdf_url, bitstream_uuid, errored = await self._resolve_pdf_bitstream(item_uuid)
        if errored or not pdf_url:
            return "", pdf_url, errored

        try:
            pdf_bytes = await self.http_client.get_bytes(pdf_url)
            if pdf_bytes is None:
                return "", pdf_url, True
            return pdf_bytes_to_text(pdf_bytes), pdf_url, False
        except Exception:
            logger.warning("WHO IRIS PDF download failed for bitstream %s", pdf_url, exc_info=True)
            return "", pdf_url, True
```

And in `get_full_text`:
```python
        pdf_text, pdf_url, errored = await self._extract_pdf_text(item.get("uuid") or "")
        if pdf_text:
            result = {"content_type": "pdf", "content": pdf_text, "pdf_url": pdf_url}
        elif abstract:
            result = {"content_type": "abstract", "content": abstract, "pdf_url": pdf_url}
        else:
            return (
                {**base, "status": "not_found", "error": "no full text or abstract available",
                 "title": title, "content_type": "none", "content": "", "pdf_url": ""},
                CacheMetadata(cached=False, cache_age=0, error=errored),
            )

        payload = {**base, "status": "success", "title": title, **result}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/medical/test_who_iris.py::test_get_full_text_returns_complete_multipage_pdf_and_pdf_url -v`
Expected: PASS.

- [ ] **Step 5: Run existing `test_who_iris.py` suite to ensure no regressions**

Run: `uv run pytest tests/medical/test_who_iris.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/medical/who_iris.py tests/medical/test_who_iris.py
git commit -m "feat(who-iris): return complete multi-page PDF content and pdf_url in get_full_text"
```

---

### Task 4: Server End-to-End Integration Tests

**Files:**
- Modify: `tests/medical/test_who_iris.py`
- Verify: `src/scholar_mcp/server.py`

**Interfaces:**
- FastMCP tool `search_who_iris_guidelines(query: str, limit: int, mode: str)`
- FastMCP tool `get_who_iris_full_text(handle: str, max_chars: int | None)`

- [ ] **Step 1: Write integration tests for FastMCP server tools**

Add to `tests/medical/test_who_iris.py`:

```python
@respx.mock
async def test_server_who_iris_tools_end_to_end(tmp_path: Path, monkeypatch):
    from scholar_mcp.server import search_who_iris_guidelines, get_who_iris_full_text
    import scholar_mcp.server as srv

    item = _iris_item(handle="10665/44626", title="Guideline: neonatal vitamin A supplementation")
    item_uuid = item["uuid"]

    respx.get(IRIS_BROWSE_TITLE_URL).respond(json=_browse_page([item]))
    respx.get(IRIS_PID_FIND_URL).respond(json=_pid_find_item(handle="10665/44626"))
    respx.get(f"{IRIS_ITEM_BUNDLES_URL}/{item_uuid}/bundles").respond(
        json=_bundles_page([_bundle(uuid="bundle-1", name="ORIGINAL")])
    )
    respx.get(f"{IRIS_BUNDLE_BITSTREAMS_URL}/bundle-1/bitstreams").respond(
        json=_bitstreams_page([_bitstream(uuid="bit-100", name="guideline.pdf", size=5000)])
    )
    respx.get(f"{IRIS_BITSTREAM_CONTENT_URL}/bit-100/content").respond(
        content=b"%PDF-fake", headers={"Content-Type": "application/pdf"}
    )

    import scholar_mcp.medical.who_iris as who_iris_mod
    monkeypatch.setattr(who_iris_mod, "pdf_bytes_to_text", lambda b: "Full guidelines content from PDF.")

    # 1. Search tool returns direct PDF link in data and Markdown
    search_res = await search_who_iris_guidelines("neonatal vitamin A")
    assert "data" in search_res
    assert search_res["data"][0]["pdf_url"] == f"{IRIS_BITSTREAM_CONTENT_URL}/bit-100/content"
    assert f"- **PDF:** {IRIS_BITSTREAM_CONTENT_URL}/bit-100/content" in search_res["markdown"]

    # 2. Full text tool returns full content and pdf_url
    ft_res = await get_who_iris_full_text("10665/44626")
    assert ft_res["status"] == "success"
    assert ft_res["content_type"] == "pdf"
    assert ft_res["content"] == "Full guidelines content from PDF."
    assert ft_res["pdf_url"] == f"{IRIS_BITSTREAM_CONTENT_URL}/bit-100/content"
```

- [ ] **Step 2: Run all test suites across the repository**

Run: `uv run pytest -v`
Expected: All unit, integration, and parser test suites PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/medical/test_who_iris.py
git commit -m "test(who-iris): add e2e server tool tests for WHO guideline search PDF links and full text"
```

---

## Verification Plan

### Automated Verification
- `uv run pytest tests/medical/test_who_iris.py -v`: Runs all WHO IRIS unit and integration tests.
- `uv run pytest -v`: Complete test suite verification ensuring zero regressions.

### Manual Verification
- Execute `search_who_iris_guidelines(query="malaria", limit=2)` and verify:
  - `data[0].pdf_url` contains the direct bitstream content link.
  - `markdown` contains `- **PDF:** https://...`.
- Execute `get_who_iris_full_text(handle="10665/311551")` and verify:
  - `status == "success"`
  - `content_type == "pdf"`
  - `pdf_url` matches the primary PDF bitstream download endpoint.
  - `content` contains clean text from all pages.
