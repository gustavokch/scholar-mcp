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
