# Query-time Reranking Fix + Citation-Aware Answer Checks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the main scholar-search ranker to use real query-text relevance (it currently ignores the query entirely), add evidence-grade / journal-impact / author-authority ranking signals, and add a `check_citations` MCP tool that verifies an answer's claims are actually supported by the papers they cite.

**Architecture:** All new scoring logic lives in `src/scholar_mcp/ranking.py` as pure functions/staticmethods on `ScoringEngine` (no network calls) plus one enrichment extension in `RankingPipeline` (reuses the existing OpenAlex batch call). `medical/ranking.py` is refactored to reuse the new shared lexical primitives but keeps its own weight profile untouched. `check_citations` is a new standalone module reusing the same lexical primitives plus the existing `WaterfallResolver`.

**Tech Stack:** Python 3.11+, pytest/pytest-asyncio/respx (existing dev deps), stdlib only for new code (`re`, `math`, `json`, `functools.lru_cache`, `csv`) — no new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-08-31-rerank-and-citation-check-design.md`

## Global Constraints

- No new runtime dependencies (no embeddings/ML libraries) — lexical scoring only.
- No free-text citation parsing — `check_citations` takes structured `{"text", "identifier"}` claim pairs.
- The 3 new ranking signals (evidence grade, journal impact, author authority) apply only to `src/scholar_mcp/ranking.py` (the `search_papers` path). `src/scholar_mcp/medical/ranking.py` stays lexical-only, no network enrichment.
- Every new/changed numeric feature must degrade to a neutral `0.0` contribution when its data is unavailable — never crash, never penalize.
- Any behavior-preserving refactor (medical ranking, `fetch_citation_counts_batch`) must keep 100% of its existing test suite passing unmodified.

---

## File Structure

New files:
- `src/scholar_mcp/citation_check.py` — `check_citations` claim-verification logic.
- `src/scholar_mcp/data/scimago_sjr.json` — static journal-impact lookup table (ships empty; populated by the update script).
- `src/scholar_mcp/data/SOURCES.md` — provenance/refresh notes for the SJR data.
- `scripts/update_scimago_data.py` — regenerates `scimago_sjr.json` from a manually downloaded Scimago CSV.
- `tests/test_scimago_data.py` — tests for the SJR loader/lookup and the CSV parser.
- `tests/test_citation_check.py` — tests for `check_citations`.

Modified files:
- `src/scholar_mcp/ranking.py` — shared lexical primitives, evidence-grade classification, SJR lookup, new `RankingWeights`/`ScoringMetrics` fields, rewritten `score_candidates`/`rank_papers`.
- `src/scholar_mcp/medical/ranking.py` — reuse shared lexical primitives instead of its own copy.
- `src/scholar_mcp/models.py` — `PaperMetadata` gains `issn`, `study_type`, `evidence_grade`, `last_author_h_index`.
- `src/scholar_mcp/providers/pubmed.py` — capture `pubtype`/`issn` from PubMed ESummary.
- `src/scholar_mcp/providers/openalex.py` — `fetch_work_details_batch`, `fetch_author_h_indices_batch`.
- `src/scholar_mcp/resolver.py` — thread `query` through to `rank_papers`.
- `src/scholar_mcp/config.py` — new ranking `Settings` fields, rebalanced defaults.
- `src/scholar_mcp/server.py` — register `check_citations` MCP tool.
- `tests/test_ranking.py`, `tests/test_openalex_s2.py`, `tests/test_config_models.py` — updated for new signatures/defaults.

---

### Task 1: Shared lexical scoring primitives

**Files:**
- Modify: `src/scholar_mcp/ranking.py`
- Modify: `src/scholar_mcp/medical/ranking.py`
- Test: `tests/test_ranking.py`

**Interfaces:**
- Produces: `ScoringEngine.tokenize(text: str | None) -> list[str]`, `ScoringEngine.text_coverage(query_terms: list[str], title: str | None, abstract: str | None) -> float`, `ScoringEngine.best_matching_sentence(query_terms: list[str], text: str) -> tuple[str, float]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ranking.py` (after the existing imports, which already include `ScoringEngine`):

```python
def test_tokenize_lowercases_and_drops_stopwords_and_short_tokens():
    tokens = ScoringEngine.tokenize("The Effects of Metformin on A1c")
    assert tokens == ["effects", "metformin", "a1c"]


def test_tokenize_handles_none_and_empty():
    assert ScoringEngine.tokenize(None) == []
    assert ScoringEngine.tokenize("") == []


def test_text_coverage_title_weighted_double_abstract():
    terms = ScoringEngine.tokenize("metformin diabetes")
    # Both terms in title -> full coverage
    assert math.isclose(
        ScoringEngine.text_coverage(terms, "Metformin for Diabetes", ""), 1.0
    )
    # Both terms in abstract only -> half weight per term -> 1.0 total (capped)
    assert math.isclose(
        ScoringEngine.text_coverage(terms, "", "Metformin and diabetes outcomes"), 1.0
    )
    # No terms anywhere -> 0
    assert ScoringEngine.text_coverage(terms, "Unrelated title", "Unrelated abstract") == 0.0
    # No query terms -> 0
    assert ScoringEngine.text_coverage([], "Metformin", "Diabetes") == 0.0


def test_best_matching_sentence_picks_highest_overlap():
    terms = ScoringEngine.tokenize("metformin renal outcomes")
    text = (
        "This study examines insulin resistance in cells. "
        "Metformin showed no significant renal outcomes in this cohort. "
        "Patients were followed for five years."
    )
    sentence, score = ScoringEngine.best_matching_sentence(terms, text)
    assert "Metformin showed no significant renal outcomes" in sentence
    assert score > 0.5


def test_best_matching_sentence_empty_inputs():
    assert ScoringEngine.best_matching_sentence([], "Some text.") == ("", 0.0)
    assert ScoringEngine.best_matching_sentence(["metformin"], "") == ("", 0.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ranking.py -k "tokenize or text_coverage or best_matching_sentence" -v`
Expected: FAIL with `AttributeError: type object 'ScoringEngine' has no attribute 'tokenize'`

- [ ] **Step 3: Implement the shared primitives in `ranking.py`**

Add these module-level constants right after the existing imports in `src/scholar_mcp/ranking.py` (before the `RankingWeights` dataclass):

```python
_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_TITLE_WEIGHT = 2.0
_ABSTRACT_WEIGHT = 1.0
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "is",
    "of", "on", "or", "the", "to", "with",
}
```

Add these three staticmethods to `ScoringEngine` (place them right before `calculate_relevance`):

```python
    @staticmethod
    def tokenize(text: str | None) -> list[str]:
        if not text:
            return []
        return [
            t for t in _WORD_SPLIT_RE.split(text.lower())
            if len(t) >= 2 and t not in _STOPWORDS
        ]

    @staticmethod
    def text_coverage(query_terms: list[str], title: str | None, abstract: str | None) -> float:
        if not query_terms:
            return 0.0
        term_count = len(query_terms)
        title_terms = set(ScoringEngine.tokenize(title))
        abstract_terms = set(ScoringEngine.tokenize(abstract))
        title_coverage = sum(1 for t in query_terms if t in title_terms) / term_count
        abstract_coverage = sum(1 for t in query_terms if t in abstract_terms) / term_count
        abstract_ratio = _ABSTRACT_WEIGHT / _TITLE_WEIGHT
        return min(1.0, title_coverage + abstract_ratio * abstract_coverage)

    @staticmethod
    def best_matching_sentence(query_terms: list[str], text: str) -> tuple[str, float]:
        if not text or not query_terms:
            return "", 0.0
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        if not sentences:
            return "", 0.0
        term_count = len(query_terms)
        best_sentence = ""
        best_score = 0.0
        for sentence in sentences:
            sentence_terms = set(ScoringEngine.tokenize(sentence))
            score = sum(1 for t in query_terms if t in sentence_terms) / term_count
            if score > best_score:
                best_score = score
                best_sentence = sentence
        return best_sentence, best_score
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ranking.py -k "tokenize or text_coverage or best_matching_sentence" -v`
Expected: PASS

- [ ] **Step 5: Refactor `medical/ranking.py` to reuse the shared primitives**

In `src/scholar_mcp/medical/ranking.py`, replace the private tokenizer/constants and the inline coverage formula with calls to the shared functions, keeping every other constant (`RECENCY_WEIGHT`, `RELEVANCE_WEIGHT`, `RECENCY_HALF_LIFE_YEARS`, `DEFAULT_AGE_YEARS`, `SOURCE_POSITION_WEIGHT`) unchanged.

Replace:

```python
import datetime
import re

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
```

with:

```python
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
```

Then inside `rank_medical_articles`, replace:

```python
    terms = _tokenize(query)
    if not terms:
        return list(articles)

    now_year = current_year if current_year is not None else datetime.datetime.now().year
    # Relevance is expressed as field coverage rather than a raw weighted hit
    # count. Dividing by (title + abstract) weights would assume every term
    # appears in both fields, capping a title-only match at 2/3 and an
    # abstract-only match at 1/3 -- low enough that the 0..1 recency term could
    # outrank genuine lexical evidence.
    _ABSTRACT_RATIO = _ABSTRACT_WEIGHT / _TITLE_WEIGHT
    term_count = len(terms)
    lexical_weight = 1.0 - position_weight

    scored: list[tuple[float, int, MedicalArticle]] = []
    for idx, article in enumerate(articles):
        title_terms = set(_tokenize(article.title))
        abstract_terms = set(_tokenize(article.abstract))

        title_coverage = sum(1 for t in terms if t in title_terms) / term_count
        abstract_coverage = sum(1 for t in terms if t in abstract_terms) / term_count
        lexical = min(1.0, title_coverage + _ABSTRACT_RATIO * abstract_coverage)

        if position_weight:
```

with:

```python
    terms = ScoringEngine.tokenize(query)
    if not terms:
        return list(articles)

    now_year = current_year if current_year is not None else datetime.datetime.now().year
    lexical_weight = 1.0 - position_weight

    scored: list[tuple[float, int, MedicalArticle]] = []
    for idx, article in enumerate(articles):
        lexical = ScoringEngine.text_coverage(terms, article.title, article.abstract)

        if position_weight:
```

(The rest of the function — the `position`/`relevance`/`recency`/`final_score` block and the final sort — is unchanged.)

- [ ] **Step 6: Run the full medical ranking suite to confirm behavior is preserved**

Run: `pytest tests/medical/test_medical_ranking.py -v`
Expected: PASS, all existing assertions unchanged (this is a behavior-preserving refactor).

- [ ] **Step 7: Commit**

```bash
git add src/scholar_mcp/ranking.py src/scholar_mcp/medical/ranking.py tests/test_ranking.py
git commit -m "refactor: extract shared lexical scoring primitives into ScoringEngine"
```

---

### Task 2: `PaperMetadata` new fields

**Files:**
- Modify: `src/scholar_mcp/models.py:33-50`

**Interfaces:**
- Produces: `PaperMetadata.issn: str | None`, `PaperMetadata.study_type: str | None`, `PaperMetadata.evidence_grade: str | None`, `PaperMetadata.last_author_h_index: int | None`.

- [ ] **Step 1: Add the fields**

In `src/scholar_mcp/models.py`, in the `PaperMetadata` dataclass, replace:

```python
    institutions: list[str] = field(default_factory=list)
    score: float | None = None
    ranking_metrics: dict[str, Any] | None = None
```

with:

```python
    institutions: list[str] = field(default_factory=list)
    issn: str | None = None
    study_type: str | None = None
    evidence_grade: str | None = None
    last_author_h_index: int | None = None
    score: float | None = None
    ranking_metrics: dict[str, Any] | None = None
```

- [ ] **Step 2: Run the existing model tests to confirm nothing broke**

Run: `pytest tests/test_config_models.py -v`
Expected: PASS (new fields default to `None`, `to_dict()`/`asdict()` picks them up automatically, no existing assertion checks the exact key set).

- [ ] **Step 3: Commit**

```bash
git add src/scholar_mcp/models.py
git commit -m "feat: add issn, study_type, evidence_grade, last_author_h_index to PaperMetadata"
```

---

### Task 3: Evidence-grade classification

**Files:**
- Modify: `src/scholar_mcp/ranking.py`
- Test: `tests/test_ranking.py`

**Interfaces:**
- Consumes: nothing new (pure function).
- Produces: `classify_evidence_grade(pubtypes: list[str] | None) -> str | None` (module-level), `ScoringEngine.calculate_evidence_feature(evidence_grade: str | None) -> float`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ranking.py`:

```python
from scholar_mcp.ranking import classify_evidence_grade


def test_classify_evidence_grade_meta_analysis_and_systematic_review():
    assert classify_evidence_grade(["Meta-Analysis"]) == "1a"
    assert classify_evidence_grade(["Journal Article", "Systematic Review"]) == "1a"


def test_classify_evidence_grade_picks_best_of_multiple():
    # RCT (1b) outranks Multicenter Study (2b) when both are present
    assert classify_evidence_grade(["Multicenter Study", "Randomized Controlled Trial"]) == "1b"


def test_classify_evidence_grade_lower_tiers():
    assert classify_evidence_grade(["Case-Control Studies"]) == "3b"
    assert classify_evidence_grade(["Case Reports"]) == "4"
    assert classify_evidence_grade(["Review"]) == "5"


def test_classify_evidence_grade_none_when_unrecognized_or_empty():
    assert classify_evidence_grade(["Journal Article"]) is None
    assert classify_evidence_grade([]) is None
    assert classify_evidence_grade(None) is None


def test_calculate_evidence_feature():
    assert ScoringEngine.calculate_evidence_feature("1a") == 1.0
    assert math.isclose(ScoringEngine.calculate_evidence_feature("1b"), 1.0 / 2)
    assert math.isclose(ScoringEngine.calculate_evidence_feature("2b"), 1.0 / 3)
    assert ScoringEngine.calculate_evidence_feature(None) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ranking.py -k "evidence_grade or evidence_feature" -v`
Expected: FAIL with `ImportError: cannot import name 'classify_evidence_grade'`

- [ ] **Step 3: Implement in `ranking.py`**

Add these module-level structures right after the `_STOPWORDS` constant added in Task 1:

```python
PUBTYPE_TO_GRADE: dict[str, str] = {
    "meta-analysis": "1a",
    "systematic review": "1a",
    "randomized controlled trial": "1b",
    "observational study": "2b",
    "comparative study": "2b",
    "multicenter study": "2b",
    "case-control studies": "3b",
    "case reports": "4",
    "review": "5",
    "editorial": "5",
    "comment": "5",
    "practice guideline": "5",
}

EVIDENCE_GRADE_RANK: dict[str, int] = {
    "1a": 1,
    "1b": 2,
    "2b": 3,
    "3b": 4,
    "4": 5,
    "5": 6,
}


def classify_evidence_grade(pubtypes: list[str] | None) -> str | None:
    """Map raw PubMed PublicationType strings to an Oxford CEBM-style grade.

    Picks the single best (lowest-rank) grade when a paper carries multiple
    publication types (e.g. both "Multicenter Study" and "Randomized
    Controlled Trial" -> the RCT grade wins).
    """
    if not pubtypes:
        return None
    best_grade: str | None = None
    best_rank: int | None = None
    for pt in pubtypes:
        grade = PUBTYPE_TO_GRADE.get(pt.strip().lower())
        if grade is None:
            continue
        rank = EVIDENCE_GRADE_RANK[grade]
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_grade = grade
    return best_grade
```

Add this staticmethod to `ScoringEngine` (near `calculate_citation_feature`):

```python
    @staticmethod
    def calculate_evidence_feature(evidence_grade: str | None) -> float:
        if evidence_grade is None:
            return 0.0
        rank = EVIDENCE_GRADE_RANK.get(evidence_grade)
        if rank is None:
            return 0.0
        return 1.0 / rank
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ranking.py -k "evidence_grade or evidence_feature" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/ranking.py tests/test_ranking.py
git commit -m "feat: classify PubMed publication types into an evidence-grade ladder"
```

---

### Task 4: Wire evidence grade + ISSN into PubMed search results

**Files:**
- Modify: `src/scholar_mcp/providers/pubmed.py:42-133` (the `search` method)
- Test: `tests/test_search_scihub_providers.py`

**Interfaces:**
- Consumes: `classify_evidence_grade` from Task 3.
- Produces: `PubMedProvider.search(...)` results now populate `issn`, `study_type`, `evidence_grade`.

`tests/test_search_scihub_providers.py` already defines module-level `ESEARCH`/`ESUMMARY` URL constants, a `client` pytest fixture (`AsyncHttpClient`), and imports `httpx`/`respx`/`PubMedProvider`/`Settings` at the top — reuse all of that, don't redefine.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_search_scihub_providers.py`, after `test_pubmed_search_sort_date`:

```python
@respx.mock
async def test_pubmed_search_captures_pubtype_and_issn(client):
    respx.get(url__startswith=ESEARCH).mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["111"]}})
    )
    respx.get(url__startswith=ESUMMARY).mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "uids": ["111"],
                    "111": {
                        "title": "A Randomized Trial of X.",
                        "authors": [{"name": "Doe J"}],
                        "pubdate": "2024",
                        "fulljournalname": "New England Journal of Medicine",
                        "pubtype": ["Journal Article", "Randomized Controlled Trial"],
                        "issn": "0028-4793",
                        "essn": "1533-4406",
                    },
                }
            },
        )
    )

    results = await PubMedProvider(client, Settings()).search("x trial", num_results=5)

    assert len(results) == 1
    assert results[0].study_type == "Journal Article; Randomized Controlled Trial"
    assert results[0].evidence_grade == "1b"
    assert results[0].issn == "0028-4793"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_search_scihub_providers.py -k pubtype_and_issn -v`
Expected: FAIL — `assert None == "Journal Article; Randomized Controlled Trial"`

- [ ] **Step 3: Implement in `providers/pubmed.py`**

Add the import at the top of `src/scholar_mcp/providers/pubmed.py`:

```python
from scholar_mcp.ranking import classify_evidence_grade
```

Inside `PubMedProvider.search`, right before the `papers.append(...)` call, add:

```python
                pubtypes = rec.get("pubtype", [])
                if not isinstance(pubtypes, list):
                    pubtypes = []
                study_type = "; ".join(pubtypes) if pubtypes else None
                evidence_grade = classify_evidence_grade(pubtypes)

                issn = rec.get("issn") or rec.get("essn") or None
```

Then update the `PaperMetadata(...)` construction in that same method to:

```python
                papers.append(
                    PaperMetadata(
                        title=title,
                        authors=authors,
                        year=year,
                        venue=venue,
                        doi=doi,
                        pmid=str(uid),
                        abstract="",
                        oa_status="unknown",
                        issn=issn,
                        study_type=study_type,
                        evidence_grade=evidence_grade,
                    )
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_search_scihub_providers.py -k pubtype_and_issn -v`
Expected: PASS

- [ ] **Step 5: Run the full PubMed provider test file to confirm no regressions**

Run: `pytest tests/test_search_scihub_providers.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/providers/pubmed.py tests/test_search_scihub_providers.py
git commit -m "feat: capture PubMed publication type and ISSN for evidence-grade ranking"
```

---

### Task 5: Journal impact (static Scimago SJR dataset)

**Files:**
- Create: `src/scholar_mcp/data/scimago_sjr.json`
- Create: `src/scholar_mcp/data/SOURCES.md`
- Create: `scripts/update_scimago_data.py`
- Modify: `src/scholar_mcp/ranking.py`
- Test: `tests/test_scimago_data.py`

**Interfaces:**
- Produces: `lookup_journal_impact(issn: str | None, venue: str | None) -> float | None` (module-level, `ranking.py`), `ScoringEngine.calculate_impact_feature(sjr: float | None) -> float`, `parse_scimago_csv(rows: list[dict[str, str]]) -> dict[str, dict[str, float]]` (module-level, `ranking.py`, reused by the update script).

**Note on data honesty:** the shipped `scimago_sjr.json` ships **empty** (`{"issn": {}, "name": {}}`). Real Scimago values are not fabricated or hand-typed into the repo — they require a manual one-time step (documented in `SOURCES.md`) where a maintainer downloads the real CSV and runs the update script. Until that's done, `journal_impact` contributes `0.0` (neutral) for every paper, which is safe and correct, not broken.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scimago_data.py`:

```python
import json

import pytest

from scholar_mcp import ranking
from scholar_mcp.ranking import (
    ScoringEngine,
    _normalize_issn,
    _normalize_journal_name,
    lookup_journal_impact,
    parse_scimago_csv,
)


@pytest.fixture(autouse=True)
def _clear_scimago_cache():
    ranking._load_scimago_table.cache_clear()
    yield
    ranking._load_scimago_table.cache_clear()


def test_normalize_issn_strips_dashes_and_uppercases():
    assert _normalize_issn("0028-0836") == "00280836"
    assert _normalize_issn("1932-6203") == "19326203"


def test_normalize_journal_name_lowercases_and_strips_punctuation():
    assert _normalize_journal_name("The New England Journal of Medicine!") == "the new england journal of medicine"


def test_lookup_journal_impact_by_issn(tmp_path, monkeypatch):
    data_file = tmp_path / "scimago_sjr.json"
    data_file.write_text(json.dumps({"issn": {"00280836": 18.5}, "name": {}}))
    monkeypatch.setattr(ranking, "_SCIMAGO_DATA_PATH", data_file)
    ranking._load_scimago_table.cache_clear()

    assert lookup_journal_impact("0028-0836", "Nature") == 18.5


def test_lookup_journal_impact_falls_back_to_name(tmp_path, monkeypatch):
    data_file = tmp_path / "scimago_sjr.json"
    data_file.write_text(json.dumps({"issn": {}, "name": {"nature": 18.5}}))
    monkeypatch.setattr(ranking, "_SCIMAGO_DATA_PATH", data_file)
    ranking._load_scimago_table.cache_clear()

    assert lookup_journal_impact(None, "Nature") == 18.5
    assert lookup_journal_impact("9999-9999", "Nature") == 18.5


def test_lookup_journal_impact_miss_returns_none(tmp_path, monkeypatch):
    data_file = tmp_path / "scimago_sjr.json"
    data_file.write_text(json.dumps({"issn": {}, "name": {}}))
    monkeypatch.setattr(ranking, "_SCIMAGO_DATA_PATH", data_file)
    ranking._load_scimago_table.cache_clear()

    assert lookup_journal_impact("0000-0000", "Unknown Journal") is None


def test_lookup_journal_impact_missing_file_returns_empty_table(tmp_path, monkeypatch):
    monkeypatch.setattr(ranking, "_SCIMAGO_DATA_PATH", tmp_path / "does_not_exist.json")
    ranking._load_scimago_table.cache_clear()

    assert lookup_journal_impact("0028-0836", "Nature") is None


def test_calculate_impact_feature():
    assert ScoringEngine.calculate_impact_feature(None) == 0.0
    assert ScoringEngine.calculate_impact_feature(0) == 0.0
    import math
    assert math.isclose(ScoringEngine.calculate_impact_feature(9.0), math.log(10.0))


def test_parse_scimago_csv_basic():
    rows = [
        {"Title": "Nature", "Issn": "00280836, 14764687", "SJR": "18,543"},
        {"Title": "PLOS ONE", "Issn": "19326203", "SJR": "0,821"},
        {"Title": "Bad Row", "Issn": "12345678", "SJR": "not-a-number"},
        {"Title": "", "Issn": "11112222", "SJR": "5,0"},
    ]
    table = parse_scimago_csv(rows)
    assert table["issn"]["00280836"] == 18.543
    assert table["issn"]["14764687"] == 18.543
    assert table["name"]["nature"] == 18.543
    assert table["issn"]["19326203"] == 0.821
    assert "12345678" not in table["issn"]
    assert "11112222" not in table["issn"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scimago_data.py -v`
Expected: FAIL with `ImportError: cannot import name 'lookup_journal_impact'`

- [ ] **Step 3: Implement the loader/lookup/parser in `ranking.py`**

Add these imports to the top of `src/scholar_mcp/ranking.py`:

```python
from functools import lru_cache
import json
from pathlib import Path
```

Add this near the `PUBTYPE_TO_GRADE` block added in Task 3:

```python
_SCIMAGO_DATA_PATH = Path(__file__).parent / "data" / "scimago_sjr.json"


def _normalize_issn(issn: str) -> str:
    return re.sub(r"[^0-9Xx]", "", issn).upper()


def _normalize_journal_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


@lru_cache(maxsize=1)
def _load_scimago_table() -> dict[str, dict[str, float]]:
    try:
        with open(_SCIMAGO_DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "issn": {k: float(v) for k, v in data.get("issn", {}).items()},
            "name": {k: float(v) for k, v in data.get("name", {}).items()},
        }
    except Exception:
        return {"issn": {}, "name": {}}


def lookup_journal_impact(issn: str | None, venue: str | None) -> float | None:
    table = _load_scimago_table()
    if issn:
        value = table["issn"].get(_normalize_issn(issn))
        if value is not None:
            return value
    if venue:
        value = table["name"].get(_normalize_journal_name(venue))
        if value is not None:
            return value
    return None


def parse_scimago_csv(rows: list[dict[str, str]]) -> dict[str, dict[str, float]]:
    """Parse Scimago Journal Rank CSV export rows (dict-per-row, semicolon-delimited
    source) into the {"issn": {...}, "name": {...}} table format used by
    scimago_sjr.json. Shared with scripts/update_scimago_data.py."""
    issn_table: dict[str, float] = {}
    name_table: dict[str, float] = {}

    for row in rows:
        title = (row.get("Title") or "").strip()
        sjr_raw = (row.get("SJR") or "").strip()
        issn_raw = (row.get("Issn") or "").strip()
        if not title or not sjr_raw:
            continue

        try:
            sjr_value = float(sjr_raw.replace(",", "."))
        except ValueError:
            continue

        name_table[_normalize_journal_name(title)] = sjr_value

        for issn in issn_raw.split(","):
            issn = issn.strip()
            if issn:
                issn_table[_normalize_issn(issn)] = sjr_value

    return {"issn": issn_table, "name": name_table}
```

Add this staticmethod to `ScoringEngine`:

```python
    @staticmethod
    def calculate_impact_feature(sjr: float | None) -> float:
        if sjr is None or sjr <= 0:
            return 0.0
        return math.log(1.0 + sjr)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scimago_data.py -v`
Expected: PASS

- [ ] **Step 5: Ship the empty seed data file**

Create `src/scholar_mcp/data/scimago_sjr.json`:

```json
{
  "issn": {},
  "name": {}
}
```

- [ ] **Step 6: Document provenance and the refresh procedure**

Create `src/scholar_mcp/data/SOURCES.md`:

```markdown
# scimago_sjr.json

Journal-impact proxy data for `journal_impact` ranking signal (no free
official Journal Impact Factor API exists, this stands in for it).

**Ships empty.** `scimago_sjr.json` is checked in with empty `issn`/`name`
tables. No SJR values are hand-typed or fabricated into this repo — the
`journal_impact` ranking feature contributes a neutral `0.0` for every
paper until this file is populated from real data.

## Populating it

1. Go to https://www.scimagojr.com/journalrank.php
2. Select "All subject areas", "All regions", the latest year, output
   format CSV, and download it.
3. Save it to `data/raw/scimago_journal_rank.csv` (create the `data/raw/`
   directory; it's gitignored).
4. Run: `python scripts/update_scimago_data.py`
5. This regenerates `src/scholar_mcp/data/scimago_sjr.json`.

Before committing a regenerated file, check Scimago's current terms of
use for redistribution of derived data on their site.

## Format

```json
{
  "issn": {"<issn-digits-no-dashes>": <sjr float>, ...},
  "name": {"<lowercased-punctuation-stripped-journal-name>": <sjr float>, ...}
}
```

Lookup tries ISSN first, falls back to normalized journal name, then `None`
(neutral) if neither matches.
```

- [ ] **Step 7: Write the update script**

Create `scripts/update_scimago_data.py`:

```python
#!/usr/bin/env python3
"""Regenerate src/scholar_mcp/data/scimago_sjr.json from a Scimago Journal
Rank CSV export.

Download the CSV from https://www.scimagojr.com/journalrank.php (select
"All subject areas", "All regions", the latest year, output format CSV)
and save it to data/raw/scimago_journal_rank.csv before running this
script. Consult Scimago's site for current data usage terms before
committing a regenerated scimago_sjr.json.
"""
import csv
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scholar_mcp.ranking import parse_scimago_csv  # noqa: E402

DEFAULT_INPUT = Path("data/raw/scimago_journal_rank.csv")
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "src" / "scholar_mcp" / "data" / "scimago_sjr.json"
)


def main(input_path: Path = DEFAULT_INPUT, output_path: Path = DEFAULT_OUTPUT) -> None:
    if not input_path.exists():
        print(f"Input CSV not found: {input_path}", file=sys.stderr)
        print(
            "Download it from https://www.scimagojr.com/journalrank.php first.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(input_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        rows = list(reader)

    table = parse_scimago_csv(rows)
    output_path.write_text(json.dumps(table, indent=2, sort_keys=True), encoding="utf-8")
    print(
        f"Wrote {len(table['issn'])} ISSN entries and "
        f"{len(table['name'])} name entries to {output_path}"
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 8: Confirm the shipped empty dataset behaves neutrally**

Run: `python -c "from scholar_mcp.ranking import lookup_journal_impact; print(lookup_journal_impact('0028-0836', 'Nature'))"`
Expected: `None`

- [ ] **Step 9: Commit**

```bash
git add src/scholar_mcp/ranking.py src/scholar_mcp/data/scimago_sjr.json src/scholar_mcp/data/SOURCES.md scripts/update_scimago_data.py tests/test_scimago_data.py
git commit -m "feat: add journal-impact (Scimago SJR) lookup with empty seed dataset"
```

---

### Task 6: Author authority (last-author h-index via OpenAlex)

**Files:**
- Modify: `src/scholar_mcp/providers/openalex.py`
- Modify: `src/scholar_mcp/ranking.py` (`RankingPipeline.enrich_citations`)
- Test: `tests/test_openalex_s2.py`
- Test: `tests/test_ranking.py`

**Interfaces:**
- Consumes: `PaperMetadata.last_author_h_index` field from Task 2.
- Produces: `OpenAlexProvider.fetch_work_details_batch(dois, pmids) -> dict[str, dict[str, Any]]` (each value: `{"citation_count": int, "last_author_id": str | None}`), `OpenAlexProvider.fetch_author_h_indices_batch(author_ids: list[str]) -> dict[str, int]`, `ScoringEngine.calculate_authority_feature(h_index: int | None) -> float`.

- [ ] **Step 1: Write the failing OpenAlex provider tests**

Add to `tests/test_openalex_s2.py`:

```python
@respx.mock
async def test_openalex_fetch_work_details_batch_extracts_last_author(client):
    respx.get(url__startswith=f"{OPENALEX_BASE}/works?").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "doi": "https://doi.org/10.1038/s41586-020-2649-2",
                        "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/32814902"},
                        "cited_by_count": 1250,
                        "authorships": [
                            {"author": {"id": "https://openalex.org/A1111"}},
                            {"author": {"id": "https://openalex.org/A2222"}},
                        ],
                    }
                ]
            },
        )
    )

    provider = OpenAlexProvider(client, email="test@example.com")
    details = await provider.fetch_work_details_batch(
        dois=["10.1038/s41586-020-2649-2"], pmids=["32814902"]
    )

    assert details["10.1038/s41586-020-2649-2"]["citation_count"] == 1250
    assert details["10.1038/s41586-020-2649-2"]["last_author_id"] == "A2222"
    assert details["32814902"]["last_author_id"] == "A2222"


@respx.mock
async def test_openalex_fetch_work_details_batch_no_authorships(client):
    respx.get(url__startswith=f"{OPENALEX_BASE}/works?").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "doi": "https://doi.org/10.1038/only-doi",
                        "cited_by_count": 88,
                    }
                ]
            },
        )
    )

    provider = OpenAlexProvider(client, email="test@example.com")
    details = await provider.fetch_work_details_batch(dois=["10.1038/only-doi"])

    assert details["10.1038/only-doi"]["citation_count"] == 88
    assert details["10.1038/only-doi"]["last_author_id"] is None


@respx.mock
async def test_openalex_fetch_author_h_indices_batch(client):
    respx.get(url__startswith=f"{OPENALEX_BASE}/authors?").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "https://openalex.org/A1111", "summary_stats": {"h_index": 42}},
                    {"id": "https://openalex.org/A2222", "summary_stats": {"h_index": 7}},
                ]
            },
        )
    )

    provider = OpenAlexProvider(client, email="test@example.com")
    h_indices = await provider.fetch_author_h_indices_batch(["A1111", "A2222"])

    assert h_indices == {"A1111": 42, "A2222": 7}


async def test_openalex_fetch_author_h_indices_batch_empty_input(client):
    provider = OpenAlexProvider(client, email="test@example.com")
    assert await provider.fetch_author_h_indices_batch([]) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_openalex_s2.py -k "work_details or h_indices" -v`
Expected: FAIL with `AttributeError: 'OpenAlexProvider' object has no attribute 'fetch_work_details_batch'`

- [ ] **Step 3: Implement in `providers/openalex.py`**

Replace the existing `_fetch_citation_counts_single_filter` method and everything below it (through the end of `fetch_citation_counts_batch`) with:

```python
    async def _fetch_works_raw(self, filter_str: str) -> list[dict[str, Any]]:
        params = self._params({"filter": filter_str, "per-page": 50})
        try:
            resp = await self.http_client.get(f"{OPENALEX_BASE}/works", params=params)
            if resp is None or resp.status_code != 200:
                return []
            data = resp.json()
            results = data.get("results", [])
            return [w for w in results if isinstance(w, dict)]
        except Exception:
            return []

    async def _fetch_citation_counts_single_filter(
        self,
        filter_str: str,
    ) -> dict[str, int]:
        works = await self._fetch_works_raw(filter_str)
        counts: dict[str, int] = {}
        for work in works:
            c = work.get("cited_by_count")
            if c is None or not isinstance(c, int):
                continue

            w_doi = _strip_doi_url(work.get("doi"))
            if w_doi:
                counts[w_doi.lower()] = c

            ids_dict = work.get("ids", {})
            if isinstance(ids_dict, dict):
                raw_pmid = ids_dict.get("pmid")
                if raw_pmid:
                    pmid_val = str(raw_pmid).split("/")[-1].strip()
                    if pmid_val:
                        counts[pmid_val] = c
                raw_doi = ids_dict.get("doi")
                if raw_doi:
                    d_val = _strip_doi_url(str(raw_doi))
                    if d_val:
                        counts[d_val.lower()] = c

        return counts

    async def fetch_citation_counts_batch(
        self,
        dois: list[str] | None = None,
        pmids: list[str] | None = None,
    ) -> dict[str, int]:
        """Fetch citation counts for multiple DOIs and PMIDs via batched OpenAlex query."""
        clean_dois = [
            _strip_doi_url(d) or d.strip()
            for d in (dois or [])
            if d and (_strip_doi_url(d) or d.strip())
        ]
        clean_pmids = [p.strip() for p in (pmids or []) if p and p.strip()]

        if not clean_dois and not clean_pmids:
            return {}

        tasks = []
        if clean_dois:
            filter_str = f"doi:{'|'.join(clean_dois[:50])}"
            tasks.append(self._fetch_citation_counts_single_filter(filter_str))
        if clean_pmids:
            filter_str = f"pmid:{'|'.join(clean_pmids[:50])}"
            tasks.append(self._fetch_citation_counts_single_filter(filter_str))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        combined: dict[str, int] = {}
        for res in results:
            if isinstance(res, dict):
                combined.update(res)
        return combined

    async def _fetch_work_details_single_filter(
        self,
        filter_str: str,
    ) -> dict[str, dict[str, Any]]:
        works = await self._fetch_works_raw(filter_str)
        details: dict[str, dict[str, Any]] = {}
        for work in works:
            c = work.get("cited_by_count")
            if c is None or not isinstance(c, int):
                continue

            last_author_id = None
            authorships = work.get("authorships")
            if isinstance(authorships, list) and authorships:
                last = authorships[-1]
                if isinstance(last, dict):
                    author_obj = last.get("author")
                    if isinstance(author_obj, dict):
                        raw_id = author_obj.get("id")
                        if raw_id:
                            last_author_id = str(raw_id).rsplit("/", 1)[-1]

            entry = {"citation_count": c, "last_author_id": last_author_id}

            w_doi = _strip_doi_url(work.get("doi"))
            if w_doi:
                details[w_doi.lower()] = entry

            ids_dict = work.get("ids", {})
            if isinstance(ids_dict, dict):
                raw_pmid = ids_dict.get("pmid")
                if raw_pmid:
                    pmid_val = str(raw_pmid).split("/")[-1].strip()
                    if pmid_val:
                        details[pmid_val] = entry
                raw_doi = ids_dict.get("doi")
                if raw_doi:
                    d_val = _strip_doi_url(str(raw_doi))
                    if d_val:
                        details[d_val.lower()] = entry

        return details

    async def fetch_work_details_batch(
        self,
        dois: list[str] | None = None,
        pmids: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Fetch citation count and last-author OpenAlex ID for multiple DOIs/PMIDs."""
        clean_dois = [
            _strip_doi_url(d) or d.strip()
            for d in (dois or [])
            if d and (_strip_doi_url(d) or d.strip())
        ]
        clean_pmids = [p.strip() for p in (pmids or []) if p and p.strip()]

        if not clean_dois and not clean_pmids:
            return {}

        tasks = []
        if clean_dois:
            tasks.append(self._fetch_work_details_single_filter(f"doi:{'|'.join(clean_dois[:50])}"))
        if clean_pmids:
            tasks.append(self._fetch_work_details_single_filter(f"pmid:{'|'.join(clean_pmids[:50])}"))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        combined: dict[str, dict[str, Any]] = {}
        for res in results:
            if isinstance(res, dict):
                combined.update(res)
        return combined

    async def fetch_author_h_indices_batch(self, author_ids: list[str]) -> dict[str, int]:
        """Fetch h-index for multiple OpenAlex author IDs via a batched query."""
        clean_ids = [a.strip() for a in (author_ids or []) if a and a.strip()]
        if not clean_ids:
            return {}

        params = self._params({
            "filter": f"openalex_id:{'|'.join(clean_ids[:50])}",
            "per-page": 50,
        })
        try:
            resp = await self.http_client.get(f"{OPENALEX_BASE}/authors", params=params)
            if resp is None or resp.status_code != 200:
                return {}

            data = resp.json()
            results = data.get("results", [])
            h_indices: dict[str, int] = {}
            for author in results:
                if not isinstance(author, dict):
                    continue
                raw_id = author.get("id")
                stats = author.get("summary_stats")
                if raw_id and isinstance(stats, dict):
                    h_index = stats.get("h_index")
                    if isinstance(h_index, int):
                        bare_id = str(raw_id).rsplit("/", 1)[-1]
                        h_indices[bare_id] = h_index
            return h_indices
        except Exception:
            return {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_openalex_s2.py -v`
Expected: PASS (both new tests and the pre-existing `fetch_citation_counts_batch` tests, since `_fetch_citation_counts_single_filter`'s output is unchanged).

- [ ] **Step 5: Add `ScoringEngine.calculate_authority_feature`**

Add to `tests/test_ranking.py`:

```python
def test_calculate_authority_feature():
    assert ScoringEngine.calculate_authority_feature(None) == 0.0
    assert ScoringEngine.calculate_authority_feature(0) == 0.0
    assert math.isclose(ScoringEngine.calculate_authority_feature(9), math.log(10.0))
```

Run: `pytest tests/test_ranking.py -k calculate_authority_feature -v` (expect FAIL first), then add to `ScoringEngine` in `ranking.py`:

```python
    @staticmethod
    def calculate_authority_feature(h_index: int | None) -> float:
        if h_index is None or h_index <= 0:
            return 0.0
        return math.log(1.0 + h_index)
```

Run the test again: expect PASS.

- [ ] **Step 6: Wire h-index enrichment into `RankingPipeline.enrich_citations`**

In `src/scholar_mcp/ranking.py`, inside `RankingPipeline.enrich_citations`, replace:

```python
        # 1. OpenAlex Batch lookup
        oa_counts: dict[str, int] = {}
        if self.settings.enable_openalex:
            try:
                oa_counts = await self.openalex.fetch_citation_counts_batch(
                    dois=missing_dois,
                    pmids=missing_pmids,
                )
            except Exception:
                oa_counts = {}

        still_missing: list[int] = []
        for i in missing_indices:
            p = papers[i]
            clean_doi = (_strip_doi_url(p.doi) or p.doi.strip()).lower() if p.doi else None
            count = None
            if clean_doi and clean_doi in oa_counts:
                count = oa_counts[clean_doi]
            elif p.pmid and p.pmid.strip() in oa_counts:
                count = oa_counts[p.pmid.strip()]

            if count is not None:
                p.citation_count = count
                if p.pmid:
                    await self.cache.set(self._cache_key(f"pmid:{p.pmid.strip()}"), count)
                if clean_doi:
                    await self.cache.set(self._cache_key(f"doi:{clean_doi}"), count)
            else:
                still_missing.append(i)
```

with:

```python
        # 1. OpenAlex Batch lookup (citation count + last-author ID)
        oa_details: dict[str, dict[str, Any]] = {}
        if self.settings.enable_openalex:
            try:
                oa_details = await self.openalex.fetch_work_details_batch(
                    dois=missing_dois,
                    pmids=missing_pmids,
                )
            except Exception:
                oa_details = {}

        last_author_ids: dict[int, str] = {}
        still_missing: list[int] = []
        for i in missing_indices:
            p = papers[i]
            clean_doi = (_strip_doi_url(p.doi) or p.doi.strip()).lower() if p.doi else None
            entry = None
            if clean_doi and clean_doi in oa_details:
                entry = oa_details[clean_doi]
            elif p.pmid and p.pmid.strip() in oa_details:
                entry = oa_details[p.pmid.strip()]

            if entry is not None:
                count = entry["citation_count"]
                p.citation_count = count
                if p.pmid:
                    await self.cache.set(self._cache_key(f"pmid:{p.pmid.strip()}"), count)
                if clean_doi:
                    await self.cache.set(self._cache_key(f"doi:{clean_doi}"), count)
                if entry.get("last_author_id"):
                    last_author_ids[i] = entry["last_author_id"]
            else:
                still_missing.append(i)
```

Then, still inside `enrich_citations`, right after the "2. Parallel Fallback" block (after the `except Exception:` clause that sets `papers[i].citation_count = 0` for `still_missing`, and before the final "Guarantee non-None citation count" loop), add:

```python
        # 3. Author authority: batch-fetch last-author h-index for papers
        # resolved via OpenAlex (same precondition as citation enrichment).
        if self.settings.enable_openalex and last_author_ids:
            unique_author_ids = list({aid for aid in last_author_ids.values()})
            try:
                h_index_map = await self.openalex.fetch_author_h_indices_batch(unique_author_ids)
            except Exception:
                h_index_map = {}
            for idx, author_id in last_author_ids.items():
                h_index = h_index_map.get(author_id)
                if h_index is not None:
                    papers[idx].last_author_h_index = h_index
```

- [ ] **Step 7: Run the ranking pipeline tests to confirm no regressions**

Run: `pytest tests/test_ranking.py -v`
Expected: PASS. `test_ranking_pipeline_enrich_and_rank`'s mocked OpenAlex response has no `authorships`, so `last_author_ids` stays empty and no `/authors` call is attempted (no new mock needed).

- [ ] **Step 8: Commit**

```bash
git add src/scholar_mcp/providers/openalex.py src/scholar_mcp/ranking.py tests/test_openalex_s2.py tests/test_ranking.py
git commit -m "feat: enrich papers with last-author h-index via OpenAlex batch lookup"
```

---

### Task 7: Rewrite `RankingWeights`/`ScoringMetrics`/`score_candidates` to blend all 6 signals

**Files:**
- Modify: `src/scholar_mcp/ranking.py`
- Test: `tests/test_ranking.py`

**Interfaces:**
- Consumes: `ScoringEngine.text_coverage`, `calculate_evidence_feature`, `calculate_impact_feature`, `calculate_authority_feature`, `lookup_journal_impact` (Tasks 1, 3, 5, 6).
- Produces: `RankingWeights` with 8 fields (`relevance`, `citations`, `recency`, `evidence_grade`, `journal_impact`, `author_authority`, `recency_half_life_years`, `position_weight`); `ScoringEngine.calculate_query_relevance(rank_idx, query_terms, title, abstract, position_weight) -> float`; `ScoringEngine.score_candidates(papers, weights, query, current_year=None) -> list[PaperMetadata]` (query is now a required positional/keyword argument).

- [ ] **Step 1: Write the failing regression test**

Add to `tests/test_ranking.py`:

```python
def test_score_candidates_query_relevance_outranks_source_position():
    papers = [
        # High source rank (idx 0) but irrelevant to the query
        PaperMetadata(title="Unrelated topic entirely", year="2024", citation_count=10, pmid="1"),
        # Low source rank (idx 4) but a strong lexical match
        PaperMetadata(title="Metformin efficacy in type 2 diabetes", year="2024", citation_count=10, pmid="2"),
        PaperMetadata(title="Filler paper A", year="2024", citation_count=10, pmid="3"),
        PaperMetadata(title="Filler paper B", year="2024", citation_count=10, pmid="4"),
        PaperMetadata(title="Filler paper C", year="2024", citation_count=10, pmid="5"),
    ]
    weights = RankingWeights(
        relevance=0.7, citations=0.1, recency=0.1,
        evidence_grade=0.05, journal_impact=0.025, author_authority=0.025,
    )

    ranked = ScoringEngine.score_candidates(papers, weights=weights, query="metformin diabetes", current_year=2026)

    assert ranked[0].pmid == "2"


def test_score_candidates_new_signals_default_neutral():
    papers = [
        PaperMetadata(title="Paper A", year="2024", citation_count=5, pmid="1"),
        PaperMetadata(title="Paper B", year="2024", citation_count=5, pmid="2"),
    ]
    weights = RankingWeights()
    ranked = ScoringEngine.score_candidates(papers, weights=weights, query="", current_year=2026)

    for p in ranked:
        assert p.ranking_metrics["z_evidence"] == 0.0
        assert p.ranking_metrics["z_impact"] == 0.0
        assert p.ranking_metrics["z_authority"] == 0.0
```

Update the existing `test_score_candidates_ordering` call:

```python
    ranked = ScoringEngine.score_candidates(papers, weights=weights, query="", current_year=2026)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ranking.py -k "score_candidates" -v`
Expected: FAIL — `TypeError: score_candidates() missing 1 required positional argument: 'query'`

- [ ] **Step 3: Rewrite `RankingWeights`, `ScoringMetrics`, and `score_candidates` in `ranking.py`**

Replace the `RankingWeights` dataclass:

```python
@dataclass
class RankingWeights:
    relevance: float = 0.30
    citations: float = 0.20
    recency: float = 0.15
    evidence_grade: float = 0.20
    journal_impact: float = 0.10
    author_authority: float = 0.05
    recency_half_life_years: float = 7.0
    position_weight: float = 0.25
```

Replace the `ScoringMetrics` dataclass:

```python
@dataclass
class ScoringMetrics:
    initial_rank: int
    citation_count: int
    pub_year: int | None
    raw_relevance: float
    raw_citation: float
    raw_recency: float
    raw_evidence: float
    raw_impact: float
    raw_authority: float
    z_relevance: float
    z_citation: float
    z_recency: float
    z_evidence: float
    z_impact: float
    z_authority: float
    final_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

Add this staticmethod to `ScoringEngine`, right after `calculate_relevance` (do **not** replace `calculate_relevance` — it stays as-is, unchanged, because `medical/ranking.py` still calls `ScoringEngine.calculate_relevance(idx)` for its own position-prior term):

```python
    @staticmethod
    def calculate_query_relevance(
        rank_idx: int,
        query_terms: list[str],
        title: str | None,
        abstract: str | None,
        position_weight: float,
    ) -> float:
        lexical = ScoringEngine.text_coverage(query_terms, title, abstract)
        position = ScoringEngine.calculate_relevance(rank_idx)
        return (1.0 - position_weight) * lexical + position_weight * position
```

Replace `score_candidates` entirely:

```python
    @classmethod
    def score_candidates(
        cls,
        papers: list[PaperMetadata],
        weights: RankingWeights,
        query: str,
        current_year: int | None = None,
    ) -> list[PaperMetadata]:
        if not papers:
            return []

        now_year = current_year if current_year is not None else datetime.datetime.now().year
        query_terms = cls.tokenize(query)

        raw_rel_list: list[float] = []
        raw_cit_list: list[float] = []
        raw_rec_list: list[float] = []
        raw_evidence_list: list[float] = []
        raw_impact_list: list[float] = []
        raw_authority_list: list[float] = []
        parsed_years: list[int | None] = []
        cit_counts: list[int] = []

        for idx, p in enumerate(papers):
            raw_rel = cls.calculate_query_relevance(
                idx, query_terms, p.title, p.abstract, weights.position_weight
            )
            raw_cit = cls.calculate_citation_feature(p.citation_count)
            raw_rec, p_year = cls.calculate_recency_feature(
                p.year,
                current_year=now_year,
                half_life_years=weights.recency_half_life_years,
            )
            raw_evidence = cls.calculate_evidence_feature(p.evidence_grade)
            raw_impact = cls.calculate_impact_feature(lookup_journal_impact(p.issn, p.venue))
            raw_authority = cls.calculate_authority_feature(p.last_author_h_index)

            raw_rel_list.append(raw_rel)
            raw_cit_list.append(raw_cit)
            raw_rec_list.append(raw_rec)
            raw_evidence_list.append(raw_evidence)
            raw_impact_list.append(raw_impact)
            raw_authority_list.append(raw_authority)
            parsed_years.append(p_year)
            cit_counts.append(p.citation_count if p.citation_count is not None else 0)

        z_rel_list = cls.calculate_z_scores(raw_rel_list)
        z_cit_list = cls.calculate_z_scores(raw_cit_list)
        z_rec_list = cls.calculate_z_scores(raw_rec_list)
        z_evidence_list = cls.calculate_z_scores(raw_evidence_list)
        z_impact_list = cls.calculate_z_scores(raw_impact_list)
        z_authority_list = cls.calculate_z_scores(raw_authority_list)

        scored_papers: list[PaperMetadata] = []
        for idx, p in enumerate(papers):
            final_score = (
                weights.relevance * z_rel_list[idx]
                + weights.citations * z_cit_list[idx]
                + weights.recency * z_rec_list[idx]
                + weights.evidence_grade * z_evidence_list[idx]
                + weights.journal_impact * z_impact_list[idx]
                + weights.author_authority * z_authority_list[idx]
            )

            metrics = ScoringMetrics(
                initial_rank=idx,
                citation_count=cit_counts[idx],
                pub_year=parsed_years[idx],
                raw_relevance=raw_rel_list[idx],
                raw_citation=raw_cit_list[idx],
                raw_recency=raw_rec_list[idx],
                raw_evidence=raw_evidence_list[idx],
                raw_impact=raw_impact_list[idx],
                raw_authority=raw_authority_list[idx],
                z_relevance=z_rel_list[idx],
                z_citation=z_cit_list[idx],
                z_recency=z_rec_list[idx],
                z_evidence=z_evidence_list[idx],
                z_impact=z_impact_list[idx],
                z_authority=z_authority_list[idx],
                final_score=final_score,
            )

            p.score = final_score
            p.ranking_metrics = metrics.to_dict()
            scored_papers.append(p)

        # Sort criteria: final_score DESC, citation_count DESC, year DESC, initial_rank ASC
        def sort_key(item: PaperMetadata) -> tuple[float, int, int, int]:
            m = item.ranking_metrics or {}
            score_val = item.score if item.score is not None else -float("inf")
            cit_val = m.get("citation_count", 0)
            yr_val = m.get("pub_year") or 0
            init_rank = m.get("initial_rank", 0)
            return (score_val, cit_val, yr_val, -init_rank)

        scored_papers.sort(key=sort_key, reverse=True)
        return scored_papers
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ranking.py -v`
Expected: PASS (all of `test_ranking.py`, including the pre-existing tests updated in this task).

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/ranking.py tests/test_ranking.py
git commit -m "feat: blend evidence grade, journal impact, and author authority into ranking score"
```

---

### Task 8: Thread `query` through `RankingPipeline.rank_papers` and `WaterfallResolver.search`

**Files:**
- Modify: `src/scholar_mcp/ranking.py` (`RankingPipeline.rank_papers`)
- Modify: `src/scholar_mcp/resolver.py:479-484`
- Test: `tests/test_ranking.py`
- Test: `tests/test_waterfall_resolver.py`

**Interfaces:**
- Consumes: `ScoringEngine.score_candidates(papers, weights, query, current_year=None)` from Task 7.
- Produces: `RankingPipeline.rank_papers(papers, query, weights=None, top_n=10)`.

- [ ] **Step 1: Update the existing pipeline test call site**

In `tests/test_ranking.py`, change:

```python
    ranked = await pipeline.rank_papers(candidates, top_n=2)
```

to:

```python
    ranked = await pipeline.rank_papers(candidates, query="", top_n=2)
```

- [ ] **Step 2: Run test to verify it now fails on the right thing**

Run: `pytest tests/test_ranking.py::test_ranking_pipeline_enrich_and_rank -v`
Expected: FAIL — `TypeError: rank_papers() missing 1 required positional argument: 'query'`

- [ ] **Step 3: Implement in `ranking.py`**

Replace `RankingPipeline.rank_papers`:

```python
    async def rank_papers(
        self,
        papers: list[PaperMetadata],
        query: str,
        weights: RankingWeights | None = None,
        top_n: int = 10,
    ) -> list[PaperMetadata]:
        if not papers:
            return []

        w = weights or RankingWeights(
            relevance=self.settings.ranking_weight_relevance,
            citations=self.settings.ranking_weight_citations,
            recency=self.settings.ranking_weight_recency,
            evidence_grade=self.settings.ranking_weight_evidence_grade,
            journal_impact=self.settings.ranking_weight_journal_impact,
            author_authority=self.settings.ranking_weight_author_authority,
            recency_half_life_years=self.settings.ranking_recency_half_life_years,
            position_weight=self.settings.ranking_position_weight,
        )

        try:
            # Enrich citations with timeout protection
            enriched = await asyncio.wait_for(
                self.enrich_citations(papers),
                timeout=self.settings.ranking_enrichment_timeout,
            )
        except Exception:
            for p in papers:
                if p.citation_count is None:
                    p.citation_count = 0
            enriched = papers

        scored = ScoringEngine.score_candidates(enriched, weights=w, query=query)
        return scored[:top_n]
```

(This task assumes `Settings` already has `ranking_weight_evidence_grade`, `ranking_weight_journal_impact`, `ranking_weight_author_authority`, `ranking_position_weight` — those are added in Task 9, which must land before this code runs. If executing tasks out of order, do Task 9 first or expect an `AttributeError` on `self.settings.ranking_weight_evidence_grade` until it does.)

In `src/scholar_mcp/resolver.py`, inside `WaterfallResolver.search`, replace:

```python
        # Re-rank if requested and candidates present
        if should_rerank and papers:
            papers = await self.ranking_pipeline.rank_papers(papers, top_n=limit)
        else:
            papers = papers[:limit]
```

with:

```python
        # Re-rank if requested and candidates present
        if should_rerank and papers:
            papers = await self.ranking_pipeline.rank_papers(papers, query, top_n=limit)
        else:
            papers = papers[:limit]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ranking.py -v`
Expected: PASS

- [ ] **Step 5: Run the full resolver test suite**

Run: `pytest tests/test_waterfall_resolver.py -v`
Expected: PASS (no call-site changes needed there — `search()`'s public signature is unchanged, only its internal call to `rank_papers` changed).

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/ranking.py src/scholar_mcp/resolver.py tests/test_ranking.py
git commit -m "feat: thread search query through to the ranking pipeline"
```

---

### Task 9: `Settings` — new ranking fields and rebalanced defaults

**Files:**
- Modify: `src/scholar_mcp/config.py`
- Test: `tests/test_config_models.py`

**Interfaces:**
- Produces: `Settings.ranking_position_weight`, `Settings.ranking_weight_evidence_grade`, `Settings.ranking_weight_journal_impact`, `Settings.ranking_weight_author_authority`; `Settings.ranking_weight_relevance`/`ranking_weight_citations`/`ranking_weight_recency` defaults change to `0.30`/`0.20`/`0.15`.

- [ ] **Step 1: Update the failing default-values test**

In `tests/test_config_models.py`, replace `test_settings_ranking_defaults`:

```python
def test_settings_ranking_defaults(monkeypatch):
    monkeypatch.delenv("RANKING_ENABLED", raising=False)
    monkeypatch.delenv("RANKING_WEIGHT_RELEVANCE", raising=False)
    monkeypatch.delenv("RANKING_WEIGHT_CITATIONS", raising=False)
    monkeypatch.delenv("RANKING_WEIGHT_RECENCY", raising=False)
    monkeypatch.delenv("RANKING_WEIGHT_EVIDENCE_GRADE", raising=False)
    monkeypatch.delenv("RANKING_WEIGHT_JOURNAL_IMPACT", raising=False)
    monkeypatch.delenv("RANKING_WEIGHT_AUTHOR_AUTHORITY", raising=False)
    monkeypatch.delenv("RANKING_POSITION_WEIGHT", raising=False)
    monkeypatch.delenv("RANKING_RECENCY_HALF_LIFE_YEARS", raising=False)
    monkeypatch.delenv("RANKING_CANDIDATE_MULTIPLIER", raising=False)
    monkeypatch.delenv("RANKING_MIN_CANDIDATES", raising=False)
    monkeypatch.delenv("RANKING_MAX_CANDIDATES", raising=False)
    monkeypatch.delenv("RANKING_ENRICHMENT_TIMEOUT", raising=False)

    s = Settings.load()
    assert s.ranking_enabled is True
    assert s.ranking_weight_relevance == 0.30
    assert s.ranking_weight_citations == 0.20
    assert s.ranking_weight_recency == 0.15
    assert s.ranking_weight_evidence_grade == 0.20
    assert s.ranking_weight_journal_impact == 0.10
    assert s.ranking_weight_author_authority == 0.05
    assert s.ranking_position_weight == 0.25
    assert s.ranking_recency_half_life_years == 7.0
    assert s.ranking_candidate_multiplier == 3
    assert s.ranking_min_candidates == 20
    assert s.ranking_max_candidates == 50
    assert s.ranking_enrichment_timeout == 1.5
```

Also add to `test_settings_ranking_custom_env`, right after the existing `monkeypatch.setenv("RANKING_WEIGHT_RECENCY", "0.3")` line:

```python
    monkeypatch.setenv("RANKING_WEIGHT_EVIDENCE_GRADE", "0.15")
    monkeypatch.setenv("RANKING_WEIGHT_JOURNAL_IMPACT", "0.05")
    monkeypatch.setenv("RANKING_WEIGHT_AUTHOR_AUTHORITY", "0.02")
    monkeypatch.setenv("RANKING_POSITION_WEIGHT", "0.4")
```

and after the existing `assert s.ranking_weight_recency == 0.3` line in that same test:

```python
    assert s.ranking_weight_evidence_grade == 0.15
    assert s.ranking_weight_journal_impact == 0.05
    assert s.ranking_weight_author_authority == 0.02
    assert s.ranking_position_weight == 0.4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config_models.py -k ranking -v`
Expected: FAIL — `AssertionError: assert 0.4 == 0.3` (old default) and `AttributeError` for the new fields.

- [ ] **Step 3: Implement in `config.py`**

Replace:

```python
    ranking_enabled: bool = True
    ranking_weight_relevance: float = 0.4
    ranking_weight_citations: float = 0.3
    ranking_weight_recency: float = 0.3
    ranking_recency_half_life_years: float = 7.0
```

with:

```python
    ranking_enabled: bool = True
    ranking_weight_relevance: float = 0.30
    ranking_weight_citations: float = 0.20
    ranking_weight_recency: float = 0.15
    ranking_weight_evidence_grade: float = 0.20
    ranking_weight_journal_impact: float = 0.10
    ranking_weight_author_authority: float = 0.05
    ranking_position_weight: float = 0.25
    ranking_recency_half_life_years: float = 7.0
```

And in `Settings.load()`, replace:

```python
            ranking_enabled=_bool(os.getenv("RANKING_ENABLED"), True),
            ranking_weight_relevance=float(os.getenv("RANKING_WEIGHT_RELEVANCE", "0.4")),
            ranking_weight_citations=float(os.getenv("RANKING_WEIGHT_CITATIONS", "0.3")),
            ranking_weight_recency=float(os.getenv("RANKING_WEIGHT_RECENCY", "0.3")),
            ranking_recency_half_life_years=float(
                os.getenv("RANKING_RECENCY_HALF_LIFE_YEARS", "7.0")
            ),
```

with:

```python
            ranking_enabled=_bool(os.getenv("RANKING_ENABLED"), True),
            ranking_weight_relevance=float(os.getenv("RANKING_WEIGHT_RELEVANCE", "0.30")),
            ranking_weight_citations=float(os.getenv("RANKING_WEIGHT_CITATIONS", "0.20")),
            ranking_weight_recency=float(os.getenv("RANKING_WEIGHT_RECENCY", "0.15")),
            ranking_weight_evidence_grade=float(
                os.getenv("RANKING_WEIGHT_EVIDENCE_GRADE", "0.20")
            ),
            ranking_weight_journal_impact=float(
                os.getenv("RANKING_WEIGHT_JOURNAL_IMPACT", "0.10")
            ),
            ranking_weight_author_authority=float(
                os.getenv("RANKING_WEIGHT_AUTHOR_AUTHORITY", "0.05")
            ),
            ranking_position_weight=float(os.getenv("RANKING_POSITION_WEIGHT", "0.25")),
            ranking_recency_half_life_years=float(
                os.getenv("RANKING_RECENCY_HALF_LIFE_YEARS", "7.0")
            ),
```

Also add two new `Settings` fields for the citation-check thresholds (used by Task 10), right after `ranking_enrichment_timeout: float = 1.5`:

```python
    citation_check_supported_threshold: float = 0.5
    citation_check_weak_threshold: float = 0.15
```

and in `Settings.load()`, right after the `ranking_enrichment_timeout=...` line:

```python
            citation_check_supported_threshold=float(
                os.getenv("CITATION_CHECK_SUPPORTED_THRESHOLD", "0.5")
            ),
            citation_check_weak_threshold=float(
                os.getenv("CITATION_CHECK_WEAK_THRESHOLD", "0.15")
            ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config_models.py -v`
Expected: PASS

- [ ] **Step 5: Run the full ranking suite once more now that `Settings` has all required fields**

Run: `pytest tests/test_ranking.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/config.py tests/test_config_models.py
git commit -m "feat: add ranking settings for evidence grade, journal impact, author authority, and citation-check thresholds"
```

---

### Task 10: `check_citations` claim verification module

**Files:**
- Create: `src/scholar_mcp/citation_check.py`
- Test: `tests/test_citation_check.py`

**Interfaces:**
- Consumes: `ScoringEngine.tokenize`/`text_coverage`/`best_matching_sentence` (Task 1), `WaterfallResolver.get_metadata`/`resolve_full_text` (existing), `Settings.citation_check_supported_threshold`/`citation_check_weak_threshold` (Task 9).
- Produces: `async def check_citations(resolver: WaterfallResolver, claims: list[dict[str, str]], deep: bool = False) -> list[dict[str, Any]]`. `MAX_CLAIMS = 25`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_citation_check.py`:

```python
from unittest.mock import AsyncMock

import pytest

from scholar_mcp.citation_check import MAX_CLAIMS, check_citations
from scholar_mcp.config import Settings
from scholar_mcp.models import FullTextResponse, PaperMetadata


class _FakeResolver:
    def __init__(self, settings, metadata_by_id=None, fulltext_by_id=None):
        self.settings = settings
        self._metadata_by_id = metadata_by_id or {}
        self._fulltext_by_id = fulltext_by_id or {}

    async def get_metadata(self, identifier):
        return self._metadata_by_id.get(identifier)

    async def resolve_full_text(self, identifier, max_chars=None, sections=None):
        return self._fulltext_by_id.get(
            identifier,
            FullTextResponse(status="not_found", source="none", content=""),
        )


@pytest.fixture
def settings():
    return Settings()


async def test_check_citations_supported(settings):
    resolver = _FakeResolver(
        settings,
        metadata_by_id={
            "10.1/x": PaperMetadata(
                title="Metformin and Renal Outcomes",
                abstract="Metformin showed no significant renal outcomes in a five-year cohort.",
            )
        },
    )
    results = await check_citations(
        resolver,
        [{"text": "Metformin showed no significant renal outcomes.", "identifier": "10.1/x"}],
    )
    assert len(results) == 1
    assert results[0]["verdict"] == "SUPPORTED"
    assert results[0]["identifier"] == "10.1/x"
    assert "renal outcomes" in results[0]["best_evidence_sentence"].lower()


async def test_check_citations_unsupported(settings):
    resolver = _FakeResolver(
        settings,
        metadata_by_id={
            "10.1/y": PaperMetadata(title="Unrelated Paper", abstract="Nothing to do with the claim.")
        },
    )
    results = await check_citations(
        resolver,
        [{"text": "Metformin cures the common cold.", "identifier": "10.1/y"}],
    )
    assert results[0]["verdict"] == "UNSUPPORTED"


async def test_check_citations_weak(settings):
    resolver = _FakeResolver(
        settings,
        metadata_by_id={
            "10.1/z": PaperMetadata(title="Metformin in general practice", abstract="A broad overview.")
        },
    )
    results = await check_citations(
        resolver,
        [{"text": "Metformin reduces renal complications in diabetic cohorts.", "identifier": "10.1/z"}],
    )
    assert results[0]["verdict"] == "WEAK"


async def test_check_citations_not_found(settings):
    resolver = _FakeResolver(settings, metadata_by_id={})
    results = await check_citations(
        resolver,
        [{"text": "Some claim.", "identifier": "10.1/missing"}],
    )
    assert results[0]["verdict"] == "NOT_FOUND"


async def test_check_citations_deep_uses_full_text(settings):
    resolver = _FakeResolver(
        settings,
        fulltext_by_id={
            "10.1/deep": FullTextResponse(
                status="full_text",
                source="pmc",
                title="Deep Paper",
                content="Results: metformin significantly reduced HbA1c in the treatment arm.",
            )
        },
    )
    results = await check_citations(
        resolver,
        [{"text": "Metformin significantly reduced HbA1c.", "identifier": "10.1/deep"}],
        deep=True,
    )
    assert results[0]["verdict"] == "SUPPORTED"
    assert results[0]["resolved_title"] == "Deep Paper"


async def test_check_citations_isolated_failure(settings):
    resolver = _FakeResolver(
        settings,
        metadata_by_id={
            "10.1/ok": PaperMetadata(title="Good Paper", abstract="Metformin reduced HbA1c significantly.")
        },
    )
    resolver.get_metadata = AsyncMock(side_effect=[Exception("boom"), PaperMetadata(title="Good Paper", abstract="Metformin reduced HbA1c significantly.")])

    results = await check_citations(
        resolver,
        [
            {"text": "Metformin reduced HbA1c.", "identifier": "10.1/bad"},
            {"text": "Metformin reduced HbA1c significantly.", "identifier": "10.1/ok"},
        ],
    )
    assert len(results) == 2
    assert results[0]["verdict"] == "NOT_FOUND"
    assert results[1]["verdict"] == "SUPPORTED"


async def test_check_citations_batch_cap(settings):
    resolver = _FakeResolver(settings)
    claims = [{"text": "x", "identifier": "10.1/x"} for _ in range(MAX_CLAIMS + 1)]
    results = await check_citations(resolver, claims)
    assert len(results) == 1
    assert results[0]["verdict"] == "error"
    assert str(MAX_CLAIMS) in results[0]["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_citation_check.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scholar_mcp.citation_check'`

- [ ] **Step 3: Implement `citation_check.py`**

Create `src/scholar_mcp/citation_check.py`:

```python
import asyncio
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.ranking import ScoringEngine
from scholar_mcp.resolver import WaterfallResolver

MAX_CLAIMS = 25


def _error_result(identifier: str, message: str) -> dict[str, Any]:
    return {
        "identifier": identifier,
        "verdict": "NOT_FOUND",
        "coverage_score": 0.0,
        "best_evidence_sentence": "",
        "resolved_title": "",
        "error": message,
    }


async def _resolve_claim_source(
    resolver: WaterfallResolver,
    identifier: str,
    deep: bool,
) -> tuple[str, str, bool]:
    """Returns (title, content, found)."""
    if deep:
        resp = await resolver.resolve_full_text(identifier)
        found = bool(resp.content) and resp.status != "not_found"
        return resp.title, resp.content, found

    meta = await resolver.get_metadata(identifier)
    if meta is None:
        return "", "", False
    return meta.title, meta.abstract, bool(meta.abstract)


async def check_claim(
    resolver: WaterfallResolver,
    claim_text: str,
    identifier: str,
    deep: bool,
    settings: Settings,
) -> dict[str, Any]:
    try:
        title, content, found = await _resolve_claim_source(resolver, identifier, deep)
    except Exception as ex:
        return _error_result(identifier, str(ex))

    if not found:
        return {
            "identifier": identifier,
            "verdict": "NOT_FOUND",
            "coverage_score": 0.0,
            "best_evidence_sentence": "",
            "resolved_title": title,
        }

    query_terms = ScoringEngine.tokenize(claim_text)
    coverage = ScoringEngine.text_coverage(query_terms, title, content)
    sentence, _ = ScoringEngine.best_matching_sentence(query_terms, content)

    if coverage >= settings.citation_check_supported_threshold:
        verdict = "SUPPORTED"
    elif coverage >= settings.citation_check_weak_threshold:
        verdict = "WEAK"
    else:
        verdict = "UNSUPPORTED"

    return {
        "identifier": identifier,
        "verdict": verdict,
        "coverage_score": round(coverage, 4),
        "best_evidence_sentence": sentence,
        "resolved_title": title,
    }


async def check_citations(
    resolver: WaterfallResolver,
    claims: list[dict[str, str]],
    deep: bool = False,
) -> list[dict[str, Any]]:
    if len(claims) > MAX_CLAIMS:
        return [
            {
                "identifier": "",
                "verdict": "error",
                "coverage_score": 0.0,
                "best_evidence_sentence": "",
                "resolved_title": "",
                "error": f"Batch size exceeds maximum limit of {MAX_CLAIMS} claims",
            }
        ]

    settings = resolver.settings
    semaphore = asyncio.Semaphore(settings.max_concurrency)

    async def _check_one(claim: dict[str, str]) -> dict[str, Any]:
        async with semaphore:
            return await check_claim(
                resolver,
                claim.get("text", ""),
                claim.get("identifier", ""),
                deep,
                settings,
            )

    return await asyncio.gather(*(_check_one(c) for c in claims))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_citation_check.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/citation_check.py tests/test_citation_check.py
git commit -m "feat: add check_citations claim-to-source grounding checker"
```

---

### Task 11: Register `check_citations` as an MCP tool

**Files:**
- Modify: `src/scholar_mcp/server.py`
- Test: `tests/test_server_tools.py`

**Interfaces:**
- Consumes: `citation_check.check_citations` from Task 10.
- Produces: `check_citations` MCP tool callable as `await srv.check_citations(claims, deep=False)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_server_tools.py` (check the existing fixture setup at the top of that file — it defines a `resolver` fixture that patches `scholar_mcp.server.resolver`; reuse that same pattern):

```python
async def test_check_citations_tool_supported(resolver, monkeypatch):
    import scholar_mcp.server as srv
    from scholar_mcp.models import PaperMetadata

    async def fake_get_metadata(identifier):
        return PaperMetadata(
            title="Metformin Trial",
            abstract="Metformin significantly reduced HbA1c in the treatment group.",
        )

    monkeypatch.setattr(srv.resolver, "get_metadata", fake_get_metadata)

    results = await srv.check_citations(
        claims=[{"text": "Metformin significantly reduced HbA1c.", "identifier": "10.1/x"}],
    )
    assert results[0]["verdict"] == "SUPPORTED"


async def test_check_citations_tool_batch_cap():
    import scholar_mcp.server as srv

    claims = [{"text": "x", "identifier": "10.1/x"} for _ in range(26)]
    results = await srv.check_citations(claims=claims)
    assert results[0]["verdict"] == "error"
```

(If the `resolver` fixture in `test_server_tools.py` works differently than assumed here — inspect it first with `grep -n "def resolver" tests/test_server_tools.py` — adapt the monkeypatch target to match how that fixture wires `scholar_mcp.server.resolver`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_server_tools.py -k check_citations -v`
Expected: FAIL — `AttributeError: module 'scholar_mcp.server' has no attribute 'check_citations'`

- [ ] **Step 3: Implement in `server.py`**

Add the import near the top of `src/scholar_mcp/server.py`, with the other `scholar_mcp` imports:

```python
from scholar_mcp import citation_check
```

Add the new tool right after `get_related_papers` and before the `@mcp.prompt("deep_paper_analysis")` block:

```python
@mcp.tool()
async def check_citations(
    claims: list[dict[str, str]],
    deep: bool = False,
) -> list[dict[str, Any]]:
    """Verify each claim is supported by its cited paper (claim-to-source grounding).

    Args:
        claims: List of {"text": <claim sentence>, "identifier": <DOI/PMID/PMCID/arXiv ID>}, max 25.
        deep: Fetch full text instead of abstract only (slower, more thorough).
    """
    try:
        return await citation_check.check_citations(resolver, claims, deep=deep)
    except Exception as ex:
        return [
            {
                "identifier": "",
                "verdict": "error",
                "coverage_score": 0.0,
                "best_evidence_sentence": "",
                "resolved_title": "",
                "error": str(ex),
            }
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_server_tools.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/server.py tests/test_server_tools.py
git commit -m "feat: register check_citations MCP tool"
```

---

### Task 12: Full-pipeline regression test

**Files:**
- Test: `tests/test_ranking.py`

**Interfaces:**
- Consumes: everything from Tasks 1-9 (no new production code).

- [ ] **Step 1: Write the end-to-end regression test**

Add to `tests/test_ranking.py`:

```python
def test_score_candidates_full_pipeline_favors_high_quality_paper():
    strong = PaperMetadata(
        title="Systematic Review of Metformin for Type 2 Diabetes",
        abstract="A systematic review and meta-analysis of metformin trials in type 2 diabetes.",
        year="2024",
        citation_count=50,
        pmid="1",
        issn="0028-4793",
        evidence_grade="1a",
        last_author_h_index=60,
    )
    weak = PaperMetadata(
        title="Systematic Review of Metformin for Type 2 Diabetes",
        abstract="A systematic review and meta-analysis of metformin trials in type 2 diabetes.",
        year="2024",
        citation_count=50,
        pmid="2",
        issn=None,
        evidence_grade=None,
        last_author_h_index=None,
    )

    import scholar_mcp.ranking as ranking_module
    ranking_module._load_scimago_table.cache_clear()

    weights = RankingWeights()
    ranked = ScoringEngine.score_candidates(
        [weak, strong], weights=weights, query="metformin type 2 diabetes", current_year=2026
    )

    assert ranked[0].pmid == "1"
    assert ranked[0].score > ranked[1].score
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `pytest tests/test_ranking.py -k full_pipeline -v`
Expected: PASS immediately if Tasks 1-9 are complete and correct (this is a regression/confirmation test, not a TDD-new-feature test — if it fails, it means one of the earlier tasks has a bug; go back and fix that task, don't special-case this test).

- [ ] **Step 3: Run the entire test suite**

Run: `pytest -v`
Expected: PASS across the whole suite (all pre-existing tests plus everything added in Tasks 1-12).

- [ ] **Step 4: Commit**

```bash
git add tests/test_ranking.py
git commit -m "test: add full-pipeline regression test for evidence/impact/authority ranking signals"
```

---

## Post-implementation manual step (not part of this plan's automated tasks)

`journal_impact` will contribute `0.0` (neutral) for every paper until a maintainer manually downloads the real Scimago CSV and runs `scripts/update_scimago_data.py`, per `src/scholar_mcp/data/SOURCES.md` (Task 5, Step 6). This is expected — flag it to the user after Task 12 passes, don't try to automate the download.
