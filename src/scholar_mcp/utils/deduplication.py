import html
import re
from typing import Any


def normalize_title(title: str | None) -> str:
    if not title:
        return ""

    decoded = html.unescape(title)

    # Remove bracketed version tags first
    decoded = re.sub(r"\[preprint\]", " ", decoded, flags=re.IGNORECASE)
    decoded = re.sub(r"\[published\]", " ", decoded, flags=re.IGNORECASE)
    decoded = re.sub(r"\barxiv:\d{4}\.\d+\b", " ", decoded, flags=re.IGNORECASE)
    decoded = re.sub(r"arXiv:\d+\.\d+", " ", decoded, flags=re.IGNORECASE)
    decoded = re.sub(r"\b(version\s+\d+|v\d+)\b", " ", decoded, flags=re.IGNORECASE)
    decoded = re.sub(
        r"\b(preprint|published version|final version)\b", " ", decoded, flags=re.IGNORECASE
    )

    # Normalize punctuation and whitespace
    normalized = re.sub(r"[\[\]\-:.,;()/'\"`]", " ", decoded)
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _levenshtein(str1: str, str2: str) -> int:
    len1 = len(str1)
    len2 = len(str2)

    if len1 == 0:
        return len2
    if len2 == 0:
        return len1

    matrix = [[0] * (len2 + 1) for _ in range(len1 + 1)]

    for i in range(len1 + 1):
        matrix[i][0] = i
    for j in range(len2 + 1):
        matrix[0][j] = j

    for i in range(1, len1 + 1):
        for j in range(1, len2 + 1):
            cost = 0 if str1[i - 1] == str2[j - 1] else 1
            matrix[i][j] = min(
                matrix[i - 1][j] + 1,      # deletion
                matrix[i][j - 1] + 1,      # insertion
                matrix[i - 1][j - 1] + cost  # substitution
            )

    return matrix[len1][len2]


def calculate_similarity(str1: str, str2: str) -> float:
    if not str1 or not str2:
        return 0.0
    if str1 == str2:
        return 1.0

    len1 = len(str1)
    len2 = len(str2)
    distance = _levenshtein(str1, str2)
    max_len = max(len1, len2)
    return 1.0 - distance / max_len


def extract_first_author(authors: list[str] | str | None) -> str | None:
    if not authors:
        return None

    if isinstance(authors, (list, tuple)):
        if len(authors) == 0:
            return None
        author_str = str(authors[0])
    else:
        author_str = str(authors)

    trimmed = author_str.strip()
    if not trimmed:
        return None

    # Handle "Last, First" or "Last, Initial" format first (e.g. "Johnson, M. et al.", "Smith, J.")
    comma_match = re.match(r"^([^,]+),", trimmed)
    if comma_match:
        words = comma_match.group(1).strip().split()
        if words:
            return words[-1].lower()

    # Handle "Smith et al" without comma
    if "et al" in trimmed.lower():
        before_et_al = re.split(r"et\s+al", trimmed, flags=re.IGNORECASE)[0].strip()
        words = before_et_al.split()
        if words:
            return words[-1].lower()

    # Try words: if "J. Watson" -> watson, "Smith J" -> smith
    words = trimmed.split()
    if len(words) > 1 and re.match(r"^[A-Za-z]\.?$", words[0]):
        return words[1].strip(".,;:").lower()
    if words:
        return words[0].strip(".,;:").lower()

    return None


def extract_year(date_or_year: str | int | None) -> str | None:
    if date_or_year is None:
        return None

    s = str(date_or_year)
    match = re.search(r"\b(19|20)\d{2}\b", s)
    if match:
        year_str = match.group(0)
        year_num = int(year_str)
        if 1900 <= year_num <= 2100:
            return year_str
    return None


def _get_paper_year(paper: dict[str, Any]) -> str | None:
    if "year" in paper and paper["year"]:
        return extract_year(paper["year"])
    if "publication_date" in paper and paper["publication_date"]:
        return extract_year(paper["publication_date"])
    if "date" in paper and paper["date"]:
        return extract_year(paper["date"])
    return None


def are_duplicates(
    paper1: dict[str, Any],
    paper2: dict[str, Any],
    similarity_threshold: float = 0.9,
) -> bool:
    doi1 = str(paper1.get("doi") or "").strip().lower()
    doi2 = str(paper2.get("doi") or "").strip().lower()

    if doi1 and doi2 and doi1 == doi2:
        return True

    t1 = normalize_title(paper1.get("title", ""))
    t2 = normalize_title(paper2.get("title", ""))

    if not t1 or not t2:
        return False

    a1 = extract_first_author(paper1.get("authors"))
    a2 = extract_first_author(paper2.get("authors"))
    y1 = _get_paper_year(paper1)
    y2 = _get_paper_year(paper2)

    if t1 == t2:
        if a1 and a2:
            if a1 == a2:
                if y1 and y2:
                    return y1 == y2
                return True
            return False
        elif not a1 and not a2:
            if y1 and y2:
                return y1 == y2
            return True
        if y1 and y2:
            return y1 == y2
        return True

    similarity = calculateSimilarity(t1, t2)
    if similarity >= similarity_threshold:
        if a1 and a2:
            if a1 == a2:
                if y1 and y2:
                    return y1 == y2
                return True
        elif not a1 and not a2:
            if y1 and y2:
                return y1 == y2
            return similarity >= 0.95

    return False


calculateSimilarity = calculate_similarity


def _metadata_count(paper: dict[str, Any]) -> int:
    score = 0
    if paper.get("title"):
        score += 1
    if paper.get("authors"):
        score += 1
    if _get_paper_year(paper):
        score += 1
    if paper.get("abstract"):
        score += len(str(paper.get("abstract"))) // 10
    return score


def deduplicate_papers(
    papers: list[dict[str, Any]],
    similarity_threshold: float = 0.9,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not papers:
        return [], {"total_input": 0, "unique_count": 0, "duplicates_removed": 0}

    unique: list[dict[str, Any]] = []

    for paper in papers:
        is_dup = False
        for j, existing in enumerate(unique):
            if are_duplicates(paper, existing, similarity_threshold):
                is_dup = True
                curr_doi = bool(paper.get("doi"))
                exist_doi = bool(existing.get("doi"))

                curr_meta = _metadata_count(paper)
                exist_meta = _metadata_count(existing)

                if (curr_doi and not exist_doi) or (
                    curr_doi == exist_doi and curr_meta > exist_meta
                ):
                    unique[j] = paper
                break

        if not is_dup:
            unique.append(paper)

    stats = {
        "total_input": len(papers),
        "unique_count": len(unique),
        "duplicates_removed": len(papers) - len(unique),
    }
    return unique, stats
