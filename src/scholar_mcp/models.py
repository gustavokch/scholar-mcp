from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class IdentifierMap:
    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    arxiv: str | None = None
    title: str | None = None
    match_score: float | None = None  # set when resolved from a title query
    ambiguous: bool = False  # True when the best title match is below threshold

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FetchAttempt:
    """One waterfall tier outcome, for debuggability."""

    tier: str  # "pmc" | "europepmc" | "unpaywall" | "scihub" | "abstract_fallback"
    outcome: str  # "hit" | "miss" | "skipped" | "error" | "timeout"
    reason: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaperMetadata:
    title: str
    authors: list[str] = field(default_factory=list)
    year: str = ""
    venue: str = ""
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    abstract: str = ""
    oa_status: str = "unknown"  # "oa" | "closed" | "unknown"
    citation_count: int | None = None
    oa_url: str | None = None
    institutions: list[str] = field(default_factory=list)
    issn: str | None = None
    study_type: str | None = None
    evidence_grade: str | None = None
    last_author_h_index: int | None = None
    score: float | None = None
    ranking_metrics: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FullTextResponse:
    status: str  # "full_text" | "abstract_only" | "ambiguous_match" | "not_found" | "error"
    source: str  # "pmc" | "europepmc" | "unpaywall" | "scihub" | "abstract_fallback" | "none"
    format: str = "markdown"  # "markdown" | "text"
    title: str = ""
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    content: str = ""
    url: str | None = None
    truncated: bool = False
    total_chars: int = 0
    sections_available: list[str] = field(default_factory=list)
    attempts: list[FetchAttempt] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FullTextSummary:
    identifier: str
    status: str
    source: str
    title: str = ""
    excerpt: str = ""
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DownloadResult:
    success: bool
    saved_path: str
    source_used: str
    file_size_bytes: int = 0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReferenceItem:
    id: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    venue: str = ""
    doi: str | None = None
    pmid: str | None = None
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CitationItem:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    venue: str = ""
    doi: str | None = None
    pmid: str | None = None
    citation_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RelatedPaper:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    venue: str = ""
    doi: str | None = None
    pmid: str | None = None
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

