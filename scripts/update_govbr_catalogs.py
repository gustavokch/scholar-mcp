#!/usr/bin/env python3
"""Regenerate a bundled gov.br catalog seed.

``--catalog az`` crawls the SVSA and guias-e-manuais publication trees and
writes ``src/scholar_mcp/data/govbr_az_catalog.json``. ``--catalog pcdt``
crawls the PCDT letter index and writes
``src/scholar_mcp/data/govbr_pcdt_catalog.json``. The seeds are the catalogs
the server searches: no search crawls gov.br.

Refuses to write a partial crawl. Any failed page, a folder cut off at the
page cap, a missing alias vocabulary, or a catalog below
MIN_CATALOG_RETENTION of the current seed exits 1 and leaves the seed as it
is.

Usage:
    uv run python scripts/update_govbr_catalogs.py --catalog az|pcdt [--output PATH]
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from scholar_mcp.config import Settings  # noqa: E402
from scholar_mcp.medical import govbr_az, govbr_pcdt  # noqa: E402
from scholar_mcp.utils.http import AsyncHttpClient  # noqa: E402
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager  # noqa: E402

DATA_DIR = REPO_ROOT / "src" / "scholar_mcp" / "data"
CATALOGS = {
    "az": (
        govbr_az.GovBrAZEngine,
        govbr_az.load_seed_catalog,
        DATA_DIR / "govbr_az_catalog.json",
    ),
    "pcdt": (
        govbr_pcdt.GovBrPCDTEngine,
        govbr_pcdt.load_seed_catalog,
        DATA_DIR / "govbr_pcdt_catalog.json",
    ),
}
MIN_EXPECTED_ROWS = 50


async def build_catalog(name: str) -> tuple[dict, bool]:
    """Crawl one catalog, measured against the current seed."""
    engine_cls, load_seed, _ = CATALOGS[name]
    settings = Settings()
    http_client = AsyncHttpClient(settings)
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = SQLiteCacheManager(Path(tmpdir) / "catalog.db", settings=settings)
        engine = engine_cls(http_client, cache, settings)
        try:
            return await engine.refresh_catalog(incumbent=load_seed())
        finally:
            await cache.close()
            await http_client.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--catalog", choices=sorted(CATALOGS), required=True)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    output = args.output or CATALOGS[args.catalog][2]

    catalog, complete = asyncio.run(build_catalog(args.catalog))
    if not complete:
        print(
            f"refusing to write a partial {args.catalog} crawl ({len(catalog)} rows); "
            "see the warnings above",
            file=sys.stderr,
        )
        return 1
    if len(catalog) < MIN_EXPECTED_ROWS:
        print(
            f"refusing to write {len(catalog)} rows "
            f"(expected at least {MIN_EXPECTED_ROWS}); gov.br may be degraded",
            file=sys.stderr,
        )
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {len(catalog)} records to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
