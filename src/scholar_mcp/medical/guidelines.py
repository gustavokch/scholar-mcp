import re

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import ClinicalGuideline, GuidelineScore, MedicalArticle
from scholar_mcp.medical.pubmed import MedicalPubMedClient
from scholar_mcp.query_relax import MAX_RELAX_EXTRA_CALLS, relax_ladder
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

# Guideline bodies matched by name: (display name, full names, acronyms).
# Full names match case-insensitively; acronyms only in capitals and as whole
# tokens, so "who", "accuracy" and "Canada" never count. A token boundary
# admits a slash, so a joint "AHA/ACC/ADA" title names all three.
KNOWN_ORGANIZATIONS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("American Academy of Pediatrics",
     ("american academy of pediatrics", "american academy pediatric"), ("AAP",)),
    ("World Health Organization", ("world health organization",), ("WHO",)),
    ("Centers for Disease Control and Prevention",
     ("centers for disease control and prevention", "centers for disease control"), ("CDC",)),
    ("National Institutes of Health", ("national institutes of health",), ("NIH",)),
    ("American Heart Association", ("american heart association",), ("AHA",)),
    ("American College of Cardiology", ("american college of cardiology",), ("ACC",)),
    ("American Diabetes Association", ("american diabetes association",), ("ADA",)),
    ("American College of Physicians", ("american college of physicians",), ("ACP",)),
    ("American College of Chest Physicians", ("american college of chest physicians",), ()),
    ("European Society of Cardiology", ("european society of cardiology",), ("ESC",)),
    ("Infectious Diseases Society of America",
     ("infectious diseases society of america",), ("IDSA",)),
    ("American College of Obstetricians and Gynecologists",
     ("american college of obstetricians and gynecologists",), ("ACOG",)),
    ("US Preventive Services Task Force",
     ("us preventive services task force", "u.s. preventive services task force"), ("USPSTF",)),
    ("National Institute for Health and Care Excellence",
     ("national institute for health and care excellence",), ("NICE",)),
    ("Endocrine Society", ("endocrine society",), ()),
)


def _full_name_re(name: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)


def _acronym_re(acronym: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(acronym)}(?![A-Za-z0-9])")


_KNOWN_ORG_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (display, tuple(map(_full_name_re, names)) + tuple(map(_acronym_re, acronyms)))
    for display, names, acronyms in KNOWN_ORGANIZATIONS
)

# Bodies outside the table: a capitalised name, case-sensitive so
# "worldwide, the practices..." never starts one, and confined to name words
# so it cannot run across a clause. The "of ..." tail keeps "European Society
# of Cardiology" whole and stops before document words ("... Medicine
# Clinical Practice Guideline", "Consensus Statement").
_NAME_WORD = (
    r"(?!(?:Guidelines?|Guidance|Consensus|Statement|Recommendations?|Update|Report"
    r"|Position|Expert|Clinical\s+Practice)\b)[A-Z][\w'-]*"
)
_GENERIC_ORG_RE = re.compile(
    r"\b(?:American|European|National|International|World|Global|Canadian|British)"
    r"\s+(?:[A-Z][\w'-]*\s+){0,8}?"
    r"(?:Association|College|Society|Academy|Institute|Foundation|Organi[sz]ation"
    r"|Committee|Ministry|Federation|Council|Physicians)\b"
    rf"(?:\s+(?:of|on|for)(?:\s+(?:the\s+|and\s+)?{_NAME_WORD})+)?"
)

MIN_SCORE_THRESHOLD = 2.5
LAYER_THRESHOLD = 5


def _is_aap_journal(journal: str) -> bool:
    """True for the AAP's own journal family, false for lookalikes.

    The AAP publishes *Pediatrics* and *Pediatrics in Review*, and indexes carry
    variants like "Pediatrics (Evanston)". ``JAMA Pediatrics`` and the many
    other journals ending in "Pediatrics" are not AAP, so the match anchors at
    the start of the name rather than anywhere in it.
    """
    return journal.strip().lower().startswith("pediatrics")


def _organizations_in(text: str) -> list[str]:
    """Bodies named in ``text``, in order of first mention: table bodies by
    display name, then capitalised names in spans no table body touches
    ("ACC ... American College of Cardiology Solution Set Oversight
    Committee" names the ACC once). Overlapping table matches keep the
    longest, so "American College of Physicians" never counts inside
    "American College of Chest Physicians"."""
    # (start, end, priority, name); priority 0 = table body, 1 = generic.
    hits: list[tuple[int, int, int, str]] = []
    for display, patterns in _KNOWN_ORG_PATTERNS:
        for pat in patterns:
            hits.extend((m.start(), m.end(), 0, display) for m in pat.finditer(text))
    hits.extend(
        (m.start(), m.end(), 1, " ".join(m.group(0).split()))
        for m in _GENERIC_ORG_RE.finditer(text)
    )
    hits.sort(key=lambda h: (h[2], h[0] - h[1], h[0]))
    kept: list[tuple[int, int, int, str]] = []
    for hit in hits:
        if not any(hit[0] < k[1] and hit[1] > k[0] for k in kept):
            kept.append(hit)
    names: list[str] = []
    for *_, name in sorted(kept):
        if name not in names:
            names.append(name)
    return names


def extract_organization(article: MedicalArticle) -> str:
    """The issuing body(ies) named in the title, else in the abstract, joined
    with " / " for a joint guideline. Falls back to the journal, then
    "Unknown Organization"."""
    for text in (article.title, article.abstract):
        names = _organizations_in(text or "")
        if names:
            return " / ".join(names)
    return article.journal or "Unknown Organization"


def organization_matches(organization: str, text: str) -> bool:
    """True when ``text`` names ``organization``, with the extraction's rules:
    word-bounded full names, capital-only acronyms."""
    wanted = organization.strip()
    lowered = wanted.lower()
    for display, names, acronyms in KNOWN_ORGANIZATIONS:
        if lowered == display.lower() or lowered in names or lowered in (
            a.lower() for a in acronyms
        ):
            patterns = [_full_name_re(display), *map(_full_name_re, names),
                        *map(_acronym_re, acronyms)]
            return any(p.search(text) for p in patterns)
    if wanted.isupper():
        return bool(_acronym_re(wanted).search(text))
    return bool(_full_name_re(wanted).search(text))


_DOI_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/")
_REPORT_TAIL_RE = re.compile(r"\ba report of the\b.*$", re.IGNORECASE | re.DOTALL)


def guideline_key(title: str) -> str:
    """Core guideline title: lowercased, without the simultaneous-publication
    "A Report of the ..." tail or punctuation."""
    core = _REPORT_TAIL_RE.sub("", title.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", core)).strip()


def same_guideline(
    title_a: str, title_b: str, doi_a: str | None = None, doi_b: str | None = None
) -> bool:
    """One guideline published twice (JACC and Circulation): same DOI, same
    title key, or one key of at least 40 characters prefixing the other."""
    norm_a = _DOI_PREFIX_RE.sub("", (doi_a or "").strip().lower())
    norm_b = _DOI_PREFIX_RE.sub("", (doi_b or "").strip().lower())
    if norm_a and norm_a == norm_b:
        return True
    key_a, key_b = guideline_key(title_a), guideline_key(title_b)
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    shorter, longer = sorted((key_a, key_b), key=len)
    return len(shorter) >= 40 and longer.startswith(shorter)


def calculate_guideline_score(
    article: MedicalArticle,
    has_publication_type: bool,
    from_keyword_layer: bool = False,
) -> GuidelineScore:
    pub_score = 2.0 if has_publication_type else (1.0 if from_keyword_layer else 0.0)

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
        # v3: older rows hold DateCompleted years, substring-matched
        # organisations ("who" in running text) and journal-duplicate copies.
        cache_key = f"guidelines:v3:{query}:{organization}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [ClinicalGuideline.from_dict(d) for d in cached_data], meta

        # Layer 1: Search with formal publication type filters, relaxed down
        # the ladder while the query keeps over-constraining PubMed to too
        # few results. Results accumulate across ladder steps, so this path
        # walks the ladder itself (relax=False) instead of the client's
        # stop-at-first-hit walk; ``filters`` keeps the publication-type
        # clause on every step.
        # relaxed_query reports the first relaxed variant that contributed
        # hits, so the caller can see the ladder worked.
        pt_query = " OR ".join(GUIDELINE_PUBLICATION_TYPES)
        articles_l1: list[MedicalArticle] = []
        seen_pmids: set[str] = set()
        errored = False
        relaxed_query: str | None = None
        # Explicit step cap: this path walks the ladder itself, so the
        # client-side MAX_RELAX_EXTRA_CALLS slice does not protect it. The
        # slice keeps the full schedule available to callers with their own
        # budget instead of shrinking relax_ladder for everyone.
        for step_idx, q in enumerate(relax_ladder(query)[: 1 + MAX_RELAX_EXTRA_CALLS]):
            articles_step, meta_step = await self.pubmed.search_articles(
                q, max_results=20, relax=False, filters=pt_query
            )
            errored = errored or meta_step.error
            if meta_step.error:
                # A fetch failure is not a zero-hit: relaxing further would
                # mistake a transport error for an over-constrained query.
                break
            added = 0
            for a in articles_step:
                pmid = a.pmid or ""
                if pmid and pmid in seen_pmids:
                    continue
                if pmid:
                    seen_pmids.add(pmid)
                articles_l1.append(a)
                added += 1
            if step_idx > 0 and added and relaxed_query is None:
                relaxed_query = q
            if len(articles_l1) >= LAYER_THRESHOLD:
                break

        candidates: list[tuple[MedicalArticle, bool, bool]] = [
            (a, True, False) for a in articles_l1
        ]

        # Layer 2: Semantic keyword fallback if Layer 1 returned few results
        if len(candidates) < LAYER_THRESHOLD:
            kw_terms = " OR ".join(f"{kw}[tiab]" for kw in GUIDELINE_KEYWORDS[:5])
            # Same explicit step cap as Layer 1: this walk is also
            # self-driven, one NCBI request per step at 3 req/s.
            for step_idx, q in enumerate(relax_ladder(query)[: 1 + MAX_RELAX_EXTRA_CALLS]):
                articles_l2, meta_l2 = await self.pubmed.search_articles(
                    q, max_results=20, relax=False, filters=kw_terms
                )
                errored = errored or meta_l2.error
                if meta_l2.error:
                    break
                added = 0
                for a in articles_l2:
                    if a.pmid and a.pmid not in seen_pmids:
                        seen_pmids.add(a.pmid)
                        candidates.append((a, False, True))
                        added += 1
                if step_idx > 0 and added and relaxed_query is None:
                    relaxed_query = q
                # Layer 2 is the salvage fallback: unlike Layer 1 it stops at
                # the first step that contributes anything — each extra step
                # is another NCBI request at 3 req/s, and the scoring gate
                # filters weak candidates regardless of which step found them.
                if added:
                    break

        # Filter by organization if specified
        if organization:
            requested_aap = organization_matches(organization, "AAP")
            filtered_candidates: list[tuple[MedicalArticle, bool, bool]] = []
            for art, has_pub, from_kw in candidates:
                art_text = f"{extract_organization(art)}\n{art.title}\n{art.abstract}\n{art.journal}"
                if organization_matches(organization, art_text):
                    filtered_candidates.append((art, has_pub, from_kw))
                    continue
                # AAP policy statements carry collective authors ("COMMITTEE
                # ON INFECTIOUS DISEASES") and no "AAP" string anywhere, but
                # they publish in Pediatrics — the journal is the org signal.
                if requested_aap and _is_aap_journal(art.journal):
                    filtered_candidates.append((art, has_pub, from_kw))
            candidates = filtered_candidates

        # One guideline published in two journals is one candidate. The merge
        # runs after the organisation filter, so the kept copy is the first
        # (Layer 1, NCBI relevance order) copy that passed it.
        unique_candidates: list[tuple[MedicalArticle, bool, bool]] = []
        for cand in candidates:
            art = cand[0]
            if not any(
                same_guideline(art.title, kept.title, art.doi, kept.doi)
                for kept, _, _ in unique_candidates
            ):
                unique_candidates.append(cand)
        candidates = unique_candidates

        # Score and build guidelines
        guidelines: list[ClinicalGuideline] = []
        for art, has_pub, from_kw in candidates:
            score = calculate_guideline_score(
                art, has_pub, from_keyword_layer=from_kw
            )
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
        return (
            guidelines,
            CacheMetadata(cached=False, cache_age=0, error=errored, relaxed_query=relaxed_query),
        )
