# gov.br Offline Catalogs and No Partial Caching — Design

Date: 2026-09-23
Status: Approved design (brainstorm), pending spec review

## Purpose

Two changes, one invariant:

1. The gov.br PCDT and A-Z catalogs become **offline-seed only**. No search
   ever crawls gov.br. The bundled seed files are the catalogs; an offline
   script regenerates them.
2. **A partial retrieval is never cached, in memory or on disk.** This holds
   for the gov.br catalogs, the per-query gov.br search caches, the
   `search_guidelines` chain cache, and the `get_full_text` cache.

## Background: what the investigation found

The starting hypothesis (handoff of 2026-09-24) was that `_stage`'s
`wait_for` cancels an in-flight catalog refresh, `CancelledError` escapes
`except Exception`, the memoization lines never run, and every later search
re-crawls and times out.

A probe test confirmed the mechanism in both engines: with the stale branch
forced (mocked `cache.get`), two cancelled calls both started a crawl and
left `_memory_catalog` at `None`.

The trigger does not occur in production. `SQLiteCacheManager.get`
(`utils/sqlite_cache.py:150-154`) deletes a row past its TTL and reports a
miss. Both catalog writers use `ttl=SEVEN_DAYS_SECONDS`, which is also the
refresh threshold, so the stale branch (`cache_age >= SEVEN_DAYS_SECONDS`)
runs only when the age is exactly 604800 s. In practice `get_catalog` falls
to the seed branch, serves the bundled seed, and re-writes it to SQLite.
The existing refresh tests reach the stale branch only by mocking
`cache.get`.

Consequences:

- The online refresh is dead code. The seed is the real catalog.
- In steady state the gov.br stages make no network request; `search` goes
  to the network only through `get_catalog`.
- The only live request-path crawl is a cold start with a missing or empty
  seed. That path is cancelled by the stage budget and restarts on every
  search.
- The SQLite catalog row holds a copy of the seed. After a package upgrade,
  the row copied from the **old** seed takes precedence over the new bundled
  seed for up to seven days.

An audit against "never cache a partial retrieval" found these violations:

| # | Location | Violation |
|---|---|---|
| 1 | `govbr_pcdt.py` / `govbr_az.py` `search` | A partial crawl is kept out of the catalog cache, but `search` caches the per-query result computed from it, with `error=False`, for `cache_ttl_brazil_moh`. |
| 2 | `govbr_az.py` `get_catalog` | A partial crawl is kept in `_memory_catalog` for the process lifetime (by design; `test_get_catalog_memoizes_partial_refresh`). |
| 3 | `brazil_moh.py` `search_guidelines` | When a gov.br stage errored and BVS was clean, the merge is cached for `DEGRADED_RESULT_TTL_SECONDS`. |
| 4 | `brazil_moh.py` `search_guidelines` | A browser-fallback success sets `errored_any = False`, which also erases gov.br stage errors; the partial merge gets the full TTL. |
| 5 | `govbr_az._crawl_folder`, `govbr_pcdt.refresh_catalog` | A folder or letter counts as OK if one page loads. A failed later page, or a folder stopped at `MAX_PAGES_PER_FOLDER`, counts as complete. The script checks only `>= 50` rows. |
| 6 | `brazil_moh.py` `get_full_text` | An abstract served after a failed PDF fetch is cached for `DEGRADED_RESULT_TTL_SECONDS`. |
| 7 | `govbr_pcdt.py` `get_catalog` | With the seed missing, the extended corpus alone is returned as a non-empty catalog, so the search reports success on a partial catalog. |

## Scope

In scope:

- `src/scholar_mcp/medical/govbr_pcdt.py`: `get_catalog`, `refresh_catalog`.
- `src/scholar_mcp/medical/govbr_az.py`: `get_catalog`, `refresh_catalog`,
  `_crawl_folder`, `_load_aliases` (completeness reporting only), docstrings.
- `src/scholar_mcp/medical/brazil_moh.py`: gov.br stage default metadata,
  chain cache write, browser-fallback error clearing, `get_full_text` cache
  write, module docstring, removal of `DEGRADED_RESULT_TTL_SECONDS`.
- `scripts/update_govbr_az_catalog.py` renamed to
  `scripts/update_govbr_catalogs.py`, with `--catalog az|pcdt`.
- References to the script in `AGENTS.md` and `src/scholar_mcp/data/SOURCES.md`;
  a new `CHANGELOG.md` entry. The existing CHANGELOG line about the old
  script name is history and stays unchanged.
- Tests in `tests/medical/`.

Out of scope:

- Regenerating either seed file. (The main checkout has an uncommitted
  change to `govbr_az_catalog.json` of unknown origin; this work does not
  touch it.)
- Background or scheduled refresh.
- BVS stage behavior, the retry ladder, and stage budget arithmetic.
- The PCDT topic fallback from #48, except that it keeps reading
  `errored_any` as before.

## Design

### 1. Catalog loading (both engines)

`get_catalog()`:

1. If `_memory_catalog` is set, return it (PCDT: merged with the extended
   corpus, as now).
2. Otherwise call `load_seed_catalog()`. If the result is non-empty, set
   `_memory_catalog` and return it (PCDT: merged).
3. If the seed is missing or empty, log an error, leave `_memory_catalog`
   unset, and return `{}`. PCDT does **not** merge the extended corpus over
   an empty seed (violation 7).

`get_catalog()` makes no network request and neither reads nor writes a
SQLite catalog row. The `govbr_pcdt:catalog` and `govbr_az:catalog` keys are
no longer used; rows already in user databases expire within seven days and
nothing reads them.

`search` is unchanged in shape: an empty catalog already returns
`error=True, error_kind="backend_error"` and is not cached. With (3), this
now covers a missing PCDT seed too. Violations 1 and 2 disappear because a
partial crawl can no longer reach `search`.

`refresh_catalog()` becomes offline-only: it does not set `_memory_catalog`
and does not write the cache.

### 2. Crawl completeness and the offline script

`refresh_catalog(incumbent=None)` returns `(catalog, complete)` in both
engines (PCDT gains the `incumbent` parameter). `complete` is `True` only
when all of these hold:

- Every page fetch succeeded. One failed page in any folder (A-Z) or letter
  (PCDT) makes the crawl incomplete.
- No A-Z folder stopped at `MAX_PAGES_PER_FOLDER` with URLs still queued.
- Both A-Z tree indexes loaded.
- The A-Z alias index and every alias letter page loaded. Without them,
  rows lose their alias text, which is a partial catalog.
- `len(catalog) >= MIN_CATALOG_RETENTION * len(incumbent)`, where
  `incumbent` is the current seed. This is the existing parser-break floor,
  now applied to PCDT too.

`_crawl_folder` reports `ok` as "every visited page loaded and the queue is
empty", not "at least one page loaded".

`scripts/update_govbr_catalogs.py --catalog az|pcdt`:

- Loads the current seed as `incumbent`, runs the matching engine's
  `refresh_catalog(incumbent=...)`.
- Writes the seed file only when `complete` is `True` and the catalog has at
  least `MIN_EXPECTED_ROWS` rows. Otherwise prints the reason to stderr and
  exits 1.
- `--output PATH` keeps working; the default is the matching bundled seed.

### 3. Chain and full-text caching (`brazil_moh.py`)

- **Stage timeout kind.** The default passed to both gov.br `_stage` calls
  becomes `CacheMetadata(cached=False, cache_age=0, error=True,
  error_kind="timeout", timeout=True)`. It covers both a timed-out stage and
  a stage skipped because the chain budget is spent.
- **No degraded chain cache.** The `elif not bvs_errored:` branch that
  caches the merge for `DEGRADED_RESULT_TTL_SECONDS` is removed. If any
  stage errored, nothing is written (violation 3).
- **Browser fallback keeps local errors.** Record
  `local_errored = pcdt_meta.error or az_meta.error` after the gov.br stages.
  On browser success, set `errored_any = local_errored` instead of `False`
  (violation 4).
- **No full-text fallback cache.** When the PDF fetch fails and the abstract
  is served, `get_full_text` writes nothing to the cache (violation 6). The
  clean path is unchanged. The replay of the stored error kind on a cache
  hit stays: every row written after this change is clean and replays
  `"ok"`, and rows written before it expire within 300 s.
- `DEGRADED_RESULT_TTL_SECONDS` is removed.
- The module docstring states: "A partial retrieval is never cached, in
  memory or on disk." It no longer describes the full-text exception.

### 4. Tests

Each test is written first, is seen to fail on `main`, and is then checked
to fail again when the guard it targets is disabled.

| Layer | Test |
|---|---|
| Catalog | `get_catalog` with the seed present makes no HTTP call and writes no `*:catalog` cache row (both engines). |
| Catalog | A stale `*:catalog` row in SQLite does not override the bundled seed (both engines). |
| Catalog | A missing seed returns `{}`, is not kept in memory, and makes `search` return `backend_error` with no cache write (both engines; PCDT with the extended corpus present). |
| Crawl | A failed second page gives `complete=False` (both engines). |
| Crawl | A folder stopped at `MAX_PAGES_PER_FOLDER` gives `complete=False`. |
| Crawl | A failed alias index gives `complete=False`. |
| Crawl | A catalog below the retention floor gives `complete=False` (both engines). |
| Script | An incomplete crawl writes no file and exits 1. |
| Chain | A gov.br stage timeout reports `error_kind="timeout"` and writes no chain cache row. |
| Chain | A gov.br stage error with clean BVS writes no chain cache row. |
| Chain | A browser-fallback success plus a gov.br stage error writes no chain cache row. |
| Full text | A failed PDF fetch with an abstract fallback writes no cache row. |

Tests removed because they cover removed behavior:
`test_pcdt_7_day_cache_refresh`, `test_get_catalog_memoizes_partial_refresh`,
and the tests asserting that `refresh_catalog` writes the cache or sets
`_memory_catalog`. Tests asserting the degraded 300 s chain or full-text
hold are rewritten to assert no write.

Offline baseline before the change: 1004 passed, 8 deselected. Verification
is offline only.

## Risks

- **Every call re-runs the BVS chain while a gov.br stage fails.** Under this
  design a gov.br stage fails only when the seed is missing or SQLite is
  slow, so the cost is close to zero.
- **Every full-text call retries the PDF while the PDF host is down.** This
  is the intended cost of never caching the fallback.
- **Catalog freshness now depends on running the script.** This is already
  true in production; the design makes it explicit.
