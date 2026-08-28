# Handoff — `scholar-mcp` implementation

**Date:** 2026-08-28
**Repo:** `/Users/gus/Git/scihub-mcp` (branch `main`, clean except one untracked directory)
**Next session's job:** implement the plan, starting at Task 1.

---

## What this project is becoming

`scihub-mcp` (published, version 0.4.0, four Sci-Hub-only tools) is being rewritten into
`scholar-mcp`: a unified academic discovery and full-text MCP server with a five-tier waterfall
resolver and six tools. This is a breaking rewrite, not a refactor — the old package directory is
deleted and the PyPI project name changes.

## Read these first — do not re-derive them

Both documents are committed and current. They are the specification of record; this handoff
deliberately does not restate their contents.

| Document | Path |
|---|---|
| Design specification (12 sections) | `docs/superpowers/specs/2026-08-28-scholar-mcp-unified-design.md` |
| Implementation plan (10 TDD tasks, full test code inline) | `docs/superpowers/plans/2026-08-28-scholar-mcp-unified-plan.md` |

Read the spec for *why*, the plan for *what to type*. The plan carries the complete failing-test
source for every task, so most tasks are transcribe-then-implement rather than design work.

Relevant commits:

- `31a64f4` — resolved the four open items (waterfall reorder, repo rename, Sci-Hub default,
  licence provenance)
- `324440b` — the post-review revision of both documents
- `becb4d4` — the original spec

Read `git show 31a64f4 --stat` and the two "Resolved Items" sections before changing any ordering
or default; each one records a decision that a reasonable implementer would otherwise reverse.

## State of the working tree

- Nothing is implemented. No source file has been written or modified in this effort.
- `src/scihub_mcp/` is still the old, working, synchronous package. Task 10 deletes it.
- `PubMed-MCP-Server/` is an untracked vendored reference (JackKuo666, MIT, Copyright (c) 2025).
  Its licence has been verified and porting from it is permitted. Task 7 may reuse its PubMed
  search logic; Task 10 adds the README credit and deletes the directory.
- There is no test suite and CI runs an import check only. Tasks 1 and 10 fix both.

## How to proceed

Work the plan's tasks in order, 1 through 10. Each task has a Files header, an Interfaces header,
and five checkboxes: write the failing test, watch it fail, implement, watch it pass, commit. Do
not batch tasks together and do not skip the verify-it-fails step — several tests in the plan
assert behaviour that is easy to satisfy accidentally, and a test that never failed proves
nothing.

Tick the checkboxes in the plan file as you go, and commit that file along with the code so
progress survives a context loss.

Task 1 is the gate for everything else: `pyproject.toml` (rename, new dependencies, a `dev`
extra, `asyncio_mode = "auto"`), `config.py` (the `Settings` dataclass), and `models.py` (the
response dataclasses). Get its tests green before touching anything else.

### Things that will bite

- **Async only.** Every network call is `httpx.AsyncClient`. `asyncio.to_thread` is permitted in
  exactly one place: the local disk write in `download_article`. The repo's current `AGENTS.md`
  documents the opposite decision and is rewritten in Task 10 — do not follow it in the meantime.
- **No live network in tests.** All HTTP is mocked with `respx`. A test that reaches the internet
  is a defect, not a slow test.
- **The JATS conformance test.** Correct Markdown output legitimately contains `<` and `>`
  (blockquotes). The plan's test matches an XML-tag regex for this reason. Do not "simplify" it
  back to a bare character check — that was a real bug in the previous revision.
- **Tier order is Europe PMC first, then PMC.** Reversed from Revision 1 and from the old code.
- **`ENABLE_SCIHUB` stays `default=True`**, and setting it to `false` must never disable
  Unpaywall. The spec's gating table is normative; there are tests for all four combinations.
- **Task 10 is destructive and outward-facing**: it deletes `src/scihub_mcp/`, renames the GitHub
  repository, and claims a new PyPI project name that needs a Trusted Publisher configured before
  the first publish. Confirm with the user before running it, even though the plan authorises it.

## Suggested skills

Call the Skill tool for these:

- **`tdd`** — the plan is red-green-refactor throughout. Invoke before Task 1.
- **`implement`** — running an existing plan or spec to completion; this is that situation. It is
  marked `disable-model-invocation`, so it must be requested explicitly by the user or invoked
  deliberately.
- **`verify-and-stop`** — for Task 10 steps 5 and 6, where the job is to prove the suite and the
  server entrypoint work without widening scope.
- **`superpowers:using-superpowers`** — loads automatically; it requires a skill check before any
  response, including clarifying questions.

Consider also `caveman-commit` for commit messages if the user's caveman mode is active — note
that persisted artifacts (code, comments, commits, documents) are written in normal prose
regardless; only chat replies are compressed.

## Open questions

None. All four previously open items were decided on 2026-08-28 and are recorded in both
documents. If something appears ambiguous, it is more likely to be answered in the spec than to be
genuinely undecided — search there before asking.

The only outstanding approval is permission to begin. The user was last asked to confirm starting
Task 1 and had not yet answered.
