# PR #20 Review Remediation — Documentation Accuracy

**Goal:** Correct every factual inaccuracy found in the PR #20 documentation review so `AGENTS.md` and `CHANGELOG.md` match the behavior actually implemented in `src/scholar_mcp/`.

**Scope:** Documentation only. No production code or test changes. The verification step for each task is a `grep` assertion against the source of truth in `src/`, plus the full test suite as a final regression gate.

**Spec reference:** PR review comment on https://github.com/gustavokch/scholar-mcp/pull/20

## Findings to remediate

| # | Severity | Location | Problem |
|---|----------|----------|---------|
| 1 | bug | `AGENTS.md` decision 7 | Claims Camoufox fallback covers Cochrane; Cochrane is routed through Europe PMC instead. |
| 2 | bug | `CHANGELOG.md` HTTP logging entry | Lists the credential *injection* set as the *redaction* set; `tool` is never redacted. |
| 3 | risk | `AGENTS.md` decision 2 | Tier order presented as unconditional; Unpaywall and Sci-Hub tiers can be skipped by settings. |
| 4 | risk | `AGENTS.md` decision 3 | TTL list reads exhaustive but omits three medical TTL settings. |
| 5 | nit | `AGENTS.md` module tree | Omits `src/scholar_mcp/data/`. |
| 6 | nit | `AGENTS.md` decision 7 | Omits the `ENABLE_BROWSER_FALLBACK` gate on the pediatrics browser fallback. |
| 7 | nit | `AGENTS.md` decision 8 | Asserts an unverifiable DSpace minor version ("7.6"). |
| 8 | nit | `docs/superpowers/plans/2026-09-07-pr19-review-round2-remediation.md` | Hardcoded absolute local path to the developer's venv. |

---

## Task 1 — Correct the Camoufox source list

**Files:** Modify `AGENTS.md` (decision 7).

**Source of truth:** `src/scholar_mcp/medical/databases.py:18-21` and `:100-103` — the Cochrane HTML site blocks headless browsers, so `_search_cochrane` queries the Europe PMC REST API and tags the records as Cochrane. `camoufox` is imported only in `src/scholar_mcp/providers/scihub.py:83` and `src/scholar_mcp/medical/pediatrics.py:182`.

**Step 1 — Verify the claim is false:**
```bash
grep -rn "camoufox" src/scholar_mcp/ | grep -c "databases.py"   # expect 0
```

**Step 2 — Edit:** Replace the bot-protected source list `(Sci-Hub mirrors, AAP Bright Futures/Policy, Cochrane)` with `(Sci-Hub mirrors, AAP Bright Futures, AAP Policy)`, and state that Cochrane is instead served through the Europe PMC REST API because its site blocks headless browsers too.

**Step 3 — Verify:**
```bash
grep -n "Cochrane" AGENTS.md   # must not describe Cochrane as a Camoufox target
```

**Step 4 — Commit:** `docs(agents): fix Camoufox fallback source list`

---

## Task 2 — Correct the credential redaction key list

**Files:** Modify `CHANGELOG.md` (HTTP diagnostic logging entry).

**Source of truth:** `src/scholar_mcp/utils/http.py:24-26` defines `SENSITIVE_QUERY_PARAMS = {"api_key", "apikey", "email", "token", "access_token"}`. `src/scholar_mcp/utils/http.py:117-122` injects `api_key`, `email`, and `tool` for NCBI hosts.

**Step 1 — Verify:**
```bash
grep -n "SENSITIVE_QUERY_PARAMS" -A5 src/scholar_mcp/utils/http.py
```

**Step 2 — Edit:** Change the redaction list to `api_key`, `apikey`, `email`, `token`, `access_token` and stop implying `tool` is redacted.

**Step 3 — Verify:**
```bash
grep -n "credential redaction" CHANGELOG.md
```

**Step 4 — Commit:** `docs(changelog): correct HTTP redaction key list`

---

## Task 3 — Document the conditional tier skips

**Files:** Modify `AGENTS.md` (decision 2).

**Source of truth:** `src/scholar_mcp/resolver.py:168-171` sets `unpaywall_skip = "PREFER_SCIHUB_OVER_UNPAYWALL"` when both `prefer_scihub_over_unpaywall` and `enable_scihub` are true; `:177-180` sets `scihub_skip` when `scihub_tier_enabled()` is false.

**Step 1 — Verify:**
```bash
sed -n '160,182p' src/scholar_mcp/resolver.py
```

**Step 2 — Edit:** Annotate Tier 3 and Tier 5 with their skip conditions, and note that Tier 4 self-skips when no arXiv ID is known.

**Step 3 — Verify:**
```bash
grep -n "PREFER_SCIHUB_OVER_UNPAYWALL" AGENTS.md
```

**Step 4 — Commit:** `docs(agents): note conditional waterfall tier skips`

---

## Task 4 — Complete the medical cache TTL list

**Files:** Modify `AGENTS.md` (decision 3).

**Source of truth:** `src/scholar_mcp/config.py:57-69` — thirteen TTL fields. The doc lists ten; `cache_ttl_pediatric_journals` (3600), `cache_ttl_child_health` (604800), and `cache_ttl_pediatric_drugs` (86400) are missing.

**Step 1 — Verify:**
```bash
grep -c "cache_ttl_" src/scholar_mcp/config.py
```

**Step 2 — Edit:** Add the three missing entries in `config.py` declaration order.

**Step 3 — Verify:**
```bash
grep -n "Pediatric Journals" AGENTS.md
```

**Step 4 — Commit:** `docs(agents): complete medical cache TTL list`

---

## Task 5 — Add the `data/` package directory to the module tree

**Files:** Modify `AGENTS.md` (Architecture tree).

**Source of truth:** `src/scholar_mcp/data/` holds `scimago_sjr.json` and `SOURCES.md`, both referenced by decision 6.

**Step 1 — Verify:**
```bash
ls src/scholar_mcp/data
```

**Step 2 — Edit:** Insert a `data/` node in the tree, alphabetically between `citation_check.py`-level entries and `medical/`.

**Step 3 — Verify:**
```bash
grep -n "scimago_sjr.json" AGENTS.md
```

**Step 4 — Commit:** `docs(agents): add data/ to module tree`

---

## Task 6 — Document the `ENABLE_BROWSER_FALLBACK` gate

**Files:** Modify `AGENTS.md` (decision 7).

**Source of truth:** `src/scholar_mcp/config.py:70` and `:169-171` — `enable_browser_fallback` defaults to true and reads `ENABLE_BROWSER_FALLBACK`, falling back to the legacy `ENABLE_PLAYWRIGHT_FALLBACK`. `src/scholar_mcp/medical/pediatrics.py:330` gates the pediatrics browser path on it.

**Step 1 — Verify:**
```bash
grep -n "enable_browser_fallback" src/scholar_mcp/config.py src/scholar_mcp/medical/pediatrics.py
```

**Step 2 — Edit:** Name the setting and its legacy alias in decision 7.

**Step 3 — Verify:**
```bash
grep -n "ENABLE_PLAYWRIGHT_FALLBACK" AGENTS.md
```

**Step 4 — Commit:** `docs(agents): note ENABLE_BROWSER_FALLBACK gate`

---

## Task 7 — Drop the unverifiable DSpace minor version

**Files:** Modify `AGENTS.md` (decision 8).

**Source of truth:** `src/scholar_mcp/medical/who_iris.py:13-20` uses DSpace 7 REST paths (`/server/api/discover/...`, `/server/api/core/...`). Nothing in the repository pins the 7.6 patch line.

**Step 1 — Verify:**
```bash
grep -rn "7\.6" src/scholar_mcp/medical/who_iris.py   # expect no match
```

**Step 2 — Edit:** Replace "DSpace 7.6 REST API" with "DSpace 7 REST API".

**Step 3 — Verify:**
```bash
grep -n "DSpace" AGENTS.md
```

**Step 4 — Commit:** `docs(agents): drop unverifiable DSpace minor version`

---

## Task 8 — Remove the hardcoded developer path from the PR #19 plan

**Files:** Modify `docs/superpowers/plans/2026-09-07-pr19-review-round2-remediation.md` (lines 36 and 110).

**Step 1 — Verify:**
```bash
grep -n "/Users/gus" docs/superpowers/plans/2026-09-07-pr19-review-round2-remediation.md
```

**Step 2 — Edit:** Replace `/Users/gus/Git/scholar-mcp/.venv/bin/pytest` with the repo-relative `.venv/bin/pytest`.

**Step 3 — Verify:**
```bash
grep -c "/Users/gus" docs/superpowers/plans/2026-09-07-pr19-review-round2-remediation.md   # expect 0
```

**Step 4 — Commit:** `docs(plan): use repo-relative venv path`

---

## Final gate

```bash
.venv/bin/python -m pytest -q     # must be 100% green
git push origin docs/sync-project-docs
```
