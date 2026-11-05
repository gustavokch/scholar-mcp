# PR #58 Review Remediation — duplicate OR retry in the BVS browser tier

- **PR:** gustavokch/scholar-mcp#58 (`fix/brazil-moh-browser-challenge-wait`)
- **Date:** 2026-11-05
- **Review:** https://github.com/gustavokch/scholar-mcp/pull/58#issuecomment-6664152418
- **Goal:** The browser tier navigates each BVS query once. Remove the
  `fallback_composed` retry, which re-sends the primary query, and keep the
  shield wait after every goto. Fix the two nits from the review.

## Architecture

`search_guidelines` calls `_camoufox_search(all_composed, ...)`. `all_composed`
is `_build_query(query, collection, operator="OR", title_scoped=False)`. The PR
also passes `fallback_composed` built with the same arguments, so when the
first payload is valid but empty the tier navigates the identical URL again.
There is no broader composition than the all-field OR, so no fallback can add
documents. The live check in the PR (six records in about 12 s) comes from the
clean-goto shield wait, which stays.

## Change

- `_camoufox_search` returns to one target and one navigation. The navigation
  budget keeps the PR's subtraction of launch plus `new_page` time, and the
  read keeps `min(_CAMOUFOX_SHIELD_CLEAR_MS, nav_timeout_ms)` after every goto.
- The `fallback_composed` parameter, the call-site argument, and the three
  direct fallback tests are removed. The duplicate `_browser_engine` helper and
  its payload constants go with them.
- A `search_guidelines`-level test pins the browser navigations for a shielded
  multi-token query, so the call site is covered.

Out of scope (needs a decision, noted on the PR): `_overfetch_count` shrinks at
`brazil_chain_timeout_s / 2`, which is 150 s under the new 300 s default and
above the worst case of the HTTP stages.

## Tech Stack

Python 3.10, pytest, respx, httpx. Tests: `tests/medical/test_brazil_moh.py`,
`tests/medical/test_enamed_2026_misses_engines.py`.

## Task 1: Failing test — a shielded search navigates the all-field query once

- **Modify:** `tests/medical/test_brazil_moh.py`
- **Consumes:** `search_guidelines`, `_install_fake_camoufox`, `_build_query`.
- Add `test_shielded_search_navigates_the_all_field_query_once`: every BVS GET
  answers 403, the fake browser renders an empty BVS payload, and the query is
  `"dengue hidratacao"` (two tokens). Assert one launch, exactly one
  navigation, and that its `q` decodes to
  `_build_query("dengue hidratacao", "all", operator="OR", title_scoped=False)`.
- Run: `pytest tests/medical/test_brazil_moh.py -k navigates_the_all_field_query_once -x`
  → RED (two identical navigations).

## Task 2: Remove the duplicate retry

- **Modify:** `src/scholar_mcp/medical/brazil_moh.py`,
  `tests/medical/test_brazil_moh.py`,
  `tests/medical/test_enamed_2026_misses_engines.py`
- **Produces:** `_camoufox_search(composed, count, ceiling)` (signature as on
  `main`).
- Drop `fallback_composed` from the call site and from `_camoufox_search`;
  replace the target loop with one navigation and one read.
- Delete the three `fallback_composed` tests, `_browser_engine`,
  `_EMPTY_BVS_PAYLOAD` and `_DENGUE_BVS_PAYLOAD`. Drop the `fallback_composed`
  argument from `test_camoufox_search_gives_up_on_a_shield_that_never_clears`
  and keep its one-budget and one-navigation assertions.
- Revert the `_fake_browser` signature in the ENAMED test.
- Run: Task 1 test → GREEN; then the three touched test files.
- Commit: `fix(brazil-moh): navigate the BVS browser query once`

## Task 3: Docs

- **Modify:** `AGENTS.md` (decision 7), `CHANGELOG.md` (Fixed entry for this PR).
- AGENTS.md: BVS reads after every goto; Sci-Hub reads after an aborted goto.
- CHANGELOG: remove the OR-retry sentences from the PR #58 Fixed entry.
- Commit: `docs: drop the BVS browser OR retry from AGENTS.md and the changelog`

## Verification

Full offline suite, then push to `fix/brazil-moh-browser-challenge-wait`.
