import datetime

from scholar_mcp.medical.models import MedicalArticle
from scholar_mcp.ranking import ScoringEngine

# Recency weight and half-life mirror the scholar path defaults (0.3 / 7 years).
RECENCY_WEIGHT = 0.3
RELEVANCE_WEIGHT = 0.7
RECENCY_HALF_LIFE_YEARS = 7.0
DEFAULT_AGE_YEARS = 10.0

# Share of the relevance component given to the source's own ordering when that
# ordering is meaningful (a single relevance-sorted source, e.g. NCBI Best
# Match). Keeps the trained upstream ranking influential without letting it
# override a clear lexical mismatch.
SOURCE_POSITION_WEIGHT = 0.35


def rank_medical_articles(
    articles: list[MedicalArticle],
    query: str,
    current_year: int | None = None,
    position_weight: float = 0.0,
) -> list[MedicalArticle]:
    """Re-rank articles by query-term overlap, source position, and recency.

    Lexical relevance counts title hits twice as heavily as abstract hits;
    recency uses the same exponential half-life decay as the scholar path.

    ``position_weight`` blends the source ordering into the relevance component
    using the scholar path's ``1/sqrt(rank + 1)`` prior. Pass a non-zero weight
    only when the input list is already relevance-ordered by a single source
    (for example NCBI Best Match). Leave it at 0.0 for merged multi-source
    pools, where list position reflects task order rather than relevance.

    Makes no network calls. Assigns ``article.score`` on the given objects in
    place and returns a new list ordered by that score, source order breaking
    ties.

    Scoring contract: ``final_score = RELEVANCE_WEIGHT * relevance +
    RECENCY_WEIGHT * recency`` (0.7 / 0.3). Relevance is field coverage
    (title terms weighted 2x abstract terms) blended with ``position_weight``
    as described above. Recency uses ``ScoringEngine.calculate_recency_feature``
    with a 7-year half-life and a 10-year default age for missing years.
    """
    if not articles:
        return []

    terms = ScoringEngine.tokenize(query)
    if not terms:
        return list(articles)

    now_year = current_year if current_year is not None else datetime.datetime.now().year
    lexical_weight = 1.0 - position_weight

    scored: list[tuple[float, int, MedicalArticle]] = []
    for idx, article in enumerate(articles):
        lexical = ScoringEngine.text_coverage(terms, article.title, article.abstract)

        if position_weight:
            position = ScoringEngine.calculate_relevance(idx)
            relevance = lexical_weight * lexical + position_weight * position
        else:
            relevance = lexical

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
