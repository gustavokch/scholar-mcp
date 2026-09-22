"""Live gate for the B1 fixture (ENAMED misses plan §4).

Runs the 20 trace-empty PubMed-backed queries through their natural engine
path and requires >= 80% to return at least one hit. Network-marked:
excluded from the default run, execute explicitly::

    uv run pytest -m network tests/medical/test_enamed_empty_queries.py -s
"""

import json
from pathlib import Path

import pytest

from scholar_mcp.config import Settings
from scholar_mcp.medical.clinical_trials import ClinicalTrialsClient
from scholar_mcp.medical.databases import MedicalDatabasesEngine
from scholar_mcp.medical.guidelines import GuidelinesEngine
from scholar_mcp.medical.pediatrics import PediatricsEngine
from scholar_mcp.medical.pubmed import MedicalPubMedClient
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

pytestmark = pytest.mark.network

FIXTURE = Path(__file__).parent / "fixtures" / "enamed_empty_queries.json"
GATE = 0.80


async def test_enamed_empty_queries_hit_after_relaxation(tmp_path: Path):
    entries = json.loads(FIXTURE.read_text(encoding="utf-8"))["queries"]
    assert len(entries) == 20

    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    pubmed = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)
    trials = ClinicalTrialsClient(http_client=http_client, cache=cache, settings=settings)
    databases = MedicalDatabasesEngine(
        pubmed=pubmed,
        clinical_trials=trials,
        http_client=http_client,
        cache=cache,
        settings=settings,
        jitter_range=None,
    )
    guidelines = GuidelinesEngine(pubmed=pubmed, cache=cache, settings=settings)
    pediatrics = PediatricsEngine(
        http_client=http_client, cache=cache, settings=settings, pubmed=pubmed
    )
    try:
        hits = 0
        for entry in entries:
            kind, query = entry["kind"], entry["query"]
            if kind == "clinical_guidelines":
                items, meta = await guidelines.search_clinical_guidelines(query)
            elif kind == "journals":
                items, meta = await databases.search_medical_journals(query)
            elif kind == "pediatric_literature":
                items, meta = await pediatrics.search_pediatric_literature(query)
            else:
                items, meta = await databases.search_medical_databases(query)
            hits += 1 if items else 0
            top = items[0].title[:80] if items else "-"
            print(f"\n{entry['id']}: hits={len(items)} "
                  f"relaxed={meta.relaxed_query!r} top={top!r}")
        rate = hits / len(entries)
        print(f"\nB1 live gate: {hits}/{len(entries)} = {rate:.0%} (need >= {GATE:.0%})")
        assert rate >= GATE
    finally:
        await cache.close()
        await http_client.aclose()
