import datetime
import re
import unicodedata
from collections.abc import Callable
from typing import Protocol, TypeVar

from scholar_mcp.medical.models import BrazilGuideline, MedicalArticle
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

# Mirrors the private pattern in scholar_mcp.ranking. Declared locally rather
# than imported: that copy is private to its module, and normalization has
# already reduced the text to ASCII, so the two are intentionally identical.
_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# Stored already accent-folded, because tokenization folds before it consults
# this set -- an accented member would never be matched. Two consumers share
# it: client-side re-ranking here, and outbound query composition in
# medical/brazil_moh.py. One source of truth keeps a term from being scored as
# substantive while being dropped from the query, or the reverse.
PORTUGUESE_STOPWORDS = frozenset({
    "a", "ao", "aos", "as", "com", "como", "da", "das", "de", "do", "dos",
    "e", "em", "entre", "na", "nao", "nas", "no", "nos", "o", "os", "ou",
    "para", "pela", "pelo", "por", "que", "se", "sem", "sob", "sobre",
    "um", "uma", "umas", "uns",
})


def normalize_portuguese(text: str | None) -> str:
    """Fold Portuguese diacritics to ASCII and lowercase.

    NFKD splits an accented character into its base plus a combining mark;
    encoding to ASCII with ``ignore`` then drops the marks. This also discards
    any non-Latin script, which is acceptable: a record whose text is entirely
    non-Latin cannot match a Portuguese query.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return folded.lower()


def tokenize_portuguese(text: str | None) -> list[str]:
    """Accent-folded, stopword-stripped tokens.

    Signature and filtering rules deliberately mirror ``ScoringEngine.tokenize``
    so the two are interchangeable wherever a tokenizer is injected.
    """
    if not text:
        return []
    return [
        t
        for t in _WORD_SPLIT_RE.split(normalize_portuguese(text))
        if len(t) >= 2 and t not in PORTUGUESE_STOPWORDS
    ]


class _Rankable(Protocol):
    """The surface ``_rank_records`` touches on a record.

    Title and abstract are reached through the caller's ``text_fields``
    selector instead of this protocol, because the two record types disagree
    on which fields carry that text.
    """

    year: str
    score: float | None


R = TypeVar("R", bound=_Rankable)


def _rank_records(
    records: list[R],
    query: str,
    *,
    tokenizer: Callable[[str | None], list[str]],
    text_fields: Callable[[R], tuple[str, str]],
    position_weight: float,
    current_year: int | None = None,
) -> list[R]:
    """Score and order records by lexical coverage, source position, and recency.

    Shared by every ranked medical path. ``tokenizer`` must be the same one
    used for both the query and the document text, so the two sides compare.
    ``text_fields`` selects the (title, abstract) text for a record, letting a
    caller widen either side -- the Brazilian path unions the Portuguese and
    English titles, and the abstract with the DeCS descriptors.

    ``position_weight`` blends the source's own ordering into relevance using
    the ``1/sqrt(rank + 1)`` prior. Pass non-zero only when the input is
    already relevance-ordered by a single source. Leave it at 0.0 for merged
    multi-source pools, where list position reflects task order.

    Makes no network calls. Assigns ``score`` on the given objects in place and
    returns a new list ordered by it, source order breaking ties. A query that
    tokenizes to nothing leaves ``score`` untouched.

    Scoring contract: ``RELEVANCE_WEIGHT * relevance + RECENCY_WEIGHT * recency``
    (0.7 / 0.3), with a 7-year recency half-life and a 10-year default age for
    a missing or unparseable year.
    """
    if not records:
        return []

    terms = tokenizer(query)
    if not terms:
        return list(records)

    now_year = current_year if current_year is not None else datetime.datetime.now().year
    lexical_weight = 1.0 - position_weight

    scored: list[tuple[float, int, R]] = []
    for idx, record in enumerate(records):
        title_text, abstract_text = text_fields(record)
        lexical = ScoringEngine.text_coverage(
            terms, title_text, abstract_text, tokenizer=tokenizer
        )

        if position_weight:
            position = ScoringEngine.calculate_relevance(idx)
            relevance = lexical_weight * lexical + position_weight * position
        else:
            relevance = lexical

        recency, _ = ScoringEngine.calculate_recency_feature(
            record.year,
            current_year=now_year,
            half_life_years=RECENCY_HALF_LIFE_YEARS,
            default_age=DEFAULT_AGE_YEARS,
        )

        final_score = RELEVANCE_WEIGHT * relevance + RECENCY_WEIGHT * recency
        record.score = final_score
        scored.append((final_score, idx, record))

    # Stable: equal scores keep source order (idx).
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [record for _, _, record in scored]


def rank_medical_articles(
    articles: list[MedicalArticle],
    query: str,
    current_year: int | None = None,
    position_weight: float = 0.0,
) -> list[MedicalArticle]:
    """Re-rank articles by query-term overlap, source position, and recency.

    Behaviour is unchanged from before ``_rank_records`` was extracted: the
    English tokenizer, the article's own title and abstract, and a default
    ``position_weight`` of 0.0. See ``_rank_records`` for the scoring contract
    and for when a non-zero ``position_weight`` is appropriate.
    """
    return _rank_records(
        articles,
        query,
        tokenizer=ScoringEngine.tokenize,
        text_fields=lambda a: (a.title, a.abstract),
        position_weight=position_weight,
        current_year=current_year,
    )


def rank_brazil_guidelines(
    guidelines: list[BrazilGuideline],
    query: str,
    current_year: int | None = None,
) -> list[BrazilGuideline]:
    """Re-rank Brazilian MoH guidelines with Portuguese-aware matching.

    Three deviations from the article path, each deliberate:

    * The tokenizer folds accents and strips Portuguese stopwords, so an
      unaccented query matches an accented title.
    * Title coverage spans the Portuguese and English titles together. Without
      ``title_en`` an English query scores zero against a Portuguese title.
    * Secondary coverage spans the abstract and the DeCS descriptors together.
      Non-conventional literature -- PCDT, Cadernos de Atencao Basica --
      routinely carries no abstract while being richly indexed, and would
      otherwise score on title alone. The cost is that a heavily-indexed
      record gets a larger secondary token set than a sparse one; it is
      bounded by the coverage cap and outweighed by the recall it buys.

    ``position_weight`` is non-zero because BVS returns a single
    relevance-sorted list, which is the condition that prior is meant for.
    """
    return _rank_records(
        guidelines,
        query,
        tokenizer=tokenize_portuguese,
        text_fields=lambda g: (
            f"{g.title or ''} {g.title_en or ''}",
            " ".join([g.abstract or "", *g.mesh_subjects]),
        ),
        position_weight=SOURCE_POSITION_WEIGHT,
        current_year=current_year,
    )
