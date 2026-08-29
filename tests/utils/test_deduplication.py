from scholar_mcp.utils.deduplication import (
    are_duplicates,
    calculate_similarity,
    deduplicate_papers,
    extract_first_author,
    extract_year,
    normalize_title,
)


def test_normalize_title():
    raw = "Efficacy of Metformin &amp; Diet in Type 2 Diabetes: [Preprint] Version 1"
    assert normalize_title(raw) == "efficacy of metformin & diet in type 2 diabetes"


def test_calculate_similarity():
    s1 = "treatment of hypertension in elderly patients"
    s2 = "treatment of hypertension in elderly patient"
    assert calculate_similarity(s1, s2) > 0.95
    assert calculate_similarity("", "abc") == 0.0
    assert calculate_similarity("exact", "exact") == 1.0


def test_extract_first_author():
    assert extract_first_author(["Smith J", "Doe A"]) == "smith"
    assert extract_first_author("Johnson, M. et al.") == "johnson"
    assert extract_first_author("J. Watson") == "watson"
    assert extract_first_author([]) is None


def test_extract_year():
    assert extract_year("2023-05-12") == "2023"
    assert extract_year("Published in 2021") == "2021"
    assert extract_year("Unknown") is None


def test_are_duplicates_doi_match():
    p1 = {
        "title": "Aspirin in Cardiovascular Disease",
        "doi": "10.1001/jama.2020.1",
        "authors": ["Smith J"],
        "year": "2020",
    }
    p2 = {
        "title": "Aspirin in cardiovascular disease",
        "doi": "10.1001/jama.2020.1",
        "authors": ["Smith, John"],
        "year": "2020",
    }
    assert are_duplicates(p1, p2) is True


def test_are_duplicates_fuzzy_match():
    p1 = {
        "title": "Treatment of Hypertension in Elderly Patients",
        "doi": None,
        "authors": ["Smith J"],
        "year": "2020",
    }
    p2 = {
        "title": "Treatment of Hypertension in Elderly Patient",
        "doi": None,
        "authors": ["Smith J"],
        "year": "2020",
    }
    assert are_duplicates(p1, p2) is True


def test_deduplicate_papers_keeps_richer_metadata():
    papers = [
        {"title": "Study A", "doi": "10.1000/1", "abstract": "Short"},
        {"title": "Study A", "doi": "10.1000/1", "abstract": "Detailed abstract with more text"},
        {"title": "Study B", "doi": "10.1000/2", "abstract": "Another study"},
    ]
    unique, stats = deduplicate_papers(papers)
    assert len(unique) == 2
    assert stats["duplicates_removed"] == 1
    assert stats["total_input"] == 3
    match_a = next(p for p in unique if p["title"] == "Study A")
    assert match_a["abstract"] == "Detailed abstract with more text"
