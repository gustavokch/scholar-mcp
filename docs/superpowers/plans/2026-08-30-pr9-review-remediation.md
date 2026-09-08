# PR #9 Review Remediation

**Goal:** Fix the findings from the PR #9 review
(https://github.com/gustavokch/scholar-mcp/pull/9#issuecomment-5472906192).

**Spec:** Review comment https://github.com/gustavokch/scholar-mcp/pull/9#issuecomment-5472906192

**Architecture:** `PediatricsEngine` scrapes two AAP-hosted SPA search pages with a
fallback chain (static scrape → PubMed publication-type search → Playwright);
`FDAClient` searches api.fda.gov with field-restricted query variants. Both sit on
`AsyncHttpClient`, which now supports `ok_statuses` for 404-as-no-match.

**Tech Stack:** Python 3.12, httpx + respx, pytest-asyncio.

---

## Task 1 — FDA NDC: check 404 before parsing JSON

**Target files:** Modify `src/scholar_mcp/medical/fda.py`; Test `tests/medical/test_fda.py`

**Consumes/Produces:** `AsyncHttpClient.get(ok_statuses={404})` (existing). Produces:
`get_drug_by_ndc` treats any 404 (JSON or not) as genuine absence, `meta.error=False`.

**Why:** `resp.json()` runs before the 404 branch. A 404 body that is not JSON
(proxy/CDN error page) raises `JSONDecodeError` → caught as fetch failure →
`meta.error=True` for a genuine no-match. `search_drugs` skips the parse on 404;
both NDC variants must do the same.

- Step 1 — failing test (append to `tests/medical/test_fda.py`):

  ```python
  @respx.mock
  async def test_get_drug_by_ndc_404_non_json_body_is_absence_not_error(tmp_path: Path):
      """A 404 with a non-JSON body (proxy error page) is still 'no such label'."""
      client, cache, http_client = await _make_client(tmp_path)
      route = respx.get(FDA_URL).respond(status_code=404, text="Gateway timeout page")

      drug, meta = await client.get_drug_by_ndc("99999-999")
      assert drug is None
      assert meta.error is False
      assert route.call_count == 2  # quoted + unquoted variants both tried

      await cache.close()
      await http_client.aclose()
  ```

- Step 2 — `uv run pytest tests/medical/test_fda.py::test_get_drug_by_ndc_404_non_json_body_is_absence_not_error -x` → fails (error is True).
- Step 3 — in both `get_drug_by_ndc` variants: replace

  ```python
  data = resp.json()
  results = [] if resp.status_code == 404 else data.get("results", [])
  ```

  with

  ```python
  if resp.status_code == 404:
      results = []  # api.fda.gov 404 = "no matches found"; skip body parsing
  else:
      data = resp.json()
      results = data.get("results", [])
  ```

- Step 4 — same pytest command → passes.
- Step 5 — `git add src/scholar_mcp/medical/fda.py tests/medical/test_fda.py && git commit -m "fix(fda): skip body parse on 404 so non-JSON error pages stay 'no-match'"`

## Task 2 — Overlap filter on direct `search_bright_futures` / `search_aap_policy` paths

**Target files:** Modify `src/scholar_mcp/medical/pediatrics.py`; Test `tests/medical/test_pediatrics.py`

**Consumes/Produces:** `PediatricsEngine._matches_query` (exists). Produces: both
public scrape methods drop SPA nav junk, matching the filter `search_aap_guidelines`
already applies.

**Why:** `server.py:457-459` routes to the two public methods directly when the
caller picks a source; those paths return unfiltered static nav items
("Quality Improvement", …) for any query.

- Step 1 — failing test (reuse the junk HTML fixture from the guidelines test):

  ```python
  @respx.mock
  async def test_direct_scrapes_drop_items_unrelated_to_query(tmp_path: Path):
      engine, cache, http_client = await _engine(tmp_path)
      respx.get(BF_URL).respond(
          html="""
      <html><body>
        <div class="search-result">
          <h3 class="title"><a href="/practice-management/bright-futures/quality">Quality Improvement</a></h3>
          <p>Site navigation.</p>
        </div>
      </body></html>
      """
      )
      respx.get(AAP_URL).respond(status_code=403)

      bf, bf_meta = await engine.search_bright_futures("nutrition")
      assert bf == []
      assert bf_meta.error is False

      await cache.close()
      await http_client.aclose()
  ```

- Step 2 — run → fails (junk item returned).
- Step 3 — add helper and use it in both public methods:

  ```python
  def _filter_matches(self, results: list[PediatricGuideline], query: str) -> list[PediatricGuideline]:
      """Drop SPA navigation junk whose title shares no word with the query."""
      query_tokens = set(re.findall(r"\w+", query.lower()))
      return [g for g in results if self._matches_query(g.title, query_tokens)]
  ```

  In `search_bright_futures` and `search_aap_policy`, wrap the scrape result:
  `results = self._filter_matches(results, query)`.
  In `search_aap_guidelines`, replace the inline list comprehension with the helper.

- Step 3b — `test_search_aap_policy_html` fixtures must still pass: their titles
  ("Bright Futures…", "AAP…") share tokens with their queries ("nutrition",
  "asthma"); verify fixture titles before running (adjust only if a fixture
  legitimately shares no token).
- Step 4 — run → pass.
- Step 5 — `git add src/scholar_mcp/medical/pediatrics.py tests/medical/test_pediatrics.py && git commit -m "fix(pediatrics): apply query-overlap filter on direct scrape paths too"`

## Task 3 — Last-resort browser scrape: camoufox (replaces Playwright) + encoded query

**Target files:** Modify `src/scholar_mcp/medical/pediatrics.py`, `src/scholar_mcp/config.py`,
`pyproject.toml`, `README.md`; Test `tests/medical/test_pediatrics.py`

**Consumes/Produces:** `camoufox.async_api.AsyncCamoufox` (stealth Firefox; installed in
venv, browser binary fetched at `~/Library/Caches/camoufox`). Produces: `_camoufox_scrape`
last-resort fetch using `urllib.parse.urlencode` for `?q=`; setting renamed
`enable_playwright_fallback` → `enable_browser_fallback` with `ENABLE_PLAYWRIGHT_FALLBACK`
kept as a legacy env alias.

**Why:** Playwright chromium gets 403'd by Cloudflare on the AAP hosts; camoufox
(anti-detection Firefox) is the replacement. It manages its own coherent
fingerprint/UA — passing `BROWSER_UA` (Chrome string) on a Firefox fork is a
detection signal, so the custom UA goes. The raw `f"{url}?q={query}"` URL is not
encoded; `&`, `#`, spaces in an agent-supplied query misroute the request — encode
regardless of which browser drives the page.

- Step 1 — failing test `test_last_resort_browser_scrape_uses_camoufox_and_encodes_query`:
  fake `camoufox.async_api.AsyncCamoufox` (context manager yielding a browser whose
  `new_page()` page records `goto` URLs), plus a fake `playwright.async_api` module
  recording that the dead path stays dead. Assert camoufox attempted and
  `?q=ibuprofen+%26+children` captured.
- Step 2 — run → fails (camoufox never attempted).
- Step 3 — implementation: `_playwright_scrape` → `_camoufox_scrape` using
  `AsyncCamoufox(headless=True)` with no UA override and the encoded URL; call
  site + gate updated; config field `enable_browser_fallback` (env
  `ENABLE_BROWSER_FALLBACK`, legacy `ENABLE_PLAYWRIGHT_FALLBACK` honored; README
  row updated); pyproject gains `camoufox>=0.5.0` (playwright stays — camoufox
  drives via it); existing `test_search_aap_guidelines_playwright_is_last_resort`
  fake rewritten to the camoufox API.
- Step 4 — run → pass.
- Step 5 — `git commit -m "fix(pediatrics): camoufox replaces playwright for last-resort scrape; encode query"`

## Task 4 — Coverage: browser fallback skipped when PubMed yields results (test-only)

**Target files:** Modify `tests/medical/test_pediatrics.py`

**Consumes/Produces:** none; locks the fallback-chain ordering guarantee that
Camoufox never launches when the PubMed fallback already returned results.

- Step — new test mirroring the last-resort test but with
  `mock_pubmed.search_articles` returning one article; assert the camoufox fake
  was never entered and the returned guideline is the PubMed one. Coverage
  addition — must pass immediately against current source; a failure means the
  ordering is broken and the source needs the fix.
- Commit — `git commit -m "test(pediatrics): browser fallback skipped when pubmed fallback yields results"`


## Verification

- `.venv/bin/python -m pytest -q` → all green (baseline 264 + additions).
- `git push origin fix/fda-404-and-aap-guideline-fallback`.
- Report on PR #9 with link back to the review comment.

## Post-plan finding (out of PR #9 scope, reported to author)

The uncommitted Cochrane → Europe PMC reroute in `databases.py` uses
`resultType=lite`, which omits `abstractText` (verified live: lite result keys
have no `abstractText`) — every Cochrane record gets an empty abstract. Use
`resultType=core`. Not applied: that work belongs to a separate change.
