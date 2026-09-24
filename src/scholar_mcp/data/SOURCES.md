# scimago_sjr.json

Journal-impact proxy data for `journal_impact` ranking signal (no free
official Journal Impact Factor API exists, this stands in for it).

**Ships empty.** `scimago_sjr.json` is checked in with empty `issn`/`name`
tables. No SJR values are hand-typed or fabricated into this repo — the
`journal_impact` ranking feature contributes a neutral `0.0` for every
paper until this file is populated from real data.

## Populating it

1. Go to https://www.scimagojr.com/journalrank.php
2. Select "All subject areas", "All regions", the latest year, output
   format CSV, and download it.
3. Save it to `data/raw/scimago_journal_rank.csv` (create the `data/raw/`
   directory; it's gitignored).
4. Run: `python scripts/update_scimago_data.py`
5. This regenerates `src/scholar_mcp/data/scimago_sjr.json`.

Before committing a regenerated file, check Scimago's current terms of
use for redistribution of derived data on their site.

## Format

```json
{
  "issn": {"<issn-digits-no-dashes>": <sjr float>, ...},
  "name": {"<lowercased-punctuation-stripped-journal-name>": <sjr float>, ...}
}
```

Lookup tries ISSN first, falls back to normalized journal name, then `None`
(neutral) if neither matches.

# govbr_az_catalog.json

Catalog index for Brazilian Ministry of Health "Saúde de A a Z" surveillance
manuals and guides.

**Sources:**
- `https://www.gov.br/saude/pt-br/centrais-de-conteudo/publicacoes/svsa`
- `https://www.gov.br/saude/pt-br/centrais-de-conteudo/publicacoes/guias-e-manuais`
- Topic abbreviations/aliases harvested from `https://www.gov.br/saude/pt-br/assuntos/saude-de-a-a-z`

**Generation Date:** 2026-09-18

**Regeneration command:**
```bash
python scripts/update_govbr_catalogs.py --catalog az
```
This crawls both publication trees, resolves pagination, attaches A-Z aliases,
and writes the resulting dictionary to `src/scholar_mcp/data/govbr_az_catalog.json`.
The script exits 1 without writing when the crawl is incomplete (any failed page, a folder cut off at the page cap, a missing alias vocabulary, or fewer than half the rows of the current file).

# govbr_pcdt_catalog.json

Catalog index for the Brazilian Ministry of Health PCDT (Protocolos Clínicos
e Diretrizes Terapêuticas) letter pages.

**Source:** `https://www.gov.br/saude/pt-br/assuntos/pcdt/<letter>`

**Regeneration command:**
```bash
python scripts/update_govbr_catalogs.py --catalog pcdt
```
Same refusal rules as the A-Z catalog: an incomplete crawl writes nothing.

