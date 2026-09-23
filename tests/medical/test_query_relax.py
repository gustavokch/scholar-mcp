"""Unit tests for the shared PubMed query-relaxation ladder (plan B1)."""

from scholar_mcp.query_relax import (
    MAX_RELAX_EXTRA_CALLS,
    content_overlap_count,
    content_tokens,
    normalize_query,
    relax_ladder,
)


def test_normalize_query_strips_operators_and_punctuation():
    assert normalize_query('what is "ibuprofen" (for children)') == "what is ibuprofen for children"
    assert normalize_query("a OR b AND c") == "a b c"
    assert normalize_query("  spaced   out  ") == "spaced out"


def test_content_tokens_fold_accents_and_drop_stopwords():
    tokens = content_tokens("tempo necessário para início de proteção")
    assert "necessario" in tokens
    assert "para" not in tokens
    assert "de" not in tokens
    assert tokens == ["tempo", "necessario", "inicio", "protecao"]


def test_content_tokens_drop_english_stopwords_and_dedupe():
    assert content_tokens("management of pelvic trauma and the pelvis") == [
        "management",
        "pelvic",
        "trauma",
        "pelvis",
    ]


def test_relax_ladder_long_query_schedule():
    ladder = relax_ladder(
        "Epstein-Barr virus infectious mononucleosis exudative tonsillitis "
        "posterior cervical lymphadenopathy rash adolescent"
    )
    assert ladder[0] == (
        "epstein barr virus infectious mononucleosis exudative tonsillitis "
        "posterior cervical lymphadenopathy rash adolescent"
    )
    assert [len(q.split()) for q in ladder] == [12, 5, 4, 3]


def test_relax_ladder_short_query_dedupes():
    assert relax_ladder("alpha beta gamma delta epsilon") == [
        "alpha beta gamma delta epsilon",
        "alpha beta gamma delta",
        "alpha beta gamma",
    ]
    assert relax_ladder("alpha beta gamma delta") == [
        "alpha beta gamma delta",
        "alpha beta gamma",
    ]
    assert relax_ladder("asthma management") == ["asthma management"]


def test_relax_ladder_single_token_kept():
    assert relax_ladder("asthma") == ["asthma"]
    assert relax_ladder("") == []


def test_relax_ladder_skips_already_normalized_full():
    # A query that is already its own content-token join still ladders down.
    assert relax_ladder("nsaids third trimester pregnancy contraindications") == [
        "nsaids third trimester pregnancy contraindications",
        "nsaids third trimester pregnancy",
        "nsaids third trimester",
    ]


def test_max_relax_extra_calls_budget():
    assert MAX_RELAX_EXTRA_CALLS == 3


def test_content_overlap_count_accent_folded():
    assert content_overlap_count("tempo necessário início proteção", "Tempo Necessário Para Proteção") >= 1
    assert content_overlap_count("veterinary oncology canine", "Human hypertension guideline") == 0
    assert content_overlap_count("", "anything") == 0
