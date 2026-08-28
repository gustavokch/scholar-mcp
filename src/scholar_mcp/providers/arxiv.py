from lxml import etree

from scholar_mcp.models import FullTextResponse, IdentifierMap, PaperMetadata
from scholar_mcp.parsers.pdf import pdf_bytes_to_text
from scholar_mcp.providers.base import BaseProvider, MIN_USEFUL_CHARS
from scholar_mcp.utils.http import AsyncHttpClient

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_PDF = "https://export.arxiv.org/pdf"
ARXIV_ABS = "https://arxiv.org/abs"

ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_NS = "http://arxiv.org/schemas/atom"


def _text(entry: etree._Element, tag: str, ns: str = ATOM_NS) -> str:
    el = entry.find(f"{{{ns}}}{tag}")
    return (el.text or "").strip() if el is not None and el.text else ""


class ArxivProvider(BaseProvider):
    """arXiv preprint provider: metadata via Atom API, full text via PDF."""

    tier: str = "arxiv"

    def __init__(self, http_client: AsyncHttpClient) -> None:
        super().__init__(http_client)

    async def fetch_full_text(self, ids: IdentifierMap) -> FullTextResponse | None:
        if not ids.arxiv:
            self.last_skip_reason = "NO_ARXIV_ID"
            return None

        try:
            pdf_bytes = await self.http_client.get_bytes(f"{ARXIV_PDF}/{ids.arxiv}")
            if not pdf_bytes:
                return None
            text = pdf_bytes_to_text(pdf_bytes)
            if len(text.strip()) < MIN_USEFUL_CHARS:
                return None
            return FullTextResponse(
                status="full_text",
                source="arxiv",
                format="text",
                content=text,
                total_chars=len(text),
                doi=ids.doi,
                pmid=ids.pmid,
                pmcid=ids.pmcid,
                url=f"{ARXIV_ABS}/{ids.arxiv}",
            )
        except Exception:
            return None

    async def fetch_metadata(self, arxiv_id: str) -> PaperMetadata | None:
        try:
            resp = await self.http_client.get(ARXIV_API, params={"id_list": arxiv_id})
            if resp is None or resp.status_code != 200:
                return None
            root = etree.fromstring(resp.content)
            entry = root.find(f"{{{ATOM_NS}}}entry")
            if entry is None:
                return None

            title = " ".join(_text(entry, "title").split())
            if not title:
                return None

            authors = [
                (a.findtext(f"{{{ATOM_NS}}}name") or "").strip()
                for a in entry.findall(f"{{{ATOM_NS}}}author")
            ]
            authors = [a for a in authors if a]

            published = _text(entry, "published")
            doi = _text(entry, "doi", ns=ARXIV_NS) or None

            return PaperMetadata(
                title=title,
                authors=authors,
                year=published[:4],
                venue=_text(entry, "journal_ref", ns=ARXIV_NS),
                doi=doi,
                abstract=" ".join(_text(entry, "summary").split()),
                oa_status="oa",
            )
        except Exception:
            return None
