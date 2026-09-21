# ENAMED Track 1: BVS retrieval fix for q26 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Brazilian dengue clinical-management manual retrievable through `brazil_guidelines` for the clinical-scenario queries the exam pipeline actually issues, by widening the document-type filter, enlarging the over-fetch window, and adding an OR-title retrieval stage.

**Architecture:** Three coupled changes to the existing BVS stage chain in `brazil_moh.py`. The type filter currently excludes the `monography` class; widening it roughly triples topical hit counts, so the over-fetch window must grow with it or targets truncate before client-side re-ranking sees them. A new OR-title stage runs after the existing strict and relaxed title stages fail and before the all-field fallback: it ORs the user's tokens in the `ti:` field, which surfaces the dengue manual where every AND conjunction returns zero.

**Tech Stack:** Python 3.12+, httpx, respx (HTTP mocking), pytest, uv.

**Spec:** `~/Git/zimqa/docs/superpowers/specs/2026-09-18-enamed-failure-remediation-design.md` (Track 1, findings F1–F4)

## Global Constraints

- Repository: `~/Git/scholar-mcp`. The spec lives in the `zimqa` repo; the code does not.
- Test command is `uv run --extra dev pytest`. Plain `uv run pytest` picks up a homebrew pytest without `openai` installed and fails for unrelated reasons.
- Live network tests are marked `pytest.mark.network` and are excluded from the default suite. Never add a network call to an unmarked test.
- `MAX_PAGE_SIZE = 200` is a hard endpoint ceiling. `count` must never exceed it.
- BVS enforces rate limits and fronts a CDN anti-bot shield that returns 403 to plain clients. Do not add per-token or per-query probe requests to the hot path; this plan adds at most one extra request per search, and only when every earlier stage returned nothing.
- `pesquisa.bvsalud.org` flaps 502 for minutes at a time. A failing live test must be re-run before being treated as a regression.

## Sequencing with the A-Z scraper plan

This plan fixes q26 only. q33 (the Tuberculosis control manual) is fixed by `docs/superpowers/plans/2026-09-18-govbr-az-publication-scraper.md`, whose Task 8 already gates both manuals.

**Execute this plan first.** It touches `brazil_moh.py` at lines 88, 125, and 578–603. The scraper plan's Task 7 touches lines 127, 442, 508–530, 552–560, and 661–683 of the same file; its line references shift by a few lines after this plan lands, and its Task 7 code blocks should be applied by context rather than by line number.

## Why the previously-designed approach is not in this plan

The spec's F2 originally proposed a title-field document-frequency ladder that drops the least-frequent token first. Live measurement refuted it: for q26 the title-DF values are `grupo`=647, `dengue`=362, `manejo`=347, `B`=302, `observação`=156, `parenteral`=55, `hidratação`=4, so ascending-DF order discards `manejo` before the generic `grupo`, which is backwards. Do not reintroduce it.

The OR-title stage in Task 2 is the measured replacement: with the widened filter and `count=100`, the target document appears at raw position 13 of the fetched window, inside the range client-side re-ranking can act on.

## File Structure

- `src/scholar_mcp/medical/brazil_moh.py` — the only production file changed. Holds the filter constants, the over-fetch factor, and the BVS stage chain. All three changes are local to it; no new module is warranted.
- `tests/medical/test_brazil_moh.py` — unit coverage with `respx`-mocked BVS responses. Two existing tests assert the old over-fetch arithmetic and must be updated in Task 1.
- `tests/medical/test_brazil_moh_live.py` — existing live-network file. Task 3 appends the q26 acceptance test here rather than creating a new file, so all live BVS checks stay in one place.

---

### Task 1: Widen the type filter and enlarge the over-fetch window

These ship as one task because they are not independently correct. Widening alone roughly triples topical hit counts while `count` stays at 30, which can push a target out of the fetched window and regress searches that currently succeed. A reviewer cannot sensibly approve one and reject the other.

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py:88` (`OVERFETCH_FACTOR`), `:125` (`BASE_FILTER`)
- Test: `tests/medical/test_brazil_moh.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `BASE_FILTER` (str) now matching both `non-conventional` and `monography`; `OVERFETCH_FACTOR = 10`. Task 2 relies on the widened filter being already in `BASE_FILTER`, since `_build_query` prepends it to every composed query.

- [ ] **Step 1: Write the failing tests**

Append to `tests/medical/test_brazil_moh.py`:

```python
def test_base_filter_admits_monography_and_non_conventional():
    # The Tuberculosis and Dengue Ministry manuals are indexed as
    # type:"monography"; the previous filter matched only
    # type:"non-conventional" and excluded them unconditionally.
    assert 'type:"monography"' in BASE_FILTER
    assert 'type:"non-conventional"' in BASE_FILTER
    assert 'la:"pt"' in BASE_FILTER


def test_build_query_keeps_the_type_alternation_grouped():
    # The type alternation must stay parenthesized: composed into a query
    # whose clauses are joined with AND, a bare OR would bind across the
    # language filter and match non-Portuguese records.
    composed = _build_query("dengue", "all")
    assert '(type:"non-conventional" OR type:"monography")' in composed
    assert composed.endswith("(dengue)")
```

Add `BASE_FILTER` and `_build_query` to the existing import block from `scholar_mcp.medical.brazil_moh` at the top of the file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev pytest tests/medical/test_brazil_moh.py -k "base_filter or type_alternation" -v`

Expected: FAIL. `test_base_filter_admits_monography_and_non_conventional` fails on the missing `type:"monography"`.

- [ ] **Step 3: Widen the filter and raise the over-fetch factor**

In `src/scholar_mcp/medical/brazil_moh.py`, replace line 88:

```python
OVERFETCH_FACTOR = 10
```

and replace line 125:

```python
BASE_FILTER = 'la:"pt" AND (type:"non-conventional" OR type:"monography")'
```

Replace the comment above `OVERFETCH_FACTOR` (or add one) with:

```python
# Over-fetch, then re-rank client-side, then slice. The factor is tied to
# BASE_FILTER's width: admitting the monography class takes the pt pool from
# roughly 23.6k to 117.8k documents and about triples the hit count of a
# topical query, so a factor of 3 would truncate targets out of the window
# before rank_brazil_guidelines ever sees them. At limit=10 this fetches 100.
OVERFETCH_FACTOR = 10
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run --extra dev pytest tests/medical/test_brazil_moh.py -k "base_filter or type_alternation" -v`

Expected: PASS (2 tests).

- [ ] **Step 5: Update the two tests that assert the old over-fetch arithmetic**

In `tests/medical/test_brazil_moh.py`, `test_search_requests_overfetched_count` asserts `requested["count"] == "30"`. With `OVERFETCH_FACTOR = 10` and `limit=10` the value is now `100`:

```python
        assert requested["count"] == "100"
```

`test_search_caps_requested_count_at_page_size` calls `search_guidelines("dengue", limit=50)` and asserts `"150"`. `min(50 * 10, 200)` is now the cap itself:

```python
        assert route.calls[0].request.url.params["count"] == "200"
```

- [ ] **Step 6: Run the full brazil suite to catch any other filter-shape assertion**

Run: `uv run --extra dev pytest tests/medical/test_brazil_moh.py -v`

Expected: PASS. If a test asserts on the exact `BASE_FILTER` string or on substring ordering within the composed query, update it to assert on the presence of the clauses rather than their order — the alternation is new, and order-sensitive assertions on it are brittle.

- [ ] **Step 7: Commit**

```bash
git add src/scholar_mcp/medical/brazil_moh.py tests/medical/test_brazil_moh.py
git commit -m "fix(brazil_moh): admit the monography class and widen the over-fetch

The Ministry manuals the exam pipeline needs are indexed as
type:\"monography\", which the previous BASE_FILTER excluded outright.
Widening it roughly triples topical hit counts, so OVERFETCH_FACTOR rises
from 3 to 10 in the same change: left at 3, the wider pool would truncate
targets out of the fetched window before client-side re-ranking runs."
```

---

### Task 2: Add the OR-title retrieval stage

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py:578-611` (insert a stage between the relaxation loop and the all-field fallback)
- Test: `tests/medical/test_brazil_moh.py`

**Interfaces:**
- Consumes: `BASE_FILTER` widened in Task 1; the existing `_build_query(query, collection, operator, title_scoped, tokens)` helper, which already supports `operator="OR"` together with `title_scoped=True` — no new query builder is needed.
- Produces: no new public symbol. The stage label string `"title-scoped-or"` is passed to `self._stage` and appears in timeout warnings.

- [ ] **Step 1: Write the failing test**

Append to `tests/medical/test_brazil_moh.py`:

```python
@respx.mock
async def test_search_falls_back_to_or_title_before_all_field(tmp_path: Path):
    # A scenario query whose tokens never co-occur in one title: every AND
    # conjunction returns nothing, and the ladder exhausts without a hit.
    # ORing the tokens in ti: is what surfaces the manual.
    engine, cache, http_client = await _engine(tmp_path)
    _stub_pcdt_empty(engine)
    try:
        target = _bvs_doc(
            record_id="biblio-dengue",
            title="Dengue: classificação de risco e manejo do paciente",
        )

        def _respond(request: httpx.Request) -> httpx.Response:
            composed = request.url.params["q"]
            # Only the OR-title composition returns the manual.
            if "ti:dengue OR" in composed:
                return httpx.Response(200, json=_bvs_response([target]))
            return httpx.Response(200, json=_bvs_response([]))

        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(side_effect=_respond)
        records, meta = await engine.search_guidelines(
            "dengue grupo manejo hidratação parenteral", limit=10
        )

        assert meta.error is False
        assert [r.record_id for r in records] == ["biblio-dengue"]

        composed_queries = [c.request.url.params["q"] for c in route.calls]
        or_title = [q for q in composed_queries if "ti:dengue OR" in q]
        assert or_title, composed_queries
        # The stage runs after the AND ladder and before the all-field
        # fallback, so no unscoped query is issued once it succeeds.
        assert not any(
            q.startswith('la:"pt"') and "ti:" not in q for q in composed_queries
        ), composed_queries
    finally:
        await cache.close()
        await http_client.aclose()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --extra dev pytest tests/medical/test_brazil_moh.py::test_search_falls_back_to_or_title_before_all_field -v`

Expected: FAIL. No OR-title query is composed, so `assert or_title` fails and the returned record list is empty.

- [ ] **Step 3: Insert the stage**

In `src/scholar_mcp/medical/brazil_moh.py`, immediately after the relaxation `for` loop ends (after the `break` on line 603) and before the `# Fall back to all-field query` comment on line 605, insert:

```python
        # OR-title stage. Every AND conjunction above requires all tokens to
        # share one title, which a clinical-scenario query rarely satisfies:
        # measured on the dengue item, the strict stage and all three
        # relaxation steps return zero while ORing the same tokens in ti:
        # surfaces the manual inside the over-fetch window for the client
        # ranker to lift. It runs before the all-field fallback because a
        # title match is a stronger signal than an abstract match, and it is
        # skipped for a single token, where it would compose identically to
        # the strict stage and waste a request.
        if (
            not records
            and len(tokens) >= 2
            and not title_relaxed_errored
            and not self._is_bvs_shielded(state)
        ):
            or_title_composed = _build_query(
                query, norm_collection, operator="OR", title_scoped=True
            )
            or_title_records, or_title_errored = await self._stage(
                "title-scoped-or",
                self._fetch_records(or_title_composed, count, state),
                ([], True),
            )
            errored_any = errored_any or or_title_errored
            bvs_errored = bvs_errored or or_title_errored
            if or_title_errored:
                title_relaxed_errored = True
            else:
                records = or_title_records
```

Setting `title_relaxed_errored` on an errored OR-title stage reuses the existing halt signal: the two stages below already consult it, and an endpoint that failed this variant will fail those too.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --extra dev pytest tests/medical/test_brazil_moh.py::test_search_falls_back_to_or_title_before_all_field -v`

Expected: PASS.

- [ ] **Step 5: Run the full brazil suite for stage-sequencing regressions**

Run: `uv run --extra dev pytest tests/medical/test_brazil_moh.py -v`

Expected: PASS. Tests that count BVS calls for a query reaching the all-field or OR fallback now see one additional call. Update those call-count assertions to the new number; do not weaken them to inequalities.

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/medical/brazil_moh.py tests/medical/test_brazil_moh.py
git commit -m "feat(brazil_moh): add an OR-title stage before the all-field fallback

A clinical-scenario query rarely has all its tokens in one document title,
so the strict title stage and the relaxation ladder both return nothing.
ORing the same tokens in ti: surfaces the manual while keeping the match
title-scoped, which is a stronger signal than the all-field fallback below."
```

---

### Task 3: Live acceptance test for the dengue item

**Files:**
- Modify: `tests/medical/test_brazil_moh_live.py`

**Interfaces:**
- Consumes: `BrazilMoHEngine.search_guidelines` with the Task 1 and Task 2 changes in place.
- Produces: nothing; this is the acceptance gate for the spec's q26 criterion.

- [ ] **Step 1: Read the existing live file's fixtures**

Run: `sed -n '1,40p' tests/medical/test_brazil_moh_live.py`

Use whatever engine fixture and `pytestmark` that file already defines. Do not introduce a second fixture style.

- [ ] **Step 2: Write the live acceptance test**

Append to `tests/medical/test_brazil_moh_live.py`, adapting the fixture name to match what Step 1 found:

```python
async def test_live_scenario_query_surfaces_the_dengue_manual(brazil_engine_live):
    """The spec's q26 acceptance criterion.

    This is the exact query the exam pipeline issued in run 10. Before the
    OR-title stage every AND conjunction returned zero and the all-field
    fallback returned topically adjacent journal articles instead.
    """
    records, meta = await brazil_engine_live.search_guidelines(
        "dengue grupo B manejo hidratação parenteral observação", limit=10
    )
    assert meta.error is False, "BVS flaps 502; re-run before treating as a failure"
    titles = [normalize_portuguese(r.title) for r in records]
    assert any(
        "dengue" in t and "manejo" in t for t in titles
    ), titles
```

Import `normalize_portuguese` from `scholar_mcp.medical.ranking` if the file does not already import it.

- [ ] **Step 3: Run the live test**

Run: `uv run --extra dev pytest tests/medical/test_brazil_moh_live.py -m network -k dengue -v`

Expected: PASS.

If it fails with an empty result list, first re-run once — the endpoint flaps 502. If it fails consistently with results present but no dengue manual among them, the document reached the fetch window but lost the client-side ranking; report this before changing ranking weights, because re-weighting `rank_brazil_guidelines` affects every Brazilian search and is outside this plan's scope.

- [ ] **Step 4: Confirm the default suite still excludes it**

Run: `uv run --extra dev pytest tests/medical/test_brazil_moh_live.py -v`

Expected: the live tests are deselected, 0 run.

- [ ] **Step 5: Run the whole suite**

Run: `uv run --extra dev pytest`

Expected: PASS, no new failures against the pre-existing baseline.

- [ ] **Step 6: Commit**

```bash
git add tests/medical/test_brazil_moh_live.py
git commit -m "test(brazil_moh): gate the q26 dengue-manual retrieval criterion"
```

---

### Task 4: Downstream regression check against ENAMED 2025

**Files:**
- No source changes. This task produces an evaluation artifact in the `zimqa` repo.

**Interfaces:**
- Consumes: the scholar-mcp changes from Tasks 1–3, installed into the zimqa environment.
- Produces: a run directory under `~/Git/zimqa/eval/results/` used to accept or reject the whole plan.

- [ ] **Step 1: Install the updated scholar-mcp into the zimqa environment**

zimqa consumes scholar-mcp as a dependency (`.venv/lib/python3.13/site-packages/scholar_mcp/`). Install the working copy so the eval exercises the new code, following whatever mechanism the zimqa lockfile already uses for this dependency. Verify before running:

```bash
cd ~/Git/zimqa && python3 -c "
from scholar_mcp.medical.brazil_moh import BASE_FILTER, OVERFETCH_FACTOR
print(BASE_FILTER); print('OVERFETCH_FACTOR', OVERFETCH_FACTOR)
"
```

Expected: the widened filter string and `OVERFETCH_FACTOR 10`. If it prints the old values the eval would measure unchanged code and its result is meaningless.

- [ ] **Step 2: Run ENAMED 2025**

```bash
cd ~/Git/zimqa && python3 scripts/run_enamed_eval.py \
  --run-name enamed-track1-or-title-1 \
  --locale BR
```

- [ ] **Step 3: Score against the persistent-failure baseline**

```bash
cd ~/Git/zimqa && python3 - <<'EOF'
import json
key={a['question']:a['answer'] for a in json.load(open('enamed/gabarito.json'))['answers']}
rows=[json.loads(l) for l in open('eval/results/enamed-track1-or-title-1/answers.jsonl')]
PERSIST={26,83,44,97,33,25}
c=n=forced=0; newly_wrong=[]
for r in rows:
    k=key.get(r['number'])
    if k in (None,'Anulada'): continue
    n+=1; ok=r.get('choice')==k; c+=ok
    forced+=bool(r.get('forced'))
    if not ok and r['number'] not in PERSIST and not r.get('forced'):
        newly_wrong.append(r['number'])
print(f"score {c}/{n}  forced={forced}")
print("q26:", "CORRECT" if next(r for r in rows if r['number']==26)['choice']==key[26] else "still wrong")
print("newly wrong (non-forced, not previously persistent):", newly_wrong)
EOF
```

Expected: `q26: CORRECT`, and `newly wrong` empty.

**Acceptance:** q26 flips to correct and no non-forced item that previously passed now fails. A non-empty `newly_wrong` list means the widened filter displaced a document some other item depended on — report the specific items rather than adjusting ranking weights, which is out of scope here.

Note that `forced` counts 429-exhaustion guesses and varies run to run by up to 16 points of score. Judge this task on `q26` and `newly_wrong`, not on the headline score.

- [ ] **Step 4: Record the result**

Append the run name, score, forced count, and the two acceptance outcomes to the spec's Track 1 section at `~/Git/zimqa/docs/superpowers/specs/2026-09-18-enamed-failure-remediation-design.md`, then commit in the zimqa repo:

```bash
cd ~/Git/zimqa && git add docs/superpowers/specs/2026-09-18-enamed-failure-remediation-design.md
git commit -m "docs(spec): record the Track 1 q26 regression result"
```

---

## After this plan

q33 remains open. Execute `docs/superpowers/plans/2026-09-18-govbr-az-publication-scraper.md` next; its Task 8 gates the Tuberculosis control manual and confirms A-Z records surface in the default collection. Apply its Task 7 edits by context, not by the line numbers it cites, since this plan shifted them.

## Self-Review

**Spec coverage.** F1 and F3 are Task 1. F4's engine-side realisation is Task 2. The spec's q26 acceptance criterion is Task 3; its regression criterion is Task 4. The spec's q33 criterion is deliberately not covered here and is delegated to the scraper plan, as recorded in "Sequencing" and "After this plan". F2's original ladder is explicitly excluded with the measurement that refuted it, so a future reader does not reinstate it. F4's prompt-bullet half (approach C, in the zimqa repo) is dropped: it was additive to a deterministic fix, and Task 2 makes it unnecessary for q26 — if the scraper plan later needs it for q33 it belongs in that plan.

**Placeholder scan.** No TBDs. Every code step carries the code. Task 3 Step 1 deliberately reads the existing fixture rather than inventing one, because inventing a second fixture style in a file I have not read would be the larger error; the step names exactly what to look for.

**Type consistency.** `_build_query(query, collection, operator, title_scoped, tokens)` is used in Task 2 with the same signature the module already defines at line 287. `_stage(label, coro, default)` matches line 485. `_is_bvs_shielded(state)` matches line 480. `BASE_FILTER` and `OVERFETCH_FACTOR` are module-level names asserted in Task 1 and consumed in Task 2 and Task 4.
