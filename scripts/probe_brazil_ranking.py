#!/usr/bin/env python3
"""Count off-topic gov.br catalog rows in live brazil_guidelines top-5s.

zimqa's ENAMED 2025 misses run (docs/handoff_enamed_2025_misses_run_analysis.md
§3.4) recorded catalog rows topping unrelated queries, e.g.
pcdt-acidentes-ofidicos on three of them. This replays that run's
brazil_guidelines queries against live BVS with a fresh cache, so no row
cached under an older ranker is read, and prints each top 5 with its origin,
body flag and score.

Exit status: 0 no watch-listed row in any top 5; 1 at least one; 2 a query
hit a backend error (the merged path was not measured -- rerun).
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scholar_mcp.config import Settings  # noqa: E402
from scholar_mcp.medical.brazil_moh import BrazilMoHEngine  # noqa: E402
from scholar_mcp.utils.http import AsyncHttpClient  # noqa: E402
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager  # noqa: E402

TOP_N = 5

# The handoff's §3.4 table (as recorded, truncated at the ellipsis), then the
# run trace's own queries (zimqa eval/results/enamed-2025-misses-run/answers.jsonl).
QUERIES = [
    "citologia oncótica LSIL lesão intraepitelial",
    "retenção de placenta conduta 30 minutos",
    "dengue grupo B manejo hidratação parenteral",
    "tuberculose retomada tratamento abandono",
    "curvas de crescimento síndrome de Down",
    "curvas crescimento síndrome Down recém-nascido puericultura",
    "dengue grupo B manejo hidratação parenteral antígeno NS1 leito de observação",
    "dengue sinais de alerta grupo B manejo hidratação",
    "terceiro estágio trabalho de parto conduta placenta retida",
    "lesão intraepitelial de baixo grau conduta colposcopia",
]

# Record-id fragments of the off-topic top hits the handoff recorded.
WATCH = (
    "acidentes-ofidicos",
    "sindrome-mielodisplasica-de-baixo-risco",
    "disturbio-mineral-osseo-na-doenca-renal-cronica",
    "deficiencia-do-hormonio-de-crescimento-hipopituitarismo",
    "acidentes-por-animais-peconhentos",
)


async def main() -> int:
    settings = Settings.load()
    watched = 0
    errored = 0
    with tempfile.TemporaryDirectory() as tmp:
        http_client = AsyncHttpClient(settings)
        cache = SQLiteCacheManager(db_path=Path(tmp) / "cache.db", settings=settings)
        engine = BrazilMoHEngine(http_client=http_client, cache=cache, settings=settings)
        try:
            for query in QUERIES:
                records, meta = await engine.search_guidelines(query, limit=10, collection="all")
                errored += bool(meta.error)
                print(f"\n{query!r}  error={meta.error} error_kind={meta.error_kind or '-'}")
                for rank, r in enumerate(records[:TOP_N], 1):
                    hit = any(w in r.record_id for w in WATCH)
                    watched += hit
                    score = "-" if r.score is None else f"{r.score:.3f}"
                    origin = getattr(r, "origin", "") or "-"
                    print(
                        f"  {rank}. {r.record_id[:70]:<70} origin={origin:<13} "
                        f"body={r.has_full_text!s:<5} score={score}"
                        + ("   <-- watch-listed" if hit else "")
                    )
        finally:
            await cache.close()
            await http_client.aclose()
    print(f"\nwatch-listed rows in a top {TOP_N}: {watched}; errored queries: {errored}")
    if errored:
        return 2
    return 1 if watched else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
