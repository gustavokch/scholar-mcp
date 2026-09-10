import pytest

from scholar_mcp.medical.models import BrazilGuideline, MedicalArticle
from scholar_mcp.medical.ranking import (
    PORTUGUESE_STOPWORDS,
    normalize_portuguese,
    rank_brazil_guidelines,
    rank_medical_articles,
    tokenize_portuguese,
)


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


def test_normalize_portuguese_folds_accents_and_lowercases():
    assert normalize_portuguese("Atenção Básica à Saúde") == "atencao basica a saude"
    assert normalize_portuguese("CÂNCER") == "cancer"
    # Every accented vowel and the cedilla, plus the grave the spec calls out.
    assert normalize_portuguese("á é í ó ú â ê ô ã õ à ç") == "a e i o u a e o a o a c"


def test_normalize_portuguese_handles_falsy_input():
    assert normalize_portuguese(None) == ""
    assert normalize_portuguese("") == ""


def test_tokenize_portuguese_strips_stopwords():
    assert tokenize_portuguese("manejo da dengue") == ["manejo", "dengue"]
    assert tokenize_portuguese("tratamento de tuberculose para adultos") == [
        "tratamento",
        "tuberculose",
        "adultos",
    ]


def test_tokenize_portuguese_strips_accented_stopword():
    # "à" folds to "a", which is a stopword; "atenção" folds to a substantive token.
    assert tokenize_portuguese("atenção à saúde") == ["atencao", "saude"]


def test_tokenize_portuguese_drops_short_tokens():
    # The >= 2 floor applies here, unlike in brazil_moh._usable_tokens.
    assert tokenize_portuguese("b dengue c") == ["dengue"]


def test_tokenize_portuguese_matches_folded_and_unfolded_forms():
    # A query term and a document term must tokenize to the same string.
    assert tokenize_portuguese("cancer") == tokenize_portuguese("câncer")


def test_tokenize_portuguese_handles_falsy_and_punctuation():
    assert tokenize_portuguese(None) == []
    assert tokenize_portuguese("") == []
    assert tokenize_portuguese("---") == []


def test_portuguese_stopwords_are_stored_accent_folded():
    # The set is consulted after folding, so an accented member would be dead.
    for word in PORTUGUESE_STOPWORDS:
        assert normalize_portuguese(word) == word


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


def test_position_weight_preserves_source_order_on_ties():
    query = "asthma"
    articles = [
        _article("Asthma outcomes A", year="2020"),
        _article("Asthma outcomes B", year="2020"),
    ]
    ranked = rank_medical_articles(
        articles, query, current_year=2026, position_weight=0.35
    )
    assert ranked[0].title == "Asthma outcomes A"
    assert ranked[0].score > ranked[1].score


def test_position_weight_does_not_override_strong_lexical_signal():
    query = "metformin diabetes"
    articles = [
        _article("Unrelated first result", year="2020"),
        _article("Metformin diabetes trial", year="2020"),
    ]
    ranked = rank_medical_articles(
        articles, query, current_year=2026, position_weight=0.35
    )
    assert ranked[0].title == "Metformin diabetes trial"


def test_position_weight_defaults_to_zero():
    query = "asthma"
    articles = [
        _article("Asthma outcomes A", year="2020"),
        _article("Asthma outcomes B", year="2020"),
    ]
    ranked = rank_medical_articles(articles, query, current_year=2026)
    assert ranked[0].score == ranked[1].score


def test_stopword_only_query_returns_new_list():
    """Every call returns a fresh list, scored path or not."""
    articles = [_article("Second paper"), _article("First paper")]
    ranked = rank_medical_articles(articles, "in the of and")
    assert ranked is not articles
    assert [a.title for a in ranked] == ["Second paper", "First paper"]


def _guideline(title: str, abstract: str = "", year: str = "", **kwargs) -> BrazilGuideline:
    return BrazilGuideline(title=title, abstract=abstract, year=year, **kwargs)


def test_rank_brazil_guidelines_folds_accents():
    # The query is unaccented; the title is not. They must still match.
    guidelines = [
        _guideline("Protocolo de rotina", year="2020"),
        _guideline("Câncer de mama", year="2020"),
    ]
    ranked = rank_brazil_guidelines(guidelines, "cancer", current_year=2026)
    assert ranked[0].title == "Câncer de mama"


def test_rank_brazil_guidelines_title_outranks_abstract_only():
    guidelines = [
        _guideline("Documento geral", abstract="Trata da dengue no Brasil.", year="2020"),
        _guideline("Manejo da dengue", year="2020"),
    ]
    ranked = rank_brazil_guidelines(guidelines, "dengue", current_year=2026)
    assert ranked[0].title == "Manejo da dengue"


def test_rank_brazil_guidelines_newer_year_wins():
    guidelines = [
        _guideline("Manejo da dengue", year="2005"),
        _guideline("Manejo da dengue", year="2024"),
    ]
    ranked = rank_brazil_guidelines(guidelines, "dengue", current_year=2026)
    assert ranked[0].year == "2024"


def test_rank_brazil_guidelines_uses_title_en():
    # Second position, so the source-position prior works against it; it must
    # still win on the strength of the English title alone.
    guidelines = [
        _guideline("Tratamento da dengue ", year="2020"),
        _guideline("Tratamento da dengue", title_en="Dengue treatment", year="2020"),
    ]
    ranked = rank_brazil_guidelines(guidelines, "dengue treatment", current_year=2026)
    assert ranked[0].title_en == "Dengue treatment"


def test_rank_brazil_guidelines_uses_mesh_subjects():
    # An abstract-less record rescued by its DeCS descriptors, again from
    # second position so the position prior does not carry it.
    guidelines = [
        _guideline("Caderno de Atenção", year="2020"),
        _guideline(
            "Caderno de Atenção",
            year="2020",
            mesh_subjects=["Atenção Primária à Saúde"],
        ),
    ]
    ranked = rank_brazil_guidelines(guidelines, "atencao primaria", current_year=2026)
    assert ranked[0].mesh_subjects == ["Atenção Primária à Saúde"]


def test_rank_brazil_guidelines_stable_on_ties():
    # Identical records must keep BVS order.
    guidelines = [
        _guideline("Manejo da dengue", record_id="first", year="2020"),
        _guideline("Manejo da dengue", record_id="second", year="2020"),
    ]
    ranked = rank_brazil_guidelines(guidelines, "dengue", current_year=2026)
    assert [g.record_id for g in ranked] == ["first", "second"]


def test_rank_brazil_guidelines_populates_score():
    guidelines = [_guideline("Manejo da dengue", year="2020")]
    ranked = rank_brazil_guidelines(guidelines, "dengue", current_year=2026)
    assert ranked[0].score is not None
    assert 0.0 <= ranked[0].score <= 1.0


def test_rank_brazil_guidelines_stopword_only_query_leaves_score_unset():
    # "sobre a" tokenizes to nothing, so there is no basis for a score.
    guidelines = [_guideline("B documento"), _guideline("A documento")]
    ranked = rank_brazil_guidelines(guidelines, "sobre a")
    assert [g.title for g in ranked] == ["B documento", "A documento"]
    assert all(g.score is None for g in ranked)


def test_rank_brazil_guidelines_empty_returns_empty():
    assert rank_brazil_guidelines([], "dengue") == []


def test_rank_brazil_guidelines_missing_year_uses_default_age():
    # An unparseable or absent year must not raise; it falls back to the
    # 10-year default age, so it scores below an otherwise identical record
    # that carries a recent year.
    guidelines = [
        _guideline("Manejo da dengue", year=""),
        _guideline("Manejo da dengue", year="2026"),
    ]
    ranked = rank_brazil_guidelines(guidelines, "dengue", current_year=2026)
    assert ranked[0].year == "2026"
    assert all(g.score is not None for g in ranked)

    garbage = [_guideline("Manejo da dengue", year="n/a")]
    assert rank_brazil_guidelines(garbage, "dengue", current_year=2026)[0].score is not None


def test_rank_brazil_guidelines_none_text_does_not_raise():
    g = BrazilGuideline(title="Manejo da dengue", year="2020")
    g.abstract = None  # type: ignore[assignment]
    g.title_en = None  # type: ignore[assignment]
    ranked = rank_brazil_guidelines([g], "dengue", current_year=2026)
    assert ranked[0].score is not None
