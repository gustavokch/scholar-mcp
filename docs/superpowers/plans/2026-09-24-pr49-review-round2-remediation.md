# PR #49 Review Round 2 Remediation

**Goal:** Close the two round-2 findings on PR #49 (https://github.com/gustavokch/scholar-mcp/pull/49#issuecomment-5808203790).
**Architecture:** Offline seed writer only; no server search path changes.
**Tech Stack:** Python 3.10, pytest, pytest-asyncio.
**Spec:** `docs/superpowers/specs/2026-09-23-govbr-offline-catalogs-no-partial-cache-design.md` — "an incomplete crawl writes nothing".

## Task 1: Cap PCDT pagination per letter

- Modify: `src/scholar_mcp/medical/govbr_pcdt.py` (`refresh_catalog`, new `MAX_PAGES_PER_LETTER = 25`)
- Test: `tests/medical/test_govbr_pcdt.py::test_refresh_page_cap_is_incomplete`
- Produces: `refresh_catalog` terminates on an endless `b_start` chain and returns `complete=False`.

1. Failing test: `get` serves letter pages whose every page links a next `b_start` page; with `MAX_PAGES_PER_LETTER` patched to 2, assert `complete is False` and at most 2 fetches per letter.
2. `uv run pytest tests/medical/test_govbr_pcdt.py -k page_cap -v` → fails (unbounded fetch count).
3. Loop condition `while urls_to_visit and len(visited) < MAX_PAGES_PER_LETTER`; leftover queue logs a warning and clears `complete`.
4. Re-run → pass.
5. `git commit -m "fix(govbr_pcdt): cap crawl pages per letter"`

## Task 2: Atomic seed write

- Modify: `scripts/update_govbr_catalogs.py` (`main`)
- Test: `tests/test_update_govbr_catalogs.py::test_failed_write_keeps_the_existing_seed`
- Produces: the existing seed survives a serialization failure mid-write.

1. Failing test: existing seed file; catalog holding a non-JSON-serializable value after valid rows; `main` raises; seed bytes unchanged, no temp file left.
2. Run → fails (seed truncated).
3. Dump to `output.with_suffix(".json.tmp")`, `os.replace` onto `output`; unlink temp on failure.
4. Re-run → pass.
5. `git commit -m "fix(scripts): write the gov.br seed atomically"`
