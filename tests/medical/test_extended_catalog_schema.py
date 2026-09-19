"""Schema and content checks for the extended MoH corpus (Task 2)."""

import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "src" / "scholar_mcp" / "data"
CATALOG_PATH = DATA_DIR / "brazil_moh_extended_catalog.json"

EXPECTED_IDS = [
    "ms-manual-tuberculose-2019",
    "ms-pnab-portaria-2436-2017",
    "ms-portaria-aps-cofinanciamento-2024",
    "sbait-trauma-pelvico-2020",
]

REQUIRED_PHRASES = {
    "ms-manual-tuberculose-2019": ["porta de entrada"],
    "ms-pnab-portaria-2436-2017": ["2.436"],
    "ms-portaria-aps-cofinanciamento-2024": ["3.493", "cofinanciamento"],
    "sbait-trauma-pelvico-2020": ["fixador externo"],
}

TITLE_TOKENS = {
    "ms-manual-tuberculose-2019": ["tuberculose"],
    "ms-pnab-portaria-2436-2017": ["pnab"],
    "ms-portaria-aps-cofinanciamento-2024": ["aps", "cofinanciamento"],
    "sbait-trauma-pelvico-2020": ["trauma", "pelvico"],
}


def _load_catalog():
    assert CATALOG_PATH.exists(), f"extended catalog missing at {CATALOG_PATH}"
    with CATALOG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def test_catalog_has_expected_ids_and_schema():
    catalog = _load_catalog()
    for record_id in EXPECTED_IDS:
        assert record_id in catalog, f"{record_id} missing from extended catalog"
        row = catalog[record_id]
        for field in ("title", "description", "file_path", "keywords", "source_url"):
            assert row.get(field), f"{record_id} missing {field}"
        file_path = row["file_path"]
        assert not file_path.startswith("/"), f"{record_id} file_path must be relative"
        assert ".." not in Path(file_path).parts, f"{record_id} file_path escapes data dir"


def test_corpus_files_exist_and_meet_lengths():
    catalog = _load_catalog()
    for record_id in EXPECTED_IDS:
        rel = catalog[record_id]["file_path"]
        full = DATA_DIR / rel
        assert full.exists(), f"{record_id} file missing: {full}"
        text = full.read_text(encoding="utf-8")
        minimum = 10000 if record_id == "ms-manual-tuberculose-2019" else 1000
        assert len(text) > minimum, f"{record_id} too short ({len(text)} chars)"


def test_corpus_files_contain_required_phrases():
    catalog = _load_catalog()
    for record_id, phrases in REQUIRED_PHRASES.items():
        text = (DATA_DIR / catalog[record_id]["file_path"]).read_text(encoding="utf-8")
        lowered = text.lower()
        assert any(p in lowered for p in phrases), (
            f"{record_id} lacks required phrase {phrases}"
        )


def test_corpus_files_carry_provenance_header():
    catalog = _load_catalog()
    for record_id in EXPECTED_IDS:
        text = (DATA_DIR / catalog[record_id]["file_path"]).read_text(encoding="utf-8")
        for marker in ("Fonte:", "URL:", "Extraído em:", "Licença:"):
            assert marker in text, f"{record_id} missing provenance marker {marker}"


def test_catalog_declares_license():
    catalog = _load_catalog()
    for record_id in EXPECTED_IDS:
        row = catalog[record_id]
        license_text = row.get("license", "")
        assert license_text, f"{record_id} missing license declaration"
        assert license_text == license_text.strip(), (
            f"{record_id} license has stray whitespace"
        )


def _fold(text: str) -> str:
    import unicodedata

    return (
        unicodedata.normalize("NFKD", text)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )


def test_titles_carry_match_tokens():
    # Folded comparison: Task 3 scoring normalizes accents, so "pélvico"
    # matches the "pelvico" query token.
    catalog = _load_catalog()
    for record_id, tokens in TITLE_TOKENS.items():
        title = _fold(catalog[record_id]["title"])
        if record_id == "ms-portaria-aps-cofinanciamento-2024":
            assert any(t in title for t in tokens), f"{record_id} title lacks {tokens}"
        else:
            for token in tokens:
                assert token in title, f"{record_id} title lacks {token}"
