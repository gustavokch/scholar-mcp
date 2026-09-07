# Brazilian Ministry of Health Guidelines — Design

Date: 2026-09-07
Status: Approved, ready for implementation planning

## Purpose

Give the medical toolset structured access to Brazilian Ministry of Health
technical publications — PCDT (Protocolos Clínicos e Diretrizes Terapêuticas),
CONITEC health-technology assessments, cadernos de atenção básica, manuais
técnicos, and normas de vigilância — with discovery and full-text retrieval.

This is the first of two subsystems. Brazilian epidemiologic data (DEMAS /
`apidadosabertos.saude.gov.br`) is deliberately excluded and gets its own spec.

## Scope

In scope:

- Discovery of Brazilian MoH grey literature through the BVS/iAHx search API.
- Full-text retrieval of those documents as Markdown, degrading to abstract.
- Two MCP tools, gated behind the existing `enable_medical_tools` setting.

Out of scope, and explicitly not to be added during implementation:

- Scraping `www.gov.br/conitec` or any gov.br property.
- A camoufox browser fallback.
- Portuguese-aware ranking or changes to `ScoringEngine`.
- The DEMAS epidemiologic data subsystem.

## Source Investigation

Every access decision below rests on a live probe performed on 2026-09-07.
The findings are recorded because several of them are counter-intuitive and
would otherwise be re-discovered painfully during implementation.

### Sources rejected

`www.gov.br/conitec` is the authoritative publisher of PCDT, but it is a Volto
single-page application behind a Dynatrace-instrumented WAF. Its Plone REST
endpoint (`/++api++/@search`) returns an "Estamos em manutenção" HTML page
rather than JSON. Not usable over plain HTTP.

`bvsms.saude.gov.br` completes a TLS handshake and then resets the connection,
with browser headers and without. DNS resolves (189.9.35.156). This is an
application-layer block. A developer on a Brazilian network will likely not
reproduce it, which makes it a trap: code that depends on this host will appear
to work locally and fail in deployment. The design routes around it entirely.

### Source selected

`pesquisa.bvsalud.org/portal/` with `output=json` returns Solr documents and
covers the target corpus. It requires browser-like request headers; the repo
default `User-Agent: ScholarMCP/1.0.0 (mailto:…)` receives HTTP 403.

Full text resolves through `fi-admin.bvsalud.org/document/view/<slug>`, which
302-redirects to a PDF on `docs.bvsalud.org`. Verified end to end: slug `cfpaj`
returned `application/pdf`, 2,408,014 bytes, 79 pages.

### Behaviours the implementation must account for

1. **`fq` is silently ignored.** `?q=tuberculose&fq=db:"BRISA"` returns the
   same 26,565 hits as the unfiltered query. All filtering must be composed
   into `q`.
2. **The default boolean operator is OR.** `tratamento tuberculose` yields
   180,399 hits; `tratamento AND tuberculose` yields 73,072.
3. **`pais_publicacao` is subfield-encoded**, e.g.
   `^iBrazil^eBrasil^pBrasil^fBrésil`. Exact match on `"br"` or `"Brasil"`
   returns 0, and the field is not wildcard-searchable — `*Brasil*` also
   returns 0.
4. **Records are duplicated across indexing collections, heavily.** Measured on
   one query (`type:"non-conventional" AND la:"pt" AND dengue`, 428 hits):

   | `count` | rows returned | unique ids | duplicate share |
   |---|---|---|---|
   | 20 | 20 | 13 | 35% |
   | 50 | 50 | 26 | 48% |
   | 100 | 100 | 43 | 57% |
   | 200 | 200 | 95 | 52% |

   Roughly a 2.1–2.3x inflation factor, which grows with page size. A
   single-record lookup by `id` likewise returned 4 rows. `count=200` is
   accepted by the server.
5. **The record id is not the full-text key.**
   `fi-admin.bvsalud.org/document/view/biblio-1701387` is a 404. The working
   key is the short slug inside the record's `ur` field.
6. **`ur` frequently points off-site** — `iris.paho.org`,
   `sciencedirect.com`, `saude.sp.gov.br` — not only at BVS-hosted PDFs.
7. **The path segment is cosmetic.** `pesquisa.bvsalud.org/bvsms/` does not
   restrict results to Ministry of Health material; it returns MEDLINE
   articles. Collection scoping must be explicit in `q`.

## Architecture

A single new module, `src/scholar_mcp/medical/brazil_moh.py`, containing one
class `BrazilMoHEngine`, constructed like `WHOIRISEngine(http, cache, settings)`.

The module structure mirrors `medical/who_iris.py`, which solves the same shape
of problem (repository search plus PDF full-text extraction) against a different
backend.

### Collaborators

`AsyncHttpClient` is used unchanged. BVS headers
(`User-Agent: Mozilla/5.0 …`, `Accept-Language: pt-BR,pt;q=0.9`) are passed
per-call through the existing `headers=` argument. No change to
`utils/http.py`.

`SQLiteCacheManager` is used with a new `brazil_moh` namespace and a new
`cache_ttl_brazil_moh` setting.

### Public surface

```python
async def search_guidelines(
    query: str,
    limit: int,
    collection: str,
) -> tuple[list[BrazilGuideline], CacheMetadata]

async def get_full_text(
    record_id: str,
    max_chars: int | None,
) -> tuple[dict[str, Any], CacheMetadata]
```

### Boundaries

The engine knows BVS Solr syntax and `fi-admin` redirect behaviour. It does not
know MCP. `server.py` knows tool shape and does not know Solr.
`format_brazil_moh_guidelines` in `medical/formatters.py` is the only place that
knows the response envelope.

## Data Model

`BrazilGuideline`, a dataclass in `medical/models.py`, following `WHOGuideline`
including `to_dict` and a `from_dict` that filters unknown keys.

Solr fields are multi-valued; the mapping is lossy by intent:

| Solr field | Model field | Note |
|---|---|---|
| `ti[0]` | `title` | |
| `ti_en[0]` | `title_en` | |
| `ab` | `abstract` | |
| `au` | `authors` | |
| `da` | `year`, `issued` | `"202609"` becomes `"2026"` and `"2026-09"`; both empty when `da` is absent or malformed |
| `db` | `collections` | |
| `mh` | `mesh_subjects` | |
| `la` | `languages` | |
| `ur[0]` | `document_url` | verbatim |
| `id` | `record_id` | e.g. `biblio-1701387` |
| `pais_publicacao` | `country` | parsed out of the subfield encoding |
| — | `fulltext_id` | derived; see below |
| — | `score` | unset in v1 |

`fulltext_id` is populated only when `document_url` matches
`fi-admin.bvsalud.org/document/view/<slug>`, and is an empty string otherwise.

## Query Construction

All filters compose into `q`:

```
type:"non-conventional" AND la:"pt" AND (<user tokens joined with AND>)
```

with `AND db:"BRISA"` appended when `collection="brisa"`.

`type:"non-conventional"` isolates grey literature — manuais, cadernos, normas
técnicas, PCDT — from journal articles. `db:"BRISA"` is the Brazilian
health-technology-assessment collection; a phrase search for
`"protocolo clínico e diretrizes terapêuticas"` returned 576 BRISA documents.

Brazil scoping is two-stage because `pais_publicacao` cannot be queried:
`la:"pt"` narrows server-side, then a client-side assertion on `^eBrasil` in
`pais_publicacao` drops Portugal and PAHO records.

Deduplication is on `id`, keeping first occurrence so that removal never
reorders survivors.

Because duplicates consume result slots at roughly a 2.1–2.3x rate, the engine
over-fetches and then trims. Module constants:

```python
MAX_RESULTS = 50        # ceiling on the tool's `limit`
OVERFETCH_FACTOR = 3    # covers the measured duplicate rate with margin
MAX_PAGE_SIZE = 200     # highest `count` verified against the server
```

The request uses `count = min(limit * OVERFETCH_FACTOR, MAX_PAGE_SIZE)`, and the
result is trimmed to `limit` after dedup. At the ceiling (`limit=50`) this
requests 150 rows to yield roughly 70 unique, so the trim is satisfied with
margin. Under-delivery remains possible when `numFound` itself is small; that is
correct behaviour, not an error, and no second page is requested in v1.
`utils/deduplication.py` is reused rather than a local set.

## Full-Text Retrieval

`get_full_text` keys on `record_id` — the value `search_guidelines` returns —
not on the fi-admin slug. The slug alone provides no abstract to degrade to, and
`fi-admin/document/view/biblio-1701387` is a 404. Keying on `record_id` makes
the two tools compose directly.

Chain:

1. `q=id:"<record_id>"` resolves the record, which carries `ab`. Expect
   duplicate rows; take the first after dedup.
2. Read `ur[0]`. If it matches the fi-admin pattern, continue; otherwise skip to
   the abstract.
3. GET the fi-admin URL with redirects followed and browser headers, arriving at
   `docs.bvsalud.org/biblioref/YYYY/MM/<id>/<name>.pdf`.
4. Require `content-type` to contain `application/pdf`.
   `AsyncHttpClient._is_unexpected_html` already treats an HTML body as a miss,
   which is what a WAF interstitial returns.
5. `pdf_bytes_to_text` from `parsers/pdf.py`, then `truncate_content`, with
   `MAX_FULL_TEXT_CHARS = 50_000` as in `who_iris.py`.

### Host allowlist

Step 3 fetches a URL taken from record content, and `record_id` is
caller-controlled. The fetch is therefore restricted to `fi-admin.bvsalud.org`
and `docs.bvsalud.org`. A `document_url` on any other host degrades to the
abstract and issues no request. Without this restriction the tool would act as
a general-purpose request proxy.

### Degradation

PDF text yields `content_type: "pdf"`. Failing that, the record abstract yields
`content_type: "abstract"`. Failing both, `status: "not_found"` with
`content_type: "none"`.

Any network failure sets `errored=True`, and an errored payload is never
written to cache, so a transient block cannot poison a 30-day TTL. This matches
`who_iris.get_full_text`.

## Ranking

There is no re-ranking in v1.

`medical/ranking.py` rests on `ScoringEngine.tokenize` and `text_coverage`,
which are tuned for English. This corpus is Portuguese, with accented terms
(`hipertensão`, `notificação`) and Portuguese stopwords (`de`, `da`, `para`,
`com`) that the tokenizer does not strip. Blending an unfolded lexical score
against Solr's relevance ordering would degrade a tuned result while appearing
principled. BVS Solr indexes this corpus with the correct analyzer chain, so its
order is preserved.

`BrazilGuideline.score` exists but is unset, and the module docstring records
this decision, so adding ranking later is additive. The prerequisite for that
work is accent folding and a Portuguese stopword list in `ScoringEngine`, which
is a separate spec.

## Caching

New setting `cache_ttl_brazil_moh: int = 2592000` (30 days), environment
variable `CACHE_TTL_BRAZIL_MOH`, matching `cache_ttl_who_iris`.

Extracted full text is stored in the medical `SQLiteCacheManager`, as
`who_iris.py` does. This does not contradict AGENTS.md decision 3, which governs
the core resolver's in-memory `TTLCache` — a different cache with a different
purpose.

## MCP Tools

Both are registered inside the existing `if settings.enable_medical_tools:`
block in `server.py`, beside the WHO IRIS pair.

```python
async def search_brazil_moh_guidelines(
    query: str,
    limit: int = 10,
    collection: str = "all",   # "all" | "brisa"
) -> dict[str, Any]

async def get_brazil_moh_full_text(
    record_id: str,
    max_chars: int | None = None,
) -> dict[str, Any]
```

`limit` clamps to `MAX_RESULTS`. `collection` is validated against the two
literals; any other value returns `status: "error"` rather than silently
searching the whole index. Docstrings state that `record_id` comes from
`search_brazil_moh_guidelines` results, since that coupling is not obvious.

Both tools follow the medical error boundary: catch `Exception`, return
`{"status": "error", "error": str(ex), "source": "brazil-moh"}`, never raise
into the tool call.

## Response Envelope

`format_brazil_moh_guidelines(guidelines, query, meta)` returns
`{"data": [g.to_dict() for g in guidelines], "markdown": …}`, with markdown
built through `append_cache_info` and empty results routed through
`_empty_state`, so that a fetch failure reads as `FETCH_FAILED_LINE` rather
than a false "nothing found".

## Testing

New `tests/medical/test_brazil_moh.py`, following
`tests/medical/test_who_iris.py`: fake HTTP client, `tmp_path` SQLite cache, no
network access.

Every async test closes the cache and HTTP client in `try/finally`. A test that
fails without closing them hangs pytest at finalize instead of reporting the
failure.

Required cases, each tied to a probe finding:

- Query builder joins user tokens with `AND`.
- Filters are composed into `q` and never passed as `fq`.
- `collection="brisa"` appends `db:"BRISA"`; an unknown value returns
  `status: "error"`.
- The Brazil assertion keeps a `^iBrazil^eBrasil…` record and drops a
  Portugal or PAHO record.
- Dedup on `id` preserves survivor order, the request applies
  `count = min(limit * OVERFETCH_FACTOR, MAX_PAGE_SIZE)`, and the result is
  trimmed to `limit`.
- A response whose unique count falls below `limit` returns the short list as a
  success rather than erroring.
- `fulltext_id` is set for a fi-admin `document_url` and empty for a
  `sciencedirect.com` one.
- An off-allowlist `document_url` degrades to abstract and issues no second
  request, asserted against the fake client's call log.
- The degradation ladder covers PDF, abstract, and `not_found`.
- An errored payload is not written to cache.
- `max_chars` truncates the served payload, not the cached one.

## Registration Checklist

- `medical/models.py` — `BrazilGuideline`.
- `medical/formatters.py` — `format_brazil_moh_guidelines`.
- `config.py` — `cache_ttl_brazil_moh`, `CACHE_TTL_BRAZIL_MOH`; extend
  `tests/test_config_medical.py`.
- `server.py` — engine construction and both tools; extend
  `tests/test_server_medical.py`.
- `AGENTS.md` architecture tree, `README.md` tool table, `CHANGELOG.md`.

## Known Limitations

Full-text coverage is partial by construction. Documents whose `ur` points
off-site return abstracts only. This is honest degradation rather than a gap to
close by adding scrapers.

`la:"pt"` plus the `^eBrasil` assertion is a proxy for Ministry of Health
authorship, not a guarantee of it. The corpus therefore includes Brazilian
state-level and society publications alongside federal ones. Narrowing further
requires a reliable publisher or affiliation filter, which the probes did not
find.
