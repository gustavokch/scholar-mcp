from unittest.mock import AsyncMock

import pytest

from scholar_mcp import server as srv
from scholar_mcp.models import DownloadResult, FullTextResponse, FullTextSummary, PaperMetadata


@pytest.fixture
def resolver(monkeypatch):
    r = AsyncMock()
    monkeypatch.setattr(srv, "resolver", r)
    return r


async def test_get_full_text_tool(resolver):
    resolver.resolve_full_text.return_value = FullTextResponse(
        status="full_text", source="pmc", title="Test Title", content="# Test Title\n\nFull text content"
    )
    result = await srv.get_full_text("32000000")
    assert result["status"] == "full_text"
    assert result["source"] == "pmc"
    assert "Full text content" in result["content"]


async def test_get_full_text_forwards_max_chars_and_sections(resolver):
    resolver.resolve_full_text.return_value = FullTextResponse(status="full_text", source="pmc")
    await srv.get_full_text("32000000", max_chars=1000, sections=["Methods"])
    kwargs = resolver.resolve_full_text.await_args.kwargs
    assert kwargs["max_chars"] == 1000
    assert kwargs["sections"] == ["Methods"]


async def test_search_papers_tool(resolver):
    resolver.search.return_value = [PaperMetadata(title="A Paper", doi="10.1/a", oa_status="oa")]
    results = await srv.search_papers("crispr", num_results=5)
    assert results["papers"][0]["title"] == "A Paper"
    assert results["papers"][0]["oa_status"] == "oa"
    assert results["degraded"] is False


async def test_search_papers_tool_forwards_rerank(resolver):
    resolver.search.return_value = [
        PaperMetadata(
            title="Ranked Paper",
            doi="10.1/ranked",
            score=1.5,
            ranking_metrics={"z_citation": 0.5},
        )
    ]
    results = await srv.search_papers("crispr", num_results=5, rerank=True)
    assert results["papers"][0]["score"] == 1.5
    assert results["papers"][0]["ranking_metrics"] == {"z_citation": 0.5}
    assert resolver.search.await_args.kwargs["rerank"] is True

    await srv.search_papers("crispr", num_results=5, rerank=False)
    assert resolver.search.await_args.kwargs["rerank"] is False



async def test_search_papers_envelope_reports_degraded_sources(resolver):
    resolver.search.return_value = [
        PaperMetadata(title="A", doi="10.1/a"),
        PaperMetadata(title="B", doi="10.1/b"),
    ]
    resolver.last_search_sources = {"pubmed": "blocked", "crossref": "ok"}
    result = await srv.search_papers("dengue")
    assert [p["title"] for p in result["papers"]] == ["A", "B"]
    assert result["sources"] == {"pubmed": "blocked", "crossref": "ok"}
    assert result["degraded"] is True


async def test_search_papers_sources_is_a_snapshot(resolver):
    """The envelope must embed a copy of the resolver's status map, not the
    live ContextScoped dict, so a later request cannot mutate this result."""
    live = {"pubmed": "blocked"}
    resolver.search.return_value = []
    resolver.last_search_sources = live
    result = await srv.search_papers("dengue")
    live["pubmed"] = "ok"
    assert result["sources"] == {"pubmed": "blocked"}
    assert result["degraded"] is True


async def test_search_papers_degraded_false_on_ok_or_empty(resolver):
    resolver.search.return_value = [PaperMetadata(title="A", doi="10.1/a")]
    resolver.last_search_sources = {"pubmed": "ok", "crossref": "empty", "s2": "disabled"}
    result = await srv.search_papers("dengue")
    assert result["degraded"] is False
    assert len(result["papers"]) == 1


async def test_brazil_moh_tool_marks_degraded_on_partial(monkeypatch):
    from scholar_mcp.medical.models import BrazilGuideline
    from scholar_mcp.utils.sqlite_cache import CacheMetadata

    mock_engine = AsyncMock()
    mock_engine.search_guidelines.return_value = (
        [BrazilGuideline(title="x")],
        CacheMetadata(cached=False, cache_age=0, error=True),
    )
    monkeypatch.setattr(srv, "brazil_moh_engine", mock_engine)
    result = await srv.search_brazil_moh_guidelines("dengue")
    assert result.get("degraded") is True


async def test_search_papers_clamps_num_results(resolver):
    resolver.search.return_value = []
    await srv.search_papers("crispr", num_results=500)
    assert resolver.search.await_args.kwargs["num_results"] == 50


async def test_search_papers_error_envelope(resolver):
    """A total failure returns the same envelope shape, flagged error."""
    resolver.search.side_effect = RuntimeError("backend exploded")
    result = await srv.search_papers("crispr")
    assert result["papers"] == []
    assert result["sources"] == {}
    assert result["degraded"] is True
    assert result["status"] == "error"
    assert "backend exploded" in result["error"]


async def test_search_papers_payload_never_drops_results_for_degradation(resolver):
    """The degradation signal lives in the envelope, not in a reserved row, so
    every paper survives: num_results=50 with a degraded backend returns all
    50 papers, and the papers list is bounded only by the clamp."""
    resolver.search.return_value = [PaperMetadata(title=f"P{i}") for i in range(50)]
    resolver.last_search_sources = {"pubmed": "blocked"}
    result = await srv.search_papers("crispr", num_results=50)
    assert len(result["papers"]) == 50
    assert result["degraded"] is True

    resolver.search.return_value = [PaperMetadata(title=f"P{i}") for i in range(20)]
    result = await srv.search_papers("crispr", num_results=10)
    assert [p["title"] for p in result["papers"]] == [f"P{i}" for i in range(10)]


async def test_get_metadata_tool_does_not_run_waterfall(resolver):
    resolver.get_metadata.return_value = PaperMetadata(title="Meta", pmid="1", abstract="abs")
    result = await srv.get_metadata("1")
    assert result["title"] == "Meta"
    resolver.resolve_full_text.assert_not_awaited()


async def test_batch_tool_rejects_over_limit(resolver):
    result = await srv.get_full_text_batch([f"10.1/{i}" for i in range(26)])
    assert result[0]["status"] == "error"
    resolver.resolve_full_text_batch.assert_not_awaited()


async def test_batch_tool_success(resolver):
    resolver.resolve_full_text_batch.return_value = [
        FullTextSummary(identifier="10.1/a", status="full_text", source="pmc", excerpt="body")
    ]
    result = await srv.get_full_text_batch(["10.1/a"])
    assert result[0]["source"] == "pmc"


async def test_download_paper_tool_reports_failure_cleanly(resolver):
    resolver.download_article.return_value = DownloadResult(
        success=False, saved_path="", source_used="none", message="Path outside download root"
    )
    result = await srv.download_paper("10.1/a", "../escape.pdf")
    assert result["success"] is False
    assert "outside" in result["message"].lower()


async def test_tool_exceptions_become_structured_errors(resolver):
    resolver.resolve_full_text.side_effect = RuntimeError("boom")
    result = await srv.get_full_text("32000000")
    assert result["status"] == "error"
    assert "boom" in result["error"]


async def test_deep_analysis_prompt_includes_content(resolver):
    resolver.resolve_full_text.return_value = FullTextResponse(
        status="full_text", source="pmc", title="Deep Paper", content="Method details here."
    )
    result = await srv.deep_paper_analysis_prompt("10.1/a")
    assert "Deep Paper" in result["analysis_prompt"]
    assert "Method details here." in result["analysis_prompt"]


def test_all_tools_registered():
    expected = {
        "search_papers",
        "get_full_text",
        "get_full_text_batch",
        "get_metadata",
        "download_paper",
        "deep_paper_analysis_prompt",
        "get_references",
        "get_citations",
        "get_related_papers",
    }
    for name in expected:
        assert callable(getattr(srv, name)), f"{name} is not exposed by scholar_mcp.server"


async def test_get_references_tool(resolver):
    from scholar_mcp.models import ReferenceItem

    resolver.get_references.return_value = [
        ReferenceItem(id="1", title="Ref Paper", doi="10.1/ref")
    ]
    res = await srv.get_references("32000000", limit=10)
    assert len(res) == 1
    assert res[0]["title"] == "Ref Paper"
    assert res[0]["doi"] == "10.1/ref"


async def test_get_citations_tool(resolver):
    from scholar_mcp.models import CitationItem

    resolver.get_citations.return_value = [
        CitationItem(title="Citing Paper", doi="10.1/cite", citation_count=5)
    ]
    res = await srv.get_citations("32000000", limit=10)
    assert len(res) == 1
    assert res[0]["title"] == "Citing Paper"
    assert res[0]["citation_count"] == 5


async def test_get_related_papers_tool(resolver):
    from scholar_mcp.models import RelatedPaper

    resolver.get_related_papers.return_value = [
        RelatedPaper(title="Related Paper", score=90.0, pmid="33000000")
    ]
    res = await srv.get_related_papers("32000000", limit=5)
    assert len(res) == 1
    assert res[0]["title"] == "Related Paper"
    assert res[0]["score"] == 90.0



async def test_check_citations_tool_supported(resolver, monkeypatch):
    from scholar_mcp.config import Settings

    # The `resolver` fixture is an AsyncMock; check_citations reads
    # resolver.settings.max_concurrency and the threshold floats, which blow up
    # on auto-created AsyncMock children (TypeError in Semaphore/threshold
    # comparison). Give it a real Settings.
    resolver.settings = Settings()

    async def fake_get_metadata(identifier):
        return PaperMetadata(
            title="Metformin Trial",
            abstract="Metformin significantly reduced HbA1c in the treatment group.",
        )

    monkeypatch.setattr(srv.resolver, "get_metadata", fake_get_metadata)

    results = await srv.check_citations(
        claims=[{"text": "Metformin significantly reduced HbA1c.", "identifier": "10.1/x"}],
    )
    assert results[0]["verdict"] == "SUPPORTED"


async def test_check_citations_tool_batch_cap():
    claims = [{"text": "x", "identifier": "10.1/x"} for _ in range(26)]
    results = await srv.check_citations(claims=claims)
    assert results[0]["verdict"] == "ERROR"
