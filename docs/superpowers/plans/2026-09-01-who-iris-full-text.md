# WHO IRIS Full-Text Retrieval Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `get_who_iris_full_text(handle, max_chars)` MCP tool that returns a WHO IRIS guideline's full text (PDF bitstream extraction, abstract fallback) by handle.

**Architecture:** New `WHOIRISEngine.get_full_text` method walks the DSpace 7 REST chain: `pid/find` → item → `ORIGINAL` bundle → largest PDF bitstream → download → `pdf_bytes_to_text`. Empty extraction or no PDF falls back to the item abstract. Results cached untruncated under source tag `who_iris` (existing 30d TTL); truncation at serve time. Server registers a thin tool wrapper using the standard medical try/except error shape.

**Tech Stack:** Python 3.12, FastMCP, httpx (via `AsyncHttpClient`), pypdf, respx, pytest-asyncio, SQLiteCacheManager.

**Spec:** User decisions (2026-09-01): (1) scope = WHO IRIS + clinical-guidelines full text; (2) exposed as separate retrieval tool, not a search flag; (3) PDF text with abstract fallback. Clinical-guidelines side needs **no new tool** — `search_clinical_guidelines` returns `MedicalArticle` dicts carrying `pmid`/`pmc_id`/`doi` (`medical/models.py:180-198`), and the existing `get_full_text` tool (`server.py:105-133`) already resolves those identifiers. That side is a README note only (Task 4).

## Global Constraints

- `AsyncHttpClient.get` returns `None` for status ≥ 400 unless the status is listed in `ok_statuses` (`utils/http.py:94-137`). To distinguish 404 from network failure on `pid/find`, pass `ok_statuses=frozenset({404})` and check `resp.status_code`.
- No cache write when the fetch chain errored partway (convention from `who_iris.py:165-171`).
- Every async test closes `cache` and `http_client` in `try/finally` (pytest shutdown hang otherwise — see `tests/medical/test_who_iris.py`).
- Cache source tag `who_iris` reuses `settings.cache_ttl_who_iris` (30d); no `sqlite_cache.py` or `config.py` change.
- Error shape for tool exceptions: `{"status": "error", "error": str(ex), "source": "who-iris"}`; not-found uses `{"status": "not_found", ...}` (convention from `server.py:182-184`).
- Full text cached **untruncated**; `max_chars` applied at serve time for both fresh and cached hits. Default 50,000 chars (matches `get_full_text` docstring convention).
- DSpace endpoints: `GET /server/api/pid/find?id=hdl:<handle>` (item json: `uuid`, `name`, `metadata`), `GET /server/api/core/items/{uuid}/bundles`, `GET /server/api/core/bundles/{bundle_uuid}/bitstreams`, `GET /server/api/core/bitstreams/{uuid}/content`. If `pid/find` rejects the `hdl:` prefix against the live API, fall back to the bare handle — note this in the PR if it happens.

---

### Task 1: Engine core — handle resolution, abstract fallback, caching, errors

**Files:**
- Create: `src/scholar_mcp/utils/text.py`
- Modify: `src/scholar_mcp/resolver.py:35-50` (replace local `_truncate_content` with import)
- Modify: `src/scholar_mcp/medical/who_iris.py`
- Test: `tests/medical/test_who_iris.py`

**Interfaces:**
- Consumes: existing `_first_meta`, `AsyncHttpClient.get/get_bytes`, `SQLiteCacheManager.get/set`, `CacheMetadata`.
- Produces: `truncate_content(content: str, max_chars: int) -> tuple[str, bool]` in `scholar_mcp.utils.text`; `WHOIRISEngine.get_full_text(handle: str, max_chars: int | None = None) -> tuple[dict[str, Any], CacheMetadata]` where the dict is the payload: `{"status": "success" | "not_found" | "error", "source": "who-iris", "handle": str, "url": str, "title": str, "content_type": "pdf" | "abstract" | "none", "content": str, "truncated": bool}` (Task 2 adds the pdf branch; Task 3 wraps it).

- [ ] **Step 1: Write failing tests for normalization, not-found, abstract fallback, cache**

Append to `tests/medical/test_who_iris.py` (reuse existing `_engine`, and add fixture builders near the existing ones):

```python
from scholar_mcp.medical.who_iris import (
    IRIS_BITSTREAM_CONTENT_URL,
    IRIS_BUNDLE_BITSTREAMS_URL,
    IRIS_ITEM_BUNDLES_URL,
    IRIS_PID_FIND_URL,
)

def _pid_find_item(handle: str = "10665/311551", abstract: str = "Recommendations for malaria.") -> dict:
    return {
        "uuid": "item-uuid-1",
        "handle": handle,
        "name": "WHO malaria guideline",
        "metadata": {
            "dc.title": [{"value": "WHO malaria guideline"}],
            "dc.description.abstract": [{"value": abstract}],
        },
    }

def _bundles_page(bundles: list[dict]) -> dict:
    return {"_embedded": {"bundles": bundles}, "page": {"totalPages": 1}}

def _bundle(uuid: str = "bundle-uuid-1", name: str = "ORIGINAL") -> dict:
    return {"uuid": uuid, "name": name, "metadata": []}

def _bitstreams_page(bits: list[dict]) -> dict:
    return {"_embedded": {"bitstreams": bits}, "page": {"totalPages": 1}}

def _bitstream(uuid: str = "bit-1", mime: str = "application/pdf", size: int = 999, name: str = "guideline.pdf") -> dict:
    return {"uuid": uuid, "name": name, "mimeType": mime, "sizeBytes": size}
```

Tests:

```python
@respx.mock
async def test_get_full_text_accepts_full_url_and_hdl_prefix(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(IRIS_PID_FIND_URL).respond(json=_pid_find_item())
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-uuid-1/bundles").respond(json=_bundles_page([]))
        for handle in (
            "https://iris.who.int/handle/10665/311551",
            "hdl:10665/311551",
            "10665/311551",
        ):
            route.calls.clear()
            payload, meta = await engine.get_full_text(handle)
            assert payload["status"] == "success"
            assert payload["content_type"] == "abstract"  # no ORIGINAL bundle -> fallback
            assert payload["content"] == "Recommendations for malaria."
            assert route.calls[0].request.url.params["id"] == "hdl:10665/311551"
    finally:
        await cache.close()
        await http_client.close()

@respx.mock
async def test_get_full_text_item_not_found(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(IRIS_PID_FIND_URL).respond(status_code=404)
        payload, meta = await engine.get_full_text("10665/000000")
        assert payload["status"] == "not_found"
        assert meta.cached is False and meta.error is False
    finally:
        await cache.close()
        await http_client.close()

@respx.mock
async def test_get_full_text_network_failure_is_error_not_cached(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(IRIS_PID_FIND_URL).mock(side_effect=httpx.ConnectError("boom"))
        payload, meta = await engine.get_full_text("10665/311551")
        assert payload["status"] == "error"
        assert meta.error is True
    finally:
        await cache.close()
        await http_client.close()

@respx.mock
async def test_get_full_text_caches_success(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        find_route = respx.get(IRIS_PID_FIND_URL).respond(json=_pid_find_item())
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-uuid-1/bundles").respond(json=_bundles_page([]))
        first, meta1 = await engine.get_full_text("10665/311551")
        second, meta2 = await engine.get_full_text("10665/311551")
        assert meta1.cached is False and meta2.cached is True
        assert second["content"] == first["content"]
        assert len(find_route.calls) == 1
    finally:
        await cache.close()
        await http_client.close()
```

- [ ] **Step 2: Run tests, verify failure**

Run: `uv run pytest tests/medical/test_who_iris.py -k full_text -x -q`
Expected: FAIL — `AttributeError: ... no attribute 'get_full_text'` (and ImportError on the new URL constants).

- [ ] **Step 3: Create `utils/text.py` and rewire resolver**

`src/scholar_mcp/utils/text.py` — move `_truncate_content` verbatim from `resolver.py:35-50`, renamed:

```python
def truncate_content(content: str, max_chars: int) -> tuple[str, bool]:
    """Truncate at a paragraph boundary when one is close to the cutoff."""
    if len(content) <= max_chars:
        return content, False

    cutoff = max_chars
    para_break = content.rfind("\n\n", 0, cutoff)
    if para_break > int(cutoff * 0.7):
        truncated_text = content[:para_break].rstrip()
    else:
        truncated_text = content[:cutoff].rstrip()

    marker = "\n\n[... Truncated due to max_chars limit ...]"
    return truncated_text + marker, True
```

In `src/scholar_mcp/resolver.py`: delete the local def, add `from scholar_mcp.utils.text import truncate_content as _truncate_content` (keeps all internal call sites unchanged).

- [ ] **Step 4: Implement engine core in `who_iris.py`**

Add constants next to the existing ones (`who_iris.py:9-15`):

```python
IRIS_PID_FIND_URL = f"{IRIS_API_BASE}/pid/find"
IRIS_ITEM_BUNDLES_URL = f"{IRIS_API_BASE}/core/items"
IRIS_BUNDLE_BITSTREAMS_URL = f"{IRIS_API_BASE}/core/bundles"
IRIS_BITSTREAM_CONTENT_URL = f"{IRIS_API_BASE}/core/bitstreams"
MAX_FULL_TEXT_CHARS = 50_000
```

Add imports: `from scholar_mcp.parsers.pdf import pdf_bytes_to_text`, `from scholar_mcp.utils.text import truncate_content`.

Add module function + method (after `search_guidelines`):

```python
def _normalize_handle(handle: str) -> str:
    h = handle.strip()
    if "/handle/" in h:
        h = h.split("/handle/", 1)[1]
    if h.lower().startswith("hdl:"):
        h = h[4:]
    return h.strip("/")
```

```python
async def get_full_text(
    self,
    handle: str,
    max_chars: int | None = None,
) -> tuple[dict[str, Any], CacheMetadata]:
    normalized = _normalize_handle(handle)
    base = {
        "source": "who-iris",
        "handle": normalized,
        "url": f"{IRIS_HANDLE_BASE}/{normalized}" if normalized else "",
        "truncated": False,
    }
    if not normalized:
        return (
            {**base, "status": "error", "error": "handle is required",
             "title": "", "content_type": "none", "content": ""},
            CacheMetadata(cached=False, cache_age=0, error=True),
        )

    cache_key = f"who_iris_fulltext:{normalized}"
    cached_data, meta = await self.cache.get(cache_key)
    if meta.cached and cached_data is not None:
        return self._serve_full_text(cached_data, max_chars), meta

    # ok_statuses lets a 404 through so not-found is distinguishable from a
    # network failure (get() otherwise collapses both to None).
    resp = await self.http_client.get(
        IRIS_PID_FIND_URL,
        params={"id": f"hdl:{normalized}"},
        headers={"Accept": "application/json"},
        ok_statuses=frozenset({404}),
    )
    if resp is None:
        return (
            {**base, "status": "error", "error": "who iris request failed",
             "title": "", "content_type": "none", "content": ""},
            CacheMetadata(cached=False, cache_age=0, error=True),
        )
    if resp.status_code == 404:
        return (
            {**base, "status": "not_found", "error": "no item for handle",
             "title": "", "content_type": "none", "content": ""},
            CacheMetadata(cached=False, cache_age=0, error=False),
        )

    item = resp.json()
    metadata = item.get("metadata") or {}
    title = _first_meta(metadata, "dc.title") or item.get("name") or ""
    abstract = (
        _first_meta(metadata, "dc.description.abstract")
        or _first_meta(metadata, "dc.description")
    )

    # PDF bitstream extraction is added in the next task; for now always
    # fall through to the abstract so the core path is testable.
    pdf_text = await self._extract_pdf_text(item.get("uuid") or "")

    errored = False  # wired to chain failures in the next task
    if pdf_text:
        result = {"content_type": "pdf", "content": pdf_text}
    elif abstract:
        result = {"content_type": "abstract", "content": abstract}
    else:
        return (
            {**base, "status": "not_found", "error": "no full text or abstract available",
             "title": title, "content_type": "none", "content": ""},
            CacheMetadata(cached=False, cache_age=0, error=errored),
        )

    payload = {**base, "status": "success", "title": title, **result}
    if not errored:
        await self.cache.set(cache_key, payload, source="who_iris")
    return self._serve_full_text(payload, max_chars), CacheMetadata(cached=False, cache_age=0, error=errored)

@staticmethod
def _serve_full_text(payload: dict[str, Any], max_chars: int | None) -> dict[str, Any]:
    served = dict(payload)
    content, truncated = truncate_content(served.get("content") or "", max_chars or MAX_FULL_TEXT_CHARS)
    served["content"] = content
    served["truncated"] = truncated
    return served
```

`_extract_pdf_text` is a stub for this task — `async def _extract_pdf_text(self, item_uuid: str) -> str: return ""`.

- [ ] **Step 5: Run new tests + resolver regression**

Run: `uv run pytest tests/medical/test_who_iris.py tests/test_waterfall_resolver.py tests/test_pdf_parser.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/utils/text.py src/scholar_mcp/resolver.py src/scholar_mcp/medical/who_iris.py tests/medical/test_who_iris.py
git commit -m "feat(who-iris): resolve guideline items by handle with abstract fallback"
```

---

### Task 2: PDF bitstream chain + truncation behavior

**Files:**
- Modify: `src/scholar_mcp/medical/who_iris.py`
- Test: `tests/medical/test_who_iris.py`

**Interfaces:**
- Consumes: `WHOIRISEngine.get_full_text` (Task 1), fixture builders (Task 1), `tests.test_pdf_parser.make_blank_pdf`.
- Produces: working `_extract_pdf_text(item_uuid) -> str` — bundles → ORIGINAL → largest `application/pdf` bitstream → `get_bytes` → `pdf_bytes_to_text`; `""` on any gap.

- [ ] **Step 1: Write failing tests**

```python
@respx.mock
async def test_get_full_text_extracts_pdf_text(tmp_path: Path, monkeypatch):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        import scholar_mcp.medical.who_iris as who_iris_mod
        monkeypatch.setattr(who_iris_mod, "pdf_bytes_to_text", lambda b: "extracted guideline text")

        respx.get(IRIS_PID_FIND_URL).respond(json=_pid_find_item(abstract="unused abstract"))
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-uuid-1/bundles").respond(
            json=_bundles_page([_bundle(), _bundle(uuid="bundle-uuid-2", name="THUMBNAIL")]))
        bits_route = respx.get(f"{IRIS_BUNDLE_BITSTREAMS_URL}/bundle-uuid-1/bitstreams").respond(
            json=_bitstreams_page([
                _bitstream(uuid="bit-small", size=100),
                _bitstream(uuid="bit-big", size=5000),
                _bitstream(uuid="bit-html", mime="text/html", size=9000),
            ]))
        content_route = respx.get(f"{IRIS_BITSTREAM_CONTENT_URL}/bit-big/content").respond(
            content=b"%PDF-fake", headers={"Content-Type": "application/pdf"})

        payload, meta = await engine.get_full_text("10665/311551")
        assert payload["status"] == "success"
        assert payload["content_type"] == "pdf"
        assert payload["content"] == "extracted guideline text"
        assert payload["truncated"] is False
        assert meta.error is False
        assert content_route.call_count == 1  # largest PDF chosen, not the html bitstream
        assert bits_route.call_count == 1
    finally:
        await cache.close()
        await http_client.close()

@respx.mock
async def test_get_full_text_empty_extraction_falls_back_to_abstract(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(IRIS_PID_FIND_URL).respond(json=_pid_find_item(abstract="abstract fallback text"))
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-uuid-1/bundles").respond(json=_bundles_page([_bundle()]))
        respx.get(f"{IRIS_BUNDLE_BITSTREAMS_URL}/bundle-uuid-1/bitstreams").respond(
            json=_bitstreams_page([_bitstream()]))
        # Real parser: blank PDF extracts to "" -> abstract fallback
        respx.get(f"{IRIS_BITSTREAM_CONTENT_URL}/bit-1/content").respond(
            content=make_blank_pdf(), headers={"Content-Type": "application/pdf"})

        payload, meta = await engine.get_full_text("10665/311551")
        assert payload["content_type"] == "abstract"
        assert payload["content"] == "abstract fallback text"
    finally:
        await cache.close()
        await http_client.close()

@respx.mock
async def test_get_full_text_bundle_fetch_failure_degrades_to_abstract_without_cache(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(IRIS_PID_FIND_URL).respond(json=_pid_find_item(abstract="degraded"))
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-uuid-1/bundles").respond(status_code=500)

        payload, meta = await engine.get_full_text("10665/311551")
        assert payload["status"] == "success"
        assert payload["content_type"] == "abstract"
        assert meta.error is True  # no cache write for degraded results
    finally:
        await cache.close()
        await http_client.close()

@respx.mock
async def test_get_full_text_truncates_served_content_not_cached(tmp_path: Path, monkeypatch):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        import scholar_mcp.medical.who_iris as who_iris_mod
        monkeypatch.setattr(who_iris_mod, "pdf_bytes_to_text", lambda b: "x" * 100)

        respx.get(IRIS_PID_FIND_URL).respond(json=_pid_find_item())
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-uuid-1/bundles").respond(json=_bundles_page([_bundle()]))
        respx.get(f"{IRIS_BUNDLE_BITSTREAMS_URL}/bundle-uuid-1/bitstreams").respond(
            json=_bitstreams_page([_bitstream()]))
        respx.get(f"{IRIS_BITSTREAM_CONTENT_URL}/bit-1/content").respond(content=b"%PDF-fake")

        first, _ = await engine.get_full_text("10665/311551", max_chars=20)
        assert first["truncated"] is True
        assert len(first["content"]) < 100
        # served again from cache with a different limit: cache holds the full text
        second, meta2 = await engine.get_full_text("10665/311551", max_chars=100_000)
        assert meta2.cached is True
        assert len(second["content"]) == 100
    finally:
        await cache.close()
        await http_client.close()
```

Also add `from tests.test_pdf_parser import make_blank_pdf` (or copy the helper if imports across test dirs are not configured — check for an existing cross-test import pattern first).

- [ ] **Step 2: Run tests, verify failure**

Run: `uv run pytest tests/medical/test_who_iris.py -k full_text -q`
Expected: FAIL — `test_get_full_text_extracts_pdf_text` gets `content_type == "abstract"` (stub returns `""`), truncation test fails on `len(second["content"]) == 100`.

- [ ] **Step 3: Implement `_extract_pdf_text` and errored wiring**

Replace the stub:

```python
async def _extract_pdf_text(self, item_uuid: str) -> tuple[str, bool]:
    """Extract the primary PDF's text. Returns (text, errored)."""
    if not item_uuid:
        return "", False
    try:
        bundles_resp = await self.http_client.get(
            f"{IRIS_ITEM_BUNDLES_URL}/{item_uuid}/bundles",
            headers={"Accept": "application/json"},
        )
        if bundles_resp is None:
            return "", True
        bundles = (bundles_resp.json().get("_embedded") or {}).get("bundles") or []
        original = next((b for b in bundles if b.get("name") == "ORIGINAL"), None)
        if original is None:
            return "", False

        bits_resp = await self.http_client.get(
            f"{IRIS_BUNDLE_BITSTREAMS_URL}/{original.get('uuid')}/bitstreams",
            headers={"Accept": "application/json"},
        )
        if bits_resp is None:
            return "", True
        bitstreams = (bits_resp.json().get("_embedded") or {}).get("bitstreams") or []
        pdfs = [b for b in bitstreams if (b.get("mimeType") or "").startswith("application/pdf")]
        if not pdfs:
            return "", False

        best = max(pdfs, key=lambda b: b.get("sizeBytes") or 0)
        pdf_bytes = await self.http_client.get_bytes(
            f"{IRIS_BITSTREAM_CONTENT_URL}/{best.get('uuid')}/content"
        )
        if pdf_bytes is None:
            return "", True
        return pdf_bytes_to_text(pdf_bytes), False
    except Exception:
        logger.warning("WHO IRIS full-text fetch failed for item %s", item_uuid, exc_info=True)
        return "", True
```

Update the call site in `get_full_text` from Task 1:

```python
pdf_text, errored = await self._extract_pdf_text(item.get("uuid") or "")
```

and delete the `errored = False` placeholder line.

- [ ] **Step 4: Run tests, verify pass**

Run: `uv run pytest tests/medical/test_who_iris.py -q`
Expected: all PASS (old search tests included).

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/medical/who_iris.py tests/medical/test_who_iris.py
git commit -m "feat(who-iris): extract full text from guideline PDF bitstreams"
```

---

### Task 3: MCP tool registration + shared guard

**Files:**
- Modify: `src/scholar_mcp/server.py` (after `search_who_iris_guidelines`, ~line 546)
- Modify: `tests/test_server_medical.py:19` (MEDICAL_TOOLS set)
- Test: `tests/test_server_medical.py`

**Interfaces:**
- Consumes: `who_iris_engine.get_full_text(handle, max_chars=None) -> (payload_dict, CacheMetadata)` (Tasks 1-2).
- Produces: MCP tool `get_who_iris_full_text(handle: str, max_chars: int | None = None) -> dict[str, Any]` — payload dict plus `"cache": {"cached": bool, "cache_age": int}`.

- [ ] **Step 1: Write failing guard test**

In `tests/test_server_medical.py`, add `"get_who_iris_full_text"` to the `MEDICAL_TOOLS` set (line ~19). The existing `test_all_medical_tools_registered` and `test_medical_tools_return_dict_type_annotation` then enforce registration and annotation. Run them:

Run: `uv run pytest tests/test_server_medical.py -q`
Expected: FAIL — tool not registered.

- [ ] **Step 2: Register the tool in `server.py`**

```python
@mcp.tool()
async def get_who_iris_full_text(
    handle: str,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Retrieve full text of a WHO IRIS guideline by handle.

    Downloads the item's primary PDF from the WHO IRIS repository and extracts
    its text. Falls back to the item abstract when no PDF is available.

    Args:
        handle: IRIS handle — bare ("10665/311551"), "hdl:"-prefixed, or the
            full landing-page URL. Handles are returned by
            search_who_iris_guidelines as `handle`.
        max_chars: Maximum character limit for the returned text (defaults to 50,000).
    """
    try:
        payload, meta = await who_iris_engine.get_full_text(handle, max_chars=max_chars)
        payload["cache"] = {"cached": meta.cached, "cache_age": meta.cache_age}
        return payload
    except Exception as ex:
        return {"status": "error", "error": str(ex), "source": "who-iris", "content": ""}
```

- [ ] **Step 3: Run guard + who-iris suites**

Run: `uv run pytest tests/test_server_medical.py tests/medical/test_who_iris.py -q`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add src/scholar_mcp/server.py tests/test_server_medical.py
git commit -m "feat(who-iris): register get_who_iris_full_text MCP tool"
```

---

### Task 4: Docs

**Files:**
- Modify: `README.md` (~line 236 tool table; clinical-guidelines row if present)

- [ ] **Step 1: README rows**

Add to the "Medical tools" table:

```markdown
| `get_who_iris_full_text` | WHO IRIS | Fetch a WHO IRIS guideline's full text by handle: extracts the primary PDF, falls back to the abstract. Cached 30d (`CACHE_TTL_WHO_IRIS`). |
```

In the `search_clinical_guidelines` row description (or directly beneath the table), add: full text for these PubMed results is available through the existing `get_full_text` tool using the returned `doi`, `pmid`, or `pmc_id`.

- [ ] **Step 2: Full test suite**

Run: `uv run pytest -q`
Expected: all PASS, no shutdown hang.

- [ ] **Step 3: Manual smoke against live IRIS (optional but recommended)**

```bash
uv run python -c "
import asyncio
from scholar_mcp.medical.who_iris import WHOIRISEngine
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager
from scholar_mcp.config import Settings

async def main():
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path='/tmp/who-iris-smoke.db', settings=settings)
    engine = WHOIRISEngine(http_client=http_client, cache=cache, settings=settings)
    try:
        payload, meta = await engine.get_full_text('10665/311551', max_chars=2000)
        print(payload['status'], payload['content_type'], len(payload['content']), payload['url'])
    finally:
        await cache.close()
        await http_client.close()

asyncio.run(main())
"
```

Expected: `success pdf <n> https://iris.who.int/handle/10665/311551` (or `abstract` if that item has no PDF — verify the endpoint/param assumptions from Global Constraints if it errors).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(who-iris): document get_who_iris_full_text and clinical-guidelines full-text path"
```

---

## Plan-file copy

On execution start, copy this plan to `docs/superpowers/plans/2026-09-01-who-iris-full-text.md` (project convention; plan mode only permitted writing it to the scratch plan path).

## Verification summary

- `uv run pytest tests/medical/test_who_iris.py tests/test_server_medical.py tests/test_waterfall_resolver.py tests/test_pdf_parser.py -q` after each task; full `uv run pytest -q` at the end.
- Live smoke of `get_full_text('10665/311551')` (Task 4 Step 3) validates the DSpace endpoint assumptions against the real API.
