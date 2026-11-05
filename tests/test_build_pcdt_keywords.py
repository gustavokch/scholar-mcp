"""The PCDT keyword builder turns section headings into search keywords."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_pcdt_keywords.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("build_pcdt_keywords", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_headings_takes_numbered_all_caps_lines(script):
    text = (
        "1. INTRODUÇÃO\n"
        "Os Protocolos Clínicos são documentos.\n"
        "5. SÍFILIS ADQUIRIDA\n"
        "5.1 Sífilis em gestantes\n"
        "10.6. DOENÇA INFLAMATÓRIA PÉLVICA (DIP)\n"
        "Em 2021, 3 casos.\n"
    )
    assert script.extract_headings(text) == [
        "INTRODUÇÃO", "SÍFILIS ADQUIRIDA", "DOENÇA INFLAMATÓRIA PÉLVICA (DIP)",
    ]


def test_build_keywords_drops_common_and_title_words(script):
    # 20 protocols: "introducao" and "tratamento" sit in every one, so they
    # exceed the 10% document-frequency ceiling (2 of 20).
    headings = {f"pcdt-{i}": ["INTRODUÇÃO", "TRATAMENTO"] for i in range(18)}
    headings["pcdt-ist"] = ["INTRODUÇÃO", "TRATAMENTO", "SÍFILIS ADQUIRIDA", "INFECÇÃO PELO HPV"]
    headings["pcdt-celiaca"] = ["INTRODUÇÃO", "TRATAMENTO", "DOENÇA CELÍACA"]
    titles = {"pcdt-ist": "Atenção Integral às Pessoas com IST",
              "pcdt-celiaca": "Doença Celíaca"}
    kw = script.build_keywords(headings, titles)
    ist = kw["pcdt-ist"].split()
    assert ist[0] == "pcdt"
    assert {"sifilis", "adquirida", "infeccao", "hpv"} <= set(ist)
    assert "introducao" not in ist and "tratamento" not in ist
    # Title words add nothing: the scorer already reads the title.
    assert kw["pcdt-celiaca"] == "pcdt"
