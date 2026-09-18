import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scholar_mcp.config import Settings
from scholar_mcp.models import FullTextResponse, IdentifierMap, PaperMetadata
from scholar_mcp.resolver import WaterfallResolver
from scholar_mcp.utils.http import FetchFailure


def make_resolver(settings: Settings) -> WaterfallResolver:
    r = WaterfallResolver(settings=settings, http_client=AsyncMock(), cache=None)
    r.resolve_ids = AsyncMock(return_value=IdentifierMap(doi="10.1038/xyz", pmcid="PMC1"))
    for name in ("europe_pmc", "pmc", "unpaywall", "arxiv", "scihub"):
        getattr(r, name).fetch_full_text = AsyncMock(return_value=None)
    r.fetch_abstract = AsyncMock(return_value=None)
    return r


def hit(source: str, content: str = "body text") -> FullTextResponse:
    return FullTextResponse(status="full_text", source=source, content=content)


async def test_europe_pmc_hit_short_circuits():
    r = make_resolver(Settings())
    r.europe_pmc.fetch_full_text.return_value = hit("europepmc")
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.source == "europepmc"
    r.pmc.fetch_full_text.assert_not_awaited()
    r.unpaywall.fetch_full_text.assert_not_awaited()


async def test_falls_through_to_pmc():
    r = make_resolver(Settings())
    r.pmc.fetch_full_text.return_value = hit("pmc")
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.source == "pmc"
    assert [a.tier for a in res.attempts] == ["europepmc", "pmc"]
    assert res.attempts[0].outcome == "miss"


async def test_falls_through_to_unpaywall():
    r = make_resolver(Settings())
    r.unpaywall.fetch_full_text.return_value = hit("unpaywall")
    assert (await r.resolve_full_text("10.1038/xyz")).source == "unpaywall"


async def test_unpaywall_tier_consulted_when_oa_url_is_null():
    """§5 Investigate 2 regression: search-derived identifiers carry no OA URL.

    Root cause, documented not changed: nothing in the resolve path populates
    oa_url before the waterfall — only the S2 search mapping and the OpenAlex
    branch of fetch_abstract ever set it (resolver.py:108-109). The waterfall
    must therefore not depend on oa_url: the Unpaywall tier consults Unpaywall
    by DOI on its own, and it must still run — and win — for a DOI-only
    identifier.
    """
    r = make_resolver(Settings())
    r.resolve_ids = AsyncMock(return_value=IdentifierMap(doi="10.1038/xyz"))
    r.unpaywall.fetch_full_text.return_value = hit("unpaywall")
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.source == "unpaywall"
    attempts = [a for a in res.attempts if a.tier == "unpaywall"]
    assert len(attempts) == 1
    assert attempts[0].outcome == "hit"


async def test_prefer_scihub_skips_unpaywall():
    r = make_resolver(Settings(prefer_scihub_over_unpaywall=True, enable_scihub=True))
    r.unpaywall.fetch_full_text.return_value = hit("unpaywall")
    r.scihub.fetch_full_text.return_value = hit("scihub")
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.source == "scihub"
    r.unpaywall.fetch_full_text.assert_not_awaited()
    skipped = [a for a in res.attempts if a.tier == "unpaywall"][0]
    assert skipped.outcome == "skipped"


async def test_enable_scihub_false_beats_preference():
    """The master switch wins: Unpaywall still runs and Sci-Hub never does."""
    r = make_resolver(Settings(enable_scihub=False, prefer_scihub_over_unpaywall=True))
    r.unpaywall.fetch_full_text.return_value = hit("unpaywall")
    r.scihub.fetch_full_text.return_value = hit("scihub")
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.source == "unpaywall"
    r.scihub.fetch_full_text.assert_not_awaited()


async def test_total_failure_falls_back_to_abstract():
    r = make_resolver(Settings())
    r.fetch_abstract.return_value = PaperMetadata(title="T", abstract="An abstract.")
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.status == "abstract_only"
    assert res.source == "abstract_fallback"
    assert "An abstract." in res.content


async def test_nothing_at_all_returns_not_found():
    res = await make_resolver(Settings()).resolve_full_text("10.1038/xyz")
    assert res.status == "not_found"
    assert len(res.attempts) == 6


async def test_ambiguous_title_does_not_fetch():
    r = make_resolver(Settings())
    r.resolve_ids = AsyncMock(return_value=IdentifierMap(ambiguous=True, match_score=10.0))
    res = await r.resolve_full_text("a vague phrase")
    assert res.status == "ambiguous_match"
    r.pmc.fetch_full_text.assert_not_awaited()


async def test_truncation_marks_and_reports_total():
    r = make_resolver(Settings(max_chars=20))
    r.pmc.fetch_full_text.return_value = hit("pmc", "x" * 500)
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.truncated is True
    assert res.total_chars == 500
    assert len(res.content) < 500


async def test_section_selection_applied():
    r = make_resolver(Settings())
    r.pmc.fetch_full_text.return_value = hit(
        "pmc", "## Introduction\n\nintro text\n\n## Methods\n\nmethod text\n"
    )
    res = await r.resolve_full_text("10.1038/xyz", sections=["Methods"])
    assert "method text" in res.content
    assert "intro text" not in res.content


async def test_budget_exhaustion_degrades_to_abstract():
    r = make_resolver(Settings(total_budget_seconds=1))

    async def slow(_ids):
        await asyncio.sleep(5)

    r.pmc.fetch_full_text = AsyncMock(side_effect=slow)
    r.fetch_abstract.return_value = PaperMetadata(title="T", abstract="Fallback abstract.")
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.status == "abstract_only"
    assert any(a.outcome == "timeout" for a in res.attempts)


async def test_batch_is_concurrent_and_bounded():
    r = make_resolver(Settings(max_concurrency=2))
    r.pmc.fetch_full_text.return_value = hit("pmc")
    out = await r.resolve_full_text_batch([f"10.1/{i}" for i in range(6)])
    assert len(out) == 6
    assert all(s.status == "full_text" for s in out)


async def test_batch_rejects_oversized_input():
    with pytest.raises(ValueError):
        await make_resolver(Settings()).resolve_full_text_batch([f"10.1/{i}" for i in range(26)])


async def test_download_rejects_path_escape(tmp_path):
    r = make_resolver(Settings(download_dir=tmp_path))
    res = await r.download_article("10.1038/xyz", "../../etc/passwd")
    assert res.success is False
    assert "outside" in res.message.lower()


async def test_download_rejects_absolute_path_outside_root(tmp_path):
    r = make_resolver(Settings(download_dir=tmp_path))
    res = await r.download_article("10.1038/xyz", "/etc/passwd")
    assert res.success is False


async def test_download_refuses_overwrite_without_flag(tmp_path):
    (tmp_path / "p.pdf").write_bytes(b"existing")
    r = make_resolver(Settings(download_dir=tmp_path))
    r.fetch_pdf_bytes = AsyncMock(return_value=(b"%PDF-new", "pmc"))
    res = await r.download_article("10.1038/xyz", "p.pdf")
    assert res.success is False
    assert "exists" in res.message.lower()
    assert (tmp_path / "p.pdf").read_bytes() == b"existing"


async def test_download_writes_inside_root(tmp_path):
    r = make_resolver(Settings(download_dir=tmp_path))
    r.fetch_pdf_bytes = AsyncMock(return_value=(b"%PDF-data", "unpaywall"))
    res = await r.download_article("10.1038/xyz", "sub/paper.pdf")
    assert res.success is True
    assert res.file_size_bytes == len(b"%PDF-data")
    assert Path(res.saved_path).read_bytes() == b"%PDF-data"


async def test_arxiv_hit_short_circuits_before_scihub():
    r = make_resolver(Settings())
    r.resolve_ids = AsyncMock(
        return_value=IdentifierMap(arxiv="2305.18290", doi="10.48550/arXiv.2305.18290")
    )
    r.arxiv.fetch_full_text.return_value = hit("arxiv")
    res = await r.resolve_full_text("arXiv:2305.18290")
    assert res.source == "arxiv"
    r.scihub.fetch_full_text.assert_not_awaited()
    assert [a.tier for a in res.attempts] == ["europepmc", "pmc", "unpaywall", "arxiv"]


async def test_arxiv_tier_reports_skip_without_arxiv_id():
    # Real ArxivProvider: no arXiv ID -> fast skip with reason, no HTTP.
    r = WaterfallResolver(settings=Settings(), http_client=AsyncMock(), cache=None)
    r.resolve_ids = AsyncMock(return_value=IdentifierMap(doi="10.1038/xyz"))
    for name in ("europe_pmc", "pmc", "unpaywall", "scihub"):
        getattr(r, name).fetch_full_text = AsyncMock(return_value=None)
    r.fetch_abstract = AsyncMock(return_value=None)
    res = await r.resolve_full_text("10.1038/xyz")
    arxiv_attempt = [a for a in res.attempts if a.tier == "arxiv"][0]
    assert arxiv_attempt.outcome == "skipped"
    assert arxiv_attempt.reason == "NO_ARXIV_ID"


async def test_fetch_pdf_bytes_prefers_arxiv_before_scihub():
    r = make_resolver(Settings(unpaywall_email=None))
    r.http_client.get_bytes = AsyncMock(return_value=b"%PDF-arxiv")
    r.scihub.fetch_pdf_bytes = AsyncMock(return_value=(b"%PDF-sh", "url"))
    b, src = await r.fetch_pdf_bytes(
        IdentifierMap(arxiv="2305.18290", doi="10.48550/arXiv.2305.18290")
    )
    assert (b, src) == (b"%PDF-arxiv", "arxiv")
    r.scihub.fetch_pdf_bytes.assert_not_awaited()


async def test_fetch_pdf_bytes_rejects_arxiv_non_pdf_body():
    """arXiv serves a 200 HTML placeholder while a PDF is still being generated."""
    r = make_resolver(Settings(unpaywall_email=None))
    r.http_client.get_bytes = AsyncMock(return_value=b"<html>PDF is being generated</html>")
    r.scihub.fetch_pdf_bytes = AsyncMock(return_value=(b"%PDF-sh", "scihub-url"))
    b, src = await r.fetch_pdf_bytes(
        IdentifierMap(arxiv="2305.18290", doi="10.48550/arXiv.2305.18290")
    )
    assert (b, src) == (b"%PDF-sh", "scihub")


async def test_fetch_pdf_bytes_survives_arxiv_transport_error():
    r = make_resolver(Settings(unpaywall_email=None))
    r.http_client.get_bytes = AsyncMock(side_effect=RuntimeError("boom"))
    r.scihub.fetch_pdf_bytes = AsyncMock(return_value=(b"%PDF-sh", "scihub-url"))
    b, src = await r.fetch_pdf_bytes(
        IdentifierMap(arxiv="2305.18290", doi="10.48550/arXiv.2305.18290")
    )
    assert (b, src) == (b"%PDF-sh", "scihub")


async def test_fetch_abstract_falls_back_to_europepmc_for_pmid():
    """PubMed efetch transport failure (None) must not be the end of the
    chain: Europe PMC metadata by PMID supplies the abstract."""
    r = WaterfallResolver(settings=Settings(), http_client=AsyncMock(), cache=None)
    r.pubmed.fetch_abstract = AsyncMock(return_value=None)
    epmc_meta = PaperMetadata(title="E", abstract="EPMC abstract.", pmid="32000000")
    r.europe_pmc.fetch_metadata = AsyncMock(return_value=epmc_meta)
    meta = await r.fetch_abstract(IdentifierMap(pmid="32000000"))
    assert meta is not None
    assert meta.abstract == "EPMC abstract."
    r.europe_pmc.fetch_metadata.assert_awaited_once()


async def test_fetch_abstract_falls_back_to_arxiv():
    r = WaterfallResolver(settings=Settings(), http_client=AsyncMock(), cache=None)
    r.pubmed.fetch_abstract = AsyncMock(return_value=None)
    r.crossref.fetch_metadata = AsyncMock(return_value=None)
    r.arxiv.fetch_metadata = AsyncMock(
        return_value=PaperMetadata(title="A", abstract="Arxiv abstract.")
    )
    meta = await r.fetch_abstract(IdentifierMap(arxiv="2305.18290"))
    assert meta is not None
    assert meta.title == "A"
    r.arxiv.fetch_metadata.assert_awaited_once_with("2305.18290")


async def test_full_text_hit_backfills_title():
    """Producers that omit title (scihub, pmc, arxiv, unpaywall) leave
    FullTextResponse.title empty; the resolver backfills it from metadata."""
    r = make_resolver(Settings())
    r.scihub.fetch_full_text.return_value = hit("scihub")
    assert r.scihub.fetch_full_text.return_value.title == ""
    r.fetch_abstract = AsyncMock(return_value=PaperMetadata(title="X"))
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.title == "X"


async def test_title_backfill_bounded_by_remaining_budget():
    """The backfill runs after the waterfall, so its ceiling must come out of
    what the waterfall left of the same budget — not a fresh 5 s on top.

    Here the waterfall burns almost the whole budget, so a metadata chain that
    needs more than the remainder must be abandoned, leaving the title empty.
    """
    settings = Settings(total_budget_seconds=1)
    r = make_resolver(settings)

    async def slow_hit(_ids):
        await asyncio.sleep(0.9)
        return hit("scihub")

    async def slow_meta(_ids):
        await asyncio.sleep(0.5)
        return PaperMetadata(title="X")

    r.scihub.fetch_full_text = slow_hit
    r.fetch_abstract = slow_meta
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.source == "scihub"
    assert res.title == ""



async def test_last_search_sources_blocked_on_403():
    r = WaterfallResolver(settings=Settings(), http_client=AsyncMock(), cache=None)
    r.pubmed.search = AsyncMock(return_value=[])
    r.pubmed.last_error = "http_403"
    r.pubmed.http_client.last_failure = FetchFailure("http", 403, "Forbidden")
    await r.search("q", source="pubmed", rerank=False)
    assert r.last_search_sources["pubmed"] == "blocked"


async def test_last_search_sources_blocked_on_429():
    r = WaterfallResolver(settings=Settings(), http_client=AsyncMock(), cache=None)
    r.pubmed.search = AsyncMock(return_value=[])
    r.pubmed.last_error = "http_429"
    r.pubmed.http_client.last_failure = FetchFailure("http", 429, "Too Many Requests")
    await r.search("q", source="pubmed", rerank=False)
    assert r.last_search_sources["pubmed"] == "blocked"


async def test_last_search_sources_failed_on_transport_error():
    r = WaterfallResolver(settings=Settings(), http_client=AsyncMock(), cache=None)
    r.pubmed.search = AsyncMock(return_value=[])
    r.pubmed.last_error = "transport"
    r.pubmed.http_client.last_failure = FetchFailure("transport", None, "ConnectError")
    await r.search("q", source="pubmed", rerank=False)
    assert r.last_search_sources["pubmed"] == "failed"


async def test_last_search_sources_failed_on_unexpected_exception():
    r = WaterfallResolver(settings=Settings(), http_client=AsyncMock(), cache=None)
    r.pubmed.search = AsyncMock(return_value=[])
    r.pubmed.last_error = "exception:ValueError"
    r.pubmed.http_client.last_failure = FetchFailure("exception", None, "ValueError")
    await r.search("q", source="pubmed", rerank=False)
    assert r.last_search_sources["pubmed"] == "failed"


async def test_last_search_sources_failed_on_provider_raise():
    r = WaterfallResolver(settings=Settings(), http_client=AsyncMock(), cache=None)
    r.pubmed.search = AsyncMock(side_effect=RuntimeError("boom"))
    await r.search("q", source="pubmed", rerank=False)
    assert r.last_search_sources["pubmed"] == "failed"


async def test_last_search_sources_empty_when_no_error():
    r = WaterfallResolver(settings=Settings(), http_client=AsyncMock(), cache=None)
    r.pubmed.search = AsyncMock(return_value=[])
    r.pubmed.last_error = None
    await r.search("q", source="pubmed", rerank=False)
    assert r.last_search_sources["pubmed"] == "empty"


async def test_disabled_s2_is_not_reported_as_empty():
    """"empty" means "queried, zero hits, no error". A backend that was never
    queried must not claim that."""
    r = WaterfallResolver(settings=Settings(enable_s2=False), http_client=AsyncMock(), cache=None)
    await r.search("q", source="s2")
    assert r.last_search_sources == {"s2": "disabled"}


async def test_last_search_sources_is_per_request():
    """The resolver is a module-level singleton in server.py, so two concurrent
    MCP calls share the instance. Each must read back only its own map.

    The fast call finishes first but reads its map only after the slow call has
    written its own status — a plain instance attribute hands it the slow
    call's value.
    """
    r = WaterfallResolver(settings=Settings(), http_client=AsyncMock(), cache=None)
    started = asyncio.Event()
    slow_done = asyncio.Event()

    async def slow_search(*args, **kwargs):
        started.set()
        await asyncio.sleep(0.05)
        return [PaperMetadata(title="A")]

    async def fast_search(*args, **kwargs):
        return []

    async def run_slow():
        r.pubmed.search = slow_search
        try:
            await r.search("slow", source="pubmed", rerank=False)
            return dict(r.last_search_sources)
        finally:
            slow_done.set()

    async def run_fast():
        await started.wait()
        r.pubmed.search = fast_search
        await r.search("fast", source="pubmed", rerank=False)
        await slow_done.wait()
        return dict(r.last_search_sources)

    slow_map, fast_map = await asyncio.gather(run_slow(), run_fast())
    assert slow_map == {"pubmed": "ok"}
    assert fast_map == {"pubmed": "empty"}


async def test_waterfall_resolver_search_with_rerank():
    r = WaterfallResolver(settings=Settings(), http_client=AsyncMock(), cache=None)
    mock_papers = [
        PaperMetadata(title="Paper 1", pmid="1", doi="10.1001/1", year="2015", citation_count=500),
        PaperMetadata(title="Paper 2", pmid="2", doi="10.1001/2", year="2026", citation_count=10),
    ]
    r.pubmed.search = AsyncMock(return_value=mock_papers)
    r.ranking_pipeline.rank_papers = AsyncMock(
        return_value=[
            PaperMetadata(title="Paper 2", pmid="2", doi="10.1001/2", score=1.2),
            PaperMetadata(title="Paper 1", pmid="1", doi="10.1001/1", score=0.8),
        ]
    )

    results = await r.search("cancer", source="pubmed", num_results=2, rerank=True)
    assert len(results) == 2
    assert results[0].title == "Paper 2"
    assert results[0].score == 1.2
    r.ranking_pipeline.rank_papers.assert_awaited_once()
    assert r.pubmed.search.await_args.kwargs.get("sort") == "relevance"


async def test_waterfall_resolver_search_without_rerank():
    r = WaterfallResolver(settings=Settings(), http_client=AsyncMock(), cache=None)
    mock_papers = [
        PaperMetadata(title="Paper 1", pmid="1", doi="10.1001/1", year="2015"),
        PaperMetadata(title="Paper 2", pmid="2", doi="10.1001/2", year="2026"),
    ]
    r.pubmed.search = AsyncMock(return_value=mock_papers)
    r.ranking_pipeline.rank_papers = AsyncMock()

    results = await r.search("cancer", source="pubmed", num_results=2, rerank=False)
    assert len(results) == 2
    assert results[0].title == "Paper 1"
    r.ranking_pipeline.rank_papers.assert_not_awaited()
    assert r.pubmed.search.await_args.kwargs.get("sort") == "relevance"

