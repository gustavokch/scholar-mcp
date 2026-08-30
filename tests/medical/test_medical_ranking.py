import pytest

from scholar_mcp.medical.models import MedicalArticle
from scholar_mcp.medical.ranking import rank_medical_articles


def _article(title: str, abstract: str = "", year: str = "", **kwargs) -> MedicalArticle:
    return MedicalArticle(title=title, abstract=abstract, year=year, **kwargs)


def test_title_match_outranks_no_match():
    query = "metformin diabetes"
    articles = [
        _article("Random other study", abstract="Nothing relevant here."),
        _article("Metformin efficacy in diabetes", abstract="Biguanide therapy."),
    ]
    ranked = rank_medical_articles(articles, query)
    assert ranked[0].title == "Metformin efficacy in diabetes"


def test_title_hit_outranks_abstract_only_hit():
    query = "insulin resistance"
    articles = [
        _article("A study of metabolism", abstract="We examine insulin resistance in cells."),
        _article("Insulin resistance mechanisms"),
    ]
    ranked = rank_medical_articles(articles, query)
    assert ranked[0].title == "Insulin resistance mechanisms"


def test_equal_relevance_newer_year_wins():
    query = "asthma"
    articles = [
        _article("Asthma outcomes", year="2005"),
        _article("Asthma outcomes", year="2023"),
    ]
    ranked = rank_medical_articles(articles, query)
    assert ranked[0].year == "2023"


def test_empty_query_preserves_order():
    articles = [_article("B paper"), _article("A paper")]
    ranked = rank_medical_articles(articles, "")
    assert [a.title for a in ranked] == ["B paper", "A paper"]


def test_empty_articles_returns_empty():
    assert rank_medical_articles([], "anything") == []


def test_scores_populated_on_articles():
    query = "metformin"
    articles = [_article("Metformin study", year="2020")]
    ranked = rank_medical_articles(articles, query)
    assert ranked[0].score is not None
    assert 0.0 <= ranked[0].score <= 1.0


def test_none_text_does_not_raise():
    # Article with explicit None abstract must not crash tokenization.
    article = MedicalArticle(title="Metformin trial", abstract=None)  # type: ignore[arg-type]
    ranked = rank_medical_articles([article], "metformin")
    assert ranked[0].score is not None


def test_stopword_only_query_preserves_order():
    articles = [_article("Second paper"), _article("First paper")]
    ranked = rank_medical_articles(articles, "in the of and")
    assert [a.title for a in ranked] == ["Second paper", "First paper"]


def test_punctuation_heavy_query_ranks_correctly():
    query = "metformin, diabetes!"
    articles = [
        _article("Other topic", abstract="Nothing."),
        _article("Metformin and diabetes outcomes"),
    ]
    ranked = rank_medical_articles(articles, query)
    assert ranked[0].title == "Metformin and diabetes outcomes"


def test_abstract_match_outranks_irrelevant_recent_article():
    """Lexical evidence must be able to outweigh the 0..1 recency term.

    Before the coverage fix, an abstract-only full match capped at 0.33 of the
    relevance range, so a zero-match 2026 article (0.3000) beat a 2010 article
    matching every query term in its abstract (0.2949).
    """
    query = "metformin diabetes"
    articles = [
        _article("Weekly news roundup", abstract="Unrelated content.", year="2026"),
        _article(
            "Cohort study of outcomes",
            abstract="We study metformin therapy in diabetes patients.",
            year="2010",
        ),
    ]
    ranked = rank_medical_articles(articles, query, current_year=2026)
    assert ranked[0].title == "Cohort study of outcomes"


def test_full_title_match_reaches_max_relevance():
    query = "metformin diabetes"
    articles = [_article("Metformin diabetes", year="2026")]
    ranked = rank_medical_articles(articles, query, current_year=2026)
    assert ranked[0].score == pytest.approx(1.0)


def test_full_abstract_only_match_reaches_half_relevance():
    query = "metformin diabetes"
    articles = [
        _article("Cohort study", abstract="Metformin in diabetes.", year="2026")
    ]
    ranked = rank_medical_articles(articles, query, current_year=2026)
    # 0.7 * 0.5 relevance + 0.3 * 1.0 recency
    assert ranked[0].score == pytest.approx(0.65)
