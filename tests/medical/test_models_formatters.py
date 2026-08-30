from scholar_mcp.medical.formatters import (
    append_cache_info,
    format_drug_details,
    format_drug_search_results,
    format_guidelines,
    format_health_indicators,
    format_medical_articles,
    format_pediatric_guidelines,
    format_rxnorm_drugs,
)
from scholar_mcp.medical.models import (
    ClinicalGuideline,
    DrugLabel,
    GuidelineScore,
    MedicalArticle,
    OpenFDAData,
    PediatricGuideline,
    RxNormDrug,
    WHOIndicatorRecord,
)
from scholar_mcp.utils.sqlite_cache import CacheMetadata


def test_append_cache_info_no_emojis():
    fresh = append_cache_info("Result content", CacheMetadata(cached=False, cache_age=0))
    cached = append_cache_info("Result content", CacheMetadata(cached=True, cache_age=120))
    assert "[Fresh response]" in fresh
    assert "[Cached: 120s old]" in cached
    for out in (fresh, cached):
        assert not any(ch in out for ch in "🔄📦🚨⚠️")


def test_format_drug_search_results_no_safety_banner():
    drug = DrugLabel(
        openfda=OpenFDAData(
            brand_name=["Advil"], generic_name=["Ibuprofen"], manufacturer_name=["Pfizer"]
        ),
        effective_time="20230101",
        purpose=["Pain reliever"],
    )
    out = format_drug_search_results([drug], "advil", CacheMetadata(cached=False, cache_age=0))
    assert "Advil" in out["markdown"]
    assert "Ibuprofen" in out["markdown"]
    assert "[Fresh response]" in out["markdown"]
    assert "SAFETY" not in out["markdown"].upper()
    assert "🚨" not in out["markdown"]
    assert len(out["data"]) == 1
    assert out["data"][0]["openfda"]["brand_name"] == ["Advil"]


def test_drug_label_from_dict_roundtrip():
    drug = DrugLabel(openfda=OpenFDAData(brand_name=["Advil"]), purpose=["Pain reliever"])
    restored = DrugLabel.from_dict(drug.to_dict())
    assert restored.openfda.brand_name == ["Advil"]
    assert restored.purpose == ["Pain reliever"]


def test_format_drug_details():
    drug = DrugLabel(
        openfda=OpenFDAData(brand_name=["Advil"], generic_name=["Ibuprofen"]),
        purpose=["Pain reliever"],
        warnings=["Do not take on empty stomach"],
        dosage_and_administration=["Take 1 tablet every 4 hours"],
    )
    out = format_drug_details(drug, "0573-0164", CacheMetadata(cached=False, cache_age=0))
    assert "Advil" in out["markdown"]
    assert "Do not take on empty stomach" in out["markdown"]
    assert "Take 1 tablet every 4 hours" in out["markdown"]
    assert "[Fresh response]" in out["markdown"]
    assert out["data"]["openfda"]["brand_name"] == ["Advil"]

    # Test not found
    out_none = format_drug_details(None, "0000-0000", CacheMetadata(cached=False, cache_age=0))
    assert "No FDA drug label found" in out_none["markdown"]
    assert out_none["data"] is None


def test_format_rxnorm_drugs():
    drugs = [
        RxNormDrug(rxcui="161", name="Acetaminophen", synonyms=["APAP"], tty="IN")
    ]
    out = format_rxnorm_drugs(drugs, "acetaminophen", CacheMetadata(cached=False, cache_age=0))
    assert "Acetaminophen" in out["markdown"]
    assert "161" in out["markdown"]
    assert "APAP" in out["markdown"]
    assert len(out["data"]) == 1


def test_format_health_indicators():
    records = [
        WHOIndicatorRecord(
            indicator_code="WHOSIS_000001",
            indicator_name="Life expectancy",
            spatial_dim="USA",
            time_dim="2020",
            numeric_value=78.5,
            unit="years",
        )
    ]
    out = format_health_indicators(records, "life expectancy", CacheMetadata(cached=False, cache_age=0))
    assert "Life expectancy" in out["markdown"]
    assert "78.5" in out["markdown"]
    assert "USA" in out["markdown"]
    assert len(out["data"]) == 1


def test_format_guidelines():
    guidelines = [
        ClinicalGuideline(
            title="Hypertension Guideline",
            organization="AHA",
            year="2020",
            url="https://example.com",
            score=4.5,
            score_details=GuidelineScore(publication_type=2.0, total=4.5),
        )
    ]
    out = format_guidelines(guidelines, "hypertension", CacheMetadata(cached=False, cache_age=0))
    assert "Hypertension Guideline" in out["markdown"]
    assert "AHA" in out["markdown"]
    assert "4.5" in out["markdown"]
    assert len(out["data"]) == 1


def test_format_pediatric_guidelines():
    guidelines = [
        PediatricGuideline(
            title="Infant Nutrition",
            organization="AAP",
            url="https://example.com",
            source="bright-futures",
            year="2021",
            age_group="0-12m",
        )
    ]
    out = format_pediatric_guidelines(guidelines, "nutrition", CacheMetadata(cached=False, cache_age=0))
    assert "Infant Nutrition" in out["markdown"]
    assert "AAP" in out["markdown"]
    assert len(out["data"]) == 1


def test_format_medical_articles():
    articles = [
        MedicalArticle(
            title="Study on Asthma",
            authors=["Smith J"],
            journal="Pediatrics",
            year="2022",
            pmid="12345",
            source_database="PubMed",
        )
    ]
    out = format_medical_articles(articles, "asthma", CacheMetadata(cached=False, cache_age=0))
    assert "Study on Asthma" in out["markdown"]
    assert "Pediatrics" in out["markdown"]
    assert len(out["data"]) == 1


ERROR_META = CacheMetadata(cached=False, cache_age=0, error=True)
FRESH_META = CacheMetadata(cached=False, cache_age=0)


def test_append_cache_info_marks_fetch_error():
    out = append_cache_info("Result content", ERROR_META)
    assert "Fetch error" in out
    assert "[Fresh response]" not in out
    assert not any(ch in out for ch in "🔄📦🚨⚠️")


def test_format_medical_articles_does_not_claim_absence_on_error():
    out = format_medical_articles([], "asthma", ERROR_META)
    assert "No medical literature found" not in out["markdown"]
    assert "not evidence that no results exist" in out["markdown"]


def test_format_medical_articles_still_reports_absence_without_error():
    out = format_medical_articles([], "asthma", FRESH_META)
    assert "No medical literature found for: asthma" in out["markdown"]


def test_format_drug_search_results_does_not_claim_absence_on_error():
    out = format_drug_search_results([], "ibuprofen", ERROR_META)
    assert "No drug labels found" not in out["markdown"]
    assert "not evidence that no results exist" in out["markdown"]


def test_format_drug_details_does_not_claim_absence_on_error():
    out = format_drug_details(None, "0002-4462", ERROR_META)
    assert "No FDA drug label found" not in out["markdown"]
    assert "not evidence that no results exist" in out["markdown"]


def test_format_rxnorm_drugs_does_not_claim_absence_on_error():
    out = format_rxnorm_drugs([], "ibuprofen", ERROR_META)
    assert "No RxNorm drug concepts found" not in out["markdown"]
    assert "not evidence that no results exist" in out["markdown"]


def test_format_health_indicators_does_not_claim_absence_on_error():
    out = format_health_indicators([], "measles", ERROR_META)
    assert "No health statistics found" not in out["markdown"]
    assert "not evidence that no results exist" in out["markdown"]


def test_format_guidelines_does_not_claim_absence_on_error():
    out = format_guidelines([], "asthma", ERROR_META)
    assert "No clinical guidelines found" not in out["markdown"]
    assert "not evidence that no results exist" in out["markdown"]


def test_format_pediatric_guidelines_does_not_claim_absence_on_error():
    out = format_pediatric_guidelines([], "nutrition", ERROR_META)
    assert "No pediatric guidelines found" not in out["markdown"]
    assert "not evidence that no results exist" in out["markdown"]


def test_format_medical_articles_renders_nct_id():
    article = MedicalArticle(
        title="Asthma Trial",
        journal="ClinicalTrials.gov",
        nct_id="NCT01234567",
        url="https://clinicaltrials.gov/study/NCT01234567",
        source_database="ClinicalTrials.gov",
    )
    out = format_medical_articles([article], "asthma", FRESH_META)
    assert "- **NCT ID:** NCT01234567" in out["markdown"]
