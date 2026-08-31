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
