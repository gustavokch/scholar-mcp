import json

import pytest

from scholar_mcp import ranking
from scholar_mcp.ranking import (
    ScoringEngine,
    _normalize_issn,
    _normalize_journal_name,
    lookup_journal_impact,
    parse_scimago_csv,
)


@pytest.fixture(autouse=True)
def _clear_scimago_cache():
    ranking._load_scimago_table.cache_clear()
    yield
    ranking._load_scimago_table.cache_clear()


def test_normalize_issn_strips_dashes_and_uppercases():
    assert _normalize_issn("0028-0836") == "00280836"
    assert _normalize_issn("1932-6203") == "19326203"


def test_normalize_journal_name_lowercases_and_strips_punctuation():
    assert _normalize_journal_name("The New England Journal of Medicine!") == "the new england journal of medicine"


def test_lookup_journal_impact_by_issn(tmp_path, monkeypatch):
    data_file = tmp_path / "scimago_sjr.json"
    data_file.write_text(json.dumps({"issn": {"00280836": 18.5}, "name": {}}))
    monkeypatch.setattr(ranking, "_SCIMAGO_DATA_PATH", data_file)
    ranking._load_scimago_table.cache_clear()

    assert lookup_journal_impact("0028-0836", "Nature") == 18.5


def test_lookup_journal_impact_falls_back_to_name(tmp_path, monkeypatch):
    data_file = tmp_path / "scimago_sjr.json"
    data_file.write_text(json.dumps({"issn": {}, "name": {"nature": 18.5}}))
    monkeypatch.setattr(ranking, "_SCIMAGO_DATA_PATH", data_file)
    ranking._load_scimago_table.cache_clear()

    assert lookup_journal_impact(None, "Nature") == 18.5
    assert lookup_journal_impact("9999-9999", "Nature") == 18.5


def test_lookup_journal_impact_miss_returns_none(tmp_path, monkeypatch):
    data_file = tmp_path / "scimago_sjr.json"
    data_file.write_text(json.dumps({"issn": {}, "name": {}}))
    monkeypatch.setattr(ranking, "_SCIMAGO_DATA_PATH", data_file)
    ranking._load_scimago_table.cache_clear()

    assert lookup_journal_impact("0000-0000", "Unknown Journal") is None


def test_lookup_journal_impact_missing_file_returns_empty_table(tmp_path, monkeypatch):
    monkeypatch.setattr(ranking, "_SCIMAGO_DATA_PATH", tmp_path / "does_not_exist.json")
    ranking._load_scimago_table.cache_clear()

    assert lookup_journal_impact("0028-0836", "Nature") is None


def test_calculate_impact_feature():
    assert ScoringEngine.calculate_impact_feature(None) == 0.0
    assert ScoringEngine.calculate_impact_feature(0) == 0.0
    import math
    assert math.isclose(ScoringEngine.calculate_impact_feature(9.0), math.log(10.0))


def test_parse_scimago_csv_basic():
    rows = [
        {"Title": "Nature", "Issn": "00280836, 14764687", "SJR": "18,543"},
        {"Title": "PLOS ONE", "Issn": "19326203", "SJR": "0,821"},
        {"Title": "Bad Row", "Issn": "12345678", "SJR": "not-a-number"},
        {"Title": "", "Issn": "11112222", "SJR": "5,0"},
    ]
    table = parse_scimago_csv(rows)
    assert table["issn"]["00280836"] == 18.543
    assert table["issn"]["14764687"] == 18.543
    assert table["name"]["nature"] == 18.543
    assert table["issn"]["19326203"] == 0.821
    assert "12345678" not in table["issn"]
    assert "11112222" not in table["issn"]
