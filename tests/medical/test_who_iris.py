from pathlib import Path

import httpx
import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.who_iris import (
    IRIS_BROWSE_TITLE_URL,
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


def _iris_item(handle="10665/44626", title="Guideline: neonatal vitamin A supplementation"):
    return {
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


@respx.mock
async def test_search_guidelines_prefix_mode_parses_item(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(IRIS_BROWSE_TITLE_URL).respond(json=_browse_page([_iris_item()]))

        guidelines, meta = await engine.search_guidelines("guideline", limit=10, mode="prefix")

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
