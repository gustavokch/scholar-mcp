import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SCIHUB_MIRRORS = [
    "https://sci-hub.hkvisa.net",
    "https://sci-hub.mksa.top",
    "https://sci-hub.ren",
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ee",
]


@dataclass
class Settings:
    pubmed_api_key: str | None = None
    pubmed_email: str | None = None
    pubmed_tool: str = "ScholarMCP"
    unpaywall_email: str | None = None
    s2_api_key: str | None = None
    openalex_email: str | None = None
    enable_openalex: bool = True
    enable_s2: bool = True
    enable_scihub: bool = True
    prefer_scihub_over_unpaywall: bool = False
    scihub_mirrors: list[str] = field(default_factory=lambda: list(DEFAULT_SCIHUB_MIRRORS))
    request_timeout: int = 30
    total_budget_seconds: int = 45
    max_concurrency: int = 5
    cache_size: int = 500
    cache_ttl_seconds: int = 3600
    max_chars: int = 50_000
    title_match_threshold: float = 80.0
    download_dir: Path = field(default_factory=lambda: Path("./downloads"))
    ranking_enabled: bool = True
    ranking_weight_relevance: float = 0.4
    ranking_weight_citations: float = 0.3
    ranking_weight_recency: float = 0.3
    ranking_recency_half_life_years: float = 7.0
    ranking_candidate_multiplier: int = 3
    ranking_min_candidates: int = 20
    ranking_max_candidates: int = 50
    ranking_enrichment_timeout: float = 1.5

    @property
    def ncbi_rate_limit(self) -> float:
        """NCBI E-utilities requests per second: 10 with an API key, 3 without."""
        return 10.0 if self.pubmed_api_key else 3.0

    def scihub_tier_enabled(self) -> bool:
        """ENABLE_SCIHUB is the master switch; the preference flag cannot override it."""
        return self.enable_scihub

    def unpaywall_configured(self) -> bool:
        return bool(self.unpaywall_email)

    @classmethod
    def load(cls) -> "Settings":
        def _bool(val: str | None, default: bool) -> bool:
            if val is None:
                return default
            return val.strip().lower() in ("1", "true", "yes", "on")

        mirrors_env = os.getenv("SCIHUB_MIRRORS")
        mirrors = (
            [m.strip() for m in mirrors_env.split(",") if m.strip()]
            if mirrors_env
            else list(DEFAULT_SCIHUB_MIRRORS)
        )

        return cls(
            pubmed_api_key=os.getenv("PUBMED_API_KEY"),
            pubmed_email=os.getenv("PUBMED_EMAIL"),
            pubmed_tool=os.getenv("PUBMED_TOOL", "ScholarMCP"),
            unpaywall_email=os.getenv("UNPAYWALL_EMAIL") or os.getenv("PUBMED_EMAIL"),
            s2_api_key=os.getenv("S2_API_KEY"),
            openalex_email=os.getenv("OPENALEX_MAILTO")
            or os.getenv("UNPAYWALL_EMAIL")
            or os.getenv("PUBMED_EMAIL"),
            enable_openalex=_bool(os.getenv("ENABLE_OPENALEX"), True),
            enable_s2=_bool(os.getenv("ENABLE_S2"), True),
            enable_scihub=_bool(os.getenv("ENABLE_SCIHUB"), True),
            prefer_scihub_over_unpaywall=_bool(
                os.getenv("PREFER_SCIHUB_OVER_UNPAYWALL"), False
            ),
            scihub_mirrors=mirrors,
            request_timeout=int(os.getenv("SCHOLAR_REQUEST_TIMEOUT", "30")),
            total_budget_seconds=int(os.getenv("SCHOLAR_TOTAL_BUDGET", "45")),
            max_concurrency=int(os.getenv("SCHOLAR_MAX_CONCURRENCY", "5")),
            cache_size=int(os.getenv("SCHOLAR_CACHE_SIZE", "500")),
            cache_ttl_seconds=int(os.getenv("SCHOLAR_CACHE_TTL", "3600")),
            max_chars=int(os.getenv("SCHOLAR_MAX_CHARS", "50000")),
            title_match_threshold=float(os.getenv("SCHOLAR_TITLE_MATCH_THRESHOLD", "80")),
            download_dir=Path(os.getenv("SCHOLAR_DOWNLOAD_DIR", "./downloads")),
            ranking_enabled=_bool(os.getenv("RANKING_ENABLED"), True),
            ranking_weight_relevance=float(os.getenv("RANKING_WEIGHT_RELEVANCE", "0.4")),
            ranking_weight_citations=float(os.getenv("RANKING_WEIGHT_CITATIONS", "0.3")),
            ranking_weight_recency=float(os.getenv("RANKING_WEIGHT_RECENCY", "0.3")),
            ranking_recency_half_life_years=float(
                os.getenv("RANKING_RECENCY_HALF_LIFE_YEARS", "7.0")
            ),
            ranking_candidate_multiplier=int(os.getenv("RANKING_CANDIDATE_MULTIPLIER", "3")),
            ranking_min_candidates=int(os.getenv("RANKING_MIN_CANDIDATES", "20")),
            ranking_max_candidates=int(os.getenv("RANKING_MAX_CANDIDATES", "50")),
            ranking_enrichment_timeout=float(os.getenv("RANKING_ENRICHMENT_TIMEOUT", "1.5")),
        )
