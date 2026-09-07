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


def test_derive_fulltext_id_matches_fi_admin_url():
    url = "https://fi-admin.bvsalud.org/document/view/cfpaj"
    assert _derive_fulltext_id(url) == "cfpaj"


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

