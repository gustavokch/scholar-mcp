from typing import Any

from scholar_mcp.medical.models import (
    ClinicalGuideline,
    DrugLabel,
    MedicalArticle,
    PediatricGuideline,
    RxNormDrug,
    WHOIndicatorRecord,
)
from scholar_mcp.utils.sqlite_cache import CacheMetadata


def append_cache_info(text: str, meta: CacheMetadata) -> str:
    if meta.cached:
        return f"{text}\n\n[Cached: {meta.cache_age}s old]"
    return f"{text}\n\n[Fresh response]"


def format_drug_search_results(
    drugs: list[DrugLabel],
    query: str,
    meta: CacheMetadata,
) -> dict[str, Any]:
    lines = [f"## Drug Search Results: {query}", ""]
    if not drugs:
        lines.append("No drug labels found matching the query.")
    else:
        for drug in drugs:
            of = drug.openfda
            brand = ", ".join(of.brand_name) if of.brand_name else "Unknown"
            generic = ", ".join(of.generic_name) if of.generic_name else "N/A"
            maker = ", ".join(of.manufacturer_name) if of.manufacturer_name else "N/A"
            purpose = "; ".join(drug.purpose) if drug.purpose else "N/A"
            ndc = ", ".join(of.product_ndc) if of.product_ndc else "N/A"
            lines.append(f"- **{brand}** ({generic}) — {maker}. NDC: {ndc}. Purpose: {purpose}")
            if drug.pediatric_dosing:
                lines.append(f"  - Pediatric Dosing: {drug.pediatric_dosing}")
            if drug.pediatric_warnings:
                lines.append(f"  - Pediatric Warnings: {drug.pediatric_warnings}")

    markdown = append_cache_info("\n".join(lines), meta)
    return {"data": [d.to_dict() for d in drugs], "markdown": markdown}


def format_drug_details(
    drug: DrugLabel | None,
    ndc: str,
    meta: CacheMetadata,
) -> dict[str, Any]:
    if drug is None:
        markdown = append_cache_info(f"No FDA drug label found for NDC: {ndc}", meta)
        return {"data": None, "markdown": markdown}

    of = drug.openfda
    brand = ", ".join(of.brand_name) if of.brand_name else "Unknown"
    generic = ", ".join(of.generic_name) if of.generic_name else "N/A"
    maker = ", ".join(of.manufacturer_name) if of.manufacturer_name else "N/A"

    lines = [
        f"## FDA Drug Label: {brand}",
        "",
        f"- **Generic Name:** {generic}",
        f"- **Manufacturer:** {maker}",
        f"- **Effective Date:** {drug.effective_time or 'N/A'}",
    ]
    if of.product_ndc:
        lines.append(f"- **NDC:** {', '.join(of.product_ndc)}")
    if of.route:
        lines.append(f"- **Route:** {', '.join(of.route)}")
    if of.dosage_form:
        lines.append(f"- **Dosage Form:** {', '.join(of.dosage_form)}")

    sections = [
        ("Purpose", drug.purpose),
        ("Indications & Usage", drug.indications_and_usage),
        ("Dosage & Administration", drug.dosage_and_administration),
        ("Warnings", drug.warnings),
        ("Contraindications", drug.contraindications),
        ("Adverse Reactions", drug.adverse_reactions),
        ("Drug Interactions", drug.drug_interactions),
        ("Use in Specific Populations", drug.use_in_specific_populations),
        ("Clinical Pharmacology", drug.clinical_pharmacology),
    ]

    for title, items in sections:
        if items:
            lines.append("")
            lines.append(f"### {title}")
            for item in items:
                lines.append(item)

    if drug.pediatric_dosing:
        lines.append("")
        lines.append("### Pediatric Dosing")
        lines.append(drug.pediatric_dosing)

    if drug.pediatric_warnings:
        lines.append("")
        lines.append("### Pediatric Warnings")
        lines.append(drug.pediatric_warnings)

    markdown = append_cache_info("\n".join(lines), meta)
    return {"data": drug.to_dict(), "markdown": markdown}


def format_rxnorm_drugs(
    drugs: list[RxNormDrug],
    query: str,
    meta: CacheMetadata,
) -> dict[str, Any]:
    lines = [f"## RxNorm Drug Nomenclature: {query}", ""]
    if not drugs:
        lines.append(f"No RxNorm drug concepts found for: {query}")
    else:
        for drug in drugs:
            lines.append(f"- **{drug.name}** (RxCUI: {drug.rxcui}, Term Type: {drug.tty or 'N/A'})")
            if drug.synonyms:
                lines.append(f"  - Synonyms: {', '.join(drug.synonyms)}")
            if drug.umlscui:
                lines.append(f"  - UMLS CUI: {', '.join(drug.umlscui)}")

    markdown = append_cache_info("\n".join(lines), meta)
    return {"data": [d.to_dict() for d in drugs], "markdown": markdown}


def format_health_indicators(
    records: list[WHOIndicatorRecord],
    query: str,
    meta: CacheMetadata,
) -> dict[str, Any]:
    lines = [f"## WHO Global Health Statistics: {query}", ""]
    if not records:
        lines.append(f"No health statistics found for: {query}")
    else:
        for record in records:
            val = record.value or (str(record.numeric_value) if record.numeric_value is not None else "N/A")
            unit_str = f" {record.unit}" if record.unit else ""
            lines.append(
                f"- **{record.indicator_name}** ({record.spatial_dim}, {record.time_dim or 'N/A'}): **{val}**{unit_str} "
                f"(Sex: {record.sex or 'All'}, Age: {record.age_group or 'All'})"
            )

    markdown = append_cache_info("\n".join(lines), meta)
    return {"data": [r.to_dict() for r in records], "markdown": markdown}


def format_guidelines(
    guidelines: list[ClinicalGuideline],
    query: str,
    meta: CacheMetadata,
) -> dict[str, Any]:
    lines = [f"## Clinical Practice Guidelines: {query}", ""]
    if not guidelines:
        lines.append(f"No clinical guidelines found for: {query}")
    else:
        for g in guidelines:
            lines.append(f"### {g.title}")
            lines.append(f"- **Organization:** {g.organization}")
            lines.append(f"- **Year:** {g.year or 'N/A'}")
            lines.append(f"- **Evidence Level:** {g.evidence_level}")
            lines.append(f"- **Relevance Score:** {g.score:.1f}")
            if g.pmid:
                lines.append(f"- **PMID:** {g.pmid}")
            if g.url:
                lines.append(f"- **URL:** {g.url}")
            if g.description:
                lines.append("")
                lines.append(g.description)
            lines.append("")

    markdown = append_cache_info("\n".join(lines).strip(), meta)
    return {"data": [g.to_dict() for g in guidelines], "markdown": markdown}


def format_pediatric_guidelines(
    guidelines: list[PediatricGuideline],
    query: str,
    meta: CacheMetadata,
) -> dict[str, Any]:
    lines = [f"## Pediatric Guidelines: {query}", ""]
    if not guidelines:
        lines.append(f"No pediatric guidelines found for: {query}")
    else:
        for g in guidelines:
            lines.append(f"### {g.title}")
            lines.append(f"- **Organization:** {g.organization}")
            lines.append(f"- **Source:** {g.source}")
            if g.year:
                lines.append(f"- **Year:** {g.year}")
            if g.age_group:
                lines.append(f"- **Age Group:** {g.age_group}")
            if g.url:
                lines.append(f"- **URL:** {g.url}")
            if g.screening_recommendations:
                lines.append(f"- **Screening:** {'; '.join(g.screening_recommendations)}")
            if g.description:
                lines.append("")
                lines.append(g.description)
            lines.append("")

    markdown = append_cache_info("\n".join(lines).strip(), meta)
    return {"data": [g.to_dict() for g in guidelines], "markdown": markdown}


def format_medical_articles(
    articles: list[MedicalArticle],
    query: str,
    meta: CacheMetadata,
) -> dict[str, Any]:
    lines = [f"## Medical Literature Results: {query}", ""]
    if not articles:
        lines.append(f"No medical literature found for: {query}")
    else:
        for a in articles:
            lines.append(f"### {a.title}")
            authors_str = ", ".join(a.authors) if a.authors else "N/A"
            lines.append(f"- **Authors:** {authors_str}")
            lines.append(f"- **Journal:** {a.journal or 'N/A'}")
            lines.append(f"- **Year:** {a.year or 'N/A'}")
            lines.append(f"- **Database:** {a.source_database}")
            if a.pmid:
                lines.append(f"- **PMID:** {a.pmid}")
            if a.doi:
                lines.append(f"- **DOI:** {a.doi}")
            if a.url:
                lines.append(f"- **URL:** {a.url}")
            if a.abstract:
                lines.append("")
                lines.append(a.abstract)
            lines.append("")

    markdown = append_cache_info("\n".join(lines).strip(), meta)
    return {"data": [a.to_dict() for a in articles], "markdown": markdown}
