# PubMed Ranking Remediation — Round 2

**Date:** 2026-08-30
**PR:** https://github.com/gustavokch/scholar-mcp/pull/6
**Branch:** `fix/pubmed-result-ranking`
**Review comment:** https://github.com/gustavokch/scholar-mcp/pull/6#issuecomment-5470918354

## Goal

Resolve the six findings from the round-2 review of PR #6. The three substantive
ones all concern `rank_medical_articles`: its relevance term cannot reach the
range that recency occupies, it throws away the NCBI Best Match ordering the same
PR just enabled, and the journal-search path ranks against its own PubMed field
filters.

## Architecture

`src/scholar_mcp/medical/ranking.py` stays a single pure-ish scoring function
with no network access. Two changes to its scoring model:

1. **Field-coverage normalisation.** Relevance becomes
   `min(1.0, title_coverage + (abstract_weight / title_weight) * abstract_coverage)`
   where coverage is the fraction of query terms present in that field. A full
   title match reaches 1.0 and a full abstract-only match reaches 0.5, so lexical
   evidence can outweigh the 0..1 recency term instead of being capped below it.

2. **Optional source-position prior.** A new `position_weight` argument blends
   `ScoringEngine.calculate_relevance(idx)` (`1/sqrt(idx+1)`, the same prior the
   scholar path uses) into the relevance component. Callers with a single
   relevance-ordered source pass a non-zero weight; the multi-database merge
   leaves it at the default `0.0` because its index order is task-concatenation
   order, not relevance.

`search_medical_journals` gains the same rank-before-truncate treatment already
applied to `search_medical_databases`, re-ranking with the raw user query so the
`"..."[Journal]` filter tokens do not score as query terms.

## Tech Stack

Python 3.11+, pytest, respx, uv.

---

## Task 1 — Relevance normalisation reaches the recency range

**Modify:** `src/scholar_mcp/medical/ranking.py`
**Test:** `tests/medical/test_medical_ranking.py`

**Consumes:** `MedicalArticle.title`, `.abstract`, `.year`, query string.
**Produces:** ranked list where a full abstract-only match outranks a zero-match
recent article.

### Step 1 — Write the failing test

```python
def test_abstract_match_outranks_irrelevant_recent_article():
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
```

### Step 2 — Confirm failure

```bash
uv run pytest tests/medical/test_medical_ranking.py -v
```

### Step 3 — Implement

Replace the `denorm` division with coverage fractions and a clamp.

### Step 4 — Confirm pass

```bash
uv run pytest tests/medical/test_medical_ranking.py -v
```

### Step 5 — Commit

```bash
git add src/scholar_mcp/medical/ranking.py tests/medical/test_medical_ranking.py
git commit -m "fix(medical): normalize relevance against achievable field weight"
```

---

## Task 2 — Preserve NCBI Best Match ordering for single-source results

**Modify:** `src/scholar_mcp/medical/ranking.py`, `src/scholar_mcp/medical/pubmed.py`
**Test:** `tests/medical/test_medical_ranking.py`

**Consumes:** article index in the source list.
**Produces:** `position_weight` argument; `search_articles` passes
`SOURCE_POSITION_WEIGHT`, `search_medical_databases` keeps the `0.0` default.

### Step 1 — Write the failing test

```python
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
```

### Step 2 — Confirm failure

```bash
uv run pytest tests/medical/test_medical_ranking.py -v
```

### Step 3 — Implement

Add the parameter, blend the prior into the relevance component, and pass
`SOURCE_POSITION_WEIGHT` from `MedicalPubMedClient.search_articles`.

### Step 4 — Confirm pass

```bash
uv run pytest tests/medical/test_medical_ranking.py tests/medical/test_pubmed.py -v
```

### Step 5 — Commit

```bash
git add src/scholar_mcp/medical/ranking.py src/scholar_mcp/medical/pubmed.py tests/medical/test_medical_ranking.py
git commit -m "fix(medical): blend NCBI source position into single-source ranking"
```

---

## Task 3 — Journal search ranks on the user query, before truncation

**Modify:** `src/scholar_mcp/medical/databases.py`
**Test:** `tests/medical/test_databases.py`

**Consumes:** raw `query`, deduplicated article pool.
**Produces:** `search_medical_journals` results ranked by the user query, sliced
to 15 after ranking.

### Step 1 — Write the failing test

Build a mocked `search_articles` returning 20 filler articles whose titles
contain journal-name tokens, plus one on-topic article last. Assert the on-topic
article survives the `[:15]` slice.

### Step 2 — Confirm failure

```bash
uv run pytest tests/medical/test_databases.py -v
```

### Step 3 — Implement

Rank `deduped` with the raw `query` before slicing.

### Step 4 — Confirm pass

```bash
uv run pytest tests/medical/test_databases.py -v
```

### Step 5 — Commit

```bash
git add src/scholar_mcp/medical/databases.py tests/medical/test_databases.py
git commit -m "fix(medical): rank journal search on user query before truncation"
```

---

## Task 4 — Contract and docstring cleanups

**Modify:** `src/scholar_mcp/medical/ranking.py`, `tests/medical/test_pubmed.py`

- Docstring: state that `article.score` is assigned in place instead of claiming
  the function is pure.
- Empty-term path: return `list(articles)` so every call returns a fresh list.
- Drop the `with respx.mock:` block nested inside the `@respx.mock` decorator in
  `test_search_articles_requests_relevance_sort`.

### Test

```python
def test_stopword_only_query_returns_new_list():
    articles = [_article("Second paper"), _article("First paper")]
    ranked = rank_medical_articles(articles, "in the of and")
    assert ranked is not articles
    assert [a.title for a in ranked] == ["Second paper", "First paper"]
```

### Commit

```bash
git add src/scholar_mcp/medical/ranking.py tests/medical/test_medical_ranking.py tests/medical/test_pubmed.py
git commit -m "refactor(medical): clarify ranking contract and drop nested respx mock"
```

---

## Verification

```bash
uv run pytest
git push origin fix/pubmed-result-ranking
```

Gate: full suite green before push.
