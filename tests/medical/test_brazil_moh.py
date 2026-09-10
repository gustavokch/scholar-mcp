from scholar_mcp.medical.brazil_moh import (
    _as_list,
    _derive_fulltext_id,
    _first,
    _parse_country,
    _parse_issued,
)
from scholar_mcp.medical.models import BrazilGuideline


def test_first_returns_first_list_element():
    assert _first(["a", "b"]) == "a"


def test_first_returns_scalar_as_string():
    assert _first(202609) == "202609"


def test_first_returns_empty_string_for_empty_or_none():
    assert _first([]) == ""
    assert _first(None) == ""


def test_as_list_wraps_scalar_and_drops_empties():
    assert _as_list("pt") == ["pt"]
    assert _as_list(["pt", "", "en"]) == ["pt", "en"]
    assert _as_list(["   ", "pt", "  "]) == ["pt"]
    assert _as_list("  ") == []
    assert _as_list(None) == []


def test_parse_issued_splits_year_and_month():
    assert _parse_issued("202609") == ("2026", "2026-09")


def test_parse_issued_accepts_year_only():
    assert _parse_issued("2026") == ("2026", "2026")


def test_parse_issued_returns_empty_for_missing_or_malformed():
    assert _parse_issued("") == ("", "")
    assert _parse_issued(None) == ("", "")
    assert _parse_issued("n/d") == ("", "")


def test_parse_country_reads_the_e_subfield():
    raw = "^iBrazil^eBrasil^pBrasil^fBrésil"
    assert _parse_country([raw]) == "Brasil"


def test_parse_country_handles_multiword_value():
    raw = "^iEl Salvador^eEl Salvador^pEl Salvador"
    assert _parse_country([raw]) == "El Salvador"


def test_parse_country_returns_empty_when_absent():
    assert _parse_country([]) == ""
    assert _parse_country(["no subfields here"]) == ""


def test_parse_country_handles_plain_string_without_subfields():
    assert _parse_country(["Brasil"]) == "Brasil"
    assert _parse_country(" Brasil ") == "Brasil"
    assert _parse_country(["brasil"]) == "Brasil"


def test_parse_country_plain_string_starting_with_e_is_not_truncated():
    # Without the "^" guard the subfield scan reads "Espanha" as "spanha".
    assert _parse_country(["Espanha"]) == ""


def test_derive_fulltext_id_matches_fi_admin_url():
    url = "https://fi-admin.bvsalud.org/document/view/cfpaj"
    assert _derive_fulltext_id(url) == "cfpaj"
    assert _derive_fulltext_id("  https://fi-admin.bvsalud.org/document/view/cfpaj  ") == "cfpaj"
    assert _derive_fulltext_id("https://fi-admin.bvsalud.org/document/view/cfpaj?lang=pt") == "cfpaj"
    assert _derive_fulltext_id("https://fi-admin.bvsalud.org/document/view/cfpaj#page=1") == "cfpaj"


def test_derive_fulltext_id_is_empty_for_offsite_url():
    assert _derive_fulltext_id("https://www.sciencedirect.com/science/article/pii/S123") == ""
    assert _derive_fulltext_id("") == ""


def test_brazil_guideline_roundtrips_and_ignores_unknown_keys():
    guideline = BrazilGuideline(
        title="Protocolo Clínico",
        record_id="biblio-1701387",
        document_url="https://fi-admin.bvsalud.org/document/view/cfpaj",
    )
    data = guideline.to_dict()
    assert data["source"] == "brazil-moh"
    assert data["score"] is None
    restored = BrazilGuideline.from_dict({**data, "unexpected_key": 1})
    assert restored.record_id == "biblio-1701387"


def test_brazil_guideline_from_dict_handles_none():
    assert BrazilGuideline.from_dict(None).title == ""


def test_build_query_joins_user_tokens_with_and():
    from scholar_mcp.medical.brazil_moh import _build_query

    built = _build_query("tratamento tuberculose", "all")
    assert built == 'type:"non-conventional" AND la:"pt" AND (tratamento AND tuberculose)'


def test_build_query_appends_brisa_filter():
    from scholar_mcp.medical.brazil_moh import _build_query

    built = _build_query("dengue", "brisa")
    assert 'db:"BRISA"' in built
    assert built.startswith('type:"non-conventional" AND la:"pt"')


def test_build_query_omits_brisa_filter_for_all():
    from scholar_mcp.medical.brazil_moh import _build_query

    assert 'db:"BRISA"' not in _build_query("dengue", "all")


def test_build_query_with_blank_query_is_filters_only():
    from scholar_mcp.medical.brazil_moh import _build_query

    assert _build_query("   ", "all") == 'type:"non-conventional" AND la:"pt"'


def test_build_query_strips_solr_special_characters():
    from scholar_mcp.medical.brazil_moh import _build_query

    built = _build_query('a "quote" (b) [c] && d || e', "all")
    # "a" and "e" are Portuguese stopwords and are dropped. The single-char
    # "b", "c", "d" are not Portuguese words, so they survive -- confirming
    # the length floor is not applied to outbound tokens.
    assert built == 'type:"non-conventional" AND la:"pt" AND (quote AND b AND c AND d)'


def test_build_query_drops_bare_boolean_words():
    from scholar_mcp.medical.brazil_moh import _build_query

    built = _build_query("dengue AND zika", "all")
    assert built == 'type:"non-conventional" AND la:"pt" AND (dengue AND zika)'


def test_build_query_all_tokens_reserved_yields_filters_only():
    from scholar_mcp.medical.brazil_moh import _build_query

    assert _build_query("AND OR NOT", "all") == 'type:"non-conventional" AND la:"pt"'


def test_usable_tokens_strips_portuguese_stopwords():
    from scholar_mcp.medical.brazil_moh import _usable_tokens

    assert _usable_tokens("manejo da dengue") == ["manejo", "dengue"]
    assert _usable_tokens("tratamento de tuberculose para adultos") == [
        "tratamento",
        "tuberculose",
        "adultos",
    ]


def test_usable_tokens_strips_accented_stopword_but_keeps_accented_terms():
    from scholar_mcp.medical.brazil_moh import _usable_tokens

    # "à" folds to the stopword "a" and is dropped. "atenção" is substantive
    # and must survive WITH its diacritics -- BVS handles Portuguese natively,
    # so folding is client-side only.
    assert _usable_tokens("atenção à saúde") == ["atenção", "saúde"]


def test_usable_tokens_keeps_single_char_non_stopwords():
    from scholar_mcp.medical.brazil_moh import _usable_tokens

    # "a" and "e" are Portuguese function words; "b" and "c" are not. The
    # >= 2 length floor from tokenize_portuguese is deliberately NOT applied
    # here: "b" is genuinely selective in this index (hepatite AND b retains
    # 71% of bare hepatite, while hepatite AND a retains 90%).
    assert _usable_tokens("hepatite b") == ["hepatite", "b"]
    assert _usable_tokens("hepatite a") == ["hepatite"]


def test_usable_tokens_all_stopwords_yields_nothing():
    from scholar_mcp.medical.brazil_moh import _usable_tokens

    assert _usable_tokens("sobre a") == []


def test_build_query_strips_stopwords_in_and_mode():
    from scholar_mcp.medical.brazil_moh import _build_query

    built = _build_query("manejo da dengue", "all")
    assert built == 'type:"non-conventional" AND la:"pt" AND (manejo AND dengue)'


def test_build_query_strips_stopwords_in_or_mode():
    from scholar_mcp.medical.brazil_moh import _build_query

    built = _build_query("manejo da dengue", "all", operator="OR")
    assert built == 'type:"non-conventional" AND la:"pt" AND (manejo OR dengue)'


def test_build_query_or_operator_leaves_base_filters_anded():
    from scholar_mcp.medical.brazil_moh import _build_query

    # Only the user-token group relaxes. The filters stay conjunctive, or the
    # query would match non-Portuguese and conventional literature.
    built = _build_query("manejo dengue", "brisa", operator="OR")
    assert built == (
        'type:"non-conventional" AND la:"pt" AND db:"BRISA" AND (manejo OR dengue)'
    )


def test_build_query_defaults_to_and():
    from scholar_mcp.medical.brazil_moh import _build_query

    assert _build_query("manejo dengue", "all") == _build_query(
        "manejo dengue", "all", operator="AND"
    )


def test_extract_docs_reads_nested_envelope():
    from scholar_mcp.medical.brazil_moh import _extract_docs

    payload = {"diaServerResponse": [{"response": {"numFound": 2, "docs": [{"id": "a"}, {"id": "b"}]}}]}
    assert _extract_docs(payload) == [{"id": "a"}, {"id": "b"}]


def test_extract_docs_returns_empty_for_malformed_payload():
    from scholar_mcp.medical.brazil_moh import _extract_docs

    assert _extract_docs({}) == []
    assert _extract_docs({"diaServerResponse": []}) == []
    assert _extract_docs(None) == []


def test_build_record_maps_solr_fields():
    from scholar_mcp.medical.brazil_moh import _build_record

    doc = {
        "id": "biblio-1701387",
        "ti": ["Protocolo Clínico e Diretrizes Terapêuticas"],
        "ti_en": ["Clinical Protocol"],
        "ab": ["Resumo do protocolo."],
        "au": ["Brasil. Ministério da Saúde"],
        "da": "202609",
        "db": ["BRISA", "LILACS"],
        "mh": ["Tuberculose"],
        "la": ["pt"],
        "ur": ["https://fi-admin.bvsalud.org/document/view/cfpaj"],
        "pais_publicacao": ["^iBrazil^eBrasil^pBrasil^fBrésil"],
    }
    record = _build_record(doc)
    assert record.record_id == "biblio-1701387"
    assert record.title == "Protocolo Clínico e Diretrizes Terapêuticas"
    assert record.title_en == "Clinical Protocol"
    assert record.abstract == "Resumo do protocolo."
    assert record.authors == ["Brasil. Ministério da Saúde"]
    assert record.year == "2026"
    assert record.issued == "2026-09"
    assert record.collections == ["BRISA", "LILACS"]
    assert record.mesh_subjects == ["Tuberculose"]
    assert record.languages == ["pt"]
    assert record.country == "Brasil"
    assert record.document_url == "https://fi-admin.bvsalud.org/document/view/cfpaj"
    assert record.fulltext_id == "cfpaj"
    assert record.source == "brazil-moh"
    assert record.score is None


def test_build_record_offsite_url_has_no_fulltext_id():
    from scholar_mcp.medical.brazil_moh import _build_record

    doc = {
        "id": "biblio-1",
        "ti": ["Artigo"],
        "ur": ["https://www.sciencedirect.com/science/article/pii/S123"],
    }
    record = _build_record(doc)
    assert record.document_url == "https://www.sciencedirect.com/science/article/pii/S123"
    assert record.fulltext_id == ""


def test_build_record_prioritizes_allowed_document_url():
    from scholar_mcp.medical.brazil_moh import _build_record

    doc = {
        "id": "biblio-1",
        "ti": ["Artigo"],
        "ur": [
            "https://pesquisa.bvsalud.org/portal/resource/pt/biblio-1",
            "https://fi-admin.bvsalud.org/document/view/cfpaj",
        ],
    }
    record = _build_record(doc)
    assert record.document_url == "https://fi-admin.bvsalud.org/document/view/cfpaj"
    assert record.fulltext_id == "cfpaj"


def test_build_record_tolerates_missing_fields():
    from scholar_mcp.medical.brazil_moh import _build_record

    record = _build_record({"id": "biblio-2"})
    assert record.record_id == "biblio-2"
    assert record.title == ""
    assert record.year == ""
    assert record.authors == []


def test_is_brazilian_keeps_brasil_and_drops_others():
    from scholar_mcp.medical.brazil_moh import _build_record, _is_brazilian

    brazilian = _build_record({"id": "a", "pais_publicacao": ["^iBrazil^eBrasil"]})
    portuguese = _build_record({"id": "b", "pais_publicacao": ["^iPortugal^ePortugal"]})
    unknown = _build_record({"id": "c"})
    assert _is_brazilian(brazilian) is True
    assert _is_brazilian(portuguese) is False
    assert _is_brazilian(unknown) is False


from pathlib import Path

import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.brazil_moh import (
    BVS_HEADERS,
    BVS_SEARCH_URL,
    MAX_FULL_TEXT_CHARS,
    BrazilMoHEngine,
    _dedupe_by_id,
)
from scholar_mcp.medical.govbr_pcdt import GOVBR_HEADERS
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager


async def _engine(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = BrazilMoHEngine(http_client=http_client, cache=cache, settings=settings)
    return engine, cache, http_client


def _bvs_doc(record_id="biblio-1", title="Protocolo", country="^iBrazil^eBrasil", **extra):
    doc = {
        "id": record_id,
        "ti": [title],
        "la": ["pt"],
        "da": "202609",
        "pais_publicacao": [country],
        "ur": ["https://fi-admin.bvsalud.org/document/view/cfpaj"],
    }
    doc.update(extra)
    return doc


def _bvs_response(docs, num_found=None):
    return {
        "diaServerResponse": [
            {
                "responseHeader": {"status": 0},
                "response": {
                    "numFound": len(docs) if num_found is None else num_found,
                    "docs": docs,
                },
            }
        ]
    }


def test_dedupe_by_id_keeps_first_occurrence_and_order():
    docs = [{"id": "a"}, {"id": "b"}, {"id": "a"}, {"id": "c"}]
    assert [d["id"] for d in _dedupe_by_id(docs)] == ["a", "b", "c"]


def test_dedupe_by_id_handles_list_ids():
    docs = [{"id": ["a"]}, {"id": "b"}, {"id": "a"}, {"id": ["b"]}]
    assert len(_dedupe_by_id(docs)) == 2
    assert _dedupe_by_id(docs) == [{"id": ["a"]}, {"id": "b"}]


def test_dedupe_by_id_keeps_records_without_id():
    docs = [{"id": ""}, {"id": ""}]
    assert len(_dedupe_by_id(docs)) == 2


@respx.mock
async def test_search_deduplicates_and_trims_to_limit(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        docs = []
        for index in range(6):
            doc = _bvs_doc(record_id=f"biblio-{index}")
            docs.extend([doc, doc])  # every record duplicated
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response(docs))
        )
        records, meta = await engine.search_guidelines("dengue", limit=4)
        assert len(records) == 4
        assert [r.record_id for r in records] == [
            "biblio-0",
            "biblio-1",
            "biblio-2",
            "biblio-3",
        ]
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_requests_overfetched_count(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        await engine.search_guidelines("dengue", limit=10)
        requested = route.calls[0].request.url.params
        assert requested["count"] == "30"
        assert requested["output"] == "json"
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_caps_requested_count_at_page_size(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        await engine.search_guidelines("dengue", limit=50)
        assert route.calls[0].request.url.params["count"] == "150"
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_composes_filters_into_q_and_never_fq(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        await engine.search_guidelines("tratamento tuberculose", limit=5, collection="brisa")
        requested = str(route.calls[0].request.url)
        assert "fq=" not in requested
        assert "non-conventional" in requested
        assert "BRISA" in requested
        assert "AND" in requested
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_sends_browser_headers(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        await engine.search_guidelines("dengue", limit=5)
        headers = route.calls[0].request.headers
        assert "Mozilla/5.0" in headers["user-agent"]
        assert headers["accept-language"].startswith("pt-BR")
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_drops_non_brazilian_records(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        docs = [
            _bvs_doc(record_id="biblio-br", country="^iBrazil^eBrasil"),
            _bvs_doc(record_id="biblio-pt", country="^iPortugal^ePortugal"),
        ]
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response(docs))
        )
        records, _ = await engine.search_guidelines("dengue", limit=10)
        assert [r.record_id for r in records] == ["biblio-br"]
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_returns_short_list_as_success(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        records, meta = await engine.search_guidelines("dengue", limit=25)
        assert len(records) == 1
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


async def test_search_rejects_unknown_collection(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        records, meta = await engine.search_guidelines("dengue", collection="everything")
        assert records == []
        assert meta.error is True
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_clamps_limit_inside_engine(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        await engine.search_guidelines("dengue", limit=9999)
        assert route.calls[0].request.url.params["count"] == "150"
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_network_failure_is_error_and_not_cached(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            side_effect=httpx.ConnectError("reset by peer")
        )
        records, meta = await engine.search_guidelines("dengue", limit=5)
        assert records == []
        assert meta.error is True
        cached, cache_meta = await cache.get("brazil_moh_search:all:5:dengue")
        assert cache_meta.cached is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_caches_success_and_serves_from_cache(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        first, first_meta = await engine.search_guidelines("dengue", limit=5)
        second, second_meta = await engine.search_guidelines("dengue", limit=5)
        assert route.call_count == 1
        assert first_meta.cached is False
        assert second_meta.cached is True
        assert [r.record_id for r in second] == [r.record_id for r in first]
    finally:
        await cache.close()
        await http_client.aclose()


from scholar_mcp.medical.brazil_moh import (
    _is_allowed_host,
    is_allowed_bvs_host,
    is_allowed_host,
)

PDF_URL = "https://docs.bvsalud.org/biblioref/2026/08/1708363/protocolo.pdf"
FI_ADMIN_URL = "https://fi-admin.bvsalud.org/document/view/cfpaj"


def test_is_allowed_host_accepts_bvs_hosts_only():
    assert is_allowed_bvs_host(FI_ADMIN_URL) is True
    assert is_allowed_bvs_host(PDF_URL) is True
    assert is_allowed_host(FI_ADMIN_URL) is True
    assert _is_allowed_host(FI_ADMIN_URL) is True
    assert _is_allowed_host(PDF_URL) is True
    assert _is_allowed_host("https://fi-admin.bvsalud.org:443/document/view/123") is True
    assert _is_allowed_host("https://user:pass@docs.bvsalud.org/file.pdf") is True
    assert _is_allowed_host("https://www.sciencedirect.com/x") is False
    assert _is_allowed_host("https://evil.example.com/fi-admin.bvsalud.org") is False
    assert _is_allowed_host("") is False
    # gov.br PCDT PDFs may be served from any *.gov.br static host; the
    # catch-all is deliberate and its boundary is pinned here.
    assert _is_allowed_host("https://www.gov.br/saude/pt-br/assuntos/pcdt/a/x.pdf") is True
    assert _is_allowed_host("https://bvsms.saude.gov.br/pcdt.pdf") is True
    assert _is_allowed_host("https://gov.br.evil.com/x") is False
    assert _is_allowed_host("https://notgov.br/x") is False
    assert _is_allowed_host("https://saude.gov.br.evil.com/x") is False


@respx.mock
async def test_get_full_text_extracts_pdf(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text",
        lambda _: "Texto integral do protocolo.",
    )
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1", ab=["Resumo."])])
            )
        )
        respx.get(FI_ADMIN_URL).mock(
            return_value=httpx.Response(
                200, content=b"%PDF-1.5 fake", headers={"content-type": "application/pdf"}
            )
        )
        payload, meta = await engine.get_full_text("biblio-1")
        assert payload["status"] == "success"
        assert payload["content_type"] == "pdf"
        assert payload["content"] == "Texto integral do protocolo."
        assert payload["source"] == "brazil-moh"
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_offsite_url_degrades_without_fetching(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        doc = _bvs_doc(record_id="biblio-1", ab=["Resumo apenas."])
        doc["ur"] = ["https://www.sciencedirect.com/science/article/pii/S123"]
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([doc]))
        )
        offsite = respx.get(url__startswith="https://www.sciencedirect.com").mock(
            return_value=httpx.Response(200, content=b"should never be requested")
        )
        payload, _ = await engine.get_full_text("biblio-1")
        assert offsite.called is False
        assert payload["content_type"] == "abstract"
        assert payload["content"] == "Resumo apenas."
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_html_response_degrades_to_abstract(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1", ab=["Resumo."])])
            )
        )
        respx.get(FI_ADMIN_URL).mock(
            return_value=httpx.Response(
                200, text="<html>Estamos em manutenção</html>",
                headers={"content-type": "text/html"},
            )
        )
        payload, _ = await engine.get_full_text("biblio-1")
        assert payload["content_type"] == "abstract"
        assert payload["content"] == "Resumo."
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_no_pdf_and_no_abstract_is_not_found(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        doc = _bvs_doc(record_id="biblio-1")
        doc["ur"] = ["https://www.sciencedirect.com/x"]
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([doc]))
        )
        payload, _ = await engine.get_full_text("biblio-1")
        assert payload["status"] == "not_found"
        assert payload["content_type"] == "none"
        assert payload["content"] == ""
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_errored_without_abstract_is_error(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc(record_id="biblio-1")]))
        )
        respx.get(FI_ADMIN_URL).mock(side_effect=httpx.ConnectError("blocked"))
        payload, meta = await engine.get_full_text("biblio-1")
        assert payload["status"] == "error"
        assert meta.error is True
        _, cache_meta = await cache.get("brazil_moh_fulltext:biblio-1")
        assert cache_meta.cached is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_unknown_record_is_not_found(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        payload, meta = await engine.get_full_text("biblio-missing")
        assert payload["status"] == "not_found"
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_escapes_record_id_in_lookup(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        payload, _ = await engine.get_full_text('bi"blio\\1')
        assert payload["status"] == "not_found"
        assert route.calls[0].request.url.params["q"] == 'id:"bi\\"blio\\\\1"'
    finally:
        await cache.close()
        await http_client.aclose()


async def test_get_full_text_requires_record_id(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        payload, meta = await engine.get_full_text("")
        assert payload["status"] == "error"
        assert meta.error is True
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_rejects_redirect_off_allowlisted_hosts(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1", ab=["Resumo."])])
            )
        )
        respx.get(FI_ADMIN_URL).mock(
            return_value=httpx.Response(
                302, headers={"location": "https://evil.example.com/x.pdf"}
            )
        )
        respx.get(url__startswith="https://evil.example.com").mock(
            return_value=httpx.Response(
                200, content=b"%PDF", headers={"content-type": "application/pdf"}
            )
        )
        payload, meta = await engine.get_full_text("biblio-1")
        assert payload["content_type"] == "abstract"
        assert payload["content"] == "Resumo."
        assert meta.error is True
        _, cache_meta = await cache.get("brazil_moh_fulltext:biblio-1")
        assert cache_meta.cached is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_follows_redirect_within_allowed_hosts(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda _: "Conteúdo."
    )
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1")])
            )
        )
        respx.get(FI_ADMIN_URL).mock(
            return_value=httpx.Response(302, headers={"location": PDF_URL})
        )
        respx.get(PDF_URL).mock(
            return_value=httpx.Response(
                200, content=b"%PDF", headers={"content-type": "application/pdf"}
            )
        )
        payload, meta = await engine.get_full_text("biblio-1")
        assert payload["status"] == "success"
        assert payload["content_type"] == "pdf"
        assert payload["content"] == "Conteúdo."
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_pdf_failure_degrades_and_is_not_cached(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1", ab=["Resumo."])])
            )
        )
        respx.get(FI_ADMIN_URL).mock(side_effect=httpx.ConnectError("blocked"))
        payload, meta = await engine.get_full_text("biblio-1")
        assert payload["content_type"] == "abstract"
        assert meta.error is True
        _, cache_meta = await cache.get("brazil_moh_fulltext:biblio-1")
        assert cache_meta.cached is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_caches_success(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda _: "Conteúdo."
    )
    engine, cache, http_client = await _engine(tmp_path)
    try:
        search = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1")])
            )
        )
        respx.get(FI_ADMIN_URL).mock(
            return_value=httpx.Response(
                200, content=b"%PDF", headers={"content-type": "application/pdf"}
            )
        )
        await engine.get_full_text("biblio-1")
        payload, meta = await engine.get_full_text("biblio-1")
        assert search.call_count == 1
        assert meta.cached is True
        assert payload["content"] == "Conteúdo."
    finally:
        await cache.close()
        await http_client.aclose()


def test_serve_full_text_clamps_zero_and_negative_max_chars():
    payload = {"content": "abcdef"}
    for bad_limit in (0, -3):
        served = BrazilMoHEngine._serve_full_text(dict(payload), bad_limit)
        # max_chars is clamped to 1: one source char survives plus the
        # truncation marker appended by truncate_content.
        assert served["content"].startswith("a\n\n[... Truncated")
        assert served["truncated"] is True


@respx.mock
async def test_get_full_text_truncates_served_not_cached(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda _: "x" * 500
    )
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1")])
            )
        )
        respx.get(FI_ADMIN_URL).mock(
            return_value=httpx.Response(
                200, content=b"%PDF", headers={"content-type": "application/pdf"}
            )
        )
        payload, _ = await engine.get_full_text("biblio-1", max_chars=100)
        assert payload["content"].startswith("x" * 100)
        assert payload["truncated"] is True
        assert len(payload["content"]) < 500
        cached, _ = await cache.get("brazil_moh_fulltext:biblio-1")
        assert len(cached["content"]) == 500
    finally:
        await cache.close()
        await http_client.aclose()





@respx.mock
async def test_get_full_text_ignores_non_matching_lookup_hit(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=_bvs_response(
                    [_bvs_doc(record_id="biblio-999", ab="Resumo do outro registro.")]
                ),
            )
        )
        payload, meta = await engine.get_full_text("biblio-1")
        assert payload["status"] == "not_found"
        assert payload["content"] == ""
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


def test_serve_full_text_clamps_max_chars_to_module_ceiling():
    payload = {"content": "x" * (MAX_FULL_TEXT_CHARS + 500)}
    served = BrazilMoHEngine._serve_full_text(dict(payload), 10_000_000)
    assert served["truncated"] is True
    assert served["content"].startswith("x" * MAX_FULL_TEXT_CHARS)
    assert served["content"][MAX_FULL_TEXT_CHARS:].lstrip().startswith("[... Truncated")


@respx.mock
async def test_get_full_text_caps_cached_content_at_ceiling(tmp_path: Path, monkeypatch):
    oversized = "x" * (MAX_FULL_TEXT_CHARS + 1000)
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda _: oversized
    )
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1")])
            )
        )
        respx.get(FI_ADMIN_URL).mock(
            return_value=httpx.Response(
                200, content=b"%PDF", headers={"content-type": "application/pdf"}
            )
        )
        await engine.get_full_text("biblio-1")
        cached, _ = await cache.get("brazil_moh_fulltext:biblio-1")
        assert len(cached["content"]) <= MAX_FULL_TEXT_CHARS
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_returns_empty_for_query_with_no_usable_tokens(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        records, meta = await engine.search_guidelines("*** AND ~~~")
        assert records == []
        assert meta.error is False
        assert route.call_count == 0
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_still_browses_on_a_blank_query(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        records, meta = await engine.search_guidelines("   ")
        assert len(records) == 1
        assert meta.error is False
        assert route.call_count == 1
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_cache_key_ignores_query_whitespace(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        first, _ = await engine.search_guidelines("dengue")
        second, meta = await engine.search_guidelines("  dengue  ")
        assert route.call_count == 1
        assert meta.cached is True
        assert [r.record_id for r in second] == [r.record_id for r in first]
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_relaxes_to_or_when_strict_returns_nothing(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            side_effect=[
                httpx.Response(200, json=_bvs_response([])),
                httpx.Response(200, json=_bvs_response([_bvs_doc(title="Dengue hemorrágica")])),
            ]
        )
        records, meta = await engine.search_guidelines("dengue hemorragica", limit=5)

        assert route.call_count == 2
        first = str(route.calls[0].request.url)
        second = str(route.calls[1].request.url)
        assert "dengue+AND+hemorragica" in first or "dengue%20AND%20hemorragica" in first
        assert "dengue+OR+hemorragica" in second or "dengue%20OR%20hemorragica" in second
        assert len(records) == 1
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_extract_pdf_text_headers_by_hostname(tmp_path, monkeypatch):
    """gov.br appearing in a query string must not switch to GOVBR headers."""
    settings = Settings()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "t.db", settings=settings)
    engine = BrazilMoHEngine(http_client=http_client, cache=cache, settings=settings)
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda b: "texto"
    )
    respx.get("https://docs.bvsalud.org/x").mock(
        return_value=httpx.Response(
            200, content=b"%PDF-1.4", headers={"Content-Type": "application/pdf"}
        )
    )
    try:
        text, errored = await engine._extract_pdf_text(
            "https://docs.bvsalud.org/x?ref=gov.br"
        )
        assert errored is False
        assert text == "texto"
        sent = respx.calls.last.request.headers
        # User-Agent is identical in both header sets; Accept is GOVBR-only.
        assert sent["accept"] != GOVBR_HEADERS["Accept"]
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_extract_pdf_text_uses_govbr_headers_on_gov_host(tmp_path, monkeypatch):
    settings = Settings()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "t.db", settings=settings)
    engine = BrazilMoHEngine(http_client=http_client, cache=cache, settings=settings)
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda b: "texto"
    )
    respx.get(url__startswith="https://www.gov.br/").mock(
        return_value=httpx.Response(
            200, content=b"%PDF-1.4", headers={"Content-Type": "application/pdf"}
        )
    )
    try:
        _text, errored = await engine._extract_pdf_text(
            "https://www.gov.br/saude/pt-br/assuntos/pcdt/a/acromegalia.pdf/@@download/file"
        )
        assert errored is False
        sent = respx.calls.last.request.headers
        assert sent["accept"] == GOVBR_HEADERS["Accept"]
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_relaxed_request_reuses_the_same_count(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            side_effect=[
                httpx.Response(200, json=_bvs_response([])),
                httpx.Response(200, json=_bvs_response([])),
            ]
        )
        await engine.search_guidelines("dengue hemorragica", limit=5)

        assert route.call_count == 2
        # count=0 returns HTTP 500 from this endpoint, so the relaxed request
        # must reuse the over-fetch count, never a count-only probe.
        assert route.calls[0].request.url.params["count"] == "15"
        assert route.calls[1].request.url.params["count"] == "15"
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_relaxes_when_strict_hits_are_all_non_brazilian(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            side_effect=[
                # Portuguese but published elsewhere: dropped by _is_brazilian,
                # so the record list is empty and relaxation must fire.
                httpx.Response(
                    200,
                    json=_bvs_response([_bvs_doc(country="^iPortugal^ePortugal")]),
                ),
                httpx.Response(200, json=_bvs_response([_bvs_doc(title="Dengue hemorrágica")])),
            ]
        )
        records, _ = await engine.search_guidelines("dengue hemorragica", limit=5)

        assert route.call_count == 2
        assert len(records) == 1
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_does_not_relax_for_a_single_token(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        records, meta = await engine.search_guidelines("dengue", limit=5)

        # The strict and relaxed groups would be byte-identical.
        assert route.call_count == 1
        assert records == []
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_does_not_relax_when_stopwords_leave_one_token(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        # Two raw tokens, one substantive. The gate counts substantive tokens.
        await engine.search_guidelines("dengue da", limit=5)

        assert route.call_count == 1
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_does_not_relax_when_strict_finds_a_brazilian_record(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(title="Dengue hemorrágica")])
            )
        )
        records, _ = await engine.search_guidelines("dengue hemorragica", limit=5)

        assert route.call_count == 1
        assert len(records) == 1
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_caches_relaxed_result_under_the_strict_key(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            side_effect=[
                httpx.Response(200, json=_bvs_response([])),
                httpx.Response(200, json=_bvs_response([_bvs_doc(title="Dengue hemorrágica")])),
            ]
        )
        first, _ = await engine.search_guidelines("dengue hemorragica", limit=5)
        second, meta = await engine.search_guidelines("dengue hemorragica", limit=5)

        # One user query, one cache row: no third request.
        assert route.call_count == 2
        assert meta.cached is True
        assert [g.record_id for g in second] == [g.record_id for g in first]
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_score_survives_the_cache_round_trip(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(title="Dengue hemorrágica")])
            )
        )
        fresh, _ = await engine.search_guidelines("dengue hemorragica", limit=5)
        cached, meta = await engine.search_guidelines("dengue hemorragica", limit=5)

        assert meta.cached is True
        assert fresh[0].score is not None
        assert cached[0].score == pytest.approx(fresh[0].score)
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_relaxed_request_failure_is_error_and_not_cached(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            side_effect=[
                httpx.Response(200, json=_bvs_response([])),
                httpx.Response(200, text="<html>Estamos em manutenção</html>"),
            ]
        )
        records, meta = await engine.search_guidelines("dengue hemorragica", limit=5)

        assert records == []
        assert meta.error is True
        composed = 'type:"non-conventional" AND la:"pt" AND (dengue AND hemorragica)'
        _payload, cache_meta = await cache.get(f"brazil_moh_search:all:5:{composed}")
        assert cache_meta.cached is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_stopword_only_query_makes_no_request(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        # Every token is a Portuguese stopword, so nothing selective remains.
        # Composing filters alone would return arbitrary top-of-index
        # documents dressed as matches for terms never searched.
        records, meta = await engine.search_guidelines("sobre a", limit=5)

        assert records == []
        assert meta.error is False
        assert route.call_count == 0
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_ranks_before_slicing(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        # limit=2 over-fetches 6. The best match sits fourth in BVS order, so
        # it only survives if ranking runs before the slice.
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=_bvs_response([
                    _bvs_doc(record_id="biblio-0", title="Relatorio anual"),
                    _bvs_doc(record_id="biblio-1", title="Nota tecnica"),
                    _bvs_doc(record_id="biblio-2", title="Informe semanal"),
                    _bvs_doc(record_id="biblio-3", title="Manejo clínico da dengue hemorrágica"),
                ]),
            )
        )
        records, _ = await engine.search_guidelines("dengue hemorragica", limit=2)

        assert len(records) == 2
        assert records[0].record_id == "biblio-3"
        assert all(g.score is not None for g in records)
    finally:
        await cache.close()
        await http_client.aclose()
