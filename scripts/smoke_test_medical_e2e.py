#!/usr/bin/env python3
"""End-to-end smoke test script for scholar-mcp medical tools.

Hits real upstream APIs (openFDA, RxNav, WHO GHO, ClinicalTrials.gov,
PubMed, AAP/Bright Futures) — no mocks. Requires network access.
"""

import asyncio
import os

from scholar_mcp.server import (
    get_child_health_statistics,
    get_drug_details,
    get_health_statistics,
    get_medical_cache_stats,
    search_aap_guidelines,
    search_clinical_guidelines,
    search_drug_nomenclature,
    search_drugs,
    search_medical_databases,
    search_medical_journals,
    search_pediatric_drugs,
    search_pediatric_guidelines,
    search_pediatric_literature,
)

FDA_QUERY = "ibuprofen"
PEDIATRIC_DRUG_QUERY = "amoxicillin"
RXNORM_QUERY = "acetaminophen"
WHO_INDICATOR = "life expectancy"
WHO_COUNTRY = "USA"
CHILD_HEALTH_INDICATOR = "mortality"
GUIDELINE_QUERY = "hypertension"
GUIDELINE_ORG = "AHA"
PEDIATRIC_GUIDELINE_QUERY = "asthma"
AAP_QUERY = "nutrition"
PEDIATRIC_LITERATURE_QUERY = "asthma"
DATABASES_QUERY = "diabetes"
JOURNALS_QUERY = "diabetes"

FAILURES: list[str] = []


def print_section(title: str) -> None:
    print("\n" + "=" * 70)
    print(f"STEP: {title}")
    print("=" * 70)


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{label}: {detail}")


async def run_smoke_test() -> None:
    print("=" * 70)
    print("Scholar MCP Medical Subsystem End-to-End Smoke Test")
    print("=" * 70)
    print(f"ENABLE_MEDICAL_TOOLS: {os.getenv('ENABLE_MEDICAL_TOOLS', 'true')}")
    print(f"ENABLE_PLAYWRIGHT_FALLBACK: {os.getenv('ENABLE_PLAYWRIGHT_FALLBACK', 'true')}")
    print(f"SCHOLAR_CACHE_DB: {os.getenv('SCHOLAR_CACHE_DB', '~/.cache/scholar_mcp/cache.db')}")

    # 1. search_drugs
    print_section("1. search_drugs")
    print(f"Query: '{FDA_QUERY}' (limit=5)")
    drugs_res = await search_drugs(query=FDA_QUERY, limit=5)
    data = drugs_res.get("data", [])
    print(f"  Results: {len(data)}")
    ndc = None
    for idx, d in enumerate(data[:3], 1):
        brand = ", ".join(d.get("openfda", {}).get("brand_name", [])) or "Unknown"
        ndcs = d.get("openfda", {}).get("product_ndc", [])
        print(f"  [{idx}] Brand: {brand} | NDC: {ndcs}")
        if not ndc and ndcs:
            ndc = ndcs[0]
    check("search_drugs returned results", len(data) > 0)
    check("search_drugs markdown non-empty", bool(drugs_res.get("markdown")))

    # 2. get_drug_details (chained off NDC discovered above)
    print_section("2. get_drug_details")
    if ndc:
        print(f"NDC: {ndc}")
        details_res = await get_drug_details(ndc=ndc)
        print(f"  Status: {details_res.get('status', 'ok')}")
        print(f"  Markdown length: {len(details_res.get('markdown', ''))} chars")
        check("get_drug_details resolved a label", details_res.get("data") is not None)
    else:
        print("  SKIPPED: no NDC discovered from search_drugs")
        check("get_drug_details prerequisite (NDC found)", False, "no NDC from step 1")

    # 3. search_pediatric_drugs
    print_section("3. search_pediatric_drugs")
    print(f"Query: '{PEDIATRIC_DRUG_QUERY}' (limit=5)")
    ped_drugs_res = await search_pediatric_drugs(query=PEDIATRIC_DRUG_QUERY, limit=5)
    ped_data = ped_drugs_res.get("data", [])
    print(f"  Results: {len(ped_data)}")
    for idx, d in enumerate(ped_data[:3], 1):
        brand = ", ".join(d.get("openfda", {}).get("brand_name", [])) or "Unknown"
        print(f"  [{idx}] Brand: {brand}")
    check("search_pediatric_drugs markdown non-empty", bool(ped_drugs_res.get("markdown")))

    # 4. search_drug_nomenclature
    print_section("4. search_drug_nomenclature")
    print(f"Query: '{RXNORM_QUERY}'")
    rxnorm_res = await search_drug_nomenclature(query=RXNORM_QUERY)
    rxnorm_data = rxnorm_res.get("data", [])
    print(f"  Results: {len(rxnorm_data)}")
    for idx, d in enumerate(rxnorm_data[:3], 1):
        print(f"  [{idx}] {d.get('name')} (RxCUI: {d.get('rxcui')}, TTY: {d.get('tty')})")
    check("search_drug_nomenclature returned concepts", len(rxnorm_data) > 0)

    # 5. get_health_statistics
    print_section("5. get_health_statistics")
    print(f"Indicator: '{WHO_INDICATOR}' Country: {WHO_COUNTRY} (limit=5)")
    who_res = await get_health_statistics(indicator=WHO_INDICATOR, country=WHO_COUNTRY, limit=5)
    who_data = who_res.get("data", [])
    print(f"  Results: {len(who_data)}")
    for idx, r in enumerate(who_data[:3], 1):
        print(f"  [{idx}] {r.get('indicator_name')}: {r.get('value')} {r.get('unit')} ({r.get('time_dim')})")
    check("get_health_statistics markdown non-empty", bool(who_res.get("markdown")))

    # 6. get_child_health_statistics
    print_section("6. get_child_health_statistics")
    print(f"Indicator: '{CHILD_HEALTH_INDICATOR}' Country: {WHO_COUNTRY} (limit=5)")
    child_res = await get_child_health_statistics(
        indicator=CHILD_HEALTH_INDICATOR, country=WHO_COUNTRY, limit=5
    )
    child_data = child_res.get("data", [])
    print(f"  Results: {len(child_data)}")
    for idx, r in enumerate(child_data[:3], 1):
        print(f"  [{idx}] {r.get('indicator_code')}: {r.get('value')} ({r.get('time_dim')})")
    check("get_child_health_statistics markdown non-empty", bool(child_res.get("markdown")))

    # 7. search_clinical_guidelines
    print_section("7. search_clinical_guidelines")
    print(f"Query: '{GUIDELINE_QUERY}' Organization: {GUIDELINE_ORG}")
    guidelines_res = await search_clinical_guidelines(query=GUIDELINE_QUERY, organization=GUIDELINE_ORG)
    guidelines_data = guidelines_res.get("data", [])
    print(f"  Results: {len(guidelines_data)}")
    for idx, g in enumerate(guidelines_data[:3], 1):
        print(f"  [{idx}] {g.get('title')} | Score: {g.get('score')} | Org: {g.get('organization')}")
    check("search_clinical_guidelines markdown non-empty", bool(guidelines_res.get("markdown")))

    # 8. search_pediatric_guidelines
    print_section("8. search_pediatric_guidelines")
    print(f"Query: '{PEDIATRIC_GUIDELINE_QUERY}' Source: all")
    ped_guide_res = await search_pediatric_guidelines(query=PEDIATRIC_GUIDELINE_QUERY, source="all")
    ped_guide_data = ped_guide_res.get("data", [])
    print(f"  Results: {len(ped_guide_data)}")
    for idx, g in enumerate(ped_guide_data[:3], 1):
        print(f"  [{idx}] {g.get('title')} | Source: {g.get('source')}")
    check("search_pediatric_guidelines markdown non-empty", bool(ped_guide_res.get("markdown")))

    # 9. search_aap_guidelines
    print_section("9. search_aap_guidelines")
    print(f"Query: '{AAP_QUERY}'")
    aap_res = await search_aap_guidelines(query=AAP_QUERY)
    aap_data = aap_res.get("data", [])
    print(f"  Results: {len(aap_data)}")
    for idx, g in enumerate(aap_data[:3], 1):
        print(f"  [{idx}] {g.get('title')} | Source: {g.get('source')}")
    check("search_aap_guidelines markdown non-empty", bool(aap_res.get("markdown")))

    # 10. search_pediatric_literature
    print_section("10. search_pediatric_literature")
    print(f"Query: '{PEDIATRIC_LITERATURE_QUERY}' (max_results=5)")
    ped_lit_res = await search_pediatric_literature(query=PEDIATRIC_LITERATURE_QUERY, max_results=5)
    ped_lit_data = ped_lit_res.get("data", [])
    print(f"  Results: {len(ped_lit_data)}")
    for idx, a in enumerate(ped_lit_data[:3], 1):
        print(f"  [{idx}] {a.get('title')} | Journal: {a.get('journal')} | PMID: {a.get('pmid')}")
    check("search_pediatric_literature markdown non-empty", bool(ped_lit_res.get("markdown")))

    # 11. search_medical_databases
    print_section("11. search_medical_databases")
    print(f"Query: '{DATABASES_QUERY}'")
    databases_res = await search_medical_databases(query=DATABASES_QUERY)
    databases_data = databases_res.get("data", [])
    print(f"  Results: {len(databases_data)}")
    for idx, a in enumerate(databases_data[:3], 1):
        print(f"  [{idx}] {a.get('title')} | Source: {a.get('source_database')}")
    check("search_medical_databases returned results", len(databases_data) > 0)

    # 12. search_medical_journals
    print_section("12. search_medical_journals")
    print(f"Query: '{JOURNALS_QUERY}'")
    journals_res = await search_medical_journals(query=JOURNALS_QUERY)
    journals_data = journals_res.get("data", [])
    print(f"  Results: {len(journals_data)}")
    for idx, a in enumerate(journals_data[:3], 1):
        print(f"  [{idx}] {a.get('title')} | Journal: {a.get('journal')}")
    check("search_medical_journals markdown non-empty", bool(journals_res.get("markdown")))

    # 13. get_medical_cache_stats
    print_section("13. get_medical_cache_stats")
    stats_res = await get_medical_cache_stats()
    print(f"  Total entries: {stats_res.get('total_entries')}")
    print(f"  Hits: {stats_res.get('hits')} | Misses: {stats_res.get('misses')}")
    print(f"  Hit rate: {stats_res.get('hit_rate')}")
    print(f"  Sources: {stats_res.get('sources')}")
    check(
        "get_medical_cache_stats reflects prior calls",
        stats_res.get("total_entries", 0) > 0,
        f"total_entries={stats_res.get('total_entries')}",
    )

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"SMOKE TEST COMPLETED WITH {len(FAILURES)} FAILURE(S)")
        for f in FAILURES:
            print(f"  - {f}")
        print("=" * 70)
        raise SystemExit(1)
    print("ALL 13 MEDICAL TOOLS EXECUTED SUCCESSFULLY")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_smoke_test())
