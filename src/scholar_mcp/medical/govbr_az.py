"""Scraper for Brazilian MoH manuals published behind "Saúde de A a Z".

The A-Z index at ``/assuntos/saude-de-a-a-z`` hosts no PDFs: its disease
pages cross-link with ``resolveuid`` URLs and their ``publicacoes`` folders
are login-gated. The manuals themselves (Dengue clinical management,
Tuberculosis control, and the rest of the surveillance set) are published
under ``/centrais-de-conteudo/publicacoes/svsa/<topic>`` and
``/centrais-de-conteudo/publicacoes/guias-e-manuais/<year>``. This module
crawls those trees, and uses the A-Z index only as a vocabulary that maps
official abbreviations (``dtha``, ``dcj``, ``dda``) to disease names.
"""

from bs4 import BeautifulSoup

from scholar_mcp.medical.govbr_common import normalize_text, parse_folder_index

GOVBR_ROOT = "https://www.gov.br"
AZ_INDEX_PATH = "/saude/pt-br/assuntos/saude-de-a-a-z"
AZ_INDEX_URL = f"{GOVBR_ROOT}{AZ_INDEX_PATH}"


def parse_az_index(html: str) -> list[str]:
    """Return the letter page URLs of the A-Z index."""
    return parse_folder_index(html, AZ_INDEX_URL, AZ_INDEX_PATH)


def parse_az_letter_page(html: str) -> dict[str, str]:
    """Map disease slug to display title for one A-Z letter page."""
    soup = BeautifulSoup(html, "html.parser")
    entries: dict[str, str] = {}
    for anchor in soup.find_all("a", class_="govbr-card-content", href=True):
        title_node = anchor.find("span", class_="titulo")
        title = (
            title_node.get_text(strip=True)
            if title_node
            else anchor.get_text(strip=True)
        )
        slug = anchor["href"].strip().split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if not title or not slug or slug == AZ_INDEX_PATH.rsplit("/", 1)[-1]:
            continue
        if len(slug) <= 1:
            continue
        entries[slug] = title
    return entries


def build_alias_text(title: str, aliases: dict[str, str]) -> str:
    """Return space-joined A-Z aliases that apply to ``title``.

    An alias applies when the disease name appears in the document title,
    so "Manual ... da Tuberculose" also scores for the query "tuberculose"
    and for abbreviations such as "dtha".
    """
    title_norm = normalize_text(title)
    if not title_norm:
        return ""
    matched: list[str] = []
    for slug, disease in aliases.items():
        disease_norm = normalize_text(disease)
        slug_norm = normalize_text(slug)
        if not disease_norm:
            continue
        if disease_norm in title_norm:
            if slug_norm not in matched and slug_norm not in title_norm:
                matched.append(slug_norm)
            elif slug_norm == disease_norm and slug_norm not in matched:
                matched.append(slug_norm)
        elif slug_norm in title_norm:
            if disease_norm not in matched and disease_norm not in title_norm:
                matched.append(disease_norm)
    return " ".join(matched)
