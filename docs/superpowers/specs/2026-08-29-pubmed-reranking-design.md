# PubMed Search Results Ranking and Re-ranking Design Spec

**Date**: 2026-08-29  
**Status**: Draft (Approved for Spec Review)  
**Topic**: Citations, Recency, and Z-Score Re-ranking for PubMed Results  

---

## 1. Goal and Problem Statement

PubMed searches via NCBI E-utilities return results sorted either by publication date or standard PubMed query matching. However, raw search outputs often miss highly impactful papers or fail to balance seminal high-citation papers against newly published discoveries.

This subsystem implements a ranking and re-ranking pipeline for PubMed search results based on:
1. **Initial Search Relevance**: Original rank position from PubMed query matching.
2. **Citation Impact**: Log-transformed citation counts enriched via a multi-provider cascade (OpenAlex $\rightarrow$ Europe PMC $\rightarrow$ CrossRef).
3. **Publication Recency**: Exponential half-life age decay.
4. **Statistical Z-Score Normalization**: Standardizing all metric distributions across candidate pool ($z = \frac{x - \mu}{\sigma}$) to allow balanced linear weighting.

---

## 2. Architecture and Data Models

### 2.1 Module Structure

A new module `src/scholar_mcp/ranking.py` houses scoring and pipeline logic.

- `ScoringEngine`: Pure mathematical functions for feature transforms, Z-scores, and composite scores.
- `RankingPipeline`: Orchestrator for candidate over-fetching, citation enrichment, scoring, and sorting.
- `OpenAlexProvider`: Added batch works query endpoint to resolve citation counts for up to 50 works in 1 HTTP call.

### 2.2 Data Classes

#### `RankingWeights` (`src/scholar_mcp/ranking.py`)
```python
from dataclasses import dataclass

@dataclass
class RankingWeights:
    relevance: float = 0.4
    citations: float = 0.3
    recency: float = 0.3
    recency_half_life_years: float = 7.0
```

#### `ScoringMetrics` (`src/scholar_mcp/ranking.py`)
```python
from dataclasses import asdict, dataclass
from typing import Any

@dataclass
class ScoringMetrics:
    initial_rank: int
    citation_count: int
    pub_year: int | None
    raw_relevance: float     # 1 / sqrt(initial_rank)
    raw_citation: float      # ln(1 + max(0, citation_count))
    raw_recency: float       # exp(-lambda * delta_years)
    z_relevance: float
    z_citation: float
    z_recency: float
    final_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

#### `PaperMetadata` Updates (`src/scholar_mcp/models.py`)
Add two optional fields:
```python
score: float | None = None
ranking_metrics: dict[str, Any] | None = None
```

---

## 3. Mathematical Scoring Specification

### 3.1 Feature Extraction

Given candidate pool of $K$ papers ($i \in [0, K-1]$):

1. **Relevance Position Feature**:
   $$p_i = \frac{1}{\sqrt{i + 1}}$$
   Where $i$ is 0-indexed initial rank from search provider.

2. **Citation Feature**:
   $$c_i = \ln(1 + \max(0, \text{citations}_i))$$
   Logarithmic compression dampens extreme citation outliers.

3. **Recency Feature**:
   $$\Delta\text{years}_i = \max(0, \text{current\_year} - \text{year}_i)$$
   $$\lambda = \frac{\ln(2)}{T_{1/2}}$$
   $$r_i = \exp(-\lambda \cdot \Delta\text{years}_i)$$
   Where $T_{1/2} = \text{ranking\_recency\_half\_life\_years}$ (default 7.0 years).
   If $\text{year}_i$ is missing or invalid, default to median year of candidate batch (or $\Delta\text{years}_i = 10$).

### 3.2 Statistical Z-Score Normalization

For each feature vector $X \in \{P, C, R\}$ across $K$ candidates:
$$\mu = \frac{1}{K} \sum_{i=1}^K x_i$$
$$\sigma = \sqrt{\frac{1}{K} \sum_{i=1}^K (x_i - \mu)^2}$$

Standardized score:
$$Z(x_i) = \begin{cases} 0.0 & \text{if } \sigma < 10^{-6} \text{ or } K \le 1 \\ \frac{x_i - \mu}{\sigma} & \text{otherwise} \end{cases}$$

### 3.3 Composite Score & Ordering

Final score $S_i$:
$$S_i = w_{\text{rel}} \cdot Z(p_i) + w_{\text{cit}} \cdot Z(c_i) + w_{\text{rec}} \cdot Z(r_i)$$

Sorting criteria (descending):
1. `S_i` (Composite score, descending)
2. `citation_count` (Raw citations, descending)
3. `year` (Publication year, descending)
4. `initial_rank` (Original retrieval order, ascending)

---

## 4. Citation Cascade Enrichment

### 4.1 Cascade Flow

```
K Candidate Papers
       │
       ▼
Check TTLCache (`citations:{pmid or doi}`)
       │
       ▼ Unresolved items
Tier 1: OpenAlex Batch Query
       • Single HTTP GET `/works?filter=pmid:1|2|...|50&per-page=50`
       • Resolves cited_by_count for batch
       │
       ▼ Still unresolved items (missing DOIs/PMIDs in OpenAlex)
Tier 2: Europe PMC / CrossRef Fallback
       • Parallel async queries for remaining uncounted papers
       │
       ▼ Unresolvable
Tier 3: Default citation_count = 0
```

### 4.2 Caching & Timeouts

- All resolved counts cached with key `citations:{identifier}` in `TTLCache`.
- Total enrichment phase bounded by 1.5s timeout. If timeout expires, scoring runs immediately with all resolved counts; remaining default to 0.

---

## 5. Configuration Settings (`src/scholar_mcp/config.py`)

New settings keys configurable via environment variables and `.env`:

| Setting Key | Env Var | Type | Default | Description |
|---|---|---|---|---|
| `ranking_enabled` | `RANKING_ENABLED` | `bool` | `True` | Global toggle for search re-ranking |
| `ranking_weight_relevance` | `RANKING_WEIGHT_RELEVANCE` | `float` | `0.4` | Weight for initial rank position |
| `ranking_weight_citations` | `RANKING_WEIGHT_CITATIONS` | `float` | `0.3` | Weight for log-citations |
| `ranking_weight_recency` | `RANKING_WEIGHT_RECENCY` | `float` | `0.3` | Weight for recency decay |
| `ranking_recency_half_life_years` | `RANKING_RECENCY_HALF_LIFE_YEARS` | `float` | `7.0` | Half-life in years for exponential age decay |
| `ranking_candidate_multiplier` | `RANKING_CANDIDATE_MULTIPLIER` | `int` | `3` | Multiplier for candidate pool size |
| `ranking_min_candidates` | `RANKING_MIN_CANDIDATES` | `int` | `20` | Minimum candidate over-fetch depth |
| `ranking_max_candidates` | `RANKING_MAX_CANDIDATES` | `int` | `50` | Maximum candidate over-fetch depth |

---

## 6. Server & Resolver Integration

### 6.1 `search_papers` Tool Signature (`src/scholar_mcp/server.py`)

```python
@mcp.tool()
async def search_papers(
    query: str,
    source: str = "auto",
    num_results: int = 10,
    rerank: bool = True,
    year_start: int | None = None,
    year_end: int | None = None,
    author: str | None = None,
    journal: str | None = None,
) -> list[dict[str, Any]]:
```

### 6.2 `WaterfallResolver.search` Flow (`src/scholar_mcp/resolver.py`)

1. If `rerank=True` and `settings.ranking_enabled=True`:
   $$K = \min(\text{settings.ranking\_max\_candidates}, \max(\text{num\_results} \times \text{settings.ranking\_candidate\_multiplier}, \text{settings.ranking\_min\_candidates}))$$
2. Fetch $K$ candidates from selected provider (`pubmed` or `auto`).
3. Call `ranking_pipeline.rank_papers(candidates, top_n=num_results)`.
4. Run batched OA status annotation on top-$N$.
5. Return top-$N$ ranked papers.

---

## 7. Error Handling & Edge Cases

1. **Zero Candidates**: Return empty list immediately.
2. **Single Candidate ($K=1$)**: Return single candidate with neutral $Z=0.0$ scores.
3. **Zero Variance in Features**: If all candidates share identical citation counts or publication years ($\sigma < 10^{-6}$), set $Z=0.0$ for that feature. No division by zero.
4. **Provider Enrichment Failures**: If OpenAlex/EPMC fail, papers default to 0 citations. Ranking gracefully falls back to relevance + recency.
5. **Missing/Invalid Publication Dates**: Extracted with regex fallback; default to batch median year if unparseable.

---

## 8. Verification & Test Plan

1. **Unit Tests (`tests/test_ranking.py`)**:
   - `test_z_score_basic`: Validate mean/std/Z-score accuracy against analytical vectors.
   - `test_z_score_zero_variance`: Confirm zero-variance inputs yield all $Z=0.0$ without division errors.
   - `test_recency_decay`: Validate half-life exponential curve ($1.0$ at age 0, $0.5$ at $T_{1/2}$, $0.25$ at $2 \times T_{1/2}$).
   - `test_log_citations`: Validate outlier damping ($10000$ vs $100$ citations).
   - `test_composite_weights`: Verify changing settings weights shifts rank between seminal older paper and recent lower-cited paper.
   - `test_openalex_batch_enrichment`: Test batch endpoint with mocked OpenAlex response.
   - `test_cascade_fallback_to_europe_pmc`: Test secondary enrichment when OpenAlex returns 404/empty.
2. **Integration Tests (`tests/test_resolver.py` & `tests/test_server.py`)**:
   - `test_search_papers_rerank_enabled`: Top results contain `.score` and `.ranking_metrics` in descending score order.
   - `test_search_papers_rerank_disabled`: When `rerank=False`, results preserve original PubMed chronological/relevance order.
   - `test_search_papers_custom_weights`: Test custom env settings alter ranking order.
