from unittest.mock import AsyncMock

import pytest

from scholar_mcp.citation_check import MAX_CLAIMS, check_citations
from scholar_mcp.config import Settings
from scholar_mcp.models import FullTextResponse, PaperMetadata


class _FakeResolver:
    def __init__(self, settings, metadata_by_id=None, fulltext_by_id=None):
        self.settings = settings
        self._metadata_by_id = metadata_by_id or {}
        self._fulltext_by_id = fulltext_by_id or {}

    async def get_metadata(self, identifier):
        return self._metadata_by_id.get(identifier)

    async def resolve_full_text(self, identifier, max_chars=None, sections=None):
        return self._fulltext_by_id.get(
            identifier,
            FullTextResponse(status="not_found", source="none", content=""),
        )


@pytest.fixture
def settings():
    return Settings()


async def test_check_citations_supported(settings):
    resolver = _FakeResolver(
        settings,
        metadata_by_id={
            "10.1/x": PaperMetadata(
                title="Metformin and Renal Outcomes",
                abstract="Metformin showed no significant renal outcomes in a five-year cohort.",
            )
        },
    )
    results = await check_citations(
        resolver,
        [{"text": "Metformin showed no significant renal outcomes.", "identifier": "10.1/x"}],
    )
    assert len(results) == 1
    assert results[0]["verdict"] == "SUPPORTED"
    assert results[0]["identifier"] == "10.1/x"
    assert "renal outcomes" in results[0]["best_evidence_sentence"].lower()


async def test_check_citations_unsupported(settings):
    resolver = _FakeResolver(
        settings,
        metadata_by_id={
            "10.1/y": PaperMetadata(title="Unrelated Paper", abstract="Nothing to do with the claim.")
        },
    )
    results = await check_citations(
        resolver,
        [{"text": "Metformin cures the common cold.", "identifier": "10.1/y"}],
    )
    assert results[0]["verdict"] == "UNSUPPORTED"


async def test_check_citations_weak(settings):
    resolver = _FakeResolver(
        settings,
        metadata_by_id={
            "10.1/z": PaperMetadata(title="Metformin in general practice", abstract="A broad overview.")
        },
    )
    results = await check_citations(
        resolver,
        [{"text": "Metformin reduces renal complications in diabetic cohorts.", "identifier": "10.1/z"}],
    )
    assert results[0]["verdict"] == "WEAK"


async def test_check_citations_not_found(settings):
    resolver = _FakeResolver(settings, metadata_by_id={})
    results = await check_citations(
        resolver,
        [{"text": "Some claim.", "identifier": "10.1/missing"}],
    )
    assert results[0]["verdict"] == "NOT_FOUND"


async def test_check_citations_deep_uses_full_text(settings):
    resolver = _FakeResolver(
        settings,
        fulltext_by_id={
            "10.1/deep": FullTextResponse(
                status="full_text",
                source="pmc",
                title="Deep Paper",
                content="Results: metformin significantly reduced HbA1c in the treatment arm.",
            )
        },
    )
    results = await check_citations(
        resolver,
        [{"text": "Metformin significantly reduced HbA1c.", "identifier": "10.1/deep"}],
        deep=True,
    )
    assert results[0]["verdict"] == "SUPPORTED"
    assert results[0]["resolved_title"] == "Deep Paper"


async def test_check_citations_isolated_failure(settings):
    resolver = _FakeResolver(
        settings,
        metadata_by_id={
            "10.1/ok": PaperMetadata(title="Good Paper", abstract="Metformin reduced HbA1c significantly.")
        },
    )
    resolver.get_metadata = AsyncMock(side_effect=[Exception("boom"), PaperMetadata(title="Good Paper", abstract="Metformin reduced HbA1c significantly.")])

    results = await check_citations(
        resolver,
        [
            {"text": "Metformin reduced HbA1c.", "identifier": "10.1/bad"},
            {"text": "Metformin reduced HbA1c significantly.", "identifier": "10.1/ok"},
        ],
    )
    assert len(results) == 2
    assert results[0]["verdict"] == "NOT_FOUND"
    assert results[1]["verdict"] == "SUPPORTED"


async def test_check_citations_batch_cap(settings):
    resolver = _FakeResolver(settings)
    claims = [{"text": "x", "identifier": "10.1/x"} for _ in range(MAX_CLAIMS + 1)]
    results = await check_citations(resolver, claims)
    assert len(results) == 1
    assert results[0]["verdict"] == "error"
    assert str(MAX_CLAIMS) in results[0]["error"]
