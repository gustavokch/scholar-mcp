# PR #3 Review Remediation

**Goal:** Resolve the review findings on PR #3 (arXiv preprint tier + OpenAlex/Semantic Scholar providers) without changing the intended feature surface.

**Architecture:** `scholar_mcp` MCP server. Provider classes under `src/scholar_mcp/providers/` are instantiated once on a long-lived `WaterfallResolver` singleton and reused across every request, so any per-request state stored on a provider instance must be reset explicitly.

**Tech stack:** Python 3.10, httpx, respx, pytest (asyncio), uv.

**Spec reference:** PR #3 — `feature/citation-graph-and-extraction`.

---

## Task 1 — Reset `last_skip_reason` per request

**Target files:** Modify `src/scholar_mcp/providers/arxiv.py`. Test `tests/test_arxiv_provider.py`.

**Consumes:** `IdentifierMap`. **Produces:** `FullTextResponse | None`, accurate `last_skip_reason`.

`ArxivProvider` is the first provider whose skip reason is derived per request rather than from
config. The waterfall loop reads `provider.last_skip_reason` after every miss, so a stale value
mislabels a real miss as a skip.

1. Write a failing test: call `fetch_full_text` without an arXiv ID, then call it again with an
   arXiv ID whose PDF fetch misses, and assert `last_skip_reason == ""`.
2. `uv run pytest tests/test_arxiv_provider.py -v` — confirm failure.
3. Set `self.last_skip_reason = ""` on entry to `fetch_full_text`.
4. Re-run — confirm pass.
5. `git commit -m "fix(arxiv): reset last_skip_reason per request"`

## Task 2 — Strengthen the arXiv full-text test

**Target files:** Modify `tests/test_arxiv_provider.py`.

The existing assertion `res is None or res.source == "arxiv"` always holds, because a blank page
extracts zero characters and trips the `MIN_USEFUL_CHARS` guard. Generate a PDF carrying real
extractable text and assert the success path strictly.

1. Build a PDF page with >10 characters of extractable text.
2. Assert `res is not None`, `res.status == "full_text"`, `res.source == "arxiv"`, `res.url`.
3. `git commit -m "test(arxiv): assert full-text success path strictly"`

## Task 3 — Stop OpenAlex enrichment from clobbering `oa_url`

**Target files:** Modify `src/scholar_mcp/resolver.py`. Test `tests/test_openalex_s2.py`.

Guard the assignment so a `None` from OpenAlex cannot overwrite a value set by an earlier provider.

1. Test: existing metadata with `oa_url` set, OpenAlex work without `oa_url`; assert the original survives.
2. `git commit -m "fix(resolver): keep existing oa_url when OpenAlex has none"`

## Task 4 — Harden the arXiv PDF download branch

**Target files:** Modify `src/scholar_mcp/resolver.py`.

Wrap the branch in `try`/`except` to match the Unpaywall branch, validate the `%PDF-` magic bytes so
an arXiv HTML placeholder page is never written to disk as a PDF, and import `ARXIV_PDF` instead of
duplicating the URL literal.

1. Tests: an HTML body must not be returned as arXiv PDF bytes; a raising `get_bytes` must fall
   through to Sci-Hub rather than propagate.
2. `git commit -m "fix(resolver): validate arXiv PDF bytes and isolate failures"`

## Task 5 — Surface ignored Semantic Scholar filters

**Target files:** Modify `src/scholar_mcp/providers/semantic_scholar.py`.

The S2 graph search API has no author or journal filter. Rather than silently returning unfiltered
results, post-filter the returned papers on author and venue so the caller's constraint is honored.

1. Test: search with `author=` set drops papers without that author.
2. `git commit -m "fix(s2): honor author and journal filters by post-filtering"`

## Task 6 — Nits

**Target files:** `src/scholar_mcp/identifiers.py`, `src/scholar_mcp/providers/arxiv.py`,
`tests/test_config_models.py`, `tests/test_openalex_s2.py`.

Remove the dead `.pdf` strip, accept arXiv URLs carrying a query string or trailing slash, delete the
redundant `__init__`, close the leaked HTTP clients in tests, hoist the mid-file import, and rename
the mislabeled S2 error test.

1. `git commit -m "chore: address PR review nits"`

---

## Verification

`uv run pytest` must be fully green before the branch is pushed.
