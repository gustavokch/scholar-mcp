import asyncio
from pathlib import Path
import time
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.identifiers import resolve_identifiers
from scholar_mcp.models import (
    CitationItem,
    DownloadResult,
    FetchAttempt,
    FullTextResponse,
    FullTextSummary,
    IdentifierMap,
    PaperMetadata,
    ReferenceItem,
    RelatedPaper,
)

from scholar_mcp.parsers.jats import list_sections, select_sections
from scholar_mcp.providers.arxiv import ARXIV_PDF, ArxivProvider
from scholar_mcp.providers.crossref import CrossRefProvider
from scholar_mcp.providers.europe_pmc import EuropePMCProvider, annotate_oa_status
from scholar_mcp.providers.openalex import OpenAlexProvider
from scholar_mcp.providers.pmc import PMCProvider
from scholar_mcp.providers.pubmed import PubMedProvider
from scholar_mcp.providers.scihub import SciHubProvider
from scholar_mcp.providers.semantic_scholar import SemanticScholarProvider
from scholar_mcp.providers.unpaywall import UNPAYWALL_BASE, UnpaywallProvider
from scholar_mcp.ranking import RankingPipeline
from scholar_mcp.utils.cache import TTLCache
from scholar_mcp.utils.http import AsyncHttpClient


def _truncate_content(content: str, max_chars: int) -> tuple[str, bool]:
    if len(content) <= max_chars:
        return content, False

    cutoff = max_chars
    # Try finding paragraph boundary before cutoff
    para_break = content.rfind("\n\n", 0, cutoff)
    if para_break > int(cutoff * 0.7):
        truncated_text = content[:para_break].rstrip()
    else:
        truncated_text = content[:cutoff].rstrip()

    marker = "\n\n[... Truncated due to max_chars limit ...]"
    return truncated_text + marker, True


class WaterfallResolver:
    """Multi-tier waterfall resolver for academic paper discovery and full-text retrieval."""

    def __init__(
        self,
        settings: Settings | None = None,
        http_client: AsyncHttpClient | None = None,
        cache: TTLCache | None = None,
    ) -> None:
        self.settings = settings or Settings.load()
        self.http_client = http_client or AsyncHttpClient(self.settings)
        self.cache = cache or TTLCache(
            maxsize=self.settings.cache_size,
            ttl_seconds=self.settings.cache_ttl_seconds,
        )
        self.europe_pmc = EuropePMCProvider(self.http_client)
        self.pmc = PMCProvider(self.http_client)
        self.arxiv = ArxivProvider(self.http_client)
        self.unpaywall = UnpaywallProvider(self.http_client, email=self.settings.unpaywall_email)
        self.scihub = SciHubProvider(self.http_client, mirrors=self.settings.scihub_mirrors)
        self.pubmed = PubMedProvider(self.http_client, self.settings)
        self.crossref = CrossRefProvider(self.http_client)
        self.openalex = OpenAlexProvider(self.http_client, email=self.settings.openalex_email)
        self.s2 = SemanticScholarProvider(self.http_client, api_key=self.settings.s2_api_key)
        self.ranking_pipeline = RankingPipeline(
            openalex=self.openalex,
            europe_pmc=self.europe_pmc,
            crossref=self.crossref,
            cache=self.cache,
            settings=self.settings,
        )

    async def resolve_ids(self, identifier: str) -> IdentifierMap:
        return await resolve_identifiers(identifier, self.http_client, self.cache, self.settings)

    async def fetch_abstract(self, ids: IdentifierMap) -> PaperMetadata | None:
        meta = await self.pubmed.fetch_abstract(ids)
        if (not meta or not meta.abstract) and ids.doi:
            meta = await self.crossref.fetch_metadata(ids.doi)
        if (not meta or not meta.abstract) and ids.arxiv:
            arxiv_meta = await self.arxiv.fetch_metadata(ids.arxiv)
            if arxiv_meta is not None:
                meta = arxiv_meta

        # Enrich with OpenAlex citation counts / OA URLs / institutions
        if self.settings.enable_openalex and ids.doi:
            if meta is None or meta.citation_count is None:
                enriched = await self.openalex.fetch_metadata(ids.doi)
                if enriched:
                    if meta is None:
                        meta = enriched
                    else:
                        meta.citation_count = enriched.citation_count
                        if enriched.oa_url:
                            meta.oa_url = enriched.oa_url
                        if not meta.institutions:
                            meta.institutions = enriched.institutions
                        if not meta.abstract and enriched.abstract:
                            meta.abstract = enriched.abstract
                        if meta.oa_status in ("", "unknown") and enriched.oa_status != "unknown":
                            meta.oa_status = enriched.oa_status
        return meta

    async def fetch_pdf_bytes(self, ids: IdentifierMap) -> tuple[bytes | None, str | None]:
        # Try Unpaywall first if configured
        if self.settings.unpaywall_configured() and ids.doi:
            try:
                resp = await self.http_client.get(
                    f"{UNPAYWALL_BASE}/{ids.doi.strip()}",
                    params={"email": self.settings.unpaywall_email},
                )
                if resp is not None and resp.status_code == 200:
                    data = resp.json()
                    if data.get("is_oa"):
                        loc = data.get("best_oa_location") or {}
                        pdf_url = loc.get("url_for_pdf") or loc.get("url")
                        if pdf_url:
                            b = await self.http_client.get_bytes(pdf_url)
                            if b:
                                return b, "unpaywall"
            except Exception:
                pass

        # Try arXiv when an arXiv ID is known (free, fast, legal)
        if ids.arxiv:
            try:
                b = await self.http_client.get_bytes(f"{ARXIV_PDF}/{ids.arxiv}")
                # arXiv answers 200 with an HTML placeholder while a PDF is still
                # being generated; never hand that to the caller as a PDF.
                if b and b.startswith(b"%PDF-"):
                    return b, "arxiv"
            except Exception:
                pass

        # Try Sci-Hub if enabled
        if self.settings.scihub_tier_enabled() and ids.doi:
            b, _url = await self.scihub.fetch_pdf_bytes(ids)
            if b:
                return b, "scihub"

        return None, None

    async def resolve_full_text(
        self,
        identifier: str,
        max_chars: int | None = None,
        sections: list[str] | None = None,
    ) -> FullTextResponse:
        max_len = max_chars if max_chars is not None else self.settings.max_chars
        attempts: list[FetchAttempt] = []

        ids = await self.resolve_ids(identifier)
        if ids.ambiguous:
            return FullTextResponse(
                status="ambiguous_match",
                source="none",
                content="",
                attempts=attempts,
            )

        # Plan tiers
        # 1. Europe PMC -> 2. PMC -> 3. Unpaywall -> 4. arXiv -> 5. Sci-Hub -> 6. Abstract fallback
        tiers_plan = [
            ("europepmc", self.europe_pmc, None),
            ("pmc", self.pmc, None),
        ]

        # Unpaywall skip checks
        unpaywall_skip = None
        if self.settings.prefer_scihub_over_unpaywall and self.settings.enable_scihub:
            unpaywall_skip = "PREFER_SCIHUB_OVER_UNPAYWALL"
        tiers_plan.append(("unpaywall", self.unpaywall, unpaywall_skip))

        # arXiv: runs only when an arXiv ID is known (provider self-skips otherwise)
        tiers_plan.append(("arxiv", self.arxiv, None))

        # Sci-Hub skip checks
        scihub_skip = None
        if not self.settings.scihub_tier_enabled():
            scihub_skip = "ENABLE_SCIHUB is False"
        tiers_plan.append(("scihub", self.scihub, scihub_skip))

        async def _execute_waterfall() -> FullTextResponse | None:
            for name, provider, skip_reason in tiers_plan:
                if skip_reason:
                    attempts.append(FetchAttempt(tier=name, outcome="skipped", reason=skip_reason))
                    continue

                start = time.monotonic()
                try:
                    res = await provider.fetch_full_text(ids)
                    elapsed = int((time.monotonic() - start) * 1000)
                    if res is not None and res.content:
                        attempts.append(FetchAttempt(tier=name, outcome="hit", elapsed_ms=elapsed))
                        return res
                    elif getattr(provider, "last_skip_reason", ""):
                        attempts.append(FetchAttempt(tier=name, outcome="skipped", reason=provider.last_skip_reason, elapsed_ms=elapsed))
                    else:
                        attempts.append(FetchAttempt(tier=name, outcome="miss", elapsed_ms=elapsed))
                except Exception as ex:
                    elapsed = int((time.monotonic() - start) * 1000)
                    attempts.append(FetchAttempt(tier=name, outcome="error", reason=str(ex), elapsed_ms=elapsed))

            return None

        winning_resp: FullTextResponse | None = None
        try:
            winning_resp = await asyncio.wait_for(
                _execute_waterfall(),
                timeout=float(self.settings.total_budget_seconds),
            )
        except asyncio.TimeoutError:
            # Mark remaining in-flight tier as timeout
            if len(attempts) < len(tiers_plan):
                in_flight_tier = tiers_plan[len(attempts)][0]
                attempts.append(FetchAttempt(tier=in_flight_tier, outcome="timeout", reason="Total budget exceeded"))

        if winning_resp is not None:
            content = winning_resp.content
            if sections:
                content = select_sections(content, sections)

            total_chars = len(content)
            available_sections = (
                winning_resp.sections_available
                if winning_resp.sections_available
                else list_sections(winning_resp.content)
            )

            truncated_content, is_truncated = _truncate_content(content, max_len)
            winning_resp.content = truncated_content
            winning_resp.truncated = is_truncated
            winning_resp.total_chars = total_chars
            winning_resp.sections_available = available_sections
            winning_resp.attempts = attempts
            return winning_resp

        # Tier 5: Abstract fallback
        start = time.monotonic()
        try:
            meta = await self.fetch_abstract(ids)
            elapsed = int((time.monotonic() - start) * 1000)
            if meta is not None and meta.abstract:
                attempts.append(FetchAttempt(tier="abstract_fallback", outcome="hit", elapsed_ms=elapsed))
                content, is_truncated = _truncate_content(meta.abstract, max_len)
                return FullTextResponse(
                    status="abstract_only",
                    source="abstract_fallback",
                    format="text",
                    title=meta.title,
                    content=content,
                    doi=meta.doi or ids.doi,
                    pmid=meta.pmid or ids.pmid,
                    pmcid=meta.pmcid or ids.pmcid,
                    truncated=is_truncated,
                    total_chars=len(meta.abstract),
                    attempts=attempts,
                )
            else:
                attempts.append(FetchAttempt(tier="abstract_fallback", outcome="miss", elapsed_ms=elapsed))
        except Exception as ex:
            elapsed = int((time.monotonic() - start) * 1000)
            attempts.append(FetchAttempt(tier="abstract_fallback", outcome="error", reason=str(ex), elapsed_ms=elapsed))

        return FullTextResponse(
            status="not_found",
            source="none",
            content="",
            doi=ids.doi,
            pmid=ids.pmid,
            pmcid=ids.pmcid,
            attempts=attempts,
        )

    async def resolve_full_text_batch(
        self,
        identifiers: list[str],
    ) -> list[FullTextSummary]:
        if len(identifiers) > 25:
            raise ValueError("Batch size exceeds maximum limit of 25 identifiers")

        semaphore = asyncio.Semaphore(self.settings.max_concurrency)

        async def _resolve_one(identifier: str) -> FullTextSummary:
            async with semaphore:
                try:
                    resp = await self.resolve_full_text(identifier)
                    excerpt = resp.content[:200].replace("\n", " ").strip() if resp.content else ""
                    return FullTextSummary(
                        identifier=identifier,
                        status=resp.status,
                        source=resp.source,
                        title=resp.title,
                        excerpt=excerpt,
                        url=resp.url,
                    )
                except Exception as ex:
                    return FullTextSummary(
                        identifier=identifier,
                        status="error",
                        source="none",
                        excerpt=str(ex),
                    )

        tasks = [_resolve_one(ident) for ident in identifiers]
        return await asyncio.gather(*tasks)

    async def get_metadata(self, identifier: str) -> PaperMetadata | None:
        ids = await self.resolve_ids(identifier)
        return await self.fetch_abstract(ids)

    async def download_article(
        self,
        identifier: str,
        output_path: str | Path,
        overwrite: bool = False,
    ) -> DownloadResult:
        root_dir = self.settings.download_dir.resolve()
        requested_path = Path(output_path)

        if requested_path.is_absolute():
            target_path = requested_path.resolve()
        else:
            target_path = (root_dir / requested_path).resolve()

        # Sandbox check: ensure target_path is inside root_dir
        try:
            target_path.relative_to(root_dir)
        except ValueError:
            return DownloadResult(
                success=False,
                saved_path=str(output_path),
                source_used="none",
                message="Target path is outside download directory sandbox",
            )

        if target_path.exists() and not overwrite:
            return DownloadResult(
                success=False,
                saved_path=str(target_path),
                source_used="none",
                message=f"File already exists: {target_path}",
            )

        ids = await self.resolve_ids(identifier)
        pdf_bytes, source_used = await self.fetch_pdf_bytes(ids)
        if not pdf_bytes:
            return DownloadResult(
                success=False,
                saved_path=str(target_path),
                source_used="none",
                message="No PDF available from providers",
            )

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(target_path.write_bytes, pdf_bytes)
            return DownloadResult(
                success=True,
                saved_path=str(target_path),
                source_used=source_used or "unknown",
                file_size_bytes=len(pdf_bytes),
                message="Downloaded successfully",
            )
        except Exception as ex:
            return DownloadResult(
                success=False,
                saved_path=str(target_path),
                source_used="none",
                message=f"Failed to write file: {ex}",
            )

    async def search(
        self,
        query: str,
        source: str = "auto",
        num_results: int = 10,
        rerank: bool = True,
        author: str | None = None,
        journal: str | None = None,
        year_start: int | None = None,
        year_end: int | None = None,
    ) -> list[PaperMetadata]:
        limit = min(num_results, 50)
        source_mode = source.lower().strip()

        # Compute candidate pool depth if reranking is enabled
        should_rerank = rerank and self.settings.ranking_enabled
        if should_rerank:
            candidate_pool_size = min(
                self.settings.ranking_max_candidates,
                max(
                    limit * self.settings.ranking_candidate_multiplier,
                    self.settings.ranking_min_candidates,
                ),
            )
            fetch_limit = candidate_pool_size
        else:
            fetch_limit = limit

        if source_mode == "pubmed":
            papers = await self.pubmed.search(
                query,
                num_results=fetch_limit,
                author=author,
                journal=journal,
                year_start=year_start,
                year_end=year_end,
                sort="relevance",
            )
        elif source_mode == "crossref":
            papers = await self.crossref.search(
                query,
                num_results=fetch_limit,
                author=author,
                journal=journal,
                year_start=year_start,
                year_end=year_end,
            )
        elif source_mode in ("s2", "semanticscholar"):
            if not self.settings.enable_s2:
                return []
            papers = await self.s2.search(
                query,
                num_results=fetch_limit,
                author=author,
                journal=journal,
                year_start=year_start,
                year_end=year_end,
            )
        else:  # auto
            # 1. Query PubMed for fetch_limit
            papers = await self.pubmed.search(
                query,
                num_results=fetch_limit,
                author=author,
                journal=journal,
                year_start=year_start,
                year_end=year_end,
                sort="relevance",
            )
            # 2. If PubMed returns fewer than fetch_limit, top up from CrossRef
            if len(papers) < fetch_limit:
                needed = fetch_limit - len(papers)
                crossref_papers = await self.crossref.search(
                    query,
                    num_results=needed * 2,
                    author=author,
                    journal=journal,
                    year_start=year_start,
                    year_end=year_end,
                )
                # Deduplicate: PubMed records win on conflict
                seen_dois = {p.doi.lower() for p in papers if p.doi}
                seen_titles = {p.title.lower().strip() for p in papers if p.title}
                for cp in crossref_papers:
                    if cp.doi and cp.doi.lower() in seen_dois:
                        continue
                    if cp.title and cp.title.lower().strip() in seen_titles:
                        continue
                    papers.append(cp)
                    if cp.doi:
                        seen_dois.add(cp.doi.lower())
                    if cp.title:
                        seen_titles.add(cp.title.lower().strip())
                    if len(papers) >= fetch_limit:
                        break

        # Re-rank if requested and candidates present
        if should_rerank and papers:
            papers = await self.ranking_pipeline.rank_papers(papers, top_n=limit)
        else:
            papers = papers[:limit]

        # Annotate OA status in one batched call
        await annotate_oa_status(papers, self.http_client)
        return papers

    async def get_references(
        self,
        identifier: str,
        limit: int = 50,
    ) -> list[ReferenceItem]:
        ids = await self.resolve_ids(identifier)
        # Try Europe PMC references
        refs = await self.europe_pmc.fetch_references(ids, limit=limit)
        if refs:
            return refs

        # Fallback to CrossRef if DOI available
        if ids.doi:
            refs = await self.crossref.fetch_references(ids.doi, limit=limit)
            if refs:
                return refs

        return []

    async def get_citations(
        self,
        identifier: str,
        limit: int = 50,
    ) -> list[CitationItem]:
        ids = await self.resolve_ids(identifier)
        cits = await self.europe_pmc.fetch_citations(ids, limit=limit)
        if cits:
            return cits
        if self.settings.enable_openalex and ids.doi:
            return await self.openalex.fetch_citations(ids.doi, limit=limit)
        return []

    async def get_related_papers(
        self,
        identifier: str,
        limit: int = 10,
    ) -> list[RelatedPaper]:
        ids = await self.resolve_ids(identifier)
        pmid = ids.pmid
        if not pmid and ids.doi:
            meta = await self.pubmed.fetch_abstract(ids)
            if meta and meta.pmid:
                pmid = meta.pmid

        if pmid:
            related = await self.pubmed.fetch_related_papers(pmid, limit=limit)
            if related:
                return related

        # Fallback: S2 recommendations by DOI or arXiv (covers non-biomed)
        if self.settings.enable_s2:
            paper_id = None
            if ids.doi:
                paper_id = f"DOI:{ids.doi}"
            elif ids.arxiv:
                paper_id = f"ARXIV:{ids.arxiv}"
            if paper_id:
                return await self.s2.fetch_recommendations(paper_id, limit=limit)

        return []

