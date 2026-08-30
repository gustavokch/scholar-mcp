from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class OpenFDAData:
    brand_name: list[str] = field(default_factory=list)
    generic_name: list[str] = field(default_factory=list)
    manufacturer_name: list[str] = field(default_factory=list)
    product_ndc: list[str] = field(default_factory=list)
    substance_name: list[str] = field(default_factory=list)
    route: list[str] = field(default_factory=list)
    dosage_form: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "OpenFDAData":
        if not data or not isinstance(data, dict):
            return cls()
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**fields)


@dataclass
class DrugLabel:
    openfda: OpenFDAData = field(default_factory=OpenFDAData)
    effective_time: str = ""
    purpose: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    adverse_reactions: list[str] = field(default_factory=list)
    drug_interactions: list[str] = field(default_factory=list)
    dosage_and_administration: list[str] = field(default_factory=list)
    indications_and_usage: list[str] = field(default_factory=list)
    contraindications: list[str] = field(default_factory=list)
    use_in_specific_populations: list[str] = field(default_factory=list)
    clinical_pharmacology: list[str] = field(default_factory=list)
    pediatric_dosing: str | None = None
    pediatric_warnings: str | None = None
    raw_sections: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DrugLabel":
        if not data or not isinstance(data, dict):
            return cls()
        payload = dict(data)
        if "openfda" in payload:
            if isinstance(payload["openfda"], dict):
                payload["openfda"] = OpenFDAData.from_dict(payload["openfda"])
            elif not isinstance(payload["openfda"], OpenFDAData):
                payload["openfda"] = OpenFDAData()
        fields = {k: v for k, v in payload.items() if k in cls.__dataclass_fields__}
        return cls(**fields)


@dataclass
class RxNormDrug:
    rxcui: str
    name: str
    tty: str = ""
    language: str = "ENG"
    suppress: str = ""
    synonyms: list[str] = field(default_factory=list)
    umlscui: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RxNormDrug":
        if not data or not isinstance(data, dict):
            return cls(rxcui="", name="")
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**fields)


@dataclass
class WHOIndicatorRecord:
    indicator_code: str
    indicator_name: str
    spatial_dim: str  # Country code or "Global"
    spatial_dim_type: str = "Country"
    time_dim: str = ""  # Year
    time_dim_type: str = "Year"
    value: str = ""
    numeric_value: float | None = None
    low: float = 0.0
    high: float = 0.0
    unit: str = ""
    age_group: str = ""
    sex: str = ""
    comments: str = ""
    date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WHOIndicatorRecord":
        if not data or not isinstance(data, dict):
            return cls(indicator_code="", indicator_name="", spatial_dim="")
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**fields)


@dataclass
class GuidelineScore:
    publication_type: float = 0.0
    title_keywords: float = 0.0
    journal_reputation: float = 0.0
    author_affiliation: float = 0.0
    abstract_keywords: float = 0.0
    mesh_terms: float = 0.0  # Weight 0.5 reserved; not parsed
    total: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "GuidelineScore":
        if not data or not isinstance(data, dict):
            return cls()
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**fields)


@dataclass
class ClinicalGuideline:
    title: str
    organization: str
    year: str
    url: str
    description: str = ""
    category: str = "General"
    evidence_level: str = "Systematic Review/Consensus"
    pmid: str | None = None
    score: float = 0.0
    score_details: GuidelineScore | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ClinicalGuideline":
        if not data or not isinstance(data, dict):
            return cls(title="", organization="", year="", url="")
        payload = dict(data)
        if "score_details" in payload and isinstance(payload["score_details"], dict):
            payload["score_details"] = GuidelineScore.from_dict(payload["score_details"])
        fields = {k: v for k, v in payload.items() if k in cls.__dataclass_fields__}
        return cls(**fields)


@dataclass
class PediatricGuideline:
    title: str
    organization: str
    url: str
    source: str  # "bright-futures" | "aap-policy"
    year: str = ""
    description: str = ""
    age_group: str = ""
    category: str = ""
    screening_recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PediatricGuideline":
        if not data or not isinstance(data, dict):
            return cls(title="", organization="", url="", source="")
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**fields)


@dataclass
class MedicalArticle:
    title: str
    authors: list[str] = field(default_factory=list)
    journal: str = ""
    year: str = ""
    abstract: str = ""
    pmid: str | None = None
    pmc_id: str | None = None
    doi: str | None = None
    nct_id: str | None = None
    url: str = ""
    citations: str = ""
    full_text_available: bool = False
    full_text: str | None = None
    source_database: str = "PubMed"
    score: float | None = None  # set by rank_medical_articles

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MedicalArticle":
        if not data or not isinstance(data, dict):
            return cls(title="")
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**fields)
