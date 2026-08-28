import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.identifiers import clean_identifier, resolve_identifiers
from scholar_mcp.utils.cache import TTLCache
from scholar_mcp.utils.http import AsyncHttpClient

IDCONV = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
CROSSREF = "https://api.crossref.org/works"


def test_clean_identifier_detects_types():
    assert clean_identifier("34567890") == ("pmid", "34567890")
    assert clean_identifier("PMID: 34567890") == ("pmid", "34567890")
    assert clean_identifier("PMC8765432") == ("pmcid", "PMC8765432")
    assert clean_identifier("pmc8765432") == ("pmcid", "PMC8765432")
    assert clean_identifier("10.1038/s41586-020-2003-7") == ("doi", "10.1038/s41586-020-2003-7")
    assert clean_identifier("https://doi.org/10.1038/s41586-020-2003-7") == (
        "doi",
        "10.1038/s41586-020-2003-7",
    )
    assert clean_identifier("doi:10.1038/abc") == ("doi", "10.1038/abc")
    assert clean_identifier("A deep learning model for genomics") == (
        "title",
        "A deep learning model for genomics",
    )


@respx.mock
async def test_resolve_from_pmid_via_idconv():
    respx.get(url__startswith=IDCONV).mock(
        return_value=httpx.Response(
            200,
            json={"records": [{"pmid": "32000000", "pmcid": "PMC7000000", "doi": "10.1038/nature123"}]},
        )
    )
    client = AsyncHttpClient(settings=Settings())
    res = await resolve_identifiers("32000000", client, TTLCache(), Settings())
    assert (res.pmid, res.pmcid, res.doi) == ("32000000", "PMC7000000", "10.1038/nature123")
    await client.aclose()


@respx.mock
async def test_resolve_title_above_threshold():
    respx.get(url__startswith=CROSSREF).mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        {
                            "DOI": "10.1038/nature123",
                            "score": 95.0,
                            "title": ["A deep learning model for genomics"],
                        }
                    ]
                }
            },
        )
    )
    respx.get(url__startswith=IDCONV).mock(return_value=httpx.Response(200, json={"records": []}))
    client = AsyncHttpClient(settings=Settings())
    res = await resolve_identifiers(
        "A deep learning model for genomics", client, TTLCache(), Settings()
    )
    assert res.doi == "10.1038/nature123"
    assert res.match_score == 95.0
    assert res.ambiguous is False
    await client.aclose()


@respx.mock
async def test_resolve_title_below_threshold_is_ambiguous():
    """A weak CrossRef match must NOT be treated as a resolved DOI."""
    respx.get(url__startswith=CROSSREF).mock(
        return_value=httpx.Response(
            200,
            json={"message": {"items": [{"DOI": "10.1038/wrong", "score": 12.0, "title": ["Unrelated"]}]}},
        )
    )
    client = AsyncHttpClient(settings=Settings())
    res = await resolve_identifiers("some very obscure phrase", client, TTLCache(), Settings())
    assert res.ambiguous is True
    assert res.doi is None
    assert res.match_score == 12.0
    await client.aclose()


@respx.mock
async def test_resolution_is_cached():
    route = respx.get(url__startswith=IDCONV).mock(
        return_value=httpx.Response(200, json={"records": [{"pmid": "1", "doi": "10.1/a"}]})
    )
    client, cache, settings = AsyncHttpClient(settings=Settings()), TTLCache(), Settings()
    await resolve_identifiers("1", client, cache, settings)
    await resolve_identifiers("1", client, cache, settings)
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_resolution_survives_upstream_failure():
    respx.get(url__startswith=IDCONV).mock(return_value=httpx.Response(500))
    client = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    res = await resolve_identifiers("32000000", client, TTLCache(), Settings())
    assert res.pmid == "32000000"  # input is preserved even when enrichment fails
    await client.aclose()


def test_clean_identifier_detects_arxiv():
    assert clean_identifier("arXiv:2305.18290") == ("arxiv", "2305.18290")
    assert clean_identifier("2305.18290v2") == ("arxiv", "2305.18290v2")
    assert clean_identifier("arxiv:hep-th/9901001") == ("arxiv", "hep-th/9901001")
    assert clean_identifier("https://arxiv.org/abs/2305.18290") == ("arxiv", "2305.18290")
    assert clean_identifier("https://arxiv.org/html/2305.18290v1") == ("arxiv", "2305.18290v1")
    assert clean_identifier("https://arxiv.org/pdf/2305.18290v3.pdf") == ("arxiv", "2305.18290v3")
    assert clean_identifier("https://arxiv.org/abs/2305.18290/") == ("arxiv", "2305.18290")
    assert clean_identifier("https://arxiv.org/abs/2305.18290?context=cs.CL") == (
        "arxiv",
        "2305.18290",
    )
    assert clean_identifier("https://arxiv.org/abs/hep-th/9901001") == ("arxiv", "hep-th/9901001")
    # No false positives
    assert clean_identifier("34567890") == ("pmid", "34567890")
    assert clean_identifier("10.1038/s41586-020-2003-7")[0] == "doi"


@respx.mock
async def test_resolve_from_arxiv_sets_doi():
    client = AsyncHttpClient(settings=Settings())
    res = await resolve_identifiers("arXiv:2305.18290", client, TTLCache(), Settings())
    assert res.arxiv == "2305.18290"
    assert res.doi == "10.48550/arXiv.2305.18290"
    await client.aclose()


@respx.mock
async def test_resolve_arxiv_doi_backfills_arxiv():
    client = AsyncHttpClient(settings=Settings())
    res = await resolve_identifiers("10.48550/arXiv.2305.18290", client, TTLCache(), Settings())
    assert res.arxiv == "2305.18290"
    assert res.doi == "10.48550/arXiv.2305.18290"
    await client.aclose()
