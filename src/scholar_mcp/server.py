from typing import Any

from fastmcp import FastMCP

from scholar_mcp import citation_check
from scholar_mcp.config import Settings
from scholar_mcp.models import (
    DownloadResult,
    FullTextResponse,
    FullTextSummary,
    PaperMetadata,
)
from scholar_mcp.resolver import WaterfallResolver
from scholar_mcp.utils.cache import TTLCache
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.medical.clinical_trials import ClinicalTrialsClient
from scholar_mcp.medical.databases import MedicalDatabasesEngine
from scholar_mcp.medical.fda import FDAClient
from scholar_mcp.medical.formatters import (
    format_drug_details,
    format_drug_search_results,
    format_guidelines,
    format_health_indicators,
    format_medical_articles,
    format_pediatric_guidelines,
    format_rxnorm_drugs,
)
from scholar_mcp.medical.guidelines import GuidelinesEngine
from scholar_mcp.medical.pediatrics import PediatricsEngine
from scholar_mcp.medical.pubmed import MedicalPubMedClient
from scholar_mcp.medical.rxnorm import RxNormClient
from scholar_mcp.medical.who import WHOClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

settings = Settings.load()
http_client = AsyncHttpClient(settings)
cache = TTLCache(maxsize=settings.cache_size, ttl_seconds=settings.cache_ttl_seconds)
resolver = WaterfallResolver(settings=settings, http_client=http_client, cache=cache)

medical_cache = SQLiteCacheManager(db_path=settings.cache_db_path, settings=settings)
pubmed_client = MedicalPubMedClient(http_client=http_client, cache=medical_cache, settings=settings)
fda_client = FDAClient(http_client=http_client, cache=medical_cache, settings=settings)
rxnorm_client = RxNormClient(http_client=http_client, cache=medical_cache, settings=settings)
who_client = WHOClient(http_client=http_client, cache=medical_cache, settings=settings)
clinical_trials_client = ClinicalTrialsClient(http_client=http_client, cache=medical_cache, settings=settings)
guidelines_engine = GuidelinesEngine(pubmed=pubmed_client, cache=medical_cache, settings=settings)
pediatrics_engine = PediatricsEngine(
    http_client=http_client, cache=medical_cache, settings=settings, pubmed=pubmed_client
)
databases_engine = MedicalDatabasesEngine(
    pubmed=pubmed_client,
    clinical_trials=clinical_trials_client,
    http_client=http_client,
    cache=medical_cache,
    settings=settings,
)

mcp = FastMCP("ScholarMCP")


@mcp.tool()
async def search_papers(
    query: str,
    source: str = "auto",
    num_results: int = 5,
    rerank: bool = True,
    year_start: int | None = None,
    year_end: int | None = None,
    author: str | None = None,
    journal: str | None = None,
) -> list[dict[str, Any]]:
    """Search for academic papers across PubMed and CrossRef with smart re-ranking.

    Args:
        query: Search keywords or query string.
        source: 'auto' (PubMed first, top up with CrossRef), 'pubmed', 'crossref', or 's2' (Semantic Scholar).
        num_results: Maximum number of results to return (max 50).
        rerank: Whether to re-rank results using citation impact, recency decay, and Z-scores (default True).
        year_start: Filter papers published in or after this year.
        year_end: Filter papers published in or before this year.
        author: Filter by author name.
        journal: Filter by journal name.
    """
    clamped_num = min(max(1, num_results), 50)
    try:
        results = await resolver.search(
            query=query,
            source=source,
            num_results=clamped_num,
            rerank=rerank,
            year_start=year_start,
            year_end=year_end,
            author=author,
            journal=journal,
        )
        return [r.to_dict() for r in results]
    except Exception as ex:
        return [{"status": "error", "error": str(ex)}]



@mcp.tool()
async def get_full_text(
    identifier: str,
    max_chars: int | None = None,
    sections: list[str] | None = None,
) -> dict[str, Any]:
    """Retrieve full text of an academic paper using multi-tier waterfall resolution.

    Tiers: Europe PMC -> PMC -> Unpaywall -> arXiv -> Sci-Hub -> Abstract fallback.

    Args:
        identifier: DOI, PMID, PMCID, arXiv ID, or paper title.
        max_chars: Maximum character limit for output (defaults to 50,000).
        sections: List of section names to extract (e.g. ['Methods', 'Results']).
    """
    try:
        resp = await resolver.resolve_full_text(
            identifier=identifier,
            max_chars=max_chars,
            sections=sections,
        )
        return resp.to_dict()
    except Exception as ex:
        return {
            "status": "error",
            "source": "none",
            "content": "",
            "error": str(ex),
        }


@mcp.tool()
async def get_full_text_batch(
    identifiers: list[str],
) -> list[dict[str, Any]]:
    """Retrieve full-text summaries for up to 25 papers concurrently.

    Args:
        identifiers: List of DOIs, PMIDs, PMCIDs, arXiv IDs, or paper titles (max 25).
    """
    if len(identifiers) > 25:
        return [
            {
                "identifier": "",
                "status": "error",
                "source": "none",
                "error": "Batch size exceeds maximum limit of 25 identifiers",
            }
        ]

    try:
        summaries = await resolver.resolve_full_text_batch(identifiers)
        return [s.to_dict() for s in summaries]
    except Exception as ex:
        return [
            {
                "identifier": "",
                "status": "error",
                "source": "none",
                "error": str(ex),
            }
        ]


@mcp.tool()
async def get_metadata(
    identifier: str,
) -> dict[str, Any]:
    """Retrieve paper metadata and abstract without running the multi-tier full-text waterfall.

    Args:
        identifier: DOI, PMID, PMCID, arXiv ID, or paper title.
    """
    try:
        meta = await resolver.get_metadata(identifier)
        if meta:
            return meta.to_dict()
        return {
            "status": "not_found",
            "error": f"Metadata not found for identifier: {identifier}",
        }
    except Exception as ex:
        return {
            "status": "error",
            "error": str(ex),
        }


@mcp.tool()
async def download_paper(
    identifier: str,
    output_path: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Download the PDF of a paper into the configured download directory sandbox.

    Args:
        identifier: DOI, PMID, PMCID, arXiv ID, or paper title.
        output_path: Relative path inside the download directory to save the PDF.
        overwrite: Whether to overwrite existing files (default False).
    """
    try:
        res = await resolver.download_article(
            identifier=identifier,
            output_path=output_path,
            overwrite=overwrite,
        )
        return res.to_dict()
    except Exception as ex:
        return {
            "success": False,
            "saved_path": output_path,
            "source_used": "none",
            "message": f"Download failed: {ex}",
        }


@mcp.tool()
async def deep_paper_analysis_prompt(
    identifier: str,
) -> dict[str, Any]:
    """Generate a structured analysis prompt loaded with the resolved paper full text.

    Args:
        identifier: DOI, PMID, PMCID, arXiv ID, or paper title.
    """
    try:
        resp = await resolver.resolve_full_text(identifier)
        prompt_text = f"""Please perform a deep, rigorous scientific analysis of the following paper.

Title: {resp.title}
DOI: {resp.doi or 'N/A'}
PMID: {resp.pmid or 'N/A'}
PMCID: {resp.pmcid or 'N/A'}
Source: {resp.source}

--- PAPER CONTENT ---
{resp.content}
---------------------

Please evaluate:
1. Core thesis and primary research questions
2. Experimental methodology, controls, and potential confounders
3. Key quantitative findings and statistical robustness
4. Strengths, critical limitations, and unaddressed questions
5. Broader scientific implications and future directions
"""
        return {
            "status": resp.status,
            "source": resp.source,
            "title": resp.title,
            "analysis_prompt": prompt_text,
        }
    except Exception as ex:
        return {
            "status": "error",
            "error": str(ex),
            "analysis_prompt": "",
        }


@mcp.tool()
async def get_references(
    identifier: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Retrieve bibliography and cited references for an academic paper.

    Args:
        identifier: DOI, PMID, PMCID, arXiv ID, or paper title.
        limit: Maximum number of references to return (max 100).
    """
    clamped_limit = min(max(1, limit), 100)
    try:
        refs = await resolver.get_references(identifier, limit=clamped_limit)
        return [r.to_dict() for r in refs]
    except Exception as ex:
        return [{"status": "error", "error": str(ex)}]


@mcp.tool()
async def get_citations(
    identifier: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Retrieve forward citations (papers that cited this paper).

    Args:
        identifier: DOI, PMID, PMCID, arXiv ID, or paper title.
        limit: Maximum number of citing papers to return (max 100).
    """
    clamped_limit = min(max(1, limit), 100)
    try:
        cits = await resolver.get_citations(identifier, limit=clamped_limit)
        return [c.to_dict() for c in cits]
    except Exception as ex:
        return [{"status": "error", "error": str(ex)}]


@mcp.tool()
async def get_related_papers(
    identifier: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Retrieve computationally related and similar papers via PubMed, with Semantic Scholar recommendations as fallback.

    Args:
        identifier: DOI, PMID, PMCID, arXiv ID, or paper title.
        limit: Maximum number of related papers to return (max 25).
    """
    clamped_limit = min(max(1, limit), 25)
    try:
        related = await resolver.get_related_papers(identifier, limit=clamped_limit)
        return [rel.to_dict() for rel in related]
    except Exception as ex:
        return [{"status": "error", "error": str(ex)}]


@mcp.tool()
async def check_citations(
    claims: list[dict[str, str]],
    deep: bool = False,
) -> list[dict[str, Any]]:
    """Verify each claim is supported by its cited paper (claim-to-source grounding).

    Args:
        claims: List of {"text": <claim sentence>, "identifier": <DOI/PMID/PMCID/arXiv ID>}, max 25.
        deep: Fetch full text instead of abstract only (slower, more thorough).
    """
    try:
        return await citation_check.check_citations(resolver, claims, deep=deep)
    except Exception as ex:
        return [
            {
                "identifier": "",
                "verdict": "ERROR",
                "coverage_score": 0.0,
                "best_evidence_sentence": "",
                "resolved_title": "",
                "error": str(ex),
            }
        ]


@mcp.prompt("deep_paper_analysis")

async def deep_paper_analysis(identifier: str) -> str:
    """Prompt template for deep paper analysis."""
    result = await deep_paper_analysis_prompt(identifier)
    return result.get("analysis_prompt", "")


if settings.enable_medical_tools:

    @mcp.tool()
    async def search_drugs(query: str, limit: int = 10) -> dict[str, Any]:
        """Search for drug information using the FDA database.

        Args:
            query: Drug name to search for (brand name or generic name).
            limit: Number of results to return (max 50).
        """
        clamped = min(max(1, limit), 50)
        try:
            drugs, meta = await fda_client.search_drugs(query, clamped)
            return format_drug_search_results(drugs, query, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "fda"}

    @mcp.tool()
    async def get_drug_details(ndc: str) -> dict[str, Any]:
        """Get detailed information about a specific drug by NDC (National Drug Code).

        Args:
            ndc: National Drug Code (NDC) of the drug.
        """
        try:
            drug, meta = await fda_client.get_drug_by_ndc(ndc)
            if drug is None:
                return {"status": "not_found", "ndc": ndc}
            return format_drug_details(drug, ndc, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "fda"}

    @mcp.tool()
    async def search_pediatric_drugs(query: str, limit: int = 10) -> dict[str, Any]:
        """Search for FDA-approved drugs with pediatric indications or dosing.

        Args:
            query: Drug name to search for.
            limit: Number of results to return (max 50).
        """
        clamped = min(max(1, limit), 50)
        try:
            drugs, meta = await fda_client.search_pediatric_drugs(query, clamped)
            return format_drug_search_results(drugs, query, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "fda"}

    @mcp.tool()
    async def search_drug_nomenclature(query: str) -> dict[str, Any]:
        """Search for standardized drug concepts and nomenclature using RxNorm.

        Args:
            query: Drug name or concept to search for.
        """
        try:
            drugs, meta = await rxnorm_client.search_drug_nomenclature(query)
            return format_rxnorm_drugs(drugs, query, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "rxnorm"}

    @mcp.tool()
    async def get_health_statistics(
        indicator: str,
        country: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Query WHO Global Health Observatory for global and country-specific health indicators.

        Args:
            indicator: Health indicator name or topic (e.g. 'life expectancy', 'mortality rate').
            country: Optional 3-letter ISO country code (e.g. 'USA', 'BRA').
            limit: Maximum number of records to return (max 20).
        """
        clamped = min(max(1, limit), 20)
        try:
            records, meta = await who_client.get_health_statistics(indicator, country=country, limit=clamped)
            return format_health_indicators(records, indicator, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "who"}

    @mcp.tool()
    async def get_child_health_statistics(
        indicator: str,
        country: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Retrieve pediatric and child health statistics from WHO Global Health Observatory.

        Args:
            indicator: Child health topic (e.g. 'mortality', 'immunization', 'nutrition').
            country: Optional 3-letter ISO country code.
            limit: Maximum number of records to return (max 20).
        """
        clamped = min(max(1, limit), 20)
        try:
            records, meta = await who_client.get_child_health_statistics(indicator, country=country, limit=clamped)
            return format_health_indicators(records, indicator, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "who"}

    @mcp.tool()
    async def search_clinical_guidelines(
        query: str,
        organization: str | None = None,
    ) -> dict[str, Any]:
        """Search PubMed for clinical practice guidelines with heuristic relevance scoring.

        Args:
            query: Medical condition, intervention, or clinical question.
            organization: Optional organization name filter (e.g. 'AHA', 'AAP', 'WHO').
        """
        try:
            guidelines, meta = await guidelines_engine.search_clinical_guidelines(query, organization=organization)
            return format_guidelines(guidelines, query, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "guidelines"}

    @mcp.tool()
    async def search_pediatric_guidelines(
        query: str,
        source: str = "all",
    ) -> dict[str, Any]:
        """Search pediatric clinical practice guidelines across AAP sources.

        Args:
            query: Pediatric topic or condition to search for.
            source: Guideline source: 'all', 'bright-futures', or 'aap-policy'.
        """
        try:
            if source == "bright-futures":
                guidelines, meta = await pediatrics_engine.search_bright_futures(query)
            elif source == "aap-policy":
                guidelines, meta = await pediatrics_engine.search_aap_policy(query)
            else:
                guidelines, meta = await pediatrics_engine.search_aap_guidelines(query)
            return format_pediatric_guidelines(guidelines, query, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "pediatrics"}

    @mcp.tool()
    async def search_aap_guidelines(query: str) -> dict[str, Any]:
        """Search Bright Futures and AAP Policy statements concurrently with deduplication.

        Args:
            query: Pediatric health topic or preventive care keyword.
        """
        try:
            guidelines, meta = await pediatrics_engine.search_aap_guidelines(query)
            return format_pediatric_guidelines(guidelines, query, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "pediatrics"}

    @mcp.tool()
    async def search_pediatric_literature(
        query: str,
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Search leading pediatric journals indexed in PubMed.

        Args:
            query: Pediatric research keywords or topic.
            max_results: Maximum number of articles to return (max 20).
        """
        clamped = min(max(1, max_results), 20)
        try:
            articles, meta = await pediatrics_engine.search_pediatric_literature(query, max_results=clamped)
            return format_medical_articles(articles, query, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "pediatrics"}

    @mcp.tool()
    async def search_medical_databases(query: str) -> dict[str, Any]:
        """Search across PubMed, ClinicalTrials.gov, and Cochrane Library with cross-database deduplication.

        Args:
            query: Medical condition, intervention, or clinical question.
        """
        try:
            articles, meta = await databases_engine.search_medical_databases(query)
            return format_medical_articles(articles, query, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "databases"}

    @mcp.tool()
    async def search_medical_journals(query: str) -> dict[str, Any]:
        """Search top-tier general medical journals (NEJM, JAMA, Lancet, BMJ, Nature Medicine) via PubMed.

        Args:
            query: Medical topic or research question.
        """
        try:
            articles, meta = await databases_engine.search_medical_journals(query)
            return format_medical_articles(articles, query, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "databases"}

    @mcp.tool()
    async def get_medical_cache_stats() -> dict[str, Any]:
        """Get statistics and performance metrics for the SQLite medical cache."""
        try:
            return await medical_cache.get_stats()
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "cache"}


def main() -> None:
    """Server entrypoint."""
    mcp.run()


if __name__ == "__main__":
    main()
