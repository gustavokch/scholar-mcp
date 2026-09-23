# PR #47 review remediation — topic gate on the PCDT fallback

Branch: `feat/brazil-fallback-topic-gate` (head `4fbfdf8`).
Review: https://github.com/gustavokch/scholar-mcp/pull/47#issuecomment-5804582204

## Goal

Close the one correctness finding on `_topic_filtered` (Solr boolean words act as
absent terms and empty every pool), pin the recall cost the docstring claims is
acceptable, and clear two hygiene nits. The zero-frequency rule keeps its designed
semantics: an absent topical term still empties the pool. That is the behaviour the
Q016 result depends on, and softening it needs live measurement rather than a
threshold tuned on three recorded pools.

Out of scope, by decision: extending the gate to `merged_records` (the non-outage
exit), and any change to the discriminating-tier rule itself. Both stay recorded in
the review comment.

## Architecture

One private helper, `_topic_filtered` in `src/scholar_mcp/medical/brazil_moh.py`,
called from the single fallback exit at L1381. Its query tokens must obey the same
discipline as every other query token in the module: `_usable_tokens` (L~381) drops
`_SOLR_BOOLEAN_WORDS` (L329) before a token reaches Solr, and the gate must drop
them before a token is treated as a topic term.

## Tech stack

Python >= 3.10, pytest (`asyncio_mode = auto`), respx for the HTTP mocks.
Test command in this worktree:

```
PYTHONPATH=src /Users/gus/Git/scholar-mcp/.worktrees/feat-warp-proxy/.venv/bin/pytest tests/medical/test_brazil_moh.py -q
```

---

## Task 1 — boolean operators must not act as topic terms

Files: modify `src/scholar_mcp/medical/brazil_moh.py`; test
`tests/medical/test_brazil_moh.py`.

Consumes: `_SOLR_BOOLEAN_WORDS`, `tokenize_portuguese`.
Produces: `_topic_filtered` term list free of Solr operators.

### Step 1 — failing test

```python
def test_topic_gate_ignores_solr_boolean_operators():
    """A query operator is not a topic term.

    ``and``/``or``/``not``/``to`` appear in no Portuguese record, so leaving
    them in the term list hands them frequency zero, which makes them the sole
    discriminating term and empties the pool. Every other query path in this
    module strips them through ``_usable_tokens``.
    """
    pool = [
        _g("Dengue: diagnóstico e manejo clínico", record_id="d1"),
        _g("Guia de manejo clínico: Bronquiolite", record_id="b1"),
    ]

    assert [r.record_id for r in _topic_filtered(pool, "dengue AND manejo")] == ["d1"]
    assert [r.record_id for r in _topic_filtered(pool, "dengue OR chikungunya")] == ["d1"]
```

### Step 2 — confirm failure

```
pytest tests/medical/test_brazil_moh.py -k solr_boolean -q
```

Expect both assertions to fail with `[] != ['d1']`.

### Step 3 — minimal implementation

In `_topic_filtered`:

```python
    terms = [
        t for t in tokenize_portuguese(query) if t not in _SOLR_BOOLEAN_WORDS
    ]
```

with a short comment naming `_usable_tokens` as the reason.

### Step 4 — confirm pass

```
pytest tests/medical/test_brazil_moh.py -k solr_boolean -q
```

### Step 5 — commit

```
git add src/scholar_mcp/medical/brazil_moh.py tests/medical/test_brazil_moh.py
git commit -m "fix(brazil_moh): strip Solr operators before the topic gate"
```

---

## Task 2 — pin the recall cost of the zero-frequency rule

Files: test only, `tests/medical/test_brazil_moh.py`.

This is a characterization test: the behaviour is already what the docstring
describes, so there is no red phase to stage. Its job is to make the tradeoff fail
loudly if anyone changes the rule in either direction.

### Step 1 — write the test

```python
def test_topic_gate_empties_a_covered_pool_when_one_query_term_is_absent():
    """The documented cost of the zero-frequency rule, pinned.

    The pool holds two genuine Dengue guides, but "gestantes" appears in none
    of them, so it takes frequency zero, becomes the sole discriminating term,
    and empties the pool. This is the conservative direction the gate chooses
    deliberately -- recorded here so a future IDF change cannot move it
    silently.
    """
    pool = [
        _g("Dengue: diagnóstico e manejo clínico", record_id="d1"),
        _g("Dengue - diagnóstico e manejo clínico adulto", record_id="d2"),
        _g("Guia de manejo clínico: Bronquiolite", record_id="b1"),
    ]

    assert _topic_filtered(pool, "dengue manejo clinico") != []
    assert _topic_filtered(pool, "dengue manejo clinico em gestantes") == []
```

### Step 2 — run

```
pytest tests/medical/test_brazil_moh.py -k one_query_term_is_absent -q
```

Expect pass on the first run; that is the point of a characterization test.

### Step 3 — commit

```
git add tests/medical/test_brazil_moh.py
git commit -m "test(brazil_moh): pin the topic gate's recall cost"
```

---

## Task 3 — hygiene nits

Files: modify `src/scholar_mcp/medical/brazil_moh.py`.

1. L449: replace the `ponytail:` marker, which appears nowhere else in the repo,
   with plain prose ("Follow-up: ...").
2. L470: `zip(records, per_record, strict=True)` — the lists are built from one
   another, and a length mismatch should fail rather than truncate.

### Verify and commit

```
pytest tests/medical/test_brazil_moh.py -q
git add src/scholar_mcp/medical/brazil_moh.py
git commit -m "refactor(brazil_moh): tighten the topic gate's zip and comment"
```

---

## Verification

1. `PYTHONPATH=src pytest tests/medical/test_brazil_moh.py -q` — was 163 passed
   before the change; expect 165 after (two new unit tests).
2. Full offline suite: `PYTHONPATH=src pytest -q`.
3. `git push origin feat/brazil-fallback-topic-gate`.
4. Report resolved findings and the ones left open by decision.
