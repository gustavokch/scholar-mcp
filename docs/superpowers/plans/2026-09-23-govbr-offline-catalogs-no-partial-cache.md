# gov.br Offline Catalogs and No Partial Caching — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the gov.br PCDT and A-Z catalogs offline-seed only, and make sure no partial retrieval is ever cached, in memory or on disk.

**Architecture:** `get_catalog()` in both gov.br engines serves memory, then the bundled seed, and stops: no network, no SQLite catalog row. `refresh_catalog()` becomes an offline-only crawler that returns `(catalog, complete)`; one script writes a seed only from a complete crawl. In `brazil_moh.py`, a gov.br stage timeout is classified, and every cache write that held a partial result (degraded chain merge, browser-cleared errors, abstract fallback) is removed.

**Tech Stack:** Python 3.10+, asyncio, pytest (`asyncio_mode = "auto"`), respx/httpx, SQLite via `SQLiteCacheManager`.

**Spec:** `docs/superpowers/specs/2026-09-23-govbr-offline-catalogs-no-partial-cache-design.md`

## Global Constraints

- Invariant: "A partial retrieval is never cached, in memory or on disk."
- Work in `/Users/gus/Git/scholar-mcp/.worktrees/govbr-stage-budget` on branch `fix/govbr-stage-budget`. Never `cd` to the main checkout.
- Test command (the worktree has no venv of its own; the main checkout's venv has no pytest):
  `PYTEST='env PYTHONPATH=/Users/gus/Git/scholar-mcp/.worktrees/govbr-stage-budget/src /Users/gus/Git/scholar-mcp/.worktrees/feat-warp-proxy/.venv/bin/pytest'`
  Use `$PYTEST ... -p no:randomly` while iterating.
- Verification is offline only. Never run tests marked `network`.
- Stage explicit paths only. Never `git add -A` or `git add .`. Run `git status --short` before each commit.
- Never `git stash` bare; prefer a WIP commit.
- Do not touch either seed JSON file (`src/scholar_mcp/data/govbr_*_catalog.json`).
- `MIN_EXPECTED_ROWS = 50`, `MIN_CATALOG_RETENTION = 0.5`, `MAX_PAGES_PER_FOLDER = 25` keep their values.
- Every new or changed test must be seen to fail before the fix, and must fail again when the guard it targets is disabled (the "discrimination check" step in each task). Restore the guard after the check.
- Offline baseline before this work: 1004 passed, 8 deselected (~530 s).

---

### Task 1: PCDT `get_catalog` serves the seed only

**Files:**
- Modify: `src/scholar_mcp/medical/govbr_pcdt.py` (`_merge_extended` docstring ~L97-104; `get_catalog` ~L235-282)
- Test: `tests/medical/test_govbr_pcdt.py`

**Interfaces:**
- Produces: `GovBrPCDTEngine.get_catalog() -> dict[str, dict[str, Any]]`. Returns seed merged with the extended corpus, or `{}` when the seed is missing/empty. Never touches `http_client` or the `govbr_pcdt:catalog` cache key. Sets `_memory_catalog` only to a non-empty seed.

- [ ] **Step 1: Write the failing tests**

In `tests/medical/test_govbr_pcdt.py`, change the imports at the top to:

```python
import json
from unittest.mock import AsyncMock

import pytest

from scholar_mcp.config import Settings
from scholar_mcp.medical import govbr_pcdt
from scholar_mcp.medical.govbr_common import (
    CACHE_SCHEMA,
    SEVEN_DAYS_SECONDS,
    normalize_text,
)
from scholar_mcp.medical.govbr_pcdt import (
    GovBrPCDTEngine,
    load_seed_catalog,
    parse_letter_page,
)
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager
```

Delete the whole `test_pcdt_7_day_cache_refresh` test (it forces an unreachable stale branch by mocking `cache.get`).

Append:

```python
def _pcdt_engine(tmp_path):
    settings = Settings()
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    return GovBrPCDTEngine(http_client=AsyncMock(), cache=cache, settings=settings), cache


async def test_get_catalog_serves_seed_without_network_or_cache_row(tmp_path):
    """The bundled seed is the catalog: no crawl and no SQLite copy of it."""
    engine, cache = _pcdt_engine(tmp_path)
    try:
        catalog = await engine.get_catalog()

        assert "pcdt-acromegalia" in catalog
        engine.http_client.get.assert_not_awaited()
        _, meta = await cache.get("govbr_pcdt:catalog")
        assert meta.cached is False, "the seed must not be copied into SQLite"
    finally:
        await cache.close()


async def test_get_catalog_seed_beats_a_leftover_cache_row(tmp_path):
    """A catalog row written by an older release must not shadow the seed."""
    engine, cache = _pcdt_engine(tmp_path)
    try:
        await cache.set(
            "govbr_pcdt:catalog",
            {"pcdt-old": {"record_id": "pcdt-old", "slug": "old", "title": "Old"}},
            source="govbr_pcdt",
            ttl=SEVEN_DAYS_SECONDS,
        )

        catalog = await engine.get_catalog()

        assert "pcdt-old" not in catalog
        assert "pcdt-acromegalia" in catalog
    finally:
        await cache.close()


async def test_missing_seed_is_an_outage_not_a_partial_catalog(tmp_path, monkeypatch):
    """Extended rows alone are a partial catalog: report the outage instead."""
    monkeypatch.setattr(govbr_pcdt, "load_seed_catalog", lambda: {})
    engine, cache = _pcdt_engine(tmp_path)
    try:
        catalog = await engine.get_catalog()

        assert catalog == {}
        assert engine._memory_catalog is None
        engine.http_client.get.assert_not_awaited()

        results, meta = await engine.search("acromegalia", limit=5)
        assert results == []
        assert meta.error is True
        assert meta.error_kind == "backend_error"
        key = f"govbr_pcdt_search:{CACHE_SCHEMA}:5:{normalize_text('acromegalia')}"
        _, cache_meta = await cache.get(key)
        assert cache_meta.cached is False
    finally:
        await cache.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/medical/test_govbr_pcdt.py -p no:randomly -q -k "seed or outage"`
Expected: 3 FAIL. `serves_seed...` fails on `meta.cached is False` (main copies the seed into SQLite). `seed_beats...` fails on `"pcdt-old" not in catalog`. `missing_seed...` fails on `catalog == {}` (main returns the extended corpus).

- [ ] **Step 3: Implement**

Replace the `_merge_extended` docstring:

```python
def _merge_extended(base: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return a NEW dict of ``base`` plus the extended corpus rows.

    The in-memory catalog stays base-only: the merged result is never
    stored, so the seed and the extended corpus stay separate sources.
    """
    return {**base, **load_extended_catalog()}
```

Replace the whole `get_catalog` method with:

```python
    async def get_catalog(self) -> dict[str, dict[str, Any]]:
        """Get the PCDT catalog from memory or the bundled seed.

        The seed is the catalog: no search crawls gov.br, and nothing here
        touches the network or the SQLite cache.
        ``scripts/update_govbr_catalogs.py --catalog pcdt`` regenerates the
        seed offline. Every path returns a NEW merged dict (base plus the
        extended corpus); the stored base is never mutated.

        A missing or empty seed returns ``{}``, without the extended corpus
        and without being kept: extended rows alone are a partial catalog,
        and ``search`` must report the outage instead of a success over them.
        """
        if self._memory_catalog:
            return _merge_extended(self._memory_catalog)

        seed = load_seed_catalog()
        if not seed:
            logger.error("PCDT seed catalog is missing or empty; reporting an outage")
            return {}
        self._memory_catalog = seed
        return _merge_extended(seed)
```

`load_seed_catalog` must be looked up at call time through the module global (it already is: `get_catalog` calls the module-level name), so the `monkeypatch.setattr(govbr_pcdt, "load_seed_catalog", ...)` in the test takes effect.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/medical/test_govbr_pcdt.py -p no:randomly -q`
Expected: all PASS.

- [ ] **Step 5: Discrimination check**

Temporarily change `return {}` in the missing-seed branch to `return _merge_extended({})`. Run `$PYTEST tests/medical/test_govbr_pcdt.py -p no:randomly -q -k outage`. Expected: FAIL. Restore. Temporarily add `await self.cache.set("govbr_pcdt:catalog", seed, source="govbr_pcdt")` before `return _merge_extended(seed)`. Run `-k serves_seed`. Expected: FAIL. Restore.

- [ ] **Step 6: Commit**

```bash
git status --short
git add src/scholar_mcp/medical/govbr_pcdt.py tests/medical/test_govbr_pcdt.py
git commit -m "fix(govbr_pcdt): serve the bundled seed only, never a partial catalog"
```

---

### Task 2: A-Z `get_catalog` serves the seed only

**Files:**
- Modify: `src/scholar_mcp/medical/govbr_az.py` (`get_catalog` ~L353-400)
- Test: `tests/medical/test_govbr_az.py`

**Interfaces:**
- Produces: `GovBrAZEngine.get_catalog() -> dict[str, dict[str, Any]]`. Returns `_memory_catalog` if set, else the seed (and keeps it), else `{}` (not kept). Never touches `http_client` or the `govbr_az:catalog` key.

- [ ] **Step 1: Write the failing tests**

In `tests/medical/test_govbr_az.py`, delete the whole `test_get_catalog_memoizes_partial_refresh` test.

Change the import block in the middle of the file to:

```python
from unittest.mock import AsyncMock

import pytest

from scholar_mcp.config import Settings
from scholar_mcp.medical import govbr_az
from scholar_mcp.medical.govbr_az import GovBrAZEngine
from scholar_mcp.medical.govbr_common import CACHE_SCHEMA, SEVEN_DAYS_SECONDS
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager
```

(`CacheMetadata` was used only by the deleted test. If anything else in the file still uses it, keep it in the import.)

Append:

```python
async def test_get_catalog_serves_seed_without_network_or_cache_row(tmp_path, responses):
    """The bundled seed is the catalog: no crawl and no SQLite copy of it."""
    engine, http = _make_engine(tmp_path, responses)
    try:
        catalog = await engine.get_catalog()

        assert catalog == load_seed_catalog()
        http.get.assert_not_awaited()
        _, meta = await engine.cache.get("govbr_az:catalog")
        assert meta.cached is False, "the seed must not be copied into SQLite"
    finally:
        await engine.cache.close()


async def test_get_catalog_seed_beats_a_leftover_cache_row(tmp_path, responses):
    """A catalog row written by an older release must not shadow the seed."""
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await engine.cache.set(
            "govbr_az:catalog",
            {"old": {"record_id": "old"}},
            source="govbr_az",
            ttl=SEVEN_DAYS_SECONDS,
        )

        catalog = await engine.get_catalog()

        assert "old" not in catalog
        assert catalog == load_seed_catalog()
    finally:
        await engine.cache.close()


async def test_missing_seed_is_an_outage_and_is_not_kept(tmp_path, responses, monkeypatch):
    monkeypatch.setattr(govbr_az, "load_seed_catalog", lambda: {})
    engine, http = _make_engine(tmp_path, responses)
    try:
        catalog = await engine.get_catalog()

        assert catalog == {}
        assert engine._memory_catalog is None
        http.get.assert_not_awaited()

        results, meta = await engine.search("dengue", limit=5)
        assert results == []
        assert meta.error_kind == "backend_error"
        key = f"govbr_az_search:{CACHE_SCHEMA}:5:dengue"
        _, cache_meta = await engine.cache.get(key)
        assert cache_meta.cached is False
    finally:
        await engine.cache.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/medical/test_govbr_az.py -p no:randomly -q -k "seed or outage"`
Expected: 3 FAIL. `serves_seed...` on `meta.cached is False`; `seed_beats...` on `"old" not in catalog`; `missing_seed...` on `http.get.assert_not_awaited()` (main crawls the fixture pages when the seed is missing).

- [ ] **Step 3: Implement**

Replace the whole `get_catalog` method of `GovBrAZEngine` with:

```python
    async def get_catalog(self) -> dict[str, dict[str, Any]]:
        """Get the catalog from memory or the bundled seed.

        The seed is the catalog: no search crawls gov.br, and nothing here
        touches the network or the SQLite cache.
        ``scripts/update_govbr_catalogs.py --catalog az`` regenerates the
        seed offline. A missing or empty seed returns ``{}`` and is not
        kept, so ``search`` reports the outage.
        """
        if self._memory_catalog:
            return self._memory_catalog

        seed = load_seed_catalog()
        if not seed:
            logger.error(
                "gov.br A-Z seed catalog is missing or empty; reporting an outage"
            )
            return {}
        self._memory_catalog = seed
        return seed
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/medical/test_govbr_az.py -p no:randomly -q`
Expected: all PASS. (The search tests still prime the catalog through `refresh_catalog`, which still sets `_memory_catalog` until Task 3.)

- [ ] **Step 5: Discrimination check**

Temporarily replace `return {}` in the missing-seed branch with `return await self.refresh_catalog()`. Run `-k outage`. Expected: FAIL on `http.get.assert_not_awaited()`. Restore. Temporarily add `await self.cache.set("govbr_az:catalog", seed, source="govbr_az")` before `return seed`. Run `-k serves_seed`. Expected: FAIL. Restore.

- [ ] **Step 6: Commit**

```bash
git status --short
git add src/scholar_mcp/medical/govbr_az.py tests/medical/test_govbr_az.py
git commit -m "fix(govbr_az): serve the bundled seed only, never crawl on a search"
```

---

### Task 3: A-Z `refresh_catalog` is offline-only and reports completeness

**Files:**
- Modify: `src/scholar_mcp/medical/govbr_common.py` (add `MIN_CATALOG_RETENTION` after `SEVEN_DAYS_SECONDS`)
- Modify: `src/scholar_mcp/medical/govbr_az.py` (imports; remove `CATALOG_CACHE_KEY` and the local `MIN_CATALOG_RETENTION`; `_load_aliases`; `_crawl_folder`; `refresh_catalog`)
- Test: `tests/medical/test_govbr_az.py`

**Interfaces:**
- Produces: `govbr_common.MIN_CATALOG_RETENTION = 0.5` (re-exported by `govbr_az`).
- Produces: `GovBrAZEngine.refresh_catalog(incumbent: dict | None = None) -> tuple[dict[str, dict[str, Any]], bool]`. Never sets `_memory_catalog`, never writes the cache.
- Produces: `GovBrAZEngine._load_aliases() -> tuple[dict[str, str], bool]`.
- `_crawl_folder(...) -> tuple[dict, bool]` keeps its signature; `ok` now means "every visited page loaded and nothing is left queued".

- [ ] **Step 1: Write the failing tests and update the existing ones**

Add this helper right after `_make_engine`:

```python
async def _prime(engine):
    """Install a crawled fixture catalog as the in-memory catalog.

    refresh_catalog is offline-only and never sets it; search tests need a
    catalog built from the fixture pages, not the bundled seed.
    """
    catalog, complete = await engine.refresh_catalog()
    assert complete
    engine._memory_catalog = catalog
```

In each of these tests, replace the line `await engine.refresh_catalog()` with `await _prime(engine)`:
`test_search_ranks_exact_topic_match_first`, `test_search_returns_empty_for_blank_query`, `test_search_respects_limit`, `test_search_uses_cache_on_second_call`, `test_get_guideline_by_record_id`, `test_get_guideline_by_slug`, `test_get_guideline_unknown_returns_none`, `test_guias_records_carry_year`, `test_az_search_pre_v1_cache_row_is_not_served`.

Replace these existing tests in full:

```python
async def test_refresh_catalog_indexes_both_trees(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        catalog, complete = await engine.refresh_catalog()

        assert complete is True
        assert "govbr-svsa-dengue-dengue-manejo-clinico" in catalog
        assert "govbr-svsa-tuberculose-manual-tuberculose" in catalog
        assert "govbr-guias-2024-guia-vigilancia" in catalog

        row = catalog["govbr-svsa-dengue-dengue-manejo-clinico"]
        assert row["download_url"].endswith("/@@download/file")
        assert row["tree"] == "svsa"
        assert row["topic"] == "dengue"
        assert "dengue" in row["aliases"]
        assert catalog["govbr-guias-2024-guia-vigilancia"]["year"] == "2024"
    finally:
        await engine.cache.close()


async def test_refresh_catalog_caches_and_keeps_nothing(tmp_path, responses):
    """Offline-only: even a complete crawl is neither cached nor kept."""
    engine, _ = _make_engine(tmp_path, responses)
    try:
        _, complete = await engine.refresh_catalog()

        assert complete is True
        _, meta = await engine.cache.get("govbr_az:catalog")
        assert meta.cached is False
        assert engine._memory_catalog is None
    finally:
        await engine.cache.close()


async def test_refresh_catalog_failed_folder_is_incomplete(tmp_path, responses):
    responses[f"https://www.gov.br{SVSA}/tuberculose"] = FakeResponse("", status_code=503)
    engine, _ = _make_engine(tmp_path, responses)
    try:
        catalog, complete = await engine.refresh_catalog()

        assert "govbr-svsa-dengue-dengue-manejo-clinico" in catalog
        assert complete is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_login_gated_folder_is_incomplete(tmp_path, responses):
    responses[f"https://www.gov.br{SVSA}/tuberculose"] = FakeResponse(LOGIN_GATE)
    engine, _ = _make_engine(tmp_path, responses)
    try:
        catalog, complete = await engine.refresh_catalog()

        assert not any(key.startswith("govbr-svsa-tuberculose") for key in catalog)
        assert complete is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_follows_pagination_once_per_url(tmp_path, responses):
    folder = f"https://www.gov.br{SVSA}/dengue"
    page_two = _listing("dengue-boletim", "Boletim da Dengue", f"{SVSA}/dengue")
    responses[f"{folder}?b_start:int=20"] = FakeResponse(page_two)
    responses[folder] = FakeResponse(
        _listing("dengue-manejo-clinico", "Dengue: manejo", f"{SVSA}/dengue")
        + f'<a href="{folder}?b_start:int=20">2</a>'
    )
    engine, http = _make_engine(tmp_path, responses)
    try:
        catalog, complete = await engine.refresh_catalog()

        assert complete is True
        assert "govbr-svsa-dengue-dengue-boletim" in catalog
        urls = [call.args[0] for call in http.get.await_args_list]
        assert len(urls) == len(set(urls))
    finally:
        await engine.cache.close()


async def test_refresh_catalog_refuses_a_shrunken_crawl(tmp_path, responses):
    """A parser break is not a smaller site: a crawl below the retention
    floor of the incumbent is incomplete even when every page answered."""
    engine, _ = _make_engine(tmp_path, responses)
    try:
        incumbent = {f"row-{i}": {"record_id": f"row-{i}"} for i in range(100)}

        catalog, complete = await engine.refresh_catalog(incumbent=incumbent)

        assert catalog, "the crawl is still returned to the caller"
        assert complete is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_accepts_a_crawl_that_holds_its_size(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        incumbent = {"row-0": {"record_id": "row-0"}, "row-1": {"record_id": "row-1"}}

        _, complete = await engine.refresh_catalog(incumbent=incumbent)

        assert complete is True
    finally:
        await engine.cache.close()
```

Delete the old versions: `test_refresh_catalog_caches_full_crawl`, `test_refresh_catalog_does_not_cache_partial_crawl`, `test_refresh_catalog_skips_login_gated_folders`, `test_refresh_catalog_refuses_to_pin_a_shrunken_crawl`, `test_refresh_catalog_pins_a_crawl_that_holds_its_size`.

Append the new completeness tests:

```python
async def test_refresh_catalog_failed_second_page_is_incomplete(tmp_path, responses):
    """One loaded page is not a loaded folder."""
    folder = f"https://www.gov.br{SVSA}/dengue"
    responses[f"{folder}?b_start:int=20"] = FakeResponse("", status_code=503)
    responses[folder] = FakeResponse(
        _listing("dengue-manejo-clinico", "Dengue: manejo", f"{SVSA}/dengue")
        + f'<a href="{folder}?b_start:int=20">2</a>'
    )
    engine, _ = _make_engine(tmp_path, responses)
    try:
        catalog, complete = await engine.refresh_catalog()

        assert "govbr-svsa-dengue-dengue-manejo-clinico" in catalog
        assert complete is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_page_cap_is_incomplete(tmp_path, responses, monkeypatch):
    """A folder cut off by MAX_PAGES_PER_FOLDER with pages still queued is
    a truncated folder, not a complete one."""
    monkeypatch.setattr(govbr_az, "MAX_PAGES_PER_FOLDER", 2)
    folder = f"https://www.gov.br{SVSA}/dengue"
    pages = [folder] + [f"{folder}?b_start:int={20 * i}" for i in range(1, 4)]
    for i, url in enumerate(pages[:-1]):
        responses[url] = FakeResponse(
            _listing(f"dengue-{i}", f"Dengue parte {i}", f"{SVSA}/dengue")
            + f'<a href="{pages[i + 1]}">{i + 2}</a>'
        )
    engine, _ = _make_engine(tmp_path, responses)
    try:
        _, complete = await engine.refresh_catalog()

        assert complete is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_failed_alias_index_is_incomplete(tmp_path, responses):
    """Rows crawled without the alias vocabulary lose their alias text."""
    responses["https://www.gov.br/saude/pt-br/assuntos/saude-de-a-a-z"] = FakeResponse(
        "", status_code=503
    )
    engine, _ = _make_engine(tmp_path, responses)
    try:
        catalog, complete = await engine.refresh_catalog()

        assert catalog
        assert complete is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_failed_alias_letter_is_incomplete(tmp_path, responses):
    responses["https://www.gov.br/saude/pt-br/assuntos/saude-de-a-a-z/t"] = FakeResponse(
        "", status_code=503
    )
    engine, _ = _make_engine(tmp_path, responses)
    try:
        _, complete = await engine.refresh_catalog()

        assert complete is False
    finally:
        await engine.cache.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/medical/test_govbr_az.py -p no:randomly -q`
Expected: every test that unpacks `catalog, complete = ...` or uses `_prime` FAILS with `ValueError: too many values to unpack` (main returns a dict). That shows the new contract is not in place yet.

- [ ] **Step 3: Implement**

In `src/scholar_mcp/medical/govbr_common.py`, directly after `SEVEN_DAYS_SECONDS = ...`, add:

```python
# A crawl that returns less than this fraction of the catalog already in
# hand is treated as a parser break, not as a smaller site, and is never
# accepted as complete.
MIN_CATALOG_RETENTION = 0.5
```

In `src/scholar_mcp/medical/govbr_az.py`:

1. In the `govbr_common` import, remove `SEVEN_DAYS_SECONDS` and add `MIN_CATALOG_RETENTION`:

```python
from scholar_mcp.medical.govbr_common import (
    CACHE_SCHEMA,
    GOVBR_HEADERS,
    MIN_CATALOG_RETENTION,
    is_login_redirect,
    normalize_text,
    parse_folder_index,
    parse_listing_page,
    score_item,
    tokenize_portuguese,
)
```

2. Delete the line `CATALOG_CACHE_KEY = "govbr_az:catalog"`.

3. Delete the local `MIN_CATALOG_RETENTION` block (its 4-line comment and `MIN_CATALOG_RETENTION = 0.5`).

4. Replace `_load_aliases`:

```python
    async def _load_aliases(self) -> tuple[dict[str, str], bool]:
        """Build the disease alias vocabulary from the A-Z index.

        Returns ``(aliases, ok)``. ``ok`` is False when the index or any
        letter page failed, or the index listed no letters: rows crawled
        without the full vocabulary lose alias text, which is a partial
        catalog.
        """
        index_html = await self._fetch_html(AZ_INDEX_URL)
        if not index_html:
            return {}, False
        letter_urls = parse_az_index(index_html)
        ok = bool(letter_urls)
        aliases: dict[str, str] = {}
        for letter_url in letter_urls:
            letter_html = await self._fetch_html(letter_url)
            if not letter_html:
                ok = False
                continue
            aliases.update(parse_az_letter_page(letter_html))
        return aliases, ok
```

5. In `_crawl_folder`, change the docstring, the failure bookkeeping and the return. The full method:

```python
    async def _crawl_folder(
        self,
        tree: str,
        folder_url: str,
        patterns: list[tuple[str, re.Pattern[str], str, re.Pattern[str] | None]],
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """Crawl one folder with pagination. Returns (rows, ok).

        ``ok`` means every visited page loaded and no page was left queued
        at the page cap. One loaded page is not a loaded folder.
        """
        topic = folder_url.rstrip("/").rsplit("/", 1)[-1]
        rows: dict[str, dict[str, Any]] = {}
        queue = [folder_url]
        visited: set[str] = set()
        failed = False

        while queue and len(visited) < MAX_PAGES_PER_FOLDER:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            html = await self._fetch_html(url)
            if html is None:
                failed = True
                continue
            items, next_urls = parse_listing_page(html, url)
            for item in items:
                record_id = f"govbr-{tree}-{topic}-{item['slug']}"
                rows[record_id] = {
                    "record_id": record_id,
                    "slug": item["slug"],
                    "title": item["title"],
                    "description": item["description"],
                    "tree": tree,
                    "topic": topic,
                    "year": topic if tree == "guias" and topic.isdigit() else "",
                    "aliases": build_alias_text_compiled(
                        f"{item['title']} {topic}", patterns
                    ),
                    "view_url": item["view_url"],
                    "download_url": item["download_url"],
                }
            for nurl in next_urls:
                if nurl not in visited and nurl not in queue:
                    queue.append(nurl)

        if queue:
            logger.warning(
                "gov.br A-Z folder %s stopped at the %d-page cap with %d pages queued",
                folder_url,
                MAX_PAGES_PER_FOLDER,
                len(queue),
            )
        return rows, not failed and not queue
```

6. Replace `refresh_catalog`:

```python
    async def refresh_catalog(
        self, incumbent: dict[str, dict[str, Any]] | None = None
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """Crawl both publication trees from gov.br. Offline use only.

        Returns ``(catalog, complete)``. ``complete`` is True only when the
        alias vocabulary, both tree indexes, and every page of every folder
        loaded, no folder stopped at the page cap, and the catalog holds at
        least ``MIN_CATALOG_RETENTION`` of ``incumbent`` (a crawl that finds
        a fraction of the known size is a parser break, not a smaller site).

        Nothing is cached or kept in memory here. The server never crawls:
        ``scripts/update_govbr_catalogs.py`` writes a complete crawl to the
        bundled seed and refuses anything else.
        """
        aliases, complete = await self._load_aliases()
        if not complete:
            logger.warning("gov.br A-Z alias vocabulary is incomplete")
        # Compiled once here and reused for every item in every folder.
        patterns = compile_alias_patterns(aliases)
        catalog: dict[str, dict[str, Any]] = {}

        for tree, tree_url, tree_path in _TREES:
            index_html = await self._fetch_html(tree_url)
            if index_html is None:
                logger.warning("gov.br A-Z tree index unavailable: %s", tree_url)
                complete = False
                continue
            folder_urls = parse_folder_index(index_html, tree_url, tree_path)
            if not folder_urls:
                logger.warning("gov.br A-Z tree index lists no folders: %s", tree_url)
                complete = False
            for folder_url in folder_urls:
                rows, ok = await self._crawl_folder(tree, folder_url, patterns)
                catalog.update(rows)
                complete = complete and ok

        size_floor = int(len(incumbent or {}) * MIN_CATALOG_RETENTION)
        if not catalog or len(catalog) < size_floor:
            logger.warning(
                "gov.br A-Z crawl returned %d rows against %d already held; "
                "incomplete (suspected parser break)",
                len(catalog),
                len(incumbent or {}),
            )
            complete = False
        return catalog, complete
```

7. Confirm nothing else in `govbr_az.py` still references `CATALOG_CACHE_KEY` or `SEVEN_DAYS_SECONDS`:

Run: `grep -n "CATALOG_CACHE_KEY\|SEVEN_DAYS_SECONDS" src/scholar_mcp/medical/govbr_az.py`
Expected: no output.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/medical/test_govbr_az.py -p no:randomly -q`
Expected: all PASS.

- [ ] **Step 5: Discrimination check**

Run each mutation, confirm the named test FAILS, then restore:
- In `_crawl_folder`, return `rows, bool(visited)` → `failed_second_page_is_incomplete` and `page_cap_is_incomplete` fail.
- In `refresh_catalog`, replace `aliases, complete = await self._load_aliases()` with `aliases, _ = await self._load_aliases(); complete = True` → both alias tests fail.
- Delete the `len(catalog) < size_floor` condition → `refuses_a_shrunken_crawl` fails.

- [ ] **Step 6: Commit**

```bash
git status --short
git add src/scholar_mcp/medical/govbr_common.py src/scholar_mcp/medical/govbr_az.py tests/medical/test_govbr_az.py
git commit -m "fix(govbr_az): offline-only crawl that reports completeness per page"
```

---

### Task 4: PCDT `refresh_catalog` is offline-only and reports completeness

**Files:**
- Modify: `src/scholar_mcp/medical/govbr_pcdt.py` (imports; `refresh_catalog` ~L284-326)
- Test: `tests/medical/test_govbr_pcdt.py`

**Interfaces:**
- Consumes: `govbr_common.MIN_CATALOG_RETENTION` (Task 3).
- Produces: `GovBrPCDTEngine.refresh_catalog(incumbent: dict | None = None) -> tuple[dict[str, dict[str, Any]], bool]`. Never sets `_memory_catalog`, never writes the cache.

- [ ] **Step 1: Write the failing tests and update the existing ones**

Replace `test_refresh_total_crawl_failure_caches_nothing` and `test_refresh_partial_crawl_not_cached` in full, and append the new tests:

```python
class _Resp:
    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


def _letter_page(letter: str, extra: str = "") -> _Resp:
    return _Resp(
        '<div id="content-core">'
        f'<a href="https://www.gov.br/saude/pt-br/assuntos/pcdt/{letter}/cond-{letter}/view">'
        f"Condicao {letter.upper()}</a>{extra}</div>"
    )


def _crawl_engine(tmp_path, get):
    settings = Settings()
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    http = AsyncMock()
    http.get.side_effect = get
    return GovBrPCDTEngine(http_client=http, cache=cache, settings=settings), cache


def _letter_of(url: str) -> str:
    return url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]


@pytest.mark.asyncio
async def test_refresh_total_crawl_failure_is_incomplete(tmp_path):
    async def get(url, **kwargs):
        return None

    engine, cache = _crawl_engine(tmp_path, get)
    try:
        catalog, complete = await engine.refresh_catalog()
        assert catalog == {}
        assert complete is False
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_refresh_partial_crawl_is_incomplete(tmp_path):
    async def get(url, **kwargs):
        return _letter_page("a") if _letter_of(url) == "a" else None

    engine, cache = _crawl_engine(tmp_path, get)
    try:
        catalog, complete = await engine.refresh_catalog()
        assert "pcdt-cond-a" in catalog
        assert complete is False
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_refresh_full_crawl_is_complete_and_keeps_nothing(tmp_path):
    """Offline-only: even a complete crawl is neither cached nor kept."""
    async def get(url, **kwargs):
        return _letter_page(_letter_of(url))

    engine, cache = _crawl_engine(tmp_path, get)
    try:
        catalog, complete = await engine.refresh_catalog()
        assert complete is True
        assert len(catalog) == len(govbr_pcdt.PCDT_LETTERS)
        _, meta = await cache.get("govbr_pcdt:catalog")
        assert meta.cached is False
        assert engine._memory_catalog is None
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_refresh_failed_second_page_is_incomplete(tmp_path):
    """One loaded page is not a loaded letter."""
    page_two = "https://www.gov.br/saude/pt-br/assuntos/pcdt/a?b_start:int=20"

    async def get(url, **kwargs):
        if "b_start" in url:
            return _Resp("", status_code=503)
        letter = _letter_of(url)
        extra = f'<a href="{page_two}">2</a>' if letter == "a" else ""
        return _letter_page(letter, extra)

    engine, cache = _crawl_engine(tmp_path, get)
    try:
        catalog, complete = await engine.refresh_catalog()
        assert "pcdt-cond-a" in catalog
        assert complete is False
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_refresh_shrunken_crawl_is_incomplete(tmp_path):
    async def get(url, **kwargs):
        return _letter_page(_letter_of(url))

    engine, cache = _crawl_engine(tmp_path, get)
    try:
        incumbent = {f"row-{i}": {"record_id": f"row-{i}"} for i in range(100)}
        catalog, complete = await engine.refresh_catalog(incumbent=incumbent)
        assert catalog
        assert complete is False
    finally:
        await cache.close()
```

Also find every other `catalog = await engine.refresh_catalog()` in the file (`grep -n "refresh_catalog()" tests/medical/test_govbr_pcdt.py`) and change it to `catalog, _ = await engine.refresh_catalog()`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/medical/test_govbr_pcdt.py -p no:randomly -q -k refresh`
Expected: FAIL with `ValueError: too many values to unpack`.

- [ ] **Step 3: Implement**

In `govbr_pcdt.py`, change the `govbr_common` import: remove `SEVEN_DAYS_SECONDS` and add `MIN_CATALOG_RETENTION`:

```python
from scholar_mcp.medical.govbr_common import (  # noqa: F401  (re-exported)
    CACHE_SCHEMA,
    GOVBR_HEADERS,
    MIN_CATALOG_RETENTION,
    PORTUGUESE_STOPWORDS,
    derive_item_urls,
    normalize_text,
    score_item,
    tokenize_portuguese,
)
```

Replace the whole `refresh_catalog` method:

```python
    async def refresh_catalog(
        self, incumbent: dict[str, dict[str, Any]] | None = None
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """Crawl a fresh catalog from the gov.br portal. Offline use only.

        Returns ``(catalog, complete)``. ``complete`` is True only when every
        page of every letter loaded and the catalog holds at least
        ``MIN_CATALOG_RETENTION`` of ``incumbent`` (a crawl that finds a
        fraction of the known size is a parser break, not a smaller site).

        Nothing is cached or kept in memory here. The server never crawls:
        ``scripts/update_govbr_catalogs.py`` writes a complete crawl to the
        bundled seed and refuses anything else.
        """
        catalog: dict[str, dict[str, Any]] = {}
        complete = True
        for letter in PCDT_LETTERS:
            urls_to_visit = [f"{PCDT_BASE_URL}/{letter}"]
            visited: set[str] = set()
            while urls_to_visit:
                curr_url = urls_to_visit.pop(0)
                if curr_url in visited:
                    continue
                visited.add(curr_url)
                try:
                    resp = await self.http_client.get(curr_url, headers=GOVBR_HEADERS)
                    if resp is None or resp.status_code != 200:
                        logger.warning(
                            "PCDT page %s answered %s",
                            curr_url,
                            getattr(resp, "status_code", None),
                        )
                        complete = False
                        continue
                    items, next_urls = parse_letter_page(resp.text, letter, curr_url)
                except Exception as exc:
                    logger.warning("Error crawling PCDT letter %s at %s: %s", letter, curr_url, exc)
                    complete = False
                    continue
                catalog.update(items)
                for nurl in next_urls:
                    if nurl not in visited and nurl not in urls_to_visit:
                        urls_to_visit.append(nurl)

        size_floor = int(len(incumbent or {}) * MIN_CATALOG_RETENTION)
        if not catalog or len(catalog) < size_floor:
            logger.warning(
                "PCDT crawl returned %d rows against %d already held; "
                "incomplete (suspected parser break)",
                len(catalog),
                len(incumbent or {}),
            )
            complete = False
        return catalog, complete
```

Run: `grep -n "SEVEN_DAYS_SECONDS\|govbr_pcdt:catalog" src/scholar_mcp/medical/govbr_pcdt.py`
Expected: no output.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/medical/test_govbr_pcdt.py -p no:randomly -q`
Expected: all PASS.

- [ ] **Step 5: Discrimination check**

- Remove `complete = False` from the non-200 branch → `failed_second_page_is_incomplete` and `partial_crawl_is_incomplete` fail.
- Delete the `len(catalog) < size_floor` condition → `shrunken_crawl_is_incomplete` fails.
Restore after each.

- [ ] **Step 6: Commit**

```bash
git status --short
git add src/scholar_mcp/medical/govbr_pcdt.py tests/medical/test_govbr_pcdt.py
git commit -m "fix(govbr_pcdt): offline-only crawl that reports completeness per page"
```

---

### Task 5: One offline script for both catalogs, refusing partial crawls

**Files:**
- Delete: `scripts/update_govbr_az_catalog.py` (via `git mv`)
- Create: `scripts/update_govbr_catalogs.py`
- Create: `tests/test_update_govbr_catalogs.py`
- Modify: `AGENTS.md` (line ~83), `src/scholar_mcp/data/SOURCES.md` (regeneration command; new PCDT section), `CHANGELOG.md` (`[Unreleased]`)

**Interfaces:**
- Consumes: `refresh_catalog(incumbent=...) -> (catalog, complete)` from both engines (Tasks 3, 4); `govbr_az.load_seed_catalog`, `govbr_pcdt.load_seed_catalog`.
- Produces: `CATALOGS: dict[str, tuple[type, Callable[[], dict], Path]]`, `async build_catalog(name: str) -> tuple[dict, bool]`, `main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_update_govbr_catalogs.py`:

```python
"""The offline seed writer must never write a partial crawl."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "update_govbr_catalogs.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("update_govbr_catalogs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(n: int) -> dict:
    return {f"row-{i}": {"record_id": f"row-{i}"} for i in range(n)}


def _stub(script, monkeypatch, catalog: dict, complete: bool) -> None:
    async def _build(name: str):
        return catalog, complete

    monkeypatch.setattr(script, "build_catalog", _build)


@pytest.mark.parametrize("name", ["az", "pcdt"])
def test_incomplete_crawl_writes_nothing(script, monkeypatch, tmp_path, name):
    _stub(script, monkeypatch, _rows(500), complete=False)
    out = tmp_path / "seed.json"

    assert script.main(["--catalog", name, "--output", str(out)]) == 1
    assert not out.exists()


def test_complete_but_tiny_crawl_writes_nothing(script, monkeypatch, tmp_path):
    _stub(script, monkeypatch, _rows(script.MIN_EXPECTED_ROWS - 1), complete=True)
    out = tmp_path / "seed.json"

    assert script.main(["--catalog", "az", "--output", str(out)]) == 1
    assert not out.exists()


@pytest.mark.parametrize("name", ["az", "pcdt"])
def test_complete_crawl_is_written(script, monkeypatch, tmp_path, name):
    rows = _rows(script.MIN_EXPECTED_ROWS)
    _stub(script, monkeypatch, rows, complete=True)
    out = tmp_path / "seed.json"

    assert script.main(["--catalog", name, "--output", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == rows


def test_each_catalog_defaults_to_its_own_seed(script):
    assert script.CATALOGS["az"][2].name == "govbr_az_catalog.json"
    assert script.CATALOGS["pcdt"][2].name == "govbr_pcdt_catalog.json"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/test_update_govbr_catalogs.py -p no:randomly -q`
Expected: ERROR/FAIL, `FileNotFoundError` for `scripts/update_govbr_catalogs.py`.

- [ ] **Step 3: Implement**

```bash
git mv scripts/update_govbr_az_catalog.py scripts/update_govbr_catalogs.py
```

Replace the contents of `scripts/update_govbr_catalogs.py` with:

```python
#!/usr/bin/env python3
"""Regenerate a bundled gov.br catalog seed.

``--catalog az`` crawls the SVSA and guias-e-manuais publication trees and
writes ``src/scholar_mcp/data/govbr_az_catalog.json``. ``--catalog pcdt``
crawls the PCDT letter index and writes
``src/scholar_mcp/data/govbr_pcdt_catalog.json``. The seeds are the catalogs
the server searches: no search crawls gov.br.

Refuses to write a partial crawl. Any failed page, a folder cut off at the
page cap, a missing alias vocabulary, or a catalog below
MIN_CATALOG_RETENTION of the current seed exits 1 and leaves the seed as it
is.

Usage:
    uv run python scripts/update_govbr_catalogs.py --catalog az|pcdt [--output PATH]
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from scholar_mcp.config import Settings  # noqa: E402
from scholar_mcp.medical import govbr_az, govbr_pcdt  # noqa: E402
from scholar_mcp.utils.http import AsyncHttpClient  # noqa: E402
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager  # noqa: E402

DATA_DIR = REPO_ROOT / "src" / "scholar_mcp" / "data"
CATALOGS = {
    "az": (
        govbr_az.GovBrAZEngine,
        govbr_az.load_seed_catalog,
        DATA_DIR / "govbr_az_catalog.json",
    ),
    "pcdt": (
        govbr_pcdt.GovBrPCDTEngine,
        govbr_pcdt.load_seed_catalog,
        DATA_DIR / "govbr_pcdt_catalog.json",
    ),
}
MIN_EXPECTED_ROWS = 50


async def build_catalog(name: str) -> tuple[dict, bool]:
    """Crawl one catalog, measured against the current seed."""
    engine_cls, load_seed, _ = CATALOGS[name]
    settings = Settings()
    http_client = AsyncHttpClient(settings)
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = SQLiteCacheManager(Path(tmpdir) / "catalog.db", settings=settings)
        engine = engine_cls(http_client, cache, settings)
        try:
            return await engine.refresh_catalog(incumbent=load_seed())
        finally:
            await cache.close()
            await http_client.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--catalog", choices=sorted(CATALOGS), required=True)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    output = args.output or CATALOGS[args.catalog][2]

    catalog, complete = asyncio.run(build_catalog(args.catalog))
    if not complete:
        print(
            f"refusing to write a partial {args.catalog} crawl ({len(catalog)} rows); "
            "see the warnings above",
            file=sys.stderr,
        )
        return 1
    if len(catalog) < MIN_EXPECTED_ROWS:
        print(
            f"refusing to write {len(catalog)} rows "
            f"(expected at least {MIN_EXPECTED_ROWS}); gov.br may be degraded",
            file=sys.stderr,
        )
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {len(catalog)} records to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Docs:

- `AGENTS.md`: replace ``The bundled A-Z catalog (`src/scholar_mcp/data/govbr_az_catalog.json`) can be regenerated via `scripts/update_govbr_az_catalog.py`.`` with ``The bundled PCDT and A-Z catalogs (`src/scholar_mcp/data/govbr_pcdt_catalog.json`, `govbr_az_catalog.json`) are the catalogs the server searches; no search crawls gov.br. Regenerate them offline with `scripts/update_govbr_catalogs.py --catalog pcdt|az`, which refuses to write a partial crawl.``
- `src/scholar_mcp/data/SOURCES.md`: in the `govbr_az_catalog.json` section, change the command to `python scripts/update_govbr_catalogs.py --catalog az` and add after the paragraph that follows it: `The script exits 1 without writing when the crawl is incomplete (any failed page, a folder cut off at the page cap, a missing alias vocabulary, or fewer than half the rows of the current file).` Then append a new section at the end of the file:

````markdown
# govbr_pcdt_catalog.json

Catalog index for the Brazilian Ministry of Health PCDT (Protocolos Clínicos
e Diretrizes Terapêuticas) letter pages.

**Source:** `https://www.gov.br/saude/pt-br/assuntos/pcdt/<letter>`

**Regeneration command:**
```bash
python scripts/update_govbr_catalogs.py --catalog pcdt
```
Same refusal rules as the A-Z catalog: an incomplete crawl writes nothing.
````

- `CHANGELOG.md`: under `## [Unreleased]`, add a `### Changed` heading after the `### Added` list if none exists, and add:

```markdown
- **gov.br catalogs are offline-seed only**: `GovBrPCDTEngine` and `GovBrAZEngine` serve the bundled seed and never crawl gov.br or copy the seed into SQLite on a search. A missing seed reports `backend_error`.
- **`scripts/update_govbr_catalogs.py --catalog az|pcdt`** replaces `scripts/update_govbr_az_catalog.py` and refuses to write a partial crawl (any failed page, page-cap truncation, missing alias vocabulary, or a catalog below half the current size).
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/test_update_govbr_catalogs.py -p no:randomly -q`
Expected: all PASS.
Run: `grep -rn "update_govbr_az_catalog" AGENTS.md src scripts tests`
Expected: no output.

- [ ] **Step 5: Discrimination check**

Delete the `if not complete:` block → `test_incomplete_crawl_writes_nothing` fails. Restore.

- [ ] **Step 6: Commit**

```bash
git status --short
git add scripts/update_govbr_catalogs.py tests/test_update_govbr_catalogs.py AGENTS.md src/scholar_mcp/data/SOURCES.md CHANGELOG.md
git commit -m "feat(scripts): one seed writer for both gov.br catalogs, never partial"
```

(`git mv` already staged the removal of the old path. Confirm `git show --stat HEAD` lists the rename.)

---

### Task 6: A gov.br stage timeout is classified as `timeout`

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py` (~L1216, `stage_error_meta`)
- Test: `tests/medical/test_brazil_moh.py`

**Interfaces:**
- Produces: the default returned by both gov.br `_stage` calls is `([], CacheMetadata(cached=False, cache_age=0, error=True, error_kind="timeout", timeout=True))`.

- [ ] **Step 1: Write the failing test**

Append to `tests/medical/test_brazil_moh.py`:

```python
@respx.mock
async def test_govbr_stage_timeout_reports_timeout_kind(tmp_path: Path):
    """A gov.br stage cut off by its budget is a timeout in the ENAMED §2
    taxonomy, not an unclassified error."""
    engine, cache, http_client = await _engine(tmp_path, backoff_base=0.01)
    _pin_fast_limiter(http_client)
    engine.settings.brazil_stage_timeout_s = 0.3
    try:
        async def _stalls(*args, **kwargs):
            await asyncio.sleep(5.0)
            return [], CacheMetadata(cached=False, cache_age=0)

        engine.pcdt_engine.search = _stalls
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        seen: dict = {}
        real_stage = engine._stage

        async def _spy(stage, coro, default, **kwargs):
            result = await real_stage(stage, coro, default, **kwargs)
            seen[stage] = result
            return result

        engine._stage = _spy

        await engine.search_guidelines("dengue", limit=5)

        _records, pcdt_meta = seen["govbr_pcdt"]
        assert pcdt_meta.error is True
        assert pcdt_meta.error_kind == "timeout"
        assert pcdt_meta.timeout is True
    finally:
        await cache.close()
        await http_client.aclose()
```

(`asyncio`, `Path`, `httpx`, `respx`, `CacheMetadata`, `BVS_SEARCH_URL`, `_bvs_response`, `_bvs_doc`, `_engine`, `_pin_fast_limiter` already exist in this file. Confirm with `grep -n "^import asyncio\|^from pathlib\|CacheMetadata" tests/medical/test_brazil_moh.py | head`.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `$PYTEST tests/medical/test_brazil_moh.py -p no:randomly -q -k govbr_stage_timeout_reports`
Expected: FAIL on `assert pcdt_meta.error_kind == "timeout"` (`''`).

- [ ] **Step 3: Implement**

In `search_guidelines`, replace

```python
        stage_error_meta = CacheMetadata(cached=False, cache_age=0, error=True)
```

with

```python
        # A gov.br stage that runs out of budget (or is skipped because the
        # chain budget is spent) is a timeout in the ENAMED §2 taxonomy, not
        # an unclassified error.
        stage_error_meta = CacheMetadata(
            cached=False, cache_age=0, error=True, error_kind="timeout", timeout=True
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `$PYTEST tests/medical/test_brazil_moh.py -p no:randomly -q -k govbr_stage_timeout_reports`
Expected: PASS.

- [ ] **Step 5: Discrimination check**

Revert the metadata to the old line → test fails. Restore.

- [ ] **Step 6: Commit**

```bash
git status --short
git add src/scholar_mcp/medical/brazil_moh.py tests/medical/test_brazil_moh.py
git commit -m "fix(brazil_moh): classify a gov.br stage timeout as timeout"
```

---

### Task 7: The search chain never caches a merge with any errored stage

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py` (after the gov.br `gather` ~L1221; browser comment ~L1357-1360; browser success ~L1381-1385; cache write ~L1466-1498)
- Test: `tests/medical/test_brazil_moh.py`

**Interfaces:**
- Consumes: Task 6 stage metadata.
- Produces: local variable `local_errored: bool` in `search_guidelines`; the chain writes `brazil_moh_search:*` only when `errored_any` is False.

- [ ] **Step 1: Write the failing tests**

Replace `test_clean_bvs_result_is_cached_when_only_an_auxiliary_stage_fails` in full with:

```python
@respx.mock
async def test_clean_bvs_result_is_not_cached_when_a_govbr_stage_fails(tmp_path: Path):
    """The merge is missing the AZ rows: a partial retrieval, never cached."""
    engine, cache, http_client = await _engine(tmp_path)
    try:
        async def _az_down(*args, **kwargs):
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        engine.az_engine.search = _az_down
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )

        first, _first_meta = await engine.search_guidelines("dengue", limit=5)
        second, second_meta = await engine.search_guidelines("dengue", limit=5)

        assert first, "BVS returned a record"
        assert second_meta.cached is False
        assert route.call_count == 2, "the second call must re-run the chain"
        composed = _build_query("dengue", "all", operator="AND", title_scoped=True)
        _, cache_meta = await cache.get(f"brazil_moh_search:{CACHE_SCHEMA}:all:5:{composed}")
        assert cache_meta.cached is False
    finally:
        await cache.close()
        await http_client.aclose()
```

Append:

```python
@respx.mock
async def test_govbr_stage_timeout_is_not_cached(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path, backoff_base=0.01)
    _pin_fast_limiter(http_client)
    engine.settings.brazil_stage_timeout_s = 0.3
    try:
        async def _stalls(*args, **kwargs):
            await asyncio.sleep(5.0)
            return [], CacheMetadata(cached=False, cache_age=0)

        engine.pcdt_engine.search = _stalls
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )

        records, _meta = await engine.search_guidelines("dengue", limit=5)

        assert records, "BVS still answered"
        composed = _build_query("dengue", "all", operator="AND", title_scoped=True)
        _, cache_meta = await cache.get(f"brazil_moh_search:{CACHE_SCHEMA}:all:5:{composed}")
        assert cache_meta.cached is False
    finally:
        await cache.close()
        await http_client.aclose()


async def test_browser_success_does_not_cache_when_a_govbr_stage_errored(
    tmp_path, monkeypatch
):
    """The browser tier answers the BVS errors only. A gov.br stage that
    also failed leaves the merge partial, so nothing is written."""
    settings = Settings(
        cache_ttl_seconds=3600,
        enable_browser_fallback=True,
        brazil_browser_fallback=True,
        request_timeout=5,
    )
    http_client = AsyncHttpClient(
        settings, max_retries=2, backoff_base=0.01, min_429_wait=0.0
    )
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = BrazilMoHEngine(http_client, cache, settings)
    monkeypatch.setattr(
        engine.pcdt_engine,
        "search",
        AsyncMock(return_value=([], CacheMetadata(cached=False, cache_age=0, error=False))),
    )
    monkeypatch.setattr(
        engine.az_engine,
        "search",
        AsyncMock(return_value=([], CacheMetadata(cached=False, cache_age=0, error=True))),
    )
    payload = {
        "diaServerResponse": [
            {
                "response": {
                    "docs": [
                        {"id": "1", "ti": "Manejo da dengue",
                         "pais_publicacao": "^eBrasil", "da": "202401",
                         "ur": ["https://bvsms.saude.gov.br/x.pdf"]},
                    ]
                }
            }
        ]
    }
    attempts, _urls, _exits, _sleeps = _install_fake_camoufox(
        monkeypatch, json.dumps(payload)
    )
    try:
        with respx.mock:
            respx.get(BVS_SEARCH_URL).mock(return_value=httpx.Response(403, text="shield"))
            records, _meta = await engine.search_guidelines("dengue", limit=10)
        assert [r.title for r in records] == ["Manejo da dengue"]
        assert attempts == [True]
        composed = _build_query("dengue", "all", operator="AND", title_scoped=True)
        _, cache_meta = await cache.get(f"brazil_moh_search:{CACHE_SCHEMA}:all:10:{composed}")
        assert cache_meta.cached is False
    finally:
        await cache.close()
        await http_client.aclose()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/medical/test_brazil_moh.py -p no:randomly -q -k "not_cached_when_a_govbr or govbr_stage_timeout_is_not_cached or browser_success_does_not_cache"`
Expected: 3 FAIL. The first two fail on the cache row (main holds the degraded merge for 300 s). The browser test fails because main clears `errored_any` and caches for the full TTL.

- [ ] **Step 3: Implement**

1. Directly after `errored_any = pcdt_meta.error or az_meta.error`, add:

```python
        # Kept apart from errored_any: a browser-tier success clears the BVS
        # errors it answered, never a gov.br stage that also failed.
        local_errored = errored_any
```

2. Replace the browser comment line `# success clears errored_any so the merged result is cached.` with:

```python
        # success clears the BVS errors, so the merge is cached unless a
        # gov.br stage also errored.
```

3. In the browser-success block, replace `errored_any = False` with `errored_any = local_errored`.

4. Replace the whole cache-write block (from the comment `# A chain with a stalled stage returns partial results; ...` through the end of the `elif not bvs_errored:` branch) with:

```python
        # A partial retrieval is never cached. A chain with any errored stage
        # -- a failed or stalled BVS stage, or a gov.br stage that timed out
        # or errored -- returns partial results, and caching them would make
        # a transient failure permanent. An origin outage is not evidence
        # about the corpus either, and pinning it would count a sick origin
        # against the caller's breaker on replay.
        #
        # The row cached is the unfiltered merge, not the sliced `records`
        # returned to this caller: `since_year` is not part of `cache_key`,
        # so the same row must reproduce the correct result for any
        # `since_year` a later call asks for (via `_finalize_records` on
        # read).
        if not errored_any:
            await self.cache.set(
                cache_key,
                [record.to_dict() for record in merged_records],
                source="brazil_moh",
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/medical/test_brazil_moh.py -p no:randomly -q`
Expected: all PASS.

- [ ] **Step 5: Discrimination check**

- Put back `errored_any = False` in the browser block → browser test fails.
- Put back the `elif not bvs_errored:` degraded write → the two gov.br tests fail.
Restore after each.

- [ ] **Step 6: Commit**

```bash
git status --short
git add src/scholar_mcp/medical/brazil_moh.py tests/medical/test_brazil_moh.py
git commit -m "fix(brazil_moh): never cache a search merge with an errored stage"
```

---

### Task 8: `get_full_text` never caches the abstract fallback; state the invariant

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py` (module docstring ~L40-56; `DEGRADED_RESULT_TTL_SECONDS` block ~L247-252; cache-hit comment ~L1835-1837; cache write ~L2020-2030)
- Test: `tests/medical/test_brazil_moh.py`, `tests/medical/test_enamed_2026_misses_engines.py`

**Interfaces:**
- Produces: `get_full_text` writes `brazil_moh_fulltext:*` only for a clean result (`errored` False). `DEGRADED_RESULT_TTL_SECONDS` no longer exists.

- [ ] **Step 1: Update the tests to the new rule (they become the failing tests)**

In `tests/medical/test_brazil_moh.py`:

- `test_get_full_text_rejects_redirect_off_allowlisted_hosts`: replace the 3-line `# C2: ...` comment with `# The abstract fallback is a success (error=False) whose error_kind carries the real PDF failure. It is a partial retrieval, so it is never cached.` and change `assert cache_meta.cached is True` to `assert cache_meta.cached is False`.
- `test_get_full_text_pdf_failure_degrades_and_is_not_cached`: same comment change, and `assert cache_meta.cached is True` → `assert cache_meta.cached is False`.
- `test_get_full_text_exhausted_budget_still_serves_the_abstract`: replace `# Cached at the degraded TTL so the next request retries the PDF.` with `# Never cached: the next request retries the PDF.` and `assert cache_meta.cached is True` → `assert cache_meta.cached is False`.

In `tests/medical/test_enamed_2026_misses_engines.py`:

- `test_fulltext_pdf_failure_with_abstract_is_success_not_error`: replace from the comment `# Degraded but reachable: cached briefly, never for the 30-day TTL.` to the end of the `try` body with:

```python
        # A partial retrieval is never cached: the second call re-fetches and
        # reports the same degradation from its own attempt.
        payload2, meta2 = await engine.get_full_text("biblio-pdf-fail")
        assert meta2.cached is False
        assert payload2["content_type"] == "abstract"
        assert meta2.error_kind == "backend_error"
```

- Replace `test_fulltext_cache_hit_keeps_degraded_error_kind` in full with:

```python
@respx.mock
async def test_fulltext_degraded_result_is_refetched_not_cached(tmp_path: Path):
    """An abstract served after a failed PDF fetch is a partial retrieval.

    Nothing is written, so a second request tries the PDF again and reports
    the kind its own attempt saw.
    """
    engine, cache, http_client = await _engine(tmp_path)
    try:
        doc = _bvs_doc(record_id="biblio-outage-abs", ab=["Resumo preservado."])
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([doc]))
        )
        pdf_route = respx.get(url__startswith="https://fi-admin.bvsalud.org").mock(
            return_value=httpx.Response(503, text="Erro 503 - Service Unavailable")
        )
        payload, meta = await engine.get_full_text("biblio-outage-abs")
        assert payload["content_type"] == "abstract"
        assert meta.cached is False
        assert meta.error_kind == "origin_outage"
        attempts_after_first = pdf_route.call_count

        payload2, meta2 = await engine.get_full_text("biblio-outage-abs")
        assert meta2.cached is False
        assert payload2["content_type"] == "abstract"
        assert meta2.error_kind == "origin_outage"
        assert pdf_route.call_count > attempts_after_first, "the PDF was not retried"
        assert "_error_kind" not in payload
        assert "_error_kind" not in payload2
    finally:
        await cache.close()
        await http_client.aclose()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/medical/test_brazil_moh.py tests/medical/test_enamed_2026_misses_engines.py -p no:randomly -q -k "full_text or fulltext"`
Expected: the 5 edited tests FAIL on `cached is False` (main holds the fallback for 300 s).

- [ ] **Step 3: Implement**

1. Module docstring: replace

```
never cached. One document fetch is the exception: an abstract served
after a failed PDF fetch is held for ``DEGRADED_RESULT_TTL_SECONDS`` and
carries its ``error_kind`` inside the cached row, so every hit in that
window reports the same degradation the first caller saw. ``record_id``
```

with

```
never cached. More generally, a partial retrieval is never cached, in
memory or on disk: a search with any errored stage and an abstract served
after a failed PDF fetch are both returned but never written, so the next
call retries. The gov.br PCDT and A-Z catalogs are the bundled seeds;
no search crawls gov.br. ``record_id``
```

2. Delete the `DEGRADED_RESULT_TTL_SECONDS` block (its 4-line comment and the constant).

3. In the full-text cache-hit branch, replace

```python
            # Replay the kind stored with the row. A degraded payload lives
            # for DEGRADED_RESULT_TTL_SECONDS, so without this every request
            # behind the first one in that window reports a clean result.
```

with

```python
            # Replay the kind stored with the row. Only clean results are
            # written, so this is "ok" for every row this release writes.
```

4. Replace the full-text write:

```python
        if not errored:
            await self.cache.set(cache_key, payload, source="brazil_moh")
        else:
            # A PDF fetch that failed and fell back to the abstract is
            # still cached -- at the shorter degraded TTL, so the next
            # request retries the PDF instead of serving a stale fallback
            # indefinitely.
            await self.cache.set(
                cache_key, payload, source="brazil_moh",
                ttl=DEGRADED_RESULT_TTL_SECONDS,
            )
```

with

```python
        # A partial retrieval is never cached: an abstract served because the
        # PDF fetch failed is written nowhere, so the next request retries
        # the PDF.
        if not errored:
            await self.cache.set(cache_key, payload, source="brazil_moh")
```

5. Run: `grep -rn "DEGRADED_RESULT_TTL" src tests`
Expected: no output.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/medical -p no:randomly -q -k "full_text or fulltext"`
Expected: all PASS.

- [ ] **Step 5: Discrimination check**

Put back the `else:` branch (with `ttl=300`) → the 5 tests fail. Restore.

- [ ] **Step 6: Commit**

```bash
git status --short
git add src/scholar_mcp/medical/brazil_moh.py tests/medical/test_brazil_moh.py tests/medical/test_enamed_2026_misses_engines.py
git commit -m "fix(brazil_moh): never cache the abstract served after a failed PDF"
```

---

### Task 9: Cleanup and full verification

**Files:**
- Delete: `tests/medical/test_probe_govbr_stale_loop.py` (untracked probe; never committed)

- [ ] **Step 1: Delete the probe**

```bash
rm tests/medical/test_probe_govbr_stale_loop.py
git status --short
```
Expected: clean tree.

- [ ] **Step 2: Sweep for leftovers**

Run: `grep -rn "govbr_az:catalog\|govbr_pcdt:catalog\|CATALOG_CACHE_KEY\|DEGRADED_RESULT_TTL\|update_govbr_az_catalog" src scripts tests AGENTS.md`
Expected: hits only in `tests/medical/test_govbr_az.py` and `tests/medical/test_govbr_pcdt.py` (the tests asserting that no catalog row is written or read). No hit in `src`, `scripts` or `AGENTS.md`.

- [ ] **Step 3: Full offline suite, random order, in the background**

Run in the background (it takes ~9 minutes):
`$PYTEST -q > /tmp/govbr-offline-suite.log 2>&1`
Expected: 0 failed. The count differs from 1004 by the tests added and deleted; report the exact line from the log.

- [ ] **Step 4: Second run without randomness**

Run: `$PYTEST -q -p no:randomly > /tmp/govbr-offline-suite-ordered.log 2>&1`
Expected: 0 failed.

- [ ] **Step 5: Report**

Report both summary lines verbatim. Do not open a PR in this task. When a PR is opened later: `gh pr create --base main ...`, then verify with `gh pr view <n> --json baseRefName`.
