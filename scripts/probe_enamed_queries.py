#!/usr/bin/env python3
"""Probe the ENAMED misses fixtures against live backends (plan §4).

Runs the B1 fixture (20 empty PubMed-backed queries) through
search_medical_databases, search_clinical_guidelines (clinical_guidelines
kind), search_medical_journals (journals kind), and
search_pediatric_literature (pediatric_literature kind), and the B2 fixture
(14 off-topic search_scholar queries) through WaterfallResolver.search.
Prints hit count, relaxed_query, top-1 title, and source per query, then
the B1/B2 pass criteria as gates (exit 1 on failure).

Network-marked: excluded from the default pytest run; run explicitly:
    uv run python scripts/probe_enamed_queries.py [--fixture b1|b2|all] [--max N]
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scholar_mcp.config import Settings  # noqa: E402
from scholar_mcp.medical.clinical_trials import ClinicalTrialsClient  # noqa: E402
from scholar_mcp.medical.databases import MedicalDatabasesEngine  # noqa: E402
from scholar_mcp.medical.guidelines import GuidelinesEngine  # noqa: E402
from scholar_mcp.medical.pediatrics import PediatricsEngine  # noqa: E402
from scholar_mcp.medical.pubmed import MedicalPubMedClient  # noqa: E402
from scholar_mcp.medical.query_relax import content_overlap_count  # noqa: E402
from scholar_mcp.resolver import WaterfallResolver  # noqa: E402
from scholar_mcp.utils.cache import TTLCache  # noqa: E402
from scholar_mcp.utils.http import AsyncHttpClient  # noqa: E402
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
B1_FIXTURE = REPO / "tests" / "medical" / "fixtures" / "enamed_empty_queries.json"
B2_FIXTURE = REPO / "tests" / "medical" / "fixtures" / "enamed_offtopic_queries.json"

B1_GATE = 0.80
B2_GATE = 0.90


def _load_fixture(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["queries"]


async def _probe_b1(entries: list[dict]) -> tuple[int, int]:
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(
        db_path=Path.home() / ".cache" / "scholar_mcp" / "probe.db",
        settings=settings,
    )
    pubmed = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)
    trials = ClinicalTrialsClient(http_client=http_client, cache=cache, settings=settings)
    databases = MedicalDatabasesEngine(
        pubmed=pubmed, clinical_trials=trials, http_client=http_client,
        cache=cache, settings=settings, jitter_range=None,
    )
    guidelines = GuidelinesEngine(pubmed=pubmed, cache=cache, settings=settings)
    pediatrics = PediatricsEngine(
        http_client=http_client, cache=cache, settings=settings, pubmed=pubmed
    )
    hits = 0
    try:
        for entry in entries:
            kind, query = entry["kind"], entry["query"]
            try:
                if kind == "clinical_guidelines":
                    items, meta = await guidelines.search_clinical_guidelines(query)
                    top = items[0].title if items else ""
                elif kind == "journals":
                    items, meta = await databases.search_medical_journals(query)
                    top = items[0].title if items else ""
                elif kind == "pediatric_literature":
                    items, meta = await pediatrics.search_pediatric_literature(query)
                    top = items[0].title if items else ""
                else:
                    items, meta = await databases.search_medical_databases(query)
                    top = items[0].title if items else ""
                n = len(items)
            except Exception as exc:  # noqa: BLE001 — probe must report, not crash
                n, top, meta = 0, f"<error: {exc}>", None
            hits += 1 if n else 0
            relaxed = meta.relaxed_query if meta is not None else None
            print(f"[B1] {entry['id']} kind={kind} hits={n} "
                  f"relaxed_query={relaxed!r} top1={top[:100]!r}")
    finally:
        await cache.close()
        await http_client.aclose()
    return hits, len(entries)


async def _probe_b2(entries: list[dict]) -> tuple[int, int, int, int]:
    """Returns (total, top1_overlap_ok, crossref_non_journal, crossref_total)."""
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    resolver = WaterfallResolver(
        settings=settings,
        http_client=http_client,
        cache=TTLCache(maxsize=settings.cache_size, ttl_seconds=settings.cache_ttl_seconds),
    )
    ok = 0
    non_journal = 0
    xref_total = 0
    try:
        for entry in entries:
            query = entry["query"]
            try:
                # Production defaults: re-ranked page of 5, as the agent sees it.
                papers = await resolver.search(query, source="auto", num_results=5, rerank=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[B2] {entry['id']} <error: {exc}>")
                continue
            top = papers[0] if papers else None
            overlap = content_overlap_count(query, top.title, top.abstract) if top else 0
            ok += 1 if overlap >= 1 else 0
            for p in papers:
                if p.source == "crossref":
                    xref_total += 1
                    if p.doc_type not in ("", "journal-article"):
                        non_journal += 1
            src = top.source if top else "-"
            print(f"[B2] {entry['id']} hits={len(papers)} top1_overlap={overlap} "
                  f"top1_source={src} top1={top.title[:100]!r}" if top else
                  f"[B2] {entry['id']} hits=0")
    finally:
        await http_client.aclose()
    return len(entries), ok, non_journal, xref_total


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", choices=["b1", "b2", "all"], default="all")
    parser.add_argument("--max", type=int, default=0, help="max queries per fixture (0 = all)")
    args = parser.parse_args()

    failures: list[str] = []
    if args.fixture in ("b1", "all"):
        b1 = _load_fixture(B1_FIXTURE)
        if args.max:
            b1 = b1[: args.max]
        hits, total = await _probe_b1(b1)
        rate = hits / total if total else 0.0
        print(f"B1 gate: {hits}/{total} with >=1 hit (need >= {B1_GATE:.0%})")
        if rate < B1_GATE:
            failures.append(f"B1 hit rate {rate:.0%} < {B1_GATE:.0%}")
    if args.fixture in ("b2", "all"):
        b2 = _load_fixture(B2_FIXTURE)
        if args.max:
            b2 = b2[: args.max]
        total, ok, non_journal, xref_total = await _probe_b2(b2)
        rate = ok / total if total else 0.0
        print(f"B2 gate: top-1 overlap>=1 for {ok}/{total} (need >= {B2_GATE:.0%}); "
              f"non-journal-article crossref records: {non_journal}/{xref_total} (need 0)")
        if rate < B2_GATE:
            failures.append(f"B2 overlap rate {rate:.0%} < {B2_GATE:.0%}")
        if non_journal:
            failures.append(f"B2 {non_journal} non-journal-article records")
    if failures:
        print("FAIL:", "; ".join(failures))
        return 1
    print("PASS: all gates hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
