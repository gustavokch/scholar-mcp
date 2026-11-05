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
        "5. ATENÇÃO À SAÚDE\n"
    )
    assert script.extract_headings(text) == [
        "INTRODUÇÃO", "SÍFILIS ADQUIRIDA", "DOENÇA INFLAMATÓRIA PÉLVICA (DIP)",
        "ATENÇÃO À SAÚDE",
    ]


def test_build_keywords_drops_common_and_title_words(script):
    # 20 protocols: "introducao" and "tratamento" sit in every one, so they
    # exceed the 10% document-frequency ceiling (2 of 20).
    headings = {f"pcdt-{i}": ["INTRODUÇÃO", "TRATAMENTO"] for i in range(18)}
    headings["pcdt-ist"] = [
        "INTRODUÇÃO", "TRATAMENTO", "SÍFILIS ADQUIRIDA", "INFECÇÃO PELO HPV", "HIV E TB", "PCDT",
    ]
    headings["pcdt-celiaca"] = ["INTRODUÇÃO", "TRATAMENTO", "DOENÇA CELÍACA"]
    titles = {"pcdt-ist": "Atenção Integral às Pessoas com IST",
              "pcdt-celiaca": "Doença Celíaca"}
    kw = script.build_keywords(headings, titles)
    ist = kw["pcdt-ist"].split()
    assert {"sifilis", "adquirida", "infeccao", "hpv", "hiv"} <= set(ist)
    assert "introducao" not in ist and "tratamento" not in ist
    # No "pcdt" token: the search strips it from queries instead.
    assert "pcdt" not in ist
    # Two-letter tokens are table noise; three-letter acronyms stay.
    assert "tb" not in ist
    # Title words and common words add nothing; a row with no word left is omitted.
    assert set(kw) == {"pcdt-ist"}


def test_build_keywords_reports_a_cut_row(script, monkeypatch, capsys):
    monkeypatch.setattr(script, "MAX_TOKENS", 2)
    kw = script.build_keywords({"pcdt-a": ["ALFA BETA GAMA DELTA"]}, {})
    assert kw["pcdt-a"].split() == ["alfa", "beta"]
    assert "pcdt-a: cut at 2 keywords, dropped 2" in capsys.readouterr().out


def test_build_keywords_drops_generic_words_and_keeps_acronyms(script):
    # 19 filler protocols keep the df ceiling (10%) above one.
    headings = {f"pcdt-{i}": ["INTRODUÇÃO"] for i in range(19)}
    headings["pcdt-ist"] = [
        "ESQUEMA DE TRATAMENTO", "PSYCHIATRIC DISORDERS IN HIV-EXPOSED UNINFECTED",
        "HIV/AIDS", "DOENÇA INFLAMATÓRIA PÉLVICA (DIP)", "PÓS-EXPOSIÇÃO",
        "CLASSIFICAÇÃO EST ATÍSTICA", "DM0 E DM1",
    ]
    ist = set(script.build_keywords(headings, {})["pcdt-ist"].split())
    # Acronyms stand alone in plain capitals; 4+ letter words pass on length.
    assert {"hiv", "aids", "dip", "doenca", "inflamatoria", "pelvica", "exposicao"} <= ist
    # Generic and English words, the folded "PÓS", a split-word fragment and
    # a digit mix are dropped.
    assert not ist & {"esquema", "tratamento", "disorders", "uninfected", "pos", "est", "dm0"}
