from scholar_mcp.medical.who_iris import _all_meta, _first_meta


def test_first_meta_returns_first_value():
    metadata = {"dc.title": [{"value": "Guideline A"}, {"value": "Guideline A (alt)"}]}
    assert _first_meta(metadata, "dc.title") == "Guideline A"


def test_first_meta_missing_key_returns_empty_string():
    assert _first_meta({}, "dc.title") == ""
    assert _first_meta({"dc.title": []}, "dc.title") == ""


def test_first_meta_skips_entry_missing_value_field():
    metadata = {"dc.date.issued": [{"language": "en"}]}
    assert _first_meta(metadata, "dc.date.issued") == ""


def test_all_meta_returns_every_value():
    metadata = {"dc.contributor.author": [{"value": "World Health Organization"}, {"value": "WHO Regional Office"}]}
    assert _all_meta(metadata, "dc.contributor.author") == [
        "World Health Organization",
        "WHO Regional Office",
    ]


def test_all_meta_missing_key_returns_empty_list():
    assert _all_meta({}, "dc.subject.mesh") == []


def test_all_meta_skips_non_dict_and_empty_value_entries():
    metadata = {"dc.subject": [{"value": "Vitamin A"}, "not-a-dict", {"value": ""}, {}]}
    assert _all_meta(metadata, "dc.subject") == ["Vitamin A"]
