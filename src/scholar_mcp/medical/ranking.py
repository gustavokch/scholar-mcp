import datetime
import re

from scholar_mcp.medical.models import MedicalArticle
from scholar_mcp.ranking import ScoringEngine

# Recency weight and half-life mirror the scholar path defaults (0.3 / 7 years).
RECENCY_WEIGHT = 0.3
RELEVANCE_WEIGHT = 0.7
RECENCY_HALF_LIFE_YEARS = 7.0
DEFAULT_AGE_YEARS = 10.0

_TITLE_WEIGHT = 2.0
_ABSTRACT_WEIGHT = 1.0

_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# Small stopword set so generic queries ("in type 2 diabetes") do not match
# every article equally. Clinical terms like "2" are kept.
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "is",
    "of", "on", "or", "the", "to", "with",
}


def _tokenize(text: str | None) -> list[str]:
    if not text:
        return []
    return [
        t for t in _WORD_SPLIT_RE.split(text.lower())
        if len(t) >= 2 and t not in _STOPWORDS
    ]


def rank_medical_articles(
    articles: list[MedicalArticle],
    query: str,
    current_year: int | None = None,
) -> list[MedicalArticle]:
    """Re-rank articles by query-term overlap and recency.

    Lexical relevance counts title hits twice as heavily as abstract hits;
    recency uses the same exponential half-life decay as the scholar path.
    Pure function: no network calls, source order breaks ties.
    """
    if not articles:
        return []

    terms = _tokenize(query)
    if not terms:
        return articles

    now_year = current_year if current_year is not None else datetime.datetime.now().year
    # Relevance is expressed as field coverage rather than a raw weighted hit
    # count. Dividing by (title + abstract) weights would assume every term
    # appears in both fields, capping a title-only match at 2/3 and an
    # abstract-only match at 1/3 -- low enough that the 0..1 recency term could
    # outrank genuine lexical evidence.
    _ABSTRACT_RATIO = _ABSTRACT_WEIGHT / _TITLE_WEIGHT
    term_count = len(terms)

    scored: list[tuple[float, int, MedicalArticle]] = []
    for idx, article in enumerate(articles):
        title_terms = set(_tokenize(article.title))
        abstract_terms = set(_tokenize(article.abstract))

        title_coverage = sum(1 for t in terms if t in title_terms) / term_count
        abstract_coverage = sum(1 for t in terms if t in abstract_terms) / term_count
        relevance = min(1.0, title_coverage + _ABSTRACT_RATIO * abstract_coverage)

        recency, _ = ScoringEngine.calculate_recency_feature(
            article.year,
            current_year=now_year,
            half_life_years=RECENCY_HALF_LIFE_YEARS,
            default_age=DEFAULT_AGE_YEARS,
        )

        final_score = RELEVANCE_WEIGHT * relevance + RECENCY_WEIGHT * recency
        article.score = final_score
        scored.append((final_score, idx, article))

    # Stable: equal scores keep source order (idx).
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [article for _, _, article in scored]
