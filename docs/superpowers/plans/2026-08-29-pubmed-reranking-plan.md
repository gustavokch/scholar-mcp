# PubMed Search Results Ranking and Re-ranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a ranking and re-ranking pipeline for PubMed search results using initial relevance rank, log-citations, exponential recency decay, and statistical Z-score normalization.

**Architecture:** A standalone mathematical scoring and cascade enrichment engine in `src/scholar_mcp/ranking.py` driven by `RankingPipeline`, batched OpenAlex citation lookups in `src/scholar_mcp/providers/openalex.py`, configured via `Settings` and wired into `WaterfallResolver.search` and `search_papers` MCP tool.

**Tech Stack:** Python 3.10+, FastMCP, httpx / AsyncHttpClient, pytest, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-29-pubmed-reranking-design.md`

## Global Constraints

- Controlled vocabulary, clean type annotations (`float | None`, `list[str]`), no unhandled exceptions in MCP server tools.
- All HTTP requests must use `AsyncHttpClient` or provider abstractions with timeout protection.
- Zero-variance distributions ($\sigma < 10^{-6}$) in Z-score calculation must default to $Z=0.0$ without division-by-zero.
- Single candidate batches ($K \le 1$) must return without score distortion.
- All settings must be configurable via environment variables with sensible defaults matching the spec.

---

### Task 1: Data Models and Settings Configuration

**Files:**
- Modify: `src/scholar_mcp/models.py:33-49`
- Modify: `src/scholar_mcp/config.py:16-88`
- Test: `tests/test_config_models.py`

**Interfaces:**
- Consumes: Existing `PaperMetadata` and `Settings` classes.
- Produces:
  - `PaperMetadata.score: float | None = None`
  - `PaperMetadata.ranking_metrics: dict[str, Any] | None = None`
  - `Settings.ranking_enabled: bool` (default `True`)
  - `Settings.ranking_weight_relevance: float` (default `0.4`)
  - `Settings.ranking_weight_citations: float` (default `0.3`)
  - `Settings.ranking_weight_recency: float` (default `0.3`)
  - `Settings.ranking_recency_half_life_years: float` (default `7.0`)
  - `Settings.ranking_candidate_multiplier: int` (default `3`)
  - `Settings.ranking_min_candidates: int` (default `20`)
  - `Settings.ranking_max_candidates: int` (default `50`)

- [ ] **Step 1: Write the failing test**

Edit `tests/test_config_models.py` to add tests for the new `PaperMetadata` fields and `Settings` ranking keys:

```python
def test_paper_metadata_ranking_fields():
    meta = PaperMetadata(
        title="Sample Paper",
        score=1.42,
        ranking_metrics={"z_citation": 0.8, "z_recency": 0.6},
    )
    d = meta.to_dict()
    assert d["score"] == 1.42
    assert d["ranking_metrics"] == {"z_citation": 0.8, "z_recency": 0.6}


def test_settings_ranking_defaults(monkeypatch):
    monkeypatch.delenv("RANKING_ENABLED", raising=False)
    monkeypatch.delenv("RANKING_WEIGHT_RELEVANCE", raising=False)
    monkeypatch.delenv("RANKING_WEIGHT_CITATIONS", raising=False)
    monkeypatch.delenv("RANKING_WEIGHT_RECENCY", raising=False)
    monkeypatch.delenv("RANKING_RECENCY_HALF_LIFE_YEARS", raising=False)
    monkeypatch.delenv("RANKING_CANDIDATE_MULTIPLIER", raising=False)
    monkeypatch.delenv("RANKING_MIN_CANDIDATES", raising=False)
    monkeypatch.delenv("RANKING_MAX_CANDIDATES", raising=False)

    s = Settings.load()
    assert s.ranking_enabled is True
    assert s.ranking_weight_relevance == 0.4
    assert s.ranking_weight_citations == 0.3
    assert s.ranking_weight_recency == 0.3
    assert s.ranking_recency_half_life_years == 7.0
    assert s.ranking_candidate_multiplier == 3
    assert s.ranking_min_candidates == 20
    assert s.ranking_max_candidates == 50


def test_settings_ranking_custom_env(monkeypatch):
    monkeypatch.setenv("RANKING_ENABLED", "0")
    monkeypatch.setenv("RANKING_WEIGHT_RELEVANCE", "0.5")
    monkeypatch.setenv("RANKING_WEIGHT_CITATIONS", "0.2")
    monkeypatch.setenv("RANKING_WEIGHT_RECENCY", "0.3")
    monkeypatch.setenv("RANKING_RECENCY_HALF_LIFE_YEARS", "5.0")
    monkeypatch.setenv("RANKING_CANDIDATE_MULTIPLIER", "4")
    monkeypatch.setenv("RANKING_MIN_CANDIDATES", "15")
    monkeypatch.setenv("RANKING_MAX_CANDIDATES", "40")

    s = Settings.load()
    assert s.ranking_enabled is False
    assert s.ranking_weight_relevance == 0.5
    assert s.ranking_weight_citations == 0.2
    assert s.ranking_weight_recency == 0.3
    assert s.ranking_recency_half_life_years == 5.0
    assert s.ranking_candidate_multiplier == 4
    assert s.ranking_min_candidates == 15
    assert s.ranking_max_candidates == 40
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_models.py -k "ranking" -v`
Expected: FAIL with `TypeError: PaperMetadata.__init__() got unexpected keyword argument 'score'` or `AttributeError: 'Settings' object has no attribute 'ranking_enabled'`.

- [ ] **Step 3: Implement changes in `models.py` and `config.py`**

In `src/scholar_mcp/models.py`:
```python
@dataclass
class PaperMetadata:
    title: str
    authors: list[str] = field(default_factory=list)
    year: str = ""
    venue: str = ""
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    abstract: str = ""
    oa_status: str = "unknown"  # "oa" | "closed" | "unknown"
    citation_count: int | None = None
    oa_url: str | None = None
    institutions: list[str] = field(default_factory=list)
    score: float | None = None
    ranking_metrics: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

In `src/scholar_mcp/config.py`:
Add the ranking fields to `Settings`:
```python
    ranking_enabled: bool = True
    ranking_weight_relevance: float = 0.4
    ranking_weight_citations: float = 0.3
    ranking_weight_recency: float = 0.3
    ranking_recency_half_life_years: float = 7.0
    ranking_candidate_multiplier: int = 3
    ranking_min_candidates: int = 20
    ranking_max_candidates: int = 50
```

And in `Settings.load()`:
```python
            ranking_enabled=_bool(os.getenv("RANKING_ENABLED"), True),
            ranking_weight_relevance=float(os.getenv("RANKING_WEIGHT_RELEVANCE", "0.4")),
            ranking_weight_citations=float(os.getenv("RANKING_WEIGHT_CITATIONS", "0.3")),
            ranking_weight_recency=float(os.getenv("RANKING_WEIGHT_RECENCY", "0.3")),
            ranking_recency_half_life_years=float(
                os.getenv("RANKING_RECENCY_HALF_LIFE_YEARS", "7.0")
            ),
            ranking_candidate_multiplier=int(os.getenv("RANKING_CANDIDATE_MULTIPLIER", "3")),
            ranking_min_candidates=int(os.getenv("RANKING_MIN_CANDIDATES", "20")),
            ranking_max_candidates=int(os.getenv("RANKING_MAX_CANDIDATES", "50")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/models.py src/scholar_mcp/config.py tests/test_config_models.py
git commit -m "feat(config): add ranking settings and metadata score fields"
```

---

### Task 2: Mathematical Scoring Engine and Dataclasses

**Files:**
- Create: `src/scholar_mcp/ranking.py`
- Create: `tests/test_ranking.py`

**Interfaces:**
- Consumes: `PaperMetadata` from `scholar_mcp.models`.
- Produces:
  - `RankingWeights` dataclass: `(relevance=0.4, citations=0.3, recency=0.3, recency_half_life_years=7.0)`
  - `ScoringMetrics` dataclass: `(initial_rank, citation_count, pub_year, raw_relevance, raw_citation, raw_recency, z_relevance, z_citation, z_recency, final_score)`
  - `ScoringEngine.calculate_z_scores(values: list[float]) -> list[float]`
  - `ScoringEngine.calculate_relevance(rank_idx: int) -> float`
  - `ScoringEngine.calculate_citation_feature(citations: int | None) -> float`
  - `ScoringEngine.calculate_recency_feature(year_str: str | None, current_year: int, half_life_years: float, default_age: float = 10.0) -> tuple[float, int | None]`
  - `ScoringEngine.score_candidates(papers: list[PaperMetadata], weights: RankingWeights, current_year: int | None = None) -> list[PaperMetadata]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ranking.py`:

```python
import math
import pytest
from scholar_mcp.models import PaperMetadata
from scholar_mcp.ranking import RankingWeights, ScoringEngine, ScoringMetrics


def test_calculate_z_scores_basic():
    values = [10.0, 20.0, 30.0]
    z_scores = ScoringEngine.calculate_z_scores(values)
    assert len(z_scores) == 3
    # Mean = 20, Std = sqrt((100 + 0 + 100)/3) = sqrt(66.6667) = 8.1649658
    assert math.isclose(z_scores[0], (10 - 20) / math.sqrt(200 / 3), rel_tol=1e-5)
    assert math.isclose(z_scores[1], 0.0, abs_tol=1e-5)
    assert math.isclose(z_scores[2], (30 - 20) / math.sqrt(200 / 3), rel_tol=1e-5)


def test_calculate_z_scores_zero_variance():
    values = [5.0, 5.0, 5.0, 5.0]
    z_scores = ScoringEngine.calculate_z_scores(values)
    assert z_scores == [0.0, 0.0, 0.0, 0.0]


def test_calculate_z_scores_empty_and_single():
    assert ScoringEngine.calculate_z_scores([]) == []
    assert ScoringEngine.calculate_z_scores([42.0]) == [0.0]


def test_calculate_recency_feature():
    current_year = 2026
    half_life = 7.0

    # Age 0 -> exp(0) = 1.0
    r0, y0 = ScoringEngine.calculate_recency_feature("2026", current_year, half_life)
    assert math.isclose(r0, 1.0, rel_tol=1e-5)
    assert y0 == 2026

    # Age 7 -> exp(-ln(2)) = 0.5
    r7, y7 = ScoringEngine.calculate_recency_feature("2019", current_year, half_life)
    assert math.isclose(r7, 0.5, rel_tol=1e-5)
    assert y7 == 2019

    # Age 14 -> exp(-2*ln(2)) = 0.25
    r14, y14 = ScoringEngine.calculate_recency_feature("2012", current_year, half_life)
    assert math.isclose(r14, 0.25, rel_tol=1e-5)
    assert y14 == 2012

    # Invalid year fallback
    r_bad, y_bad = ScoringEngine.calculate_recency_feature("unknown", current_year, half_life, default_age=10.0)
    expected_decay = math.exp(-(math.log(2) / 7.0) * 10.0)
    assert math.isclose(r_bad, expected_decay, rel_tol=1e-5)
    assert y_bad is None


def test_calculate_citation_feature():
    assert math.isclose(ScoringEngine.calculate_citation_feature(0), math.log(1), rel_tol=1e-5)
    assert math.isclose(ScoringEngine.calculate_citation_feature(None), math.log(1), rel_tol=1e-5)
    assert math.isclose(ScoringEngine.calculate_citation_feature(100), math.log(101), rel_tol=1e-5)


def test_score_candidates_ordering():
    papers = [
        # Paper 0: Old, massive citations
        PaperMetadata(title="Seminal Classic", year="2010", citation_count=5000, pmid="1"),
        # Paper 1: Recent, moderate citations
        PaperMetadata(title="Recent High Impact", year="2025", citation_count=100, pmid="2"),
        # Paper 2: Very recent, zero citations
        PaperMetadata(title="Brand New Paper", year="2026", citation_count=0, pmid="3"),
    ]

    weights = RankingWeights(relevance=0.2, citations=0.4, recency=0.4, recency_half_life_years=7.0)
    ranked = ScoringEngine.score_candidates(papers, weights=weights, current_year=2026)

    assert len(ranked) == 3
    # Verify all papers have score and ranking_metrics
    for p in ranked:
        assert p.score is not None
        assert p.ranking_metrics is not None
        assert "z_citation" in p.ranking_metrics
        assert "z_recency" in p.ranking_metrics
        assert "z_relevance" in p.ranking_metrics

    # Scores must be sorted descending
    assert ranked[0].score >= ranked[1].score >= ranked[2].score
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ranking.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scholar_mcp.ranking'`

- [ ] **Step 3: Implement `src/scholar_mcp/ranking.py`**

Create `src/scholar_mcp/ranking.py`:

```python
from dataclasses import asdict, dataclass
import datetime
import math
import re
from typing import Any

from scholar_mcp.models import PaperMetadata


@dataclass
class RankingWeights:
    relevance: float = 0.4
    citations: float = 0.3
    recency: float = 0.3
    recency_half_life_years: float = 7.0


@dataclass
class ScoringMetrics:
    initial_rank: int
    citation_count: int
    pub_year: int | None
    raw_relevance: float
    raw_citation: float
    raw_recency: float
    z_relevance: float
    z_citation: float
    z_recency: float
    final_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScoringEngine:
    """Pure mathematical methods for search re-ranking and Z-score standardization."""

    @staticmethod
    def calculate_z_scores(values: list[float]) -> list[float]:
        k = len(values)
        if k <= 1:
            return [0.0] * k

        mean = sum(values) / k
        variance = sum((x - mean) ** 2 for x in values) / k
        std = math.sqrt(variance)

        if std < 1e-6:
            return [0.0] * k

        return [(x - mean) / std for x in values]

    @staticmethod
    def calculate_relevance(rank_idx: int) -> float:
        return 1.0 / math.sqrt(rank_idx + 1)

    @staticmethod
    def calculate_citation_feature(citations: int | None) -> float:
        count = max(0, citations if citations is not None else 0)
        return math.log(1.0 + count)

    @staticmethod
    def parse_year(year_str: str | None) -> int | None:
        if not year_str:
            return None
        match = re.search(r"\b(19\d\d|20\d\d)\b", str(year_str))
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
        return None

    @classmethod
    def calculate_recency_feature(
        cls,
        year_str: str | None,
        current_year: int,
        half_life_years: float,
        default_age: float = 10.0,
    ) -> tuple[float, int | None]:
        parsed_year = cls.parse_year(year_str)
        if parsed_year is not None:
            age = max(0.0, float(current_year - parsed_year))
        else:
            age = default_age

        half_life = max(0.1, half_life_years)
        decay_rate = math.log(2) / half_life
        recency_val = math.exp(-decay_rate * age)
        return recency_val, parsed_year

    @classmethod
    def score_candidates(
        cls,
        papers: list[PaperMetadata],
        weights: RankingWeights,
        current_year: int | None = None,
    ) -> list[PaperMetadata]:
        if not papers:
            return []

        now_year = current_year if current_year is not None else datetime.datetime.now().year

        raw_rel_list: list[float] = []
        raw_cit_list: list[float] = []
        raw_rec_list: list[float] = []
        parsed_years: list[int | None] = []
        cit_counts: list[int] = []

        for idx, p in enumerate(papers):
            raw_rel = cls.calculate_relevance(idx)
            raw_cit = cls.calculate_citation_feature(p.citation_count)
            raw_rec, p_year = cls.calculate_recency_feature(
                p.year,
                current_year=now_year,
                half_life_years=weights.recency_half_life_years,
            )

            raw_rel_list.append(raw_rel)
            raw_cit_list.append(raw_cit)
            raw_rec_list.append(raw_rec)
            parsed_years.append(p_year)
            cit_counts.append(p.citation_count if p.citation_count is not None else 0)

        z_rel_list = cls.calculate_z_scores(raw_rel_list)
        z_cit_list = cls.calculate_z_scores(raw_cit_list)
        z_rec_list = cls.calculate_z_scores(raw_rec_list)

        scored_papers: list[PaperMetadata] = []
        for idx, p in enumerate(papers):
            final_score = (
                weights.relevance * z_rel_list[idx]
                + weights.citations * z_cit_list[idx]
                + weights.recency * z_rec_list[idx]
            )

            metrics = ScoringMetrics(
                initial_rank=idx,
                citation_count=cit_counts[idx],
                pub_year=parsed_years[idx],
                raw_relevance=raw_rel_list[idx],
                raw_citation=raw_cit_list[idx],
                raw_recency=raw_rec_list[idx],
                z_relevance=z_rel_list[idx],
                z_citation=z_cit_list[idx],
                z_recency=z_rec_list[idx],
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

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ranking.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/ranking.py tests/test_ranking.py
git commit -m "feat(ranking): implement scoring engine and z-score normalization"
```

---

### Task 3: OpenAlex Batch Works Query Endpoint

**Files:**
- Modify: `src/scholar_mcp/providers/openalex.py:31-100`
- Modify: `tests/test_openalex_s2.py`

**Interfaces:**
- Consumes: `AsyncHttpClient` from `scholar_mcp.utils.http`.
- Produces: `OpenAlexProvider.fetch_citation_counts_batch(dois: list[str], pmids: list[str]) -> dict[str, int]` mapping lowercase DOI and PMID strings to integer `cited_by_count`.

- [ ] **Step 1: Write the failing test**

Add test in `tests/test_openalex_s2.py`:

```python
@pytest.mark.asyncio
async def test_openalex_fetch_citation_counts_batch(httpx_mock, settings):
    client = AsyncHttpClient(settings)
    provider = OpenAlexProvider(client, email="test@example.com")

    # Mock batch response from OpenAlex
    batch_response = {
        "results": [
            {
                "doi": "https://doi.org/10.1038/s41586-020-2649-2",
                "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/32814902", "doi": "https://doi.org/10.1038/s41586-020-2649-2"},
                "cited_by_count": 1250,
            },
            {
                "doi": "https://doi.org/10.1016/j.cell.2021.01.001",
                "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/33503445"},
                "cited_by_count": 340,
            }
        ]
    }

    httpx_mock.add_response(
        url=re.compile(r"https://api\.openalex\.org/works\?.*"),
        json=batch_response,
        status_code=200,
    )

    dois = ["10.1038/s41586-020-2649-2", "10.1016/j.cell.2021.01.001"]
    pmids = ["32814902", "33503445"]

    counts = await provider.fetch_citation_counts_batch(dois=dois, pmids=pmids)

    assert counts.get("10.1038/s41586-020-2649-2") == 1250
    assert counts.get("32814902") == 1250
    assert counts.get("10.1016/j.cell.2021.01.001") == 340
    assert counts.get("33503445") == 340
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_openalex_s2.py -k "fetch_citation_counts_batch" -v`
Expected: FAIL with `AttributeError: 'OpenAlexProvider' object has no attribute 'fetch_citation_counts_batch'`

- [ ] **Step 3: Implement `fetch_citation_counts_batch` in `OpenAlexProvider`**

In `src/scholar_mcp/providers/openalex.py`:

```python
    async def fetch_citation_counts_batch(
        self,
        dois: list[str] | None = None,
        pmids: list[str] | None = None,
    ) -> dict[str, int]:
        """Fetch citation counts for multiple DOIs and PMIDs in single batched OpenAlex query."""
        clean_dois = [
            _strip_doi_url(d) or d.strip()
            for d in (dois or [])
            if d and (_strip_doi_url(d) or d.strip())
        ]
        clean_pmids = [p.strip() for p in (pmids or []) if p and p.strip()]

        if not clean_dois and not clean_pmids:
            return {}

        filter_parts: list[str] = []
        if clean_dois:
            filter_parts.append(f"doi:{'|'.join(clean_dois[:50])}")
        if clean_pmids:
            filter_parts.append(f"pmid:{'|'.join(clean_pmids[:50])}")

        filter_str = ",".join(filter_parts)
        params = self._params({"filter": filter_str, "per-page": 50})

        try:
            resp = await self.http_client.get(f"{OPENALEX_BASE}/works", params=params)
            if resp is None or resp.status_code != 200:
                return {}

            data = resp.json()
            results = data.get("results", [])
            counts: dict[str, int] = {}

            for work in results:
                if not isinstance(work, dict):
                    continue
                c = work.get("cited_by_count")
                if c is None or not isinstance(c, int):
                    continue

                # Map work DOI
                w_doi = _strip_doi_url(work.get("doi"))
                if w_doi:
                    counts[w_doi.lower()] = c

                # Map work PMID and other IDs
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
        except Exception:
            return {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_openalex_s2.py -k "fetch_citation_counts_batch" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/providers/openalex.py tests/test_openalex_s2.py
git commit -m "feat(openalex): add batched citation counts retrieval"
```

---

### Task 4: RankingPipeline Cascade Citation Enrichment and End-to-End Ranking

**Files:**
- Modify: `src/scholar_mcp/ranking.py`
- Modify: `tests/test_ranking.py`

**Interfaces:**
- Consumes:
  - `OpenAlexProvider`, `EuropePMCProvider`, `CrossRefProvider`, `TTLCache`, `Settings`
- Produces:
  - `RankingPipeline.enrich_citations(papers: list[PaperMetadata]) -> list[PaperMetadata]`
  - `RankingPipeline.rank_papers(papers: list[PaperMetadata], weights: RankingWeights | None = None, top_n: int = 10) -> list[PaperMetadata]`

- [ ] **Step 1: Write the failing tests**

Add tests to `tests/test_ranking.py`:

```python
@pytest.mark.asyncio
async def test_ranking_pipeline_enrich_and_rank(httpx_mock, settings):
    client = AsyncHttpClient(settings)
    cache = TTLCache(maxsize=100, ttl_seconds=3600)
    openalex = OpenAlexProvider(client)
    europe_pmc = EuropePMCProvider(client)
    crossref = CrossRefProvider(client)

    pipeline = RankingPipeline(
        openalex=openalex,
        europe_pmc=europe_pmc,
        crossref=crossref,
        cache=cache,
        settings=settings,
    )

    # Mock OpenAlex batch works response
    httpx_mock.add_response(
        url=re.compile(r"https://api\.openalex\.org/works\?.*"),
        json={
            "results": [
                {
                    "doi": "https://doi.org/10.1001/paper1",
                    "ids": {"pmid": "111"},
                    "cited_by_count": 500,
                },
                {
                    "doi": "https://doi.org/10.1001/paper2",
                    "ids": {"pmid": "222"},
                    "cited_by_count": 10,
                }
            ]
        },
        status_code=200,
    )

    candidates = [
        PaperMetadata(title="Paper 1", doi="10.1001/paper1", pmid="111", year="2020"),
        PaperMetadata(title="Paper 2", doi="10.1001/paper2", pmid="222", year="2025"),
        PaperMetadata(title="Paper 3 (No OpenAlex)", doi="10.1001/paper3", pmid="333", year="2026"),
    ]

    # Mock Europe PMC search for Paper 3 fallback
    httpx_mock.add_response(
        url=re.compile(r"https://www\.ebi\.ac\.uk/europepmc/webservices/rest/search\?.*"),
        json={
            "resultList": {
                "result": [{"doi": "10.1001/paper3", "pmid": "333", "citedByCount": 25}]
            }
        },
        status_code=200,
    )

    ranked = await pipeline.rank_papers(candidates, top_n=2)

    assert len(ranked) == 2
    assert ranked[0].score is not None
    assert ranked[1].score is not None
    assert ranked[0].score >= ranked[1].score

    # Verify cached values
    assert cache.get("cit:pmid:111") == 500
    assert cache.get("cit:pmid:222") == 10
    assert cache.get("cit:doi:10.1001/paper3") == 25
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ranking.py -k "ranking_pipeline" -v`
Expected: FAIL with `NameError: name 'RankingPipeline' is not defined`

- [ ] **Step 3: Implement `RankingPipeline` in `src/scholar_mcp/ranking.py`**

Add `RankingPipeline` class to `src/scholar_mcp/ranking.py`:

```python
import asyncio
from scholar_mcp.config import Settings
from scholar_mcp.providers.crossref import CrossRefProvider
from scholar_mcp.providers.europe_pmc import EuropePMCProvider
from scholar_mcp.providers.openalex import OpenAlexProvider
from scholar_mcp.utils.cache import TTLCache
from scholar_mcp.utils.http import AsyncHttpClient


class RankingPipeline:
    """Orchestrates candidate citation enrichment, feature extraction, and re-ranking."""

    def __init__(
        self,
        openalex: OpenAlexProvider,
        europe_pmc: EuropePMCProvider,
        crossref: CrossRefProvider,
        cache: TTLCache,
        settings: Settings | None = None,
    ) -> None:
        self.openalex = openalex
        self.europe_pmc = europe_pmc
        self.crossref = crossref
        self.cache = cache
        self.settings = settings or Settings.load()

    def _cache_key(self, identifier: str) -> str:
        return f"cit:{identifier.lower().strip()}"

    async def _fetch_epmc_citations(self, pmid: str | None, doi: str | None) -> int | None:
        query_part = f'EXT_ID:"{pmid}"' if pmid else f'DOI:"{doi}"'
        try:
            resp = await self.europe_pmc.http_client.get(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                params={"query": query_part, "format": "json", "resultType": "core"},
            )
            if resp is not None and resp.status_code == 200:
                results = resp.json().get("resultList", {}).get("result", [])
                if results and isinstance(results[0], dict):
                    c = results[0].get("citedByCount")
                    if isinstance(c, int):
                        return c
        except Exception:
            pass
        return None

    async def _fetch_crossref_citations(self, doi: str) -> int | None:
        try:
            meta = await self.crossref.fetch_metadata(doi)
            if meta and meta.citation_count is not None:
                return meta.citation_count
        except Exception:
            pass
        return None

    async def enrich_citations(self, papers: list[PaperMetadata]) -> list[PaperMetadata]:
        """Enrich candidates with citation counts via cache, OpenAlex batch, and fallback."""
        missing_indices: list[int] = []
        for idx, p in enumerate(papers):
            if p.citation_count is not None:
                continue

            # Check cache
            cached_count = None
            if p.pmid:
                cached_count = self.cache.get(self._cache_key(f"pmid:{p.pmid}"))
            if cached_count is None and p.doi:
                cached_count = self.cache.get(self._cache_key(f"doi:{p.doi}"))

            if cached_count is not None:
                p.citation_count = cached_count
            else:
                missing_indices.append(idx)

        if not missing_indices:
            return papers

        missing_dois = [papers[i].doi for i in missing_indices if papers[i].doi]
        missing_pmids = [papers[i].pmid for i in missing_indices if papers[i].pmid]

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
            count = None
            if p.doi and p.doi.lower() in oa_counts:
                count = oa_counts[p.doi.lower()]
            elif p.pmid and p.pmid in oa_counts:
                count = oa_counts[p.pmid]

            if count is not None:
                p.citation_count = count
                if p.pmid:
                    self.cache.set(self._cache_key(f"pmid:{p.pmid}"), count)
                if p.doi:
                    self.cache.set(self._cache_key(f"doi:{p.doi}"), count)
            else:
                still_missing.append(i)

        # 2. Parallel Fallback (Europe PMC / CrossRef)
        if still_missing:
            async def resolve_single_fallback(idx: int) -> tuple[int, int | None]:
                paper = papers[idx]
                c_val = await self._fetch_epmc_citations(paper.pmid, paper.doi)
                if c_val is None and paper.doi:
                    c_val = await self._fetch_crossref_citations(paper.doi)
                return idx, c_val

            tasks = [resolve_single_fallback(i) for i in still_missing]
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for res in results:
                    if isinstance(res, tuple):
                        i, c_val = res
                        final_c = c_val if c_val is not None else 0
                        papers[i].citation_count = final_c
                        if papers[i].pmid:
                            self.cache.set(self._cache_key(f"pmid:{papers[i].pmid}"), final_c)
                        if papers[i].doi:
                            self.cache.set(self._cache_key(f"doi:{papers[i].doi}"), final_c)
            except Exception:
                for i in still_missing:
                    if papers[i].citation_count is None:
                        papers[i].citation_count = 0

        # Guarantee non-None citation count
        for p in papers:
            if p.citation_count is None:
                p.citation_count = 0

        return papers

    async def rank_papers(
        self,
        papers: list[PaperMetadata],
        weights: RankingWeights | None = None,
        top_n: int = 10,
    ) -> list[PaperMetadata]:
        if not papers:
            return []

        w = weights or RankingWeights(
            relevance=self.settings.ranking_weight_relevance,
            citations=self.settings.ranking_weight_citations,
            recency=self.settings.ranking_weight_recency,
            recency_half_life_years=self.settings.ranking_recency_half_life_years,
        )

        try:
            # Enrich citations with 1.5s timeout protection
            enriched = await asyncio.wait_for(self.enrich_citations(papers), timeout=1.5)
        except Exception:
            for p in papers:
                if p.citation_count is None:
                    p.citation_count = 0
            enriched = papers

        scored = ScoringEngine.score_candidates(enriched, weights=w)
        return scored[:top_n]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ranking.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/ranking.py tests/test_ranking.py
git commit -m "feat(ranking): implement RankingPipeline with cascade citation enrichment"
```

---

### Task 5: WaterfallResolver and FastMCP Server Integration

**Files:**
- Modify: `src/scholar_mcp/resolver.py:50-80,375-460`
- Modify: `src/scholar_mcp/server.py:24-60`
- Test: `tests/test_waterfall_resolver.py`
- Test: `tests/test_server_tools.py`

**Interfaces:**
- Consumes: `RankingPipeline` in `scholar_mcp.ranking`.
- Produces:
  - `WaterfallResolver.search(..., rerank: bool = True) -> list[PaperMetadata]`
  - `search_papers(..., rerank: bool = True) -> list[dict[str, Any]]` tool.

- [ ] **Step 1: Write the failing tests**

In `tests/test_server_tools.py` and `tests/test_waterfall_resolver.py`, add tests for `rerank` parameter and score metadata:

```python
@pytest.mark.asyncio
async def test_search_papers_tool_with_reranking(httpx_mock, monkeypatch):
    # Mock PubMed search and summary
    esearch_resp = {"esearchresult": {"idlist": ["100", "200", "300"]}}
    esummary_resp = {
        "result": {
            "uids": ["100", "200", "300"],
            "100": {"title": "Paper 100", "pubdate": "2015", "authors": [{"name": "A"}], "articleids": [{"idtype": "doi", "value": "10.1001/100"}]},
            "200": {"title": "Paper 200", "pubdate": "2026", "authors": [{"name": "B"}], "articleids": [{"idtype": "doi", "value": "10.1001/200"}]},
            "300": {"title": "Paper 300", "pubdate": "2020", "authors": [{"name": "C"}], "articleids": [{"idtype": "doi", "value": "10.1001/300"}]},
        }
    }

    httpx_mock.add_response(
        url=re.compile(r"https://eutils\.ncbi\.nlm\.nih\.gov/entrez/eutils/esearch\.fcgi\?.*"),
        json=esearch_resp,
        status_code=200,
    )
    httpx_mock.add_response(
        url=re.compile(r"https://eutils\.ncbi\.nlm\.nih\.gov/entrez/eutils/esummary\.fcgi\?.*"),
        json=esummary_resp,
        status_code=200,
    )
    # OpenAlex batch
    httpx_mock.add_response(
        url=re.compile(r"https://api\.openalex\.org/works\?.*"),
        json={"results": [{"doi": "https://doi.org/10.1001/100", "cited_by_count": 1000}, {"doi": "https://doi.org/10.1001/200", "cited_by_count": 5}]},
        status_code=200,
    )
    # OA status annotation mock
    httpx_mock.add_response(
        url=re.compile(r"https://www\.ebi\.ac\.uk/europepmc/webservices/rest/search\?.*"),
        json={"resultList": {"result": []}},
        status_code=200,
    )

    results = await search_papers("cancer immunotherapy", source="pubmed", num_results=2, rerank=True)
    assert len(results) == 2
    assert "score" in results[0]
    assert results[0]["score"] is not None
    assert results[0]["ranking_metrics"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_server_tools.py -k "search_papers_tool_with_reranking" -v`
Expected: FAIL with `search_papers() got unexpected keyword argument 'rerank'` or missing score.

- [ ] **Step 3: Update `resolver.py` and `server.py`**

In `src/scholar_mcp/resolver.py`:
- Instantiate `self.ranking_pipeline = RankingPipeline(self.openalex, self.europe_pmc, self.crossref, self.cache, self.settings)` in `__init__`.
- Update `search()` signature and implementation:
```python
    async def search(
        self,
        query: str,
        source: str = "auto",
        num_results: int = 10,
        rerank: bool = True,
        author: str | None = None,
        journal: str | None = None,
        year_start: int | None = None,
        year_end: int | None = None,
    ) -> list[PaperMetadata]:
        limit = min(num_results, 50)
        source_mode = source.lower().strip()

        # Compute candidate pool depth if reranking is enabled
        should_rerank = rerank and self.settings.ranking_enabled
        if should_rerank:
            candidate_pool_size = min(
                self.settings.ranking_max_candidates,
                max(
                    limit * self.settings.ranking_candidate_multiplier,
                    self.settings.ranking_min_candidates,
                ),
            )
            fetch_limit = candidate_pool_size
        else:
            fetch_limit = limit

        if source_mode == "pubmed":
            papers = await self.pubmed.search(
                query,
                num_results=fetch_limit,
                author=author,
                journal=journal,
                year_start=year_start,
                year_end=year_end,
            )
        elif source_mode == "crossref":
            papers = await self.crossref.search(
                query,
                num_results=fetch_limit,
                author=author,
                journal=journal,
                year_start=year_start,
                year_end=year_end,
            )
        elif source_mode in ("s2", "semanticscholar"):
            if not self.settings.enable_s2:
                return []
            papers = await self.s2.search(
                query,
                num_results=fetch_limit,
                author=author,
                journal=journal,
                year_start=year_start,
                year_end=year_end,
            )
        else:  # auto
            papers = await self.pubmed.search(
                query,
                num_results=fetch_limit,
                author=author,
                journal=journal,
                year_start=year_start,
                year_end=year_end,
            )
            if len(papers) < fetch_limit:
                needed = fetch_limit - len(papers)
                crossref_papers = await self.crossref.search(
                    query,
                    num_results=needed * 2,
                    author=author,
                    journal=journal,
                    year_start=year_start,
                    year_end=year_end,
                )
                seen_dois = {p.doi.lower() for p in papers if p.doi}
                seen_titles = {p.title.lower().strip() for p in papers if p.title}
                for cp in crossref_papers:
                    if cp.doi and cp.doi.lower() in seen_dois:
                        continue
                    if cp.title and cp.title.lower().strip() in seen_titles:
                        continue
                    papers.append(cp)
                    if cp.doi:
                        seen_dois.add(cp.doi.lower())
                    if cp.title:
                        seen_titles.add(cp.title.lower().strip())
                    if len(papers) >= fetch_limit:
                        break

        # Re-rank if requested and candidates present
        if should_rerank and papers:
            papers = await self.ranking_pipeline.rank_papers(papers, top_n=limit)
        else:
            papers = papers[:limit]

        # Annotate OA status in one batched call
        await annotate_oa_status(papers, self.http_client)
        return papers
```

In `src/scholar_mcp/server.py`:
Update `search_papers` tool definition:
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
    """Search for academic papers across PubMed and CrossRef with smart re-ranking.

    Args:
        query: Search keywords or query string.
        source: 'auto' (PubMed first, top up with CrossRef), 'pubmed', 'crossref', or 's2'.
        num_results: Maximum number of results to return (max 50).
        rerank: Whether to re-rank results using citation impact, recency decay, and Z-scores (default True).
        year_start: Filter papers published in or after this year.
        year_end: Filter papers published in or before this year.
        author: Filter by author name.
        journal: Filter by journal name.
    """
    clamped_num = min(max(1, num_results), 50)
    try:
        results = await resolver.search(
            query=query,
            source=source,
            num_results=clamped_num,
            rerank=rerank,
            year_start=year_start,
            year_end=year_end,
            author=author,
            journal=journal,
        )
        return [r.to_dict() for r in results]
    except Exception as ex:
        return [{"status": "error", "error": str(ex)}]
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/resolver.py src/scholar_mcp/server.py tests/test_server_tools.py tests/test_waterfall_resolver.py
git commit -m "feat(search): wire ranking pipeline into WaterfallResolver and FastMCP server"
```
