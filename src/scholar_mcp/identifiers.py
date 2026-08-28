import re
import urllib.parse
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap
from scholar_mcp.utils.cache import TTLCache
from scholar_mcp.utils.http import AsyncHttpClient

IDCONV_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
CROSSREF_URL = "https://api.crossref.org/works"

PMCID_RE = re.compile(r"^(?:pmcid:\s*)?(PMC\d+)$", re.IGNORECASE)
PMID_RE = re.compile(r"^(?:pmid:\s*)?(\d+)$", re.IGNORECASE)
DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
DOI_RE = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Za-z0-9]+$")
ARXIV_NEW_RE = re.compile(r"^(?:arxiv:\s*)?(\d{4}\.\d{4,5})(v\d+)?$", re.IGNORECASE)
ARXIV_OLD_RE = re.compile(r"^(?:arxiv:\s*)?([a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?$", re.IGNORECASE)
ARXIV_URL_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf|html)/([a-z0-9.\-/]+?)(v\d+)?(?:\.pdf)?$", re.IGNORECASE
)
ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)


def clean_identifier(raw: str) -> tuple[str, str]:
    """Detect and clean identifier type (pmid, pmcid, doi, or title)."""
    s = raw.strip()

    # 1. PMCID check
    m_pmc = PMCID_RE.match(s)
    if m_pmc:
        # Standardize to uppercase PMC...
        pmcid_val = m_pmc.group(1).upper()
        return "pmcid", pmcid_val

    # 2. DOI check
    stripped_doi = DOI_PREFIX_RE.sub("", s).strip().rstrip(".,;")
    if DOI_RE.match(stripped_doi):
        return "doi", stripped_doi

    # 3. PMID check
    m_pmid = PMID_RE.match(s)
    if m_pmid:
        return "pmid", m_pmid.group(1)

    # 4. arXiv check (URL, new-style, old-style)
    # Drop any query string, fragment, and trailing slash so the URL pattern,
    # which is end-anchored, still sees the bare identifier.
    url_part = s.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    m_url = ARXIV_URL_RE.search(url_part)
    if m_url:
        return "arxiv", f"{m_url.group(1).strip()}{m_url.group(2) or ''}"
    for rx in (ARXIV_NEW_RE, ARXIV_OLD_RE):
        m_arxiv = rx.match(s)
        if m_arxiv:
            return "arxiv", f"{m_arxiv.group(1)}{m_arxiv.group(2) or ''}"

    # 5. Fallback to Title
    return "title", s


async def resolve_identifiers(
    identifier: str,
    client: AsyncHttpClient,
    cache: TTLCache,
    settings: Settings,
) -> IdentifierMap:
    """Resolve PMID, PMCID, and DOI across services, with title fuzzy-matching and caching."""
    id_type, cleaned = clean_identifier(identifier)
    cache_key = f"idmap:{cleaned.lower()}"

    cached = await cache.get(cache_key)
    if cached is not None and isinstance(cached, IdentifierMap):
        return cached

    id_map = IdentifierMap()
    if id_type == "pmid":
        id_map.pmid = cleaned
    elif id_type == "pmcid":
        id_map.pmcid = cleaned
    elif id_type == "doi":
        id_map.doi = cleaned
        m_arxiv_doi = ARXIV_DOI_RE.match(cleaned)
        if m_arxiv_doi:
            id_map.arxiv = m_arxiv_doi.group(1)
    elif id_type == "arxiv":
        id_map.arxiv = cleaned
        base = re.sub(r"v\d+$", "", cleaned)
        id_map.doi = f"10.48550/arXiv.{base}"
    elif id_type == "title":
        id_map.title = cleaned

    try:
        # Step 1: If title, resolve to DOI via CrossRef bibliographic search
        if id_type == "title":
            resp = await client.get(
                CROSSREF_URL,
                params={"query.bibliographic": cleaned, "rows": 1},
            )
            if resp is not None and resp.status_code == 200:
                data = resp.json()
                items = data.get("message", {}).get("items", [])
                if items:
                    top = items[0]
                    score = float(top.get("score", 0.0))
                    id_map.match_score = score
                    if score >= settings.title_match_threshold:
                        id_map.doi = top.get("DOI")
                        titles = top.get("title", [])
                        if titles and isinstance(titles, list):
                            id_map.title = titles[0]
                        id_map.ambiguous = False
                    else:
                        id_map.ambiguous = True
                        id_map.doi = None
                else:
                    id_map.ambiguous = True
            else:
                id_map.ambiguous = True

        # Step 2: Enrich with NCBI idconv if we have pmid, pmcid, or doi
        query_id = id_map.pmcid or id_map.doi or id_map.pmid
        if query_id and not id_map.ambiguous:
            resp = await client.get(
                IDCONV_URL,
                params={"ids": query_id, "format": "json"},
            )
            if resp is not None and resp.status_code == 200:
                data = resp.json()
                records = data.get("records", [])
                if records:
                    rec = records[0]
                    if rec.get("pmid"):
                        id_map.pmid = str(rec["pmid"])
                    if rec.get("pmcid"):
                        id_map.pmcid = str(rec["pmcid"])
                    if rec.get("doi"):
                        id_map.doi = str(rec["doi"])
    except Exception:
        # Never crash; return best-effort map
        pass

    # Cache under all known keys
    keys_to_cache = {cache_key}
    if id_map.doi:
        keys_to_cache.add(f"idmap:{id_map.doi.lower()}")
    if id_map.pmid:
        keys_to_cache.add(f"idmap:{id_map.pmid.lower()}")
    if id_map.pmcid:
        keys_to_cache.add(f"idmap:{id_map.pmcid.lower()}")
    if id_map.arxiv:
        keys_to_cache.add(f"idmap:{id_map.arxiv.lower()}")

    for k in keys_to_cache:
        await cache.set(k, id_map)

    return id_map
