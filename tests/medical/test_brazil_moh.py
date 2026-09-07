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
