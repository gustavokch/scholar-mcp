# Brazilian MoH Retrieval Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Portuguese-aware re-ranking and two-stage query relaxation to the `brazil-moh` guideline search, by generalizing the existing ranking function rather than duplicating it.

**Architecture:** `ScoringEngine.text_coverage` gains an injectable tokenizer. `medical/ranking.py` gains Portuguese normalization plus a private generic `_rank_records`, over which both `rank_medical_articles` (behaviour unchanged) and a new `rank_brazil_guidelines` are thin wrappers. `brazil_moh.py` strips Portuguese stopwords from every outbound query, and `search_guidelines` retries a zero-hit multi-token search with `OR` instead of `AND`, then ranks before slicing.

**Tech Stack:** Python 3.11+, `httpx`, `pytest` with `asyncio_mode = "auto"`, `respx` for HTTP mocking, `ruff` 0.16.6 (no repo config — default rules).

**Spec:** `docs/superpowers/specs/2026-09-09-brazil-moh-retrieval-design.md` (revision 4, approved)

## Global Constraints

- Python interpreter for every command: `/Users/gus/Git/scholar-mcp/.venv/bin/python`. Never run `uv sync`.
- Test runner: `.venv/bin/python -m pytest`. `asyncio_mode = "auto"` — async tests need no decorator.
- `rank_medical_articles` must stay **behaviourally identical**. Its signature, defaults, and scoring contract do not change.
- Scoring contract constants, reused not redefined: `RELEVANCE_WEIGHT = 0.7`, `RECENCY_WEIGHT = 0.3`, `RECENCY_HALF_LIFE_YEARS = 7.0`, `DEFAULT_AGE_YEARS = 10.0`, `SOURCE_POSITION_WEIGHT = 0.35`.
- `PORTUGUESE_STOPWORDS` is stored **accent-folded** and is the single source of truth for both re-ranking and query composition.
- Outbound BVS queries keep their diacritics. Accent folding is client-side only.
- Stopword stripping applies to **both** the `AND` and `OR` query paths, never one.
- Do not add a `mm` parameter, a single-character stopword exception, a positional heuristic, or a designator whitelist. The spec's Evidence sections close these.
- `ruff check` baseline is **15 pre-existing findings** across the files in scope. Introduce no new ones. In particular, keep `src/scholar_mcp/medical/ranking.py` free of `I001` — its import block is currently clean and must stay sorted.
- Every async test that opens a cache or HTTP client closes it in a `finally` block. Omitting this hangs `pytest` at finalize.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/scholar_mcp/ranking.py` | Modify `text_coverage` (line 204-214), add one import | Accept an injectable tokenizer; default behaviour untouched |
| `src/scholar_mcp/medical/ranking.py` | Modify throughout | Portuguese normalization; the shared `_rank_records` generic; both public wrappers |
| `src/scholar_mcp/medical/models.py` | Modify docstring (line 246) | Stop claiming `BrazilGuideline.score` is unset |
| `src/scholar_mcp/medical/brazil_moh.py` | Modify `_usable_tokens` (141), `_build_query` (155), `search_guidelines` (280), module docstring (14-17) | Stopword stripping, operator parameter, two-stage search, rank-then-slice |
| `tests/test_ranking.py` | Extend | `text_coverage` tokenizer injection |
| `tests/medical/test_medical_ranking.py` | Extend | Normalization, `rank_brazil_guidelines`, `rank_medical_articles` regression |
| `tests/medical/test_brazil_moh.py` | Extend + fix one existing test | Stopword stripping, relaxation, caching, rank-before-slice |

Task order is dependency order: Task 1 unblocks Task 3; Task 2 unblocks Tasks 3 and 4; Task 4 unblocks Task 5.

---

## Task 1: Injectable tokenizer on `ScoringEngine.text_coverage`

**Files:**
- Modify: `src/scholar_mcp/ranking.py:204-214` (and one import near line 2)
- Test: `tests/test_ranking.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `ScoringEngine.text_coverage(query_terms: list[str], title: str | None, abstract: str | None, tokenizer: Callable[[str | None], list[str]] | None = None) -> float`. When `tokenizer` is `None` it resolves to `ScoringEngine.tokenize`. Task 3 passes `tokenize_portuguese` here.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ranking.py`:

```python
def test_text_coverage_accepts_injected_tokenizer():
    # A tokenizer that folds "ç" to "c" makes an accented title match a plain query term.
    def folding_tokenizer(text):
        if not text:
            return []
        return [t for t in text.lower().replace("ç", "c").split() if t]

    terms = ["cancer"]
    # Default tokenizer: "câncer" does not fold, so no match.
    assert ScoringEngine.text_coverage(terms, "Câncer de mama", "") == 0.0
    # Injected tokenizer folds the cedilla, so the title matches fully.
    assert ScoringEngine.text_coverage(
        terms, "Cancer de mama", "", tokenizer=folding_tokenizer
    ) == pytest.approx(1.0)


def test_text_coverage_default_tokenizer_unchanged():
    # Regression: omitting `tokenizer` must behave exactly as before.
    terms = ["metformin", "diabetes"]
    assert ScoringEngine.text_coverage(terms, "Metformin for Diabetes", "") == pytest.approx(1.0)
    assert ScoringEngine.text_coverage(terms, "", "Metformin and diabetes outcomes") == pytest.approx(0.5)
    assert ScoringEngine.text_coverage(terms, "Unrelated title", "Unrelated abstract") == 0.0
    assert ScoringEngine.text_coverage([], "Metformin", "Diabetes") == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ranking.py::test_text_coverage_accepts_injected_tokenizer -v`

Expected: FAIL with `TypeError: text_coverage() got an unexpected keyword argument 'tokenizer'`.

- [ ] **Step 3: Add the import**

In `src/scholar_mcp/ranking.py`, insert immediately after `import asyncio` (line 1):

```python
from collections.abc import Callable
```

The import block already carries a baseline `I001`; do not reformat the rest of it.

- [ ] **Step 4: Write the implementation**

Replace `src/scholar_mcp/ranking.py:204-214` with:

```python
    @staticmethod
    def text_coverage(
        query_terms: list[str],
        title: str | None,
        abstract: str | None,
        tokenizer: Callable[[str | None], list[str]] | None = None,
    ) -> float:
        """Fraction of query terms present, title weighted 2x abstract.

        ``tokenizer`` defaults to ``ScoringEngine.tokenize``. Pass an
        alternative to score a corpus this module's English tokenizer would
        mis-handle -- the Brazilian path injects a Portuguese tokenizer that
        folds accents and strips Portuguese stopwords. The same tokenizer must
        have produced ``query_terms``, or the two sides will not compare.
        """
        if not query_terms:
            return 0.0
        tokenize = tokenizer if tokenizer is not None else ScoringEngine.tokenize
        term_count = len(query_terms)
        title_terms = set(tokenize(title))
        abstract_terms = set(tokenize(abstract))
        title_coverage = sum(1 for t in query_terms if t in title_terms) / term_count
        abstract_coverage = sum(1 for t in query_terms if t in abstract_terms) / term_count
        abstract_ratio = _ABSTRACT_WEIGHT / _TITLE_WEIGHT
        return min(1.0, title_coverage + abstract_ratio * abstract_coverage)
```

- [ ] **Step 5: Run the new tests**

Run: `.venv/bin/python -m pytest tests/test_ranking.py -v -k text_coverage`

Expected: PASS, 3 tests (the pre-existing `test_text_coverage_title_weighted_double_abstract` plus the two new ones).

- [ ] **Step 6: Run every caller's tests**

`text_coverage` is called by `calculate_query_relevance`, `score_candidates`, and `rank_medical_articles`. Prove none moved:

Run: `.venv/bin/python -m pytest tests/test_ranking.py tests/medical/test_medical_ranking.py -q`

Expected: PASS, no failures.

- [ ] **Step 7: Check ruff introduced nothing new**

Run: `ruff check src/scholar_mcp/ranking.py --output-format=concise`

Expected: exactly the 11 baseline findings for this file — `I001` at 1:1, `F401` at 17:36, `DTZ005` at 318:66, `S110` at 440 and 449, and `BLE001` at 95, 440, 449, 503, 567, 589, 633. No new codes, and nothing pointing into `text_coverage`.

- [ ] **Step 8: Commit**

```bash
git add src/scholar_mcp/ranking.py tests/test_ranking.py
git commit -m "feat(ranking): allow injecting a tokenizer into text_coverage

Lets a non-English corpus reuse the coverage formula instead of copying
it. Default resolves to ScoringEngine.tokenize, so every existing caller
is unchanged."
```

---

## Task 2: Portuguese normalization and tokenization

**Files:**
- Modify: `src/scholar_mcp/medical/ranking.py` (imports, plus new module-level functions and constants)
- Test: `tests/medical/test_medical_ranking.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces, all importable from `scholar_mcp.medical.ranking`:
  - `normalize_portuguese(text: str | None) -> str` — NFKD accent folding, ASCII, lowercased. `""` for falsy input.
  - `PORTUGUESE_STOPWORDS: frozenset[str]` — accent-folded.
  - `tokenize_portuguese(text: str | None) -> list[str]` — same shape as `ScoringEngine.tokenize`, so it is substitutable as an injected tokenizer.

  Task 3 injects `tokenize_portuguese`. Task 4 imports `normalize_portuguese` and `PORTUGUESE_STOPWORDS`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/medical/test_medical_ranking.py`:

```python
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
```

Extend the import at the top of the file to:

```python
from scholar_mcp.medical.ranking import (
    PORTUGUESE_STOPWORDS,
    normalize_portuguese,
    rank_medical_articles,
    tokenize_portuguese,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/medical/test_medical_ranking.py -q`

Expected: collection error — `ImportError: cannot import name 'PORTUGUESE_STOPWORDS'`.

- [ ] **Step 3: Replace the import block**

Replace lines 1-4 of `src/scholar_mcp/medical/ranking.py` with exactly this. The order is what `ruff check --select I001 --fix` produces, so `I001` stays absent:

```python
import datetime
import re
import unicodedata
from collections.abc import Callable
from typing import Protocol, TypeVar

from scholar_mcp.medical.models import BrazilGuideline, MedicalArticle
from scholar_mcp.ranking import ScoringEngine
```

`BrazilGuideline`, `Callable`, `Protocol`, and `TypeVar` are unused until Task 3. If you are running Task 2 standalone and `ruff` reports `F401` for them, that is expected and clears in Task 3 — do not delete them.

- [ ] **Step 4: Write the implementation**

Insert after the existing `SOURCE_POSITION_WEIGHT` constant block (currently ending at line 16):

```python
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
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/medical/test_medical_ranking.py -q`

Expected: PASS, all tests including the 8 new ones.

- [ ] **Step 6: Check ruff**

Run: `ruff check src/scholar_mcp/medical/ranking.py --output-format=concise`

Expected: the baseline `DTZ005` (now shifted off line 53 by the inserted code), plus `F401` for `BrazilGuideline`, `Callable`, `Protocol`, `TypeVar`. No `I001`. If `I001` appears, the import block in Step 3 was not copied verbatim.

- [ ] **Step 7: Commit**

```bash
git add src/scholar_mcp/medical/ranking.py tests/medical/test_medical_ranking.py
git commit -m "feat(medical): add Portuguese normalization and tokenization

NFKD accent folding plus an accent-folded Portuguese stopword set, shaped
to be substitutable for ScoringEngine.tokenize. The stopword set is shared
with outbound query composition so a term cannot be scored as substantive
while being dropped from the query."
```

---

## Task 3: Shared ranking core and `rank_brazil_guidelines`

**Files:**
- Modify: `src/scholar_mcp/medical/ranking.py` (replace the whole `rank_medical_articles` function; add `_Rankable`, `R`, `_rank_records`, `rank_brazil_guidelines`)
- Modify: `src/scholar_mcp/medical/models.py:246` (docstring line only)
- Test: `tests/medical/test_medical_ranking.py`

**Interfaces:**
- Consumes: `ScoringEngine.text_coverage(..., tokenizer=...)` from Task 1; `tokenize_portuguese` from Task 2.
- Produces: `rank_brazil_guidelines(guidelines: list[BrazilGuideline], query: str, current_year: int | None = None) -> list[BrazilGuideline]`. Assigns `.score` in place and returns a new list, best first. Task 5 calls this.
- Unchanged and relied on elsewhere: `rank_medical_articles(articles, query, current_year=None, position_weight=0.0)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/medical/test_medical_ranking.py`:

```python
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
```

Extend the imports at the top of the file to:

```python
from scholar_mcp.medical.models import BrazilGuideline, MedicalArticle
from scholar_mcp.medical.ranking import (
    PORTUGUESE_STOPWORDS,
    normalize_portuguese,
    rank_brazil_guidelines,
    rank_medical_articles,
    tokenize_portuguese,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/medical/test_medical_ranking.py -q`

Expected: collection error — `ImportError: cannot import name 'rank_brazil_guidelines'`.

- [ ] **Step 3: Replace `rank_medical_articles` with the generic plus two wrappers**

Delete the entire existing `rank_medical_articles` function and put this in its place:

```python
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
```

- [ ] **Step 4: Run the new tests**

Run: `.venv/bin/python -m pytest tests/medical/test_medical_ranking.py -q`

Expected: PASS, all tests. The 10 new `rank_brazil_guidelines` tests and every pre-existing `rank_medical_articles` test.

- [ ] **Step 5: Prove `rank_medical_articles` did not move**

Its callers are the PubMed and multi-database medical paths. Run their suites:

Run: `.venv/bin/python -m pytest tests/medical/ tests/test_ranking.py tests/test_server_medical.py -q`

Expected: PASS, no failures. Any failure here means the extraction changed behaviour — fix the generic, do not adjust the test.

- [ ] **Step 6: Correct the `BrazilGuideline.score` docstring**

In `src/scholar_mcp/medical/models.py`, replace this line inside the `BrazilGuideline` docstring:

```
    ``score`` is reserved for a future ranking pass and is unset in v1.
```

with:

```
    ``score`` is populated by ``rank_brazil_guidelines`` on the search path.
    It stays ``None`` when the query tokenizes to nothing, and on rows cached
    before ranking existed.
```

- [ ] **Step 7: Check ruff**

Run: `ruff check src/scholar_mcp/medical/ranking.py src/scholar_mcp/medical/models.py --output-format=concise`

Expected: only the baseline `DTZ005` for `datetime.datetime.now()` in `ranking.py`. The `F401` findings from Task 2 are now gone, since all four imports are used. No `I001`.

- [ ] **Step 8: Commit**

```bash
git add src/scholar_mcp/medical/ranking.py src/scholar_mcp/medical/models.py tests/medical/test_medical_ranking.py
git commit -m "feat(medical): rank Brazilian guidelines with Portuguese matching

Generalize the ranking function over an injected tokenizer and a
text-field selector, rather than duplicating the 0.7/0.3 scoring
contract in a second copy. rank_medical_articles is unchanged.

The Brazilian wrapper unions the Portuguese and English titles for title
coverage, and the abstract with the DeCS descriptors for secondary
coverage: MoH records often carry no abstract but are richly indexed."
```

---

## Task 4: Stopword stripping and the `operator` parameter

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py` (imports, `_usable_tokens` at 141-152, `_build_query` at 155-167)
- Test: `tests/medical/test_brazil_moh.py` (add tests; **fix one existing test**)

**Interfaces:**
- Consumes: `normalize_portuguese` and `PORTUGUESE_STOPWORDS` from Task 2.
- Produces:
  - `_usable_tokens(query: str) -> list[str]` — same signature, now also drops Portuguese stopwords. Task 5's relaxation gate counts these.
  - `_build_query(query: str, collection: str, operator: str = "AND") -> str` — `operator` is `"AND"` or `"OR"`. Task 5 calls it with both.

**Why this task exists:** measured against the live endpoint, `(da)` alone matches 25,635 records and `(a)` 24,779 — the iAHx analyzer does not strip Portuguese stopwords, so these are live clauses. Adding one to an `OR` group inflates the pool from 2,538 to 25,759 (10.2x) against an over-fetch of at most 150 documents, which makes relaxation return noise. Full figures are in the spec's Evidence sections.

- [ ] **Step 1: Write the failing tests**

Append to `tests/medical/test_brazil_moh.py`, near the other `_build_query` tests:

```python
def test_usable_tokens_strips_portuguese_stopwords():
    from scholar_mcp.medical.brazil_moh import _usable_tokens

    assert _usable_tokens("manejo da dengue") == ["manejo", "dengue"]
    assert _usable_tokens("tratamento de tuberculose para adultos") == [
        "tratamento",
        "tuberculose",
        "adultos",
    ]


def test_usable_tokens_strips_accented_stopword_but_keeps_accented_terms():
    from scholar_mcp.medical.brazil_moh import _usable_tokens

    # "à" folds to the stopword "a" and is dropped. "atenção" is substantive
    # and must survive WITH its diacritics -- BVS handles Portuguese natively,
    # so folding is client-side only.
    assert _usable_tokens("atenção à saúde") == ["atenção", "saúde"]


def test_usable_tokens_keeps_single_char_non_stopwords():
    from scholar_mcp.medical.brazil_moh import _usable_tokens

    # "a" and "e" are Portuguese function words; "b" and "c" are not. The
    # >= 2 length floor from tokenize_portuguese is deliberately NOT applied
    # here: "b" is genuinely selective in this index (hepatite AND b retains
    # 71% of bare hepatite, while hepatite AND a retains 90%).
    assert _usable_tokens("hepatite b") == ["hepatite", "b"]
    assert _usable_tokens("hepatite a") == ["hepatite"]


def test_usable_tokens_all_stopwords_yields_nothing():
    from scholar_mcp.medical.brazil_moh import _usable_tokens

    assert _usable_tokens("sobre a") == []


def test_build_query_strips_stopwords_in_and_mode():
    from scholar_mcp.medical.brazil_moh import _build_query

    built = _build_query("manejo da dengue", "all")
    assert built == 'type:"non-conventional" AND la:"pt" AND (manejo AND dengue)'


def test_build_query_strips_stopwords_in_or_mode():
    from scholar_mcp.medical.brazil_moh import _build_query

    built = _build_query("manejo da dengue", "all", operator="OR")
    assert built == 'type:"non-conventional" AND la:"pt" AND (manejo OR dengue)'


def test_build_query_or_operator_leaves_base_filters_anded():
    from scholar_mcp.medical.brazil_moh import _build_query

    # Only the user-token group relaxes. The filters stay conjunctive, or the
    # query would match non-Portuguese and conventional literature.
    built = _build_query("manejo dengue", "brisa", operator="OR")
    assert built == (
        'type:"non-conventional" AND la:"pt" AND db:"BRISA" AND (manejo OR dengue)'
    )


def test_build_query_defaults_to_and():
    from scholar_mcp.medical.brazil_moh import _build_query

    assert _build_query("manejo dengue", "all") == _build_query(
        "manejo dengue", "all", operator="AND"
    )
```

- [ ] **Step 2: Fix the one existing test the change breaks**

`test_build_query_strips_solr_special_characters` (`tests/medical/test_brazil_moh.py:129`) asserts a token list containing `a` and `e`, both Portuguese stopwords. First observe the failure rather than assuming it:

Run: `.venv/bin/python -m pytest tests/medical/test_brazil_moh.py -q -k build_query`

Expected at this point: the new tests fail (no `operator` parameter, no stripping). `test_build_query_strips_solr_special_characters` still passes, because the implementation has not changed yet. You will re-run it in Step 6.

Change its assertion now, so the expected value is recorded before you implement:

```python
def test_build_query_strips_solr_special_characters():
    from scholar_mcp.medical.brazil_moh import _build_query

    built = _build_query('a "quote" (b) [c] && d || e', "all")
    # "a" and "e" are Portuguese stopwords and are dropped. The single-char
    # "b", "c", "d" are not Portuguese words, so they survive -- confirming
    # the length floor is not applied to outbound tokens.
    assert built == 'type:"non-conventional" AND la:"pt" AND (quote AND b AND c AND d)'
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/medical/test_brazil_moh.py -q -k build_query`

Expected: FAIL. `test_build_query_strips_stopwords_in_or_mode` errors with `TypeError: _build_query() got an unexpected keyword argument 'operator'`, and the stripping assertions fail with `da`/`a`/`e` still present.

- [ ] **Step 4: Add the import**

In `src/scholar_mcp/medical/brazil_moh.py`, add after the existing `from scholar_mcp.medical.models import BrazilGuideline` line:

```python
from scholar_mcp.medical.ranking import PORTUGUESE_STOPWORDS, normalize_portuguese
```

This direction is safe: `medical/ranking.py` imports only `medical.models` and `scholar_mcp.ranking`, so there is no cycle.

- [ ] **Step 5: Write the implementation**

Replace `_usable_tokens` (currently lines 141-152) with:

```python
def _usable_tokens(query: str) -> list[str]:
    """User tokens that survive sanitization, in order.

    A token reduced to nothing by ``_sanitize_token``, reserved as a boolean
    word, or serving only as a Portuguese function word contributes no useful
    matching text and is dropped.

    Portuguese stopwords are stripped because this index does not strip them
    itself: measured against the live endpoint, ``(da)`` alone matches 25,635
    records and ``(a)`` 24,779. Left in, a stopword inflates a relaxed ``OR``
    pool by roughly 10x (2,538 -> 25,759 for one added token) against an
    over-fetch of at most ``MAX_PAGE_SIZE`` documents, so the topical matches
    never get retrieved. In the strict ``AND`` path the effect is narrower --
    the token is near-universal in Portuguese prose, so it usually changes
    nothing -- but it still discards short-title records carrying no abstract,
    which is the class this module most wants to surface.

    Stripping happens here rather than in ``_build_query`` so that both
    operators and the relaxation gate see one identical token list. Stripping
    in only one stage would let the relaxed query match records the strict
    query never could, for a reason unrelated to the operator.

    Membership is tested on the accent-folded form while the original token is
    emitted: ``PORTUGUESE_STOPWORDS`` is stored folded, so the raw token "à"
    would otherwise escape the set, and BVS handles Portuguese diacritics
    natively so there is no reason to strip them from the query.

    The ``len >= 2`` floor used by ``tokenize_portuguese`` is deliberately not
    applied. A single character that is not a Portuguese word is selective
    here -- ``hepatite AND b`` retains 71% of bare ``hepatite`` -- while the
    single-character function words are already covered by the stopword set.
    """
    tokens: list[str] = []
    for token in (query or "").split():
        cleaned = _sanitize_token(token)
        if not cleaned:
            continue
        folded = normalize_portuguese(cleaned)
        if folded in _SOLR_BOOLEAN_WORDS or folded in PORTUGUESE_STOPWORDS:
            continue
        tokens.append(cleaned)
    return tokens
```

Then replace `_build_query` (currently lines 155-167) with:

```python
def _build_query(query: str, collection: str, operator: str = "AND") -> str:
    """Compose every filter into ``q``.

    ``fq`` is silently ignored by this API, and the default operator is OR, so
    the user tokens get an explicit operator inside their own group.

    ``operator`` relaxes only that group. ``BASE_FILTER`` and ``BRISA_FILTER``
    stay conjunctive regardless: an ``OR`` across them would match
    conventional and non-Portuguese literature.
    """
    clauses = [BASE_FILTER]
    if collection == "brisa":
        clauses.append(BRISA_FILTER)
    tokens = _usable_tokens(query)
    if tokens:
        clauses.append("(" + f" {operator} ".join(tokens) + ")")
    return " AND ".join(clauses)
```

Note `_SOLR_BOOLEAN_WORDS` is already lowercase-keyed, and `normalize_portuguese` lowercases, so the boolean-word check keeps working — it previously used `cleaned.lower()`.

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/medical/test_brazil_moh.py -q -k build_query`

Expected: PASS, including the corrected `test_build_query_strips_solr_special_characters`.

- [ ] **Step 7: Run the whole file**

Run: `.venv/bin/python -m pytest tests/medical/test_brazil_moh.py -q`

Expected: PASS. `search_guidelines` has not changed yet, so the search tests still make one HTTP call each.

- [ ] **Step 8: Check ruff**

Run: `ruff check src/scholar_mcp/medical/brazil_moh.py --output-format=concise`

Expected: only the baseline `BLE001` for the blind `except Exception` in `_extract_pdf_text` (its line number will have shifted).

- [ ] **Step 9: Commit**

```bash
git add src/scholar_mcp/medical/brazil_moh.py tests/medical/test_brazil_moh.py
git commit -m "fix(brazil-moh): strip Portuguese stopwords from outbound queries

The iAHx index does not strip them: (da) alone matches 25,635 records.
Left in an OR group, one stopword inflates the candidate pool ~10x
(2,538 -> 25,759) against a 150-document over-fetch, so topical matches
are never retrieved. In the AND path the token is near-universal and
usually inert, but still drops short-title records with no abstract.

Stripping lives in _usable_tokens so both operators and the relaxation
gate see one identical token list. Membership is tested on the
accent-folded form while the original token is sent, since BVS handles
Portuguese diacritics natively.

Also add the operator parameter to _build_query, unused until the
relaxation stage lands."
```

---

## Task 5: Two-stage search, rank-before-slice, docstring

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py` — module docstring (lines 1-18), one import, `search_guidelines` (280-336), plus a new `_fetch_records` helper
- Test: `tests/medical/test_brazil_moh.py`

**Interfaces:**
- Consumes: `_build_query(query, collection, operator=...)` and stopword-stripped `_usable_tokens` from Task 4; `rank_brazil_guidelines` from Task 3.
- Produces: `search_guidelines` returns records ordered by `.score` with `.score` populated, and retries a zero-hit multi-token search with `OR`. Public signature unchanged.
- New private helper: `BrazilMoHEngine._fetch_records(composed: str, count: int) -> tuple[list[BrazilGuideline], bool]` returning `(records, errored)`.

**Existing test that changes behaviour silently:** `test_search_composes_filters_into_q_and_never_fq` searches `"tratamento tuberculose"` (two substantive tokens) against an empty response, so it will now make **two** HTTP calls instead of one. It asserts on `route.calls[0]`, so it still passes. Leave it alone; the explicit relaxation tests below cover the new behaviour.

- [ ] **Step 1: Write the failing tests**

`tests/medical/test_brazil_moh.py` does not import `pytest`. Add it beside the existing `import httpx` (line 250):

```python
import httpx
import pytest
import respx
```

Then append to the same file:

```python
@respx.mock
async def test_search_relaxes_to_or_when_strict_returns_nothing(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            side_effect=[
                httpx.Response(200, json=_bvs_response([])),
                httpx.Response(200, json=_bvs_response([_bvs_doc(title="Manejo da dengue")])),
            ]
        )
        records, meta = await engine.search_guidelines("manejo dengue", limit=5)

        assert route.call_count == 2
        first = str(route.calls[0].request.url)
        second = str(route.calls[1].request.url)
        assert "manejo+AND+dengue" in first or "manejo%20AND%20dengue" in first
        assert "manejo+OR+dengue" in second or "manejo%20OR%20dengue" in second
        assert len(records) == 1
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_relaxed_request_reuses_the_same_count(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            side_effect=[
                httpx.Response(200, json=_bvs_response([])),
                httpx.Response(200, json=_bvs_response([])),
            ]
        )
        await engine.search_guidelines("manejo dengue", limit=5)

        assert route.call_count == 2
        # count=0 returns HTTP 500 from this endpoint, so the relaxed request
        # must reuse the over-fetch count, never a count-only probe.
        assert route.calls[0].request.url.params["count"] == "15"
        assert route.calls[1].request.url.params["count"] == "15"
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_relaxes_when_strict_hits_are_all_non_brazilian(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            side_effect=[
                # Portuguese but published elsewhere: dropped by _is_brazilian,
                # so the record list is empty and relaxation must fire.
                httpx.Response(
                    200,
                    json=_bvs_response([_bvs_doc(country="^iPortugal^ePortugal")]),
                ),
                httpx.Response(200, json=_bvs_response([_bvs_doc(title="Manejo da dengue")])),
            ]
        )
        records, _ = await engine.search_guidelines("manejo dengue", limit=5)

        assert route.call_count == 2
        assert len(records) == 1
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_does_not_relax_for_a_single_token(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        records, meta = await engine.search_guidelines("dengue", limit=5)

        # The strict and relaxed groups would be byte-identical.
        assert route.call_count == 1
        assert records == []
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_does_not_relax_when_stopwords_leave_one_token(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        # Two raw tokens, one substantive. The gate counts substantive tokens.
        await engine.search_guidelines("manejo da", limit=5)

        assert route.call_count == 1
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_does_not_relax_when_strict_finds_a_brazilian_record(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(title="Manejo da dengue")])
            )
        )
        records, _ = await engine.search_guidelines("manejo dengue", limit=5)

        assert route.call_count == 1
        assert len(records) == 1
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_caches_relaxed_result_under_the_strict_key(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            side_effect=[
                httpx.Response(200, json=_bvs_response([])),
                httpx.Response(200, json=_bvs_response([_bvs_doc(title="Manejo da dengue")])),
            ]
        )
        first, _ = await engine.search_guidelines("manejo dengue", limit=5)
        second, meta = await engine.search_guidelines("manejo dengue", limit=5)

        # One user query, one cache row: no third request.
        assert route.call_count == 2
        assert meta.cached is True
        assert [g.record_id for g in second] == [g.record_id for g in first]
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_score_survives_the_cache_round_trip(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(title="Manejo da dengue")])
            )
        )
        fresh, _ = await engine.search_guidelines("manejo dengue", limit=5)
        cached, meta = await engine.search_guidelines("manejo dengue", limit=5)

        assert meta.cached is True
        assert fresh[0].score is not None
        assert cached[0].score == pytest.approx(fresh[0].score)
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_relaxed_request_failure_is_error_and_not_cached(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            side_effect=[
                httpx.Response(200, json=_bvs_response([])),
                httpx.Response(200, text="<html>Estamos em manutenção</html>"),
            ]
        )
        records, meta = await engine.search_guidelines("manejo dengue", limit=5)

        assert records == []
        assert meta.error is True
        composed = 'type:"non-conventional" AND la:"pt" AND (manejo AND dengue)'
        _payload, cache_meta = await cache.get(f"brazil_moh_search:all:5:{composed}")
        assert cache_meta.cached is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_stopword_only_query_makes_no_request(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        # Every token is a Portuguese stopword, so nothing selective remains.
        # Composing filters alone would return arbitrary top-of-index
        # documents dressed as matches for terms never searched.
        records, meta = await engine.search_guidelines("sobre a", limit=5)

        assert records == []
        assert meta.error is False
        assert route.call_count == 0
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_ranks_before_slicing(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        # limit=2 over-fetches 6. The best match sits fourth in BVS order, so
        # it only survives if ranking runs before the slice.
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=_bvs_response([
                    _bvs_doc(record_id="biblio-0", title="Relatorio anual"),
                    _bvs_doc(record_id="biblio-1", title="Nota tecnica"),
                    _bvs_doc(record_id="biblio-2", title="Informe semanal"),
                    _bvs_doc(record_id="biblio-3", title="Manejo clínico da dengue"),
                ]),
            )
        )
        records, _ = await engine.search_guidelines("manejo dengue", limit=2)

        assert len(records) == 2
        assert records[0].record_id == "biblio-3"
        assert all(g.score is not None for g in records)
    finally:
        await cache.close()
        await http_client.aclose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/medical/test_brazil_moh.py -q -k "relax or ranks_before or score_survives"`

Expected: FAIL. The relaxation tests fail on `assert route.call_count == 2` (getting 1), and `test_search_ranks_before_slicing` fails because `biblio-3` was sliced away in BVS order.

- [ ] **Step 3: Add the import**

In `src/scholar_mcp/medical/brazil_moh.py`, extend the Task 4 import to:

```python
from scholar_mcp.medical.ranking import (
    PORTUGUESE_STOPWORDS,
    normalize_portuguese,
    rank_brazil_guidelines,
)
```

- [ ] **Step 4: Extract `_fetch_records`**

Add this method to `BrazilMoHEngine`, immediately before `search_guidelines`:

```python
    async def _fetch_records(
        self,
        composed: str,
        count: int,
    ) -> tuple[list[BrazilGuideline], bool]:
        """One BVS search request, parsed, deduplicated and Brazil-filtered.

        Returns ``(records, errored)``. Extracted so the strict and relaxed
        stages cannot drift apart in how they parse or filter.
        """
        resp = await self.http_client.get(
            BVS_SEARCH_URL,
            headers=BVS_HEADERS,
            params={
                "q": composed,
                "output": "json",
                "count": count,
            },
        )
        if resp is None:
            return [], True

        try:
            data = resp.json()
        except ValueError:
            logger.warning("brazil_moh search returned non-JSON payload")
            return [], True

        records = [_build_record(doc) for doc in _dedupe_by_id(_extract_docs(data))]
        return [record for record in records if _is_brazilian(record)], False
```

- [ ] **Step 5: Rewrite `search_guidelines`**

Replace the body from `clamped = min(...)` through the final `return` with:

```python
        clamped = min(max(1, limit), MAX_RESULTS)

        # A blank query deliberately browses the collection. A query that
        # carries text but sanitizes away to nothing is different: composing
        # filters alone would return arbitrary top-of-index documents dressed
        # as matches for terms that were never searched. Stopword stripping
        # widens this: "sobre a" now lands here rather than being searched.
        tokens = _usable_tokens(query)
        if (query or "").strip() and not tokens:
            logger.info("brazil_moh query %r has no searchable tokens", query)
            return [], CacheMetadata(cached=False, cache_age=0, error=False)

        # Keyed on the composed strict query, not the raw one: "dengue" and
        # "  dengue  " compose identically and must share one cache row. The
        # relaxed query never enters the key -- it derives from the same user
        # query, so one user query keeps one row.
        composed_strict = _build_query(query, norm_collection)
        cache_key = f"brazil_moh_search:{norm_collection}:{clamped}:{composed_strict}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [BrazilGuideline.from_dict(item) for item in cached_data], meta

        count = min(clamped * OVERFETCH_FACTOR, MAX_PAGE_SIZE)
        records, errored = await self._fetch_records(composed_strict, count)
        if errored:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        # The strict conjunction found nothing usable -- either no hits at all,
        # or only records the Brazil assertion dropped. Retry the same tokens
        # ORed. A single substantive token is skipped: the two groups would be
        # byte-identical, so the request would be pure waste.
        if not records and len(tokens) >= 2:
            composed_relaxed = _build_query(query, norm_collection, operator="OR")
            records, errored = await self._fetch_records(composed_relaxed, count)
            if errored:
                return [], CacheMetadata(cached=False, cache_age=0, error=True)

        # Rank, then slice. Slicing first would hand the ranker only `clamped`
        # of the `count` over-fetched candidates and discard the rest in BVS
        # order, defeating the over-fetch.
        records = rank_brazil_guidelines(records, query)[:clamped]

        await self.cache.set(
            cache_key,
            [record.to_dict() for record in records],
            source="brazil_moh",
        )
        return records, CacheMetadata(cached=False, cache_age=0, error=False)
```

- [ ] **Step 6: Run the new tests**

Run: `.venv/bin/python -m pytest tests/medical/test_brazil_moh.py -q -k "relax or ranks_before or score_survives"`

Expected: PASS, all 10 new tests.

- [ ] **Step 7: Run the whole file and confirm nothing regressed**

Run: `.venv/bin/python -m pytest tests/medical/test_brazil_moh.py -q`

Expected: PASS. Two pre-existing tests to watch:
- `test_search_deduplicates_and_trims_to_limit` — every doc shares the title `Protocolo` and the date `202609`, so all scores tie and the stable sort preserves BVS order. It must still pass unchanged. If it fails, the sort is not stable.
- `test_search_still_browses_on_a_blank_query` — a blank query tokenizes to nothing, so `rank_brazil_guidelines` returns the list untouched with `score` still `None`. It must still pass.

- [ ] **Step 8: Rewrite the module docstring**

Replace the whole docstring at the top of `src/scholar_mcp/medical/brazil_moh.py` (lines 1-18) with:

```python
"""Brazilian Ministry of Health technical publications via BVS/iAHx.

Discovery uses the BVS portal search API. Several of its behaviours are
counter-intuitive and are load-bearing for this module:

* ``fq`` is silently ignored, so every filter is composed into ``q``.
* The default boolean operator is OR, so user tokens carry an explicit
  operator inside their own group.
* ``pais_publicacao`` is subfield-encoded and is neither exact-matchable
  nor wildcard-searchable, so Brazil scoping is ``la:"pt"`` server-side
  plus a client-side assertion on the parsed country.
* Records are duplicated across indexing collections at roughly 2.1-2.3x,
  so the engine over-fetches and trims after deduplication.
* The index does not strip Portuguese stopwords, so ``_usable_tokens``
  does. Measured, ``(da)`` alone matches 25,635 records.
* ``count=0`` returns HTTP 500 rather than a count-only response, and the
  host is unreliable enough that the error paths here are live.

Search runs in two stages. The strict stage ANDs the user tokens; if it
yields no Brazilian records and two or more substantive tokens remain,
the same tokens are retried ORed. Relaxation loosens the operator and
nothing else -- both stages compose from one stopword-stripped token
list, so a relaxed hit is never one the strict stage structurally could
not have matched.

Results are then re-ranked by ``rank_brazil_guidelines``. Accent folding
and Portuguese stopword stripping are what made that viable: without
them, blending a generic scorer against a Solr ordering tuned for this
corpus degraded it. The BVS ordering is not discarded but retained as a
weighted position prior, because a relaxed ``OR`` pool is far larger than
the over-fetch window and the server still chooses which slice we see.
"""
```

- [ ] **Step 9: Run the full suite**

Run: `.venv/bin/python -m pytest -q`

Expected: PASS, no failures, no hang at finalize. A hang means a test opened a cache or HTTP client without closing it in `finally`.

- [ ] **Step 10: Check ruff against the baseline**

Run: `ruff check src/ tests/ --output-format=concise | wc -l`

Expected: the same count as before this plan started. To compare precisely:

```bash
ruff check src/scholar_mcp/ranking.py src/scholar_mcp/medical/ranking.py \
  src/scholar_mcp/medical/brazil_moh.py tests/medical/test_medical_ranking.py \
  tests/medical/test_brazil_moh.py --output-format=concise
```

Expected: 15 findings, matching the baseline by rule code — `BLE001` x8, `S110` x2, `DTZ005` x2, `I001` x1, `F401` x1, `RUF059` x1. Line numbers will have shifted. No new codes.

- [ ] **Step 11: Commit**

```bash
git add src/scholar_mcp/medical/brazil_moh.py tests/medical/test_brazil_moh.py
git commit -m "feat(brazil-moh): relax AND to OR on zero hits, rank before slicing

A strict conjunction that yields no Brazilian records retries the same
stopword-stripped tokens ORed, when two or more substantive tokens
remain. One token is skipped: the two groups would be identical.

Results are re-ranked by rank_brazil_guidelines before the slice. The
slice previously ran first, which handed the ranker only `limit` of the
`limit * 3` over-fetched candidates and discarded the rest in BVS order,
defeating the over-fetch.

Extract _fetch_records so the two stages cannot drift apart in how they
parse and filter. Rewrite the module docstring, which documented the
absence of ranking as a deliberate decision."
```

---

## Done When

- [ ] `.venv/bin/python -m pytest -q` passes with no failures and no hang.
- [ ] `ruff check` over the five touched files reports the 15 baseline findings and no new rule codes.
- [ ] `rank_medical_articles` has an unchanged signature, and every pre-existing test of it passes untouched.
- [ ] `git log --oneline` shows five commits, one per task.
