import io
from pathlib import Path

import httpx
import respx
from pypdf import PdfWriter

from scholar_mcp.config import Settings
from scholar_mcp.medical.who_iris import (
    IRIS_BITSTREAM_CONTENT_URL,
    IRIS_BUNDLE_BITSTREAMS_URL,
    IRIS_BROWSE_TITLE_URL,
    IRIS_ITEM_BUNDLES_URL,
    IRIS_PID_FIND_URL,
    IRIS_SEARCH_URL,
    WHOIRISEngine,
    _all_meta,
    _first_meta,
)
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager


async def _engine(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = WHOIRISEngine(http_client=http_client, cache=cache, settings=settings)
    return engine, cache, http_client


def _browse_page(items, page=0, total_pages=1, total_elements=None):
    return {
        "_embedded": {"items": items},
        "page": {
            "number": page,
            "size": len(items),
            "totalPages": total_pages,
            "totalElements": total_elements if total_elements is not None else len(items),
        },
    }


def _iris_item(
    handle="10665/44626",
    title="Guideline: neonatal vitamin A supplementation",
    pdf_url: str = "",
):
    item = {
        "id": "20a72e4a-b421-418f-87d7-7712e5f77f74",
        "uuid": "20a72e4a-b421-418f-87d7-7712e5f77f74",
        "handle": handle,
        "name": title,
        "metadata": {
            "dc.title": [{"value": title}],
            "dc.date.issued": [{"value": "1998"}],
            "dc.description.abstract": [{"value": "Guidance on neonatal vitamin A supplementation."}],
            "dc.contributor.author": [{"value": "World Health Organization"}],
            "dc.language.iso": [{"value": "en"}],
            "dc.subject.mesh": [{"value": "Vitamin A"}, {"value": "Infant, Newborn"}],
            "dc.identifier.isbn": [{"value": "9789241547965"}],
            "dc.publisher": [{"value": "World Health Organization"}],
        },
    }
    if pdf_url:
        item["_embedded"] = {
            "bundles": {
                "_embedded": {
                    "bundles": [
                        {
                            "name": "ORIGINAL",
                            "_embedded": {
                                "bitstreams": [
                                    {
                                        "uuid": "bit-1",
                                        "name": "guideline.pdf",
                                        "_links": {"content": {"href": pdf_url}},
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        }
    return item


@respx.mock
async def test_search_guidelines_prefix_mode_parses_item(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(IRIS_BROWSE_TITLE_URL).respond(json=_browse_page([_iris_item()]))

        guidelines, meta = await engine.search_guidelines("guideline", limit=10, mode="prefix")

        request_params = route.calls[0].request.url.params
        assert request_params["startsWith"] == "guideline"
        assert request_params["size"] == "10"
        assert request_params["page"] == "0"
        assert "filter" not in request_params  # the browse endpoint ignores it

        assert len(guidelines) == 1
        g = guidelines[0]
        assert g.title == "Guideline: neonatal vitamin A supplementation"
        assert g.handle == "10665/44626"
        assert g.url == "https://iris.who.int/handle/10665/44626"
        assert g.year == "1998"
        assert g.authors == ["World Health Organization"]
        assert g.languages == ["en"]
        assert g.mesh_subjects == ["Vitamin A", "Infant, Newborn"]
        assert g.isbn == "9789241547965"
        assert g.source == "who-iris"
        assert g.item_type == ""  # browse payloads carry no dc.type to read
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


def test_first_meta_returns_first_value():
    metadata = {"dc.title": [{"value": "Guideline A"}, {"value": "Guideline A (alt)"}]}
    assert _first_meta(metadata, "dc.title") == "Guideline A"


def test_first_meta_missing_key_returns_empty_string():
    assert _first_meta({}, "dc.title") == ""
    assert _first_meta({"dc.title": []}, "dc.title") == ""


def test_first_meta_skips_entry_missing_value_field():
    metadata = {"dc.date.issued": [{"language": "en"}]}
    assert _first_meta(metadata, "dc.date.issued") == ""


def test_all_meta_returns_every_value():
    metadata = {"dc.contributor.author": [{"value": "World Health Organization"}, {"value": "WHO Regional Office"}]}
    assert _all_meta(metadata, "dc.contributor.author") == [
        "World Health Organization",
        "WHO Regional Office",
    ]


def test_all_meta_missing_key_returns_empty_list():
    assert _all_meta({}, "dc.subject.mesh") == []


def test_all_meta_skips_non_dict_and_empty_value_entries():
    metadata = {"dc.subject": [{"value": "Vitamin A"}, "not-a-dict", {"value": ""}, {}]}
    assert _all_meta(metadata, "dc.subject") == ["Vitamin A"]


@respx.mock
async def test_search_guidelines_prefix_mode_paginates_across_pages(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        page0_items = [_iris_item(handle=f"10665/{100 + i}") for i in range(20)]
        page1_items = [_iris_item(handle=f"10665/{200 + i}") for i in range(5)]
        route = respx.get(IRIS_BROWSE_TITLE_URL)
        route.side_effect = [
            httpx.Response(200, json=_browse_page(page0_items, page=0, total_pages=2, total_elements=25)),
            httpx.Response(200, json=_browse_page(page1_items, page=1, total_pages=2, total_elements=25)),
        ]

        guidelines, meta = await engine.search_guidelines("guideline", limit=25, mode="prefix")

        assert len(guidelines) == 25
        assert route.call_count == 2
        assert route.calls[0].request.url.params["page"] == "0"
        assert route.calls[1].request.url.params["page"] == "1"
        assert route.calls[0].request.url.params["size"] == "25"
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_guidelines_prefix_mode_stops_after_limit_within_one_page(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        page0_items = [_iris_item(handle=f"10665/{100 + i}") for i in range(20)]
        route = respx.get(IRIS_BROWSE_TITLE_URL).respond(
            json=_browse_page(page0_items, page=0, total_pages=2, total_elements=25)
        )

        guidelines, meta = await engine.search_guidelines("guideline", limit=10, mode="prefix")

        assert len(guidelines) == 10
        assert route.call_count == 1
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_guidelines_marks_error_and_skips_cache_on_failure(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(IRIS_BROWSE_TITLE_URL).mock(side_effect=httpx.ConnectError("boom"))

        guidelines, meta = await engine.search_guidelines("guideline", limit=10, mode="prefix")
        assert guidelines == []
        assert meta.error is True

        after_first = route.call_count
        await engine.search_guidelines("guideline", limit=10, mode="prefix")
        assert route.call_count > after_first
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_guidelines_empty_result_is_cached(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(IRIS_BROWSE_TITLE_URL).respond(json=_browse_page([], total_elements=0))

        guidelines, meta = await engine.search_guidelines("zzzznomatch", limit=10, mode="prefix")
        assert guidelines == []
        assert meta.error is False

        after_first = route.call_count
        await engine.search_guidelines("zzzznomatch", limit=10, mode="prefix")
        assert route.call_count == after_first  # served from cache, no second request
    finally:
        await cache.close()
        await http_client.aclose()


def _search_page(items, page=0, total_pages=1, total_elements=None):
    return {
        "_embedded": {
            "searchResult": {
                "_embedded": {
                    "objects": [{"_embedded": {"indexableObject": item}} for item in items]
                },
                "page": {
                    "number": page,
                    "size": len(items),
                    "totalPages": total_pages,
                    "totalElements": total_elements if total_elements is not None else len(items),
                },
            }
        }
    }


@respx.mock
async def test_search_guidelines_fulltext_mode_parses_item(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(IRIS_SEARCH_URL).respond(json=_search_page([_iris_item()]))

        guidelines, meta = await engine.search_guidelines("vitamin A", limit=10, mode="fulltext")

        request_params = route.calls[0].request.url.params
        assert request_params["query"] == "vitamin A"
        assert request_params["f.itemtype"] == "Publications,equals"  # this endpoint honours it
        assert len(guidelines) == 1
        assert guidelines[0].title == "Guideline: neonatal vitamin A supplementation"
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_guidelines_fulltext_mode_paginates(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        page0_items = [_iris_item(handle=f"10665/{300 + i}") for i in range(20)]
        page1_items = [_iris_item(handle=f"10665/{400 + i}") for i in range(3)]
        route = respx.get(IRIS_SEARCH_URL)
        route.side_effect = [
            httpx.Response(200, json=_search_page(page0_items, page=0, total_pages=2, total_elements=23)),
            httpx.Response(200, json=_search_page(page1_items, page=1, total_pages=2, total_elements=23)),
        ]

        guidelines, meta = await engine.search_guidelines("vitamin", limit=23, mode="fulltext")

        assert len(guidelines) == 23
        assert route.call_count == 2
        assert route.calls[0].request.url.params["page"] == "0"
        assert route.calls[1].request.url.params["page"] == "1"
        assert route.calls[0].request.url.params["size"] == "23"
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_guidelines_fulltext_mode_marks_error_and_skips_cache(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(IRIS_SEARCH_URL).mock(side_effect=httpx.ConnectError("boom"))

        guidelines, meta = await engine.search_guidelines("vitamin", limit=10, mode="fulltext")
        assert guidelines == []
        assert meta.error is True

        after_first = route.call_count
        await engine.search_guidelines("vitamin", limit=10, mode="fulltext")
        assert route.call_count > after_first
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_guidelines_partial_failure_is_not_cached(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        page0_items = [_iris_item(handle=f"10665/{500 + i}") for i in range(20)]

        def _page1_always_fails(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("page") == "0":
                return httpx.Response(
                    200,
                    json=_browse_page(page0_items, page=0, total_pages=2, total_elements=25),
                )
            raise httpx.ConnectError("boom")

        route = respx.get(IRIS_BROWSE_TITLE_URL).mock(side_effect=_page1_always_fails)

        guidelines, meta = await engine.search_guidelines("guideline", limit=25, mode="prefix")
        assert len(guidelines) == 20
        assert meta.error is True

        after_first = route.call_count
        await engine.search_guidelines("guideline", limit=25, mode="prefix")
        assert route.call_count > after_first  # partial result was not cached
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_guidelines_clamps_limit_inside_engine(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        page_items = [
            _iris_item(
                handle=f"10665/{700 + i}",
                pdf_url=f"https://iris.who.int/server/api/core/bitstreams/bit-{i}/content",
            )
            for i in range(100)
        ]
        route = respx.get(IRIS_BROWSE_TITLE_URL).respond(
            json=_browse_page(page_items, total_pages=999, total_elements=99900)
        )

        guidelines, _meta = await engine.search_guidelines("guideline", limit=10_000, mode="prefix")

        assert route.call_count == 2  # clamped to 200 items: pages 0 and 1 only
        assert len(guidelines) == 200
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_guidelines_rejects_unknown_mode(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(IRIS_BROWSE_TITLE_URL).respond(json=_browse_page([_iris_item()]))

        guidelines, meta = await engine.search_guidelines("guideline", limit=10, mode="fulltxt")

        assert guidelines == []
        assert meta.error is True
        assert route.call_count == 0  # rejected before any HTTP traffic
    finally:
        await cache.close()
        await http_client.aclose()


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


@respx.mock
async def test_get_full_text_selects_pdf_by_name_when_mime_type_null(tmp_path: Path, monkeypatch):
    """Live IRIS payloads carry mimeType: null; the .pdf name suffix identifies PDFs."""
    engine, cache, http_client = await _engine(tmp_path)
    try:
        import scholar_mcp.medical.who_iris as who_iris_mod
        monkeypatch.setattr(who_iris_mod, "pdf_bytes_to_text", lambda b: "extracted guideline text")

        respx.get(IRIS_PID_FIND_URL).respond(json=_pid_find_item())
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-uuid-1/bundles").respond(json=_bundles_page([_bundle()]))
        content_route = respx.get(f"{IRIS_BITSTREAM_CONTENT_URL}/bit-null-mime/content").respond(
            content=b"%PDF-fake", headers={"Content-Type": "application/pdf"})
        respx.get(f"{IRIS_BUNDLE_BITSTREAMS_URL}/bundle-uuid-1/bitstreams").respond(
            json=_bitstreams_page([
                _bitstream(uuid="bit-null-mime", mime=None, size=4_949_229, name="9789242514735-fre.pdf"),
                _bitstream(uuid="bit-html", mime="text/html", size=9_000, name="index.html"),
            ]))

        payload, meta = await engine.get_full_text("10665/311551")
        assert payload["content_type"] == "pdf"
        assert payload["content"] == "extracted guideline text"
        assert meta.error is False
        assert content_route.call_count == 1
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_accepts_full_url_and_hdl_prefix(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(IRIS_PID_FIND_URL).respond(json=_pid_find_item())
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-uuid-1/bundles").respond(json=_bundles_page([]))
        # Distinct handles per iteration: all three normalize to a unique
        # item, so no call is served from the previous iteration's cache.
        for raw, normalized in (
            ("https://iris.who.int/handle/10665/311551", "10665/311551"),
            ("hdl:10665/311552", "10665/311552"),
            ("10665/311553", "10665/311553"),
        ):
            route.calls.clear()
            payload, meta = await engine.get_full_text(raw)
            assert payload["status"] == "success"
            assert payload["content_type"] == "abstract"  # no ORIGINAL bundle -> fallback
            assert payload["content"] == "Recommendations for malaria."
            assert payload["handle"] == normalized
            assert route.calls[0].request.url.params["id"] == f"hdl:{normalized}"
    finally:
        await cache.close()
        await http_client.aclose()


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
        await http_client.aclose()


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
        await http_client.aclose()


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
        await http_client.aclose()


def make_blank_pdf(pages: int = 1) -> bytes:
    """Copy of tests.test_pdf_parser.make_blank_pdf: no cross-test import pattern exists."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


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
                _bitstream(uuid="bit-html", mime="text/html", size=9000, name="index.html"),
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
        await http_client.aclose()


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
        await http_client.aclose()


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
        await http_client.aclose()


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
        # max_chars=0 is honored, not silently replaced by the default limit
        zero, _ = await engine.get_full_text("10665/311551", max_chars=0)
        assert zero["truncated"] is True
        assert zero["content"].endswith("[... Truncated due to max_chars limit ...]")
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_requests_full_page_size(tmp_path: Path):
    """Bundles/bitstreams lists must request the full DSpace page size, not the default 20."""
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(IRIS_PID_FIND_URL).respond(json=_pid_find_item())
        bundles_route = respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-uuid-1/bundles").respond(
            json=_bundles_page([_bundle()]))
        bits_route = respx.get(f"{IRIS_BUNDLE_BITSTREAMS_URL}/bundle-uuid-1/bitstreams").respond(
            json=_bitstreams_page([_bitstream(mime="text/html", name="index.html")]))
        payload, meta = await engine.get_full_text("10665/311554")
        assert payload["content_type"] == "abstract"
        assert meta.error is False
        assert bundles_route.calls[0].request.url.params["size"] == "100"
        assert bits_route.calls[0].request.url.params["size"] == "100"
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_strips_query_and_fragment(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(IRIS_PID_FIND_URL).respond(json=_pid_find_item())
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-uuid-1/bundles").respond(json=_bundles_page([]))
        for raw, normalized in (
            ("https://iris.who.int/handle/10665/311555?show=full", "10665/311555"),
            ("hdl:10665/311556#abstract", "10665/311556"),
        ):
            route.calls.clear()
            payload, _ = await engine.get_full_text(raw)
            assert payload["handle"] == normalized
            assert route.calls[0].request.url.params["id"] == f"hdl:{normalized}"
    finally:
        await cache.close()
        await http_client.aclose()


async def test_get_full_text_requires_handle(tmp_path: Path):
    """Whitespace-only handle errors out without any network call."""
    engine, cache, http_client = await _engine(tmp_path)
    try:
        payload, meta = await engine.get_full_text("   ")
        assert payload["status"] == "error"
        assert payload["error"] == "handle is required"
        assert meta.error is True
    finally:
        await cache.close()
        await http_client.aclose()


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


def make_multipage_pdf_bytes(pages_text: list[str]) -> bytes:
    """Build real multi-page PDF bytes with extractable text via pypdf pages."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    # In-memory Helvetica Type1 font shared by every page.
    def _font_dict() -> DictionaryObject:
        return DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )

    # Fresh indirect font object per content stream PDF (stable object layout).
    def _make_pdf_bytes(single_page_text: str) -> bytes:
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        escaped = (
            single_page_text.replace("\\", "\\\\")
            .replace("(", "\\(")
            .replace(")", "\\)")
        )
        stream = DecodedStreamObject()
        stream.set_data(
            f"BT /F1 12 Tf 20 150 Td ({escaped}) Tj ET".encode("latin-1")
        )
        font_ref = writer._add_object(_font_dict())
        resources = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        page[NameObject("/Resources")] = resources
        page[NameObject("/Contents")] = writer._add_object(stream)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    merged = PdfWriter()
    for text in pages_text:
        single = PdfReader(io.BytesIO(_make_pdf_bytes(text)))
        merged.add_page(single.pages[0])
    out = io.BytesIO()
    merged.write(out)
    return out.getvalue()


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


@respx.mock
async def test_server_who_iris_tools_end_to_end(tmp_path: Path, monkeypatch):
    """Server tools expose pdf_url in search data/markdown and full-text payloads."""
    import scholar_mcp.server as srv
    import scholar_mcp.medical.who_iris as who_iris_mod

    item = _iris_item(handle="10665/44626", title="Guideline: neonatal vitamin A supplementation")
    item_uuid = item["uuid"]

    respx.get(IRIS_BROWSE_TITLE_URL).respond(json=_browse_page([item]))
    pid_item = _pid_find_item(handle="10665/44626")
    pid_item["uuid"] = item_uuid
    respx.get(IRIS_PID_FIND_URL).respond(json=pid_item)
    respx.get(f"{IRIS_ITEM_BUNDLES_URL}/{item_uuid}/bundles").respond(
        json=_bundles_page([_bundle(uuid="bundle-1", name="ORIGINAL")])
    )
    respx.get(f"{IRIS_BUNDLE_BITSTREAMS_URL}/bundle-1/bitstreams").respond(
        json=_bitstreams_page([_bitstream(uuid="bit-100", name="guideline.pdf", size=5000)])
    )
    respx.get(f"{IRIS_BITSTREAM_CONTENT_URL}/bit-100/content").respond(
        content=b"%PDF-fake", headers={"Content-Type": "application/pdf"}
    )
    monkeypatch.setattr(
        who_iris_mod, "pdf_bytes_to_text", lambda b: "Full guidelines content from PDF."
    )
    monkeypatch.setattr(srv, "who_iris_engine", WHOIRISEngine(
        http_client=AsyncHttpClient(srv.settings),
        cache=SQLiteCacheManager(db_path=tmp_path / "e2e.db", settings=srv.settings),
        settings=srv.settings,
    ))
    try:
        search_res = await srv.search_who_iris_guidelines("neonatal vitamin A")
        assert "data" in search_res
        assert search_res["data"][0]["pdf_url"] == f"{IRIS_BITSTREAM_CONTENT_URL}/bit-100/content"
        assert f"- **PDF:** {IRIS_BITSTREAM_CONTENT_URL}/bit-100/content" in search_res["markdown"]

        ft_res = await srv.get_who_iris_full_text("10665/44626")
        assert ft_res["status"] == "success"
        assert ft_res["content_type"] == "pdf"
        assert ft_res["content"] == "Full guidelines content from PDF."
        assert ft_res["pdf_url"] == f"{IRIS_BITSTREAM_CONTENT_URL}/bit-100/content"
    finally:
        await srv.who_iris_engine.cache.close()
        await srv.who_iris_engine.http_client.aclose()


@respx.mock
async def test_resolve_pdf_bitstream_prefers_content_link_and_handles_safe_size(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        custom_content_url = "https://iris.who.int/custom/bitstreams/bit-special/content"
        bitstream_payload = {
            "uuid": "bit-special",
            "name": "malaria-policy.pdf",
            "mimeType": "application/pdf",
            "sizeBytes": "12345",  # string sizeBytes
            "_links": {"content": {"href": custom_content_url}},
        }
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-1/bundles").respond(
            json=_bundles_page([_bundle(uuid="b-1", name="ORIGINAL")])
        )
        respx.get(f"{IRIS_BUNDLE_BITSTREAMS_URL}/b-1/bitstreams").respond(
            json={"_embedded": {"bitstreams": [bitstream_payload]}}
        )

        pdf_url, best_uuid, errored = await engine._resolve_pdf_bitstream("item-1")
        assert pdf_url == custom_content_url
        assert best_uuid == "bit-special"
        assert errored is False
    finally:
        await cache.close()
        await http_client.aclose()


def test_extract_pdf_link_from_item_handles_string_size_and_content_link():
    from scholar_mcp.medical.who_iris import _extract_pdf_link_from_item

    item = {
        "_embedded": {
            "bundles": {
                "_embedded": {
                    "bundles": [
                        {
                            "name": "ORIGINAL",
                            "_embedded": {
                                "bitstreams": [
                                    {
                                        "uuid": "bit-small",
                                        "name": "doc.pdf",
                                        "sizeBytes": "100",
                                        "_links": {"content": {"href": "https://iris.who.int/bit-small/content"}},
                                    },
                                    {
                                        "uuid": "bit-large",
                                        "name": "doc_full.pdf",
                                        "sizeBytes": "9999",
                                        "_links": {"content": {"href": "https://iris.who.int/bit-large/content"}},
                                    },
                                ]
                            },
                        }
                    ]
                }
            }
        }
    }
    extracted = _extract_pdf_link_from_item(item)
    assert extracted == "https://iris.who.int/bit-large/content"


def test_safe_size_handles_non_finite_and_bad_values():
    from scholar_mcp.medical.who_iris import _safe_size

    assert _safe_size({"sizeBytes": float("nan")}) == 0
    assert _safe_size({"sizeBytes": float("inf")}) == 0
    assert _safe_size({"sizeBytes": "abc"}) == 0
    assert _safe_size({"sizeBytes": None}) == 0
    assert _safe_size({}) == 0
    assert _safe_size({"sizeBytes": 123}) == 123
    assert _safe_size({"sizeBytes": "123"}) == 123


def test_extract_pdf_link_survives_nan_size():
    from scholar_mcp.medical.who_iris import _extract_pdf_link_from_item

    item = {
        "_embedded": {
            "bundles": {
                "_embedded": {
                    "bundles": [
                        {
                            "name": "ORIGINAL",
                            "_embedded": {
                                "bitstreams": [
                                    {
                                        "uuid": "bit-nan",
                                        "name": "broken.pdf",
                                        "sizeBytes": float("nan"),
                                    },
                                    {
                                        "uuid": "bit-ok",
                                        "name": "ok.pdf",
                                        "sizeBytes": 4096,
                                    },
                                ]
                            },
                        }
                    ]
                }
            }
        }
    }
    extracted = _extract_pdf_link_from_item(item)
    assert extracted == f"{IRIS_BITSTREAM_CONTENT_URL}/bit-ok/content"


@respx.mock
async def test_search_guidelines_bitstream_error_not_cached(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        item = _iris_item(handle="10665/44626", title="Guideline A")
        respx.get(IRIS_BROWSE_TITLE_URL).respond(json=_browse_page([item]))
        bundles_route = respx.get(
            f"{IRIS_ITEM_BUNDLES_URL}/{item['uuid']}/bundles"
        ).respond(status_code=500)

        first, _ = await engine.search_guidelines("guideline", limit=1, mode="prefix")
        calls_after_first = bundles_route.call_count
        second, second_meta = await engine.search_guidelines(
            "guideline", limit=1, mode="prefix"
        )

        assert len(first) == 1 and first[0].pdf_url == ""
        # Enrichment errored: results must not be served from cache for the TTL.
        assert second_meta.cached is False
        assert bundles_route.call_count > calls_after_first
        assert second == first
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_not_found_includes_pdf_url(tmp_path: Path):
    """not_found payload keeps the resolved pdf_url even when the download fails."""
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(IRIS_PID_FIND_URL).respond(
            json=_pid_find_item(handle="10665/311551", abstract="")
        )
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-uuid-1/bundles").respond(
            json=_bundles_page([_bundle(uuid="bundle-uuid-1", name="ORIGINAL")])
        )
        respx.get(f"{IRIS_BUNDLE_BITSTREAMS_URL}/bundle-uuid-1/bitstreams").respond(
            json=_bitstreams_page([
                _bitstream(uuid="bit-x", name="guideline.pdf", size=1000)
            ])
        )
        respx.get(f"{IRIS_BITSTREAM_CONTENT_URL}/bit-x/content").respond(
            status_code=404
        )

        payload, _meta = await engine.get_full_text("10665/311551")

        assert payload["status"] == "not_found"
        assert payload["pdf_url"] == f"{IRIS_BITSTREAM_CONTENT_URL}/bit-x/content"
    finally:
        await cache.close()
        await http_client.aclose()
