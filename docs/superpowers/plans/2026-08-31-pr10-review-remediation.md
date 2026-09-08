# PR #10 Review Remediation

**Goal:** Fix the three findings from the 2026-08-31 review of PR #10 (query-aware reranking, evidence/journal/author signals, `check_citations` tool) plus one import-order nit.

**Architecture:** No new modules. Task 1 touches only `.gitignore`. Task 2 changes the `check_citations` verdict taxonomy inside `src/scholar_mcp/citation_check.py` and the two error-path mirrors in `src/scholar_mcp/server.py`. Task 3 extends `RankingPipeline.enrich_citations` so the author-authority signal survives cache hits by caching the last-author OpenAlex ID per paper key and the h-index per author ID.

**Tech Stack:** Python 3.11+, pytest/pytest-asyncio/respx (existing dev deps). Test runner on this machine: `.venv/bin/python -m pytest` (bare `uv run` stalls).

**Spec:** PR #10 review comment https://github.com/gustavokch/scholar-mcp/pull/10#issuecomment-5482881336

## Global Constraints

- No new runtime dependencies.
- Every changed verdict/verdict-casing must be covered by an updated or new test.
- Behavior-preserving for all existing green paths except the verdict strings named in Task 2.
- Stage only the files each task touches. Do NOT commit the locally populated `src/scholar_mcp/data/scimago_sjr.json`, `data/`, `uv.lock`, or the locally modified plan docs.

---

### Task 1: Ignore `data/raw/`

**Files:**
- Modify: `.gitignore`

**Interfaces:**
- Produces: `data/raw/` ignored by git, making the SOURCES.md claim ("create the `data/raw/` directory; it's gitignored") true.

**Steps:**

1. **Red:** `git check-ignore data/raw/scimago_journal_rank.csv` exits non-zero (nothing ignores it).
2. Add to `.gitignore`, in the OS/misc section:

```
# Scimago raw CSV download (see src/scholar_mcp/data/SOURCES.md)
data/raw/
```

3. **Green:** `git check-ignore data/raw/scimago_journal_rank.csv` prints the path and exits zero. `git status` no longer lists `data/` as untracked (the raw CSV inside it is the only content).
4. Commit: `git add .gitignore && git commit -m "fix: gitignore data/raw for the Scimago CSV download"`

---

### Task 2: `check_citations` verdict taxonomy (ERROR / NOT_FOUND / NO_TEXT)

**Files:**
- Modify: `src/scholar_mcp/citation_check.py`
- Modify: `src/scholar_mcp/server.py` (error-path verdict casing + import order)
- Test: `tests/test_citation_check.py`, `tests/test_server_tools.py`

**Interfaces:**
- Produces: verdict values `SUPPORTED | WEAK | UNSUPPORTED | NOT_FOUND | NO_TEXT | ERROR` (all uppercase).
  - `ERROR` — resolver raised (network failure, malformed identifier), or batch exceeded `MAX_CLAIMS`, or the server-level catch fired. Retry may succeed.
  - `NOT_FOUND` — the identifier resolved to nothing (metadata `None`, or deep fetch returned no usable content).
  - `NO_TEXT` — the paper resolved but has no abstract (non-deep mode); caller should retry with `deep=True`.

**Step 1: Write the failing tests**

In `tests/test_citation_check.py`:

- Change `test_check_citations_isolated_failure` to assert `results[0]["verdict"] == "ERROR"` and `"boom" in results[0]["error"]`.
- Change `test_check_citations_batch_cap` to assert `results[0]["verdict"] == "ERROR"`.
- Add:

```python
async def test_check_citations_no_abstract_returns_no_text(settings):
    resolver = _FakeResolver(
        settings,
        metadata_by_id={
            "10.1/noabs": PaperMetadata(title="Paper Without Abstract", abstract="")
        },
    )
    results = await check_citations(
        resolver,
        [{"text": "Metformin reduced HbA1c.", "identifier": "10.1/noabs"}],
    )
    assert results[0]["verdict"] == "NO_TEXT"
    assert results[0]["resolved_title"] == "Paper Without Abstract"
```

In `tests/test_server_tools.py`:

- Change `test_check_citations_tool_batch_cap` to assert `results[0]["verdict"] == "ERROR"`.

**Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_citation_check.py tests/test_server_tools.py -v`
Expected: the four changed/new tests FAIL (current code returns `NOT_FOUND` for exceptions and empty abstracts, `"error"` for the cap).

**Step 3: Implement**

In `src/scholar_mcp/citation_check.py`:

- `_error_result`: verdict becomes `"ERROR"`.
- The batch-cap result in `check_citations`: verdict becomes `"ERROR"`.
- `_resolve_claim_source` non-deep branch: return a third element or restructure so the caller can distinguish "no metadata" from "metadata without abstract". Minimal shape: return `(title, abstract_text, mode)` where mode is `"not_found" | "no_text" | "ok"`, and `check_claim` maps mode to `NOT_FOUND` / `NO_TEXT` / scoring. Deep branch: contentless response stays `NOT_FOUND`.
- Keep the deep path's `found` logic unchanged in effect.

In `src/scholar_mcp/server.py`:

- The `except` handler's fallback dict: verdict becomes `"ERROR"`.
- Move `from scholar_mcp import citation_check` above `from scholar_mcp.config import Settings` (alphabetical).

**Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_citation_check.py tests/test_server_tools.py -v`

**Step 5: Commit**

```bash
git add src/scholar_mcp/citation_check.py src/scholar_mcp/server.py tests/test_citation_check.py tests/test_server_tools.py
git commit -m "fix: distinguish ERROR/NOT_FOUND/NO_TEXT verdicts in check_citations"
```

---

### Task 3: Author-authority signal survives warm-cache searches

**Files:**
- Modify: `src/scholar_mcp/ranking.py` (`enrich_citations`)
- Test: `tests/test_ranking.py`

**Interfaces:**
- Produces: cache keys `cit:la:pmid:<pmid>` / `cit:la:doi:<doi>` (last-author OpenAlex ID, via the existing `_cache_key` prefix) and `cit:ah:<author_id>` (h-index). `enrich_citations` populates `last_author_h_index` identically on cold and warm runs.

**Step 1: Write the failing test**

Add to `tests/test_ranking.py` (mirror the fixtures of `test_ranking_pipeline_enrich_and_rank` — real `TTLCache`, respx-mocked OpenAlex):

```python
@respx.mock
async def test_enrich_citations_warm_cache_keeps_authority():
    settings = Settings()
    client = AsyncHttpClient(settings)
    cache = TTLCache(maxsize=100, ttl_seconds=3600)
    pipeline = RankingPipeline(
        openalex=OpenAlexProvider(client),
        europe_pmc=EuropePMCProvider(client),
        crossref=CrossRefProvider(client),
        cache=cache,
        settings=settings,
    )

    respx.get(url__startswith=f"{OPENALEX_BASE}/works?").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "doi": "https://doi.org/10.1001/auth1",
                        "ids": {"pmid": "777"},
                        "cited_by_count": 300,
                        "authorships": [
                            {"author": {"id": "https://openalex.org/A9999"}},
                            {"author": {"id": "https://openalex.org/A8888"}},
                        ],
                    }
                ]
            },
        )
    )
    respx.get(url__startswith=f"{OPENALEX_BASE}/authors?").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "https://openalex.org/A8888", "summary_stats": {"h_index": 41}}
                ]
            },
        )
    )

    try:
        cold = [PaperMetadata(title="Authority Paper", doi="10.1001/auth1", pmid="777", year="2024")]
        cold = await pipeline.enrich_citations(cold)
        assert cold[0].last_author_h_index == 41
        assert await cache.get("cit:ah:A8888") == 41

        warm = [PaperMetadata(title="Authority Paper", doi="10.1001/auth1", pmid="777", year="2024")]
        warm = await pipeline.enrich_citations(warm)
        assert warm[0].citation_count == 300
        assert warm[0].last_author_h_index == 41
    finally:
        await client.aclose()
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ranking.py -k warm_cache_keeps_authority -v`
Expected: FAIL — `warm[0].last_author_h_index` is `None` (cache-hit path skips last-author lookup and the `if not missing_indices: return papers` early-return skips the h-index fetch).

**Step 3: Implement in `enrich_citations`**

- In the initial per-paper cache-hit loop: after setting `p.citation_count` from cache, also look up `la:pmid:<pmid>` then `la:doi:<doi>`; if an author ID string comes back, record `last_author_ids[idx] = author_id`. Hoist `last_author_ids` above this loop.
- In the OpenAlex network path: when an entry carries `last_author_id`, `cache.set` it under the same `pmid:`/`doi:` key shapes prefixed `la:` (only for the keys the count itself was cached under).
- Replace `if not missing_indices: return papers` with a guard that skips only the OpenAlex batch + fallback sections, so the h-index section still runs when `last_author_ids` came from cache.
- In the h-index section: read `ah:<author_id>` from cache per unique author; batch-fetch only the uncached IDs; `cache.set` each fetched h-index; apply to papers as today.

**Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ranking.py -v` (whole file — guards the refactor).

**Step 5: Commit**

```bash
git add src/scholar_mcp/ranking.py tests/test_ranking.py
git commit -m "fix: cache last-author ID and h-index so authority signal survives warm cache"
```

---

### Task 4: Full-suite gate and push

1. Run: `.venv/bin/python -m pytest` — must be 100% green.
2. `git push origin feat/rerank-and-citation-check`.
3. Confirm the locally populated `scimago_sjr.json`, `data/`, `uv.lock`, and untracked plan docs were NOT pushed (they are uncommitted).
