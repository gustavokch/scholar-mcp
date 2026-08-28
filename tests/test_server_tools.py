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
    assert results[0]["title"] == "A Paper"
    assert results[0]["oa_status"] == "oa"


async def test_search_papers_clamps_num_results(resolver):
    resolver.search.return_value = []
    await srv.search_papers("crispr", num_results=500)
    assert resolver.search.await_args.kwargs["num_results"] == 50


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
    }
    for name in expected:
        assert callable(getattr(srv, name)), f"{name} is not exposed by scholar_mcp.server"
