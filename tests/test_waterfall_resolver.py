import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scholar_mcp.config import Settings
from scholar_mcp.models import FullTextResponse, IdentifierMap, PaperMetadata
from scholar_mcp.resolver import WaterfallResolver


def make_resolver(settings: Settings) -> WaterfallResolver:
    r = WaterfallResolver(settings=settings, http_client=AsyncMock(), cache=None)
    r.resolve_ids = AsyncMock(return_value=IdentifierMap(doi="10.1038/xyz", pmcid="PMC1"))
    for name in ("europe_pmc", "pmc", "unpaywall", "scihub"):
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
    assert len(res.attempts) == 5


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
