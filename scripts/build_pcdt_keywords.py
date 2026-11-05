"""Build the PCDT keyword index from each protocol's section headings.

The PCDT catalog search matches titles, slugs, descriptions and keywords
(govbr_pcdt._score_item). Umbrella protocols name none of the conditions they
cover in the title: "Atenção Integral às Pessoas com Infecções Sexualmente
Transmissíveis" holds the syphilis chapter, so a search for the condition
misses it. This script reads every seed PCDT's full text, keeps the words of
its numbered all-caps section headings, drops words found in the headings of
more than MAX_DF of the protocols, already in the title, in STOPWORDS or
shorter than MIN_TOKEN_LEN (acronyms excepted), and writes
src/scholar_mcp/data/govbr_pcdt_keywords.json ({record_id: "kw kw ..."}; protocols
with no surviving word get no row).

Offline use only (network: one PDF fetch per protocol). Refuses to write when
more than MAX_FAILED_SHARE of the fetches fail.

Usage:
    uv run python scripts/build_pcdt_keywords.py [--output PATH]
"""

import argparse
import asyncio
import json
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from scholar_mcp.config import Settings
from scholar_mcp.medical.brazil_moh import MAX_FULL_TEXT_CHARS, BrazilMoHEngine
from scholar_mcp.medical.govbr_common import tokenize_portuguese
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

DATA_DIR = REPO_ROOT / "src" / "scholar_mcp" / "data"
SEED_PATH = DATA_DIR / "govbr_pcdt_catalog.json"
DEFAULT_OUTPUT = DATA_DIR / "govbr_pcdt_keywords.json"
MAX_DF = 0.10           # drop words in the headings of more than 10% of protocols
MAX_TOKENS = 60         # rarest words first, so a long protocol cannot match everything
MIN_TOKEN_LEN = 4       # shorter tokens are split-word fragments ("ape", "pes") ...
MIN_ACRONYM_LEN = 3     # ... unless an acronym (see _is_acronym); 2 letters never ("tb", "ni")
MAX_FAILED_SHARE = 0.2
CONCURRENCY = 4

# Words that name a document part or a care process, not a condition, and
# heading noise. Each kind matched unrelated queries from the 2026-10-30
# traces: "esquema" put colangite and distonias in an HIV search, and
# "atendimento" / "conduta" took 55 keyword-only pool slots over 334 queries.
STOPWORDS = frozenset({
    # English words from bilingual PDFs
    "the", "and", "of", "in", "for", "with", "not", "non", "disorders", "uninfected",
    # care-process words
    "esquema", "esquemas", "historico", "alteracoes", "farmaco", "pos",
    "acompanhamento", "paciente", "pacientes", "medicamento", "medicamentos",
    "medicamentoso", "tratamento", "diagnostico", "monitoramento", "atendimento",
    "conduta", "consulta", "equipe", "especialistas", "terapia", "terapeutica",
    "terapeuticas", "clinico", "clinicas", "exames", "abordagem", "fluxo",
    # population words
    "pessoa", "pessoas", "crianca", "criancas", "adolescentes", "adultos",
    # document-part and method words
    "escopo", "elaboracao", "material", "suplementar", "diretrizes",
    "recomendacoes", "evidencia", "evidencias", "referencia", "reuniao",
    "informacoes", "apresentacao", "finalidade", "buscas", "embase", "lilacs",
    "analise", "resultados", "pesquisa", "metodologicas", "anexos", "outros",
    "outras", "ecr", "erc", "bvs", "sus",
    # split-word fragments and table cells that pass the acronym rule
    "est", "tos", "doe", "ane", "mas", "sim", "uso", "iii",
})

# A numbered, all-caps section heading: "5. SÍFILIS ADQUIRIDA", "10.6. DIP".
_HEADING = re.compile(
    r"^\s*\d{1,2}(?:\.\d{1,2}){0,2}\.?\s+"
    r"([A-ZÁÀÉÍÓÚÂÊÔÃÕÇ][A-ZÁÀÉÍÓÚÂÊÔÃÕÇ0-9 ,:/()\-–]{2,90})\s*$",
    re.MULTILINE,
)


def extract_headings(text: str) -> list[str]:
    """Numbered all-caps heading lines, in document order."""
    return [h.strip() for h in _HEADING.findall(text or "")]


def _is_acronym(token: str, headings: list[str]) -> bool:
    """A short token stands alone in plain capitals in one of the headings.

    "HIV", "(DIP)" and "HIV/AIDS" qualify; "PÓS" (the folded token is "pos")
    and digit mixes ("DM0") do not.
    """
    if len(token) < MIN_ACRONYM_LEN or not token.isalpha():
        return False
    word = re.compile(rf"(?<!\w){token.upper()}(?!\w)")
    return any(word.search(h) for h in headings)


def build_keywords(headings: dict[str, list[str]], titles: dict[str, str]) -> dict[str, str]:
    """``{record_id: "kw ..."}``; protocols with no surviving word are omitted.

    No "pcdt" token: every row is one, and the search engine strips the word
    from queries (a token on each row would tie them all against rows without it).
    """
    tokens = {rid: set(tokenize_portuguese(" ".join(hs))) for rid, hs in headings.items()}
    df = Counter(t for toks in tokens.values() for t in toks)
    limit = max(1, int(MAX_DF * len(tokens)))
    out: dict[str, str] = {}
    for rid, toks in tokens.items():
        title = set(tokenize_portuguese(titles.get(rid, "")))
        ranked = sorted(
            (
                t for t in toks
                if (len(t) >= MIN_TOKEN_LEN or _is_acronym(t, headings[rid]))
                and t != "pcdt" and t not in STOPWORDS and df[t] <= limit
                and t not in title and not t.isdigit()
            ),
            key=lambda t: (df[t], t),
        )
        if len(ranked) > MAX_TOKENS:
            print(f"{rid}: cut at {MAX_TOKENS} keywords, dropped {len(ranked) - MAX_TOKENS}", flush=True)
        if ranked:
            out[rid] = " ".join(ranked[:MAX_TOKENS])
    return out


async def fetch_headings(seed: dict) -> tuple[dict[str, list[str]], list[str]]:
    settings = Settings()
    headings: dict[str, list[str]] = {}
    failed: list[str] = []
    sem = asyncio.Semaphore(CONCURRENCY)
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = SQLiteCacheManager(Path(tmpdir) / "keywords.db", settings=settings)
        engine = BrazilMoHEngine(AsyncHttpClient(settings), cache, settings)

        async def one(rid: str) -> None:
            try:
                async with sem:
                    payload, _ = await engine.get_full_text(rid, max_chars=MAX_FULL_TEXT_CHARS)
            except Exception as exc:  # noqa: BLE001 -- one bad protocol must not lose the run
                print(f"{rid}: fetch raised {type(exc).__name__}: {exc}", flush=True)
                failed.append(rid)
                return
            if payload.get("status") != "success" or payload.get("abstract_fallback"):
                failed.append(rid)
                return
            headings[rid] = extract_headings(payload.get("content", ""))
            print(f"{rid}: {len(headings[rid])} headings", flush=True)

        await asyncio.gather(*(one(rid) for rid in sorted(seed)))
    return headings, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args()
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    headings, failed = asyncio.run(fetch_headings(seed))
    if len(failed) > MAX_FAILED_SHARE * len(seed):
        print(f"refusing to write: {len(failed)}/{len(seed)} fetches failed: {failed}")
        return 1
    keywords = build_keywords(headings, {rid: row.get("title", "") for rid, row in seed.items()})
    args.output.write_text(
        json.dumps(keywords, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(keywords)} rows to {args.output}; {len(failed)} failed: {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
