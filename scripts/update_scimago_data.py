#!/usr/bin/env python3
"""Regenerate src/scholar_mcp/data/scimago_sjr.json from a Scimago Journal
Rank CSV export.

Download the CSV from https://www.scimagojr.com/journalrank.php (select
"All subject areas", "All regions", the latest year, output format CSV)
and save it to data/raw/scimago_journal_rank.csv before running this
script. Consult Scimago's site for current data usage terms before
committing a regenerated scimago_sjr.json.
"""
import csv
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scholar_mcp.ranking import parse_scimago_csv  # noqa: E402

DEFAULT_INPUT = Path("data/raw/scimago_journal_rank.csv")
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "src" / "scholar_mcp" / "data" / "scimago_sjr.json"
)


def main(input_path: Path = DEFAULT_INPUT, output_path: Path = DEFAULT_OUTPUT) -> None:
    if not input_path.exists():
        print(f"Input CSV not found: {input_path}", file=sys.stderr)
        print(
            "Download it from https://www.scimagojr.com/journalrank.php first.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(input_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        rows = list(reader)

    table = parse_scimago_csv(rows)
    output_path.write_text(json.dumps(table, indent=2, sort_keys=True), encoding="utf-8")
    print(
        f"Wrote {len(table['issn'])} ISSN entries and "
        f"{len(table['name'])} name entries to {output_path}"
    )


if __name__ == "__main__":
    main()
