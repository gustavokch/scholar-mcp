#!/usr/bin/env python3
"""Regenerate the bundled gov.br A-Z publication catalog.

Crawls the live SVSA and guias-e-manuais trees and writes the result to
``src/scholar_mcp/data/govbr_az_catalog.json``. Run after gov.br
reorganizes its publication folders. Refuses to write a partial crawl.

Usage:
    uv run python scripts/update_govbr_az_catalog.py [--output PATH]
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
from scholar_mcp.medical.govbr_az import GovBrAZEngine  # noqa: E402
from scholar_mcp.utils.http import AsyncHttpClient  # noqa: E402
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT / "src" / "scholar_mcp" / "data" / "govbr_az_catalog.json"
MIN_EXPECTED_ROWS = 50


async def build_catalog() -> dict:
    settings = Settings()
    http_client = AsyncHttpClient(settings)
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = SQLiteCacheManager(Path(tmpdir) / "catalog.db", settings=settings)
        engine = GovBrAZEngine(http_client, cache, settings)
        try:
            return await engine.refresh_catalog()
        finally:
            await cache.close()
            await http_client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    catalog = asyncio.run(build_catalog())
    if len(catalog) < MIN_EXPECTED_ROWS:
        print(
            f"refusing to write {len(catalog)} rows "
            f"(expected at least {MIN_EXPECTED_ROWS}); gov.br may be degraded",
            file=sys.stderr,
        )
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {len(catalog)} records to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
