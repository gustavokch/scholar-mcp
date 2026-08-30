import re

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import ClinicalGuideline, GuidelineScore, MedicalArticle
from scholar_mcp.medical.pubmed import MedicalPubMedClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

GUIDELINE_PUBLICATION_TYPES = [
    '"practice guideline"[pt]',
    '"guideline"[pt]',
    '"consensus development conference"[pt]',
    '"consensus development conference, nih"[pt]',
    '"technical report"[pt]',
]

GUIDELINE_KEYWORDS = [
    "guideline",
    "recommendation",
    "consensus",
    "position statement",
    "standard of care",
    "best practice",
    "evidence-based",
    "expert consensus",
]

KNOWN_GUIDELINE_JOURNALS = [
    "journal of the american",
    "new england journal",
    "lancet",
    "bmj",
    "annals of",
    "guidelines",
    "recommendations",
]

ORG_EXTRACTION_PATTERNS = [
    r"(American|European|National|International|World|Global).*?"
    r"(Association|College|Society|Academy|Institute|Foundation|Organization|Committee|Ministry)(?:\s+of\s+[A-Za-z]+)?",
    r"(World Health Organization|WHO)",
    r"(Centers for Disease Control|CDC)",
    r"(National Institutes of Health|NIH)",
]

ORG_ABBREVIATIONS = {
    "aap": ["american academy of pediatrics", "american academy pediatric"],
    "who": ["world health organization"],
    "cdc": ["centers for disease control"],
    "aha": ["american heart association"],
    "acc": ["american college of cardiology"],
    "ada": ["american diabetes association"],
    "acp": ["american college of physicians"],
}

MIN_SCORE_THRESHOLD = 2.5
LAYER_THRESHOLD = 5


def extract_organization(article: MedicalArticle) -> str:
    org = "Unknown Organization"
    if article.journal:
        org = article.journal

    # Search in abstract for prominent organizational patterns
    if article.abstract:
        for pat in ORG_EXTRACTION_PATTERNS:
            match = re.search(pat, article.abstract, flags=re.IGNORECASE)
            if match:
                return match.group(0).strip()

    # Search in title
    if org == "Unknown Organization" and article.title:
        for pat in ORG_EXTRACTION_PATTERNS:
            match = re.search(pat, article.title, flags=re.IGNORECASE)
            if match:
                return match.group(0).strip()

    return org


def calculate_guideline_score(
    article: MedicalArticle,
    has_publication_type: bool,
) -> GuidelineScore:
    pub_score = 2.0 if has_publication_type else 0.0

    title_lower = article.title.lower() if article.title else ""
    title_score = 1.0 if any(kw in title_lower for kw in GUIDELINE_KEYWORDS) else 0.0

    journal_lower = article.journal.lower() if article.journal else ""
    journal_score = 1.0 if any(kj in journal_lower for kj in KNOWN_GUIDELINE_JOURNALS) else 0.0

    org = extract_organization(article)
    affiliation_score = 1.0 if org != "Unknown Organization" else 0.0

    abstract_lower = article.abstract.lower() if article.abstract else ""
    kw_hits = sum(1 for kw in GUIDELINE_KEYWORDS if kw in abstract_lower)
    abstract_score = min(1.0, kw_hits * 0.5)

    mesh_score = 0.0
    total = pub_score + title_score + journal_score + affiliation_score + abstract_score + mesh_score

    return GuidelineScore(
        publication_type=pub_score,
        title_keywords=title_score,
        journal_reputation=journal_score,
        author_affiliation=affiliation_score,
        abstract_keywords=abstract_score,
        mesh_terms=mesh_score,
        total=round(total, 2),
    )


def resolve_organization_aliases(organization: str) -> list[str]:
    org_lower = organization.lower().strip()
    aliases = {org_lower}

    # Abbreviation given -> add its full-name expansions.
    aliases.update(ORG_ABBREVIATIONS.get(org_lower, []))

    # Full name given -> add the abbreviation it expands from.
    for abbrev, full_names in ORG_ABBREVIATIONS.items():
        if org_lower in full_names or any(org_lower in fn or fn in org_lower for fn in full_names):
            aliases.add(abbrev)

    return list(aliases)


class GuidelinesEngine:
    def __init__(
        self,
        pubmed: MedicalPubMedClient,
        cache: SQLiteCacheManager,
        settings: Settings,
    ) -> None:
        self.pubmed = pubmed
        self.cache = cache
        self.settings = settings

    async def search_clinical_guidelines(
        self,
        query: str,
        organization: str | None = None,
    ) -> tuple[list[ClinicalGuideline], CacheMetadata]:
        cache_key = f"guidelines:{query}:{organization}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [ClinicalGuideline.from_dict(d) for d in cached_data], meta

        # Layer 1: Search with formal publication type filters
        pt_query = " OR ".join(GUIDELINE_PUBLICATION_TYPES)
        l1_query = f"({query}) AND ({pt_query})"
        articles_l1, meta_l1 = await self.pubmed.search_articles(l1_query, max_results=20)
        errored = meta_l1.error

        candidates: list[tuple[MedicalArticle, bool]] = [(a, True) for a in articles_l1]
        seen_pmids = {a.pmid for a in articles_l1 if a.pmid}

        # Layer 2: Semantic keyword fallback if Layer 1 returned few results
        if len(candidates) < LAYER_THRESHOLD:
            kw_terms = " OR ".join(f"{kw}[tiab]" for kw in GUIDELINE_KEYWORDS[:5])
            l2_query = f"({query}) AND ({kw_terms})"
            articles_l2, meta_l2 = await self.pubmed.search_articles(l2_query, max_results=20)
            errored = errored or meta_l2.error
            for a in articles_l2:
                if a.pmid and a.pmid not in seen_pmids:
                    seen_pmids.add(a.pmid)
                    candidates.append((a, False))

        # Filter by organization if specified
        if organization:
            aliases = resolve_organization_aliases(organization)

            filtered_candidates: list[tuple[MedicalArticle, bool]] = []
            for art, has_pub in candidates:
                extracted_org = extract_organization(art).lower()
                art_text = f"{extracted_org} {art.title.lower()} {art.abstract.lower()} {art.journal.lower()}"
                if any(alias in art_text for alias in aliases):
                    filtered_candidates.append((art, has_pub))
            candidates = filtered_candidates

        # Score and build guidelines
        guidelines: list[ClinicalGuideline] = []
        for art, has_pub in candidates:
            score = calculate_guideline_score(art, has_pub)
            if score.total >= MIN_SCORE_THRESHOLD:
                url = art.url or (f"https://pubmed.ncbi.nlm.nih.gov/{art.pmid}/" if art.pmid else "")
                desc = (art.abstract or "")[:300]
                guidelines.append(
                    ClinicalGuideline(
                        title=art.title,
                        organization=extract_organization(art),
                        year=art.year,
                        url=url,
                        description=desc,
                        pmid=art.pmid,
                        score=score.total,
                        score_details=score,
                    )
                )

        guidelines.sort(key=lambda g: g.score, reverse=True)

        if errored and not guidelines:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(
            cache_key,
            [g.to_dict() for g in guidelines],
            source="guidelines",
        )
        return guidelines, CacheMetadata(cached=False, cache_age=0, error=errored)
