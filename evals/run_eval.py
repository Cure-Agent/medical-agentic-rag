"""ablation 실행 CLI — 같은 노드 코드에 preset만 바꿔 3개 구성을 돌린다.

    python -m evals.run_eval --dataset evals/dataset.jsonl --out results

결과는 results/<preset>.jsonl에 1문항 1행으로 남는다. 집계는 evals.metrics가 한다.
동시성 2인 이유: 리랭크 시뮬 실측에서 워커 8은 429 폭탄이었다 — 2 + 백오프가 안정.
"""

import argparse
import asyncio
import json
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.agent.graph import PRESETS, build_graph
from app.agent.nodes import make_default_nodes
from app.agent.state import initial_state
from app.config import get_settings
from app.llm.client import make_embeddings, make_llm
from app.retrieval.factory import make_retriever

CONCURRENCY = 2
# 리랭크 구성은 1로 내린다 — 호출 1회가 후보 57개(~11k 토큰)라 문항 2개를 동시에 돌리면
# 분해 구성에서 질의 3개 × 2문항 = 6개의 대형 호출이 동시에 떠 TPM을 넘긴다(429 실측 10/36).
RERANK_CONCURRENCY = 1


def load_dataset(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for row in rows:
        assert {"id", "category", "question"} <= row.keys(), f"필드 누락: {row}"
    return rows


async def run_one(agent, item: dict, preset: str, policy_version: str, sem: asyncio.Semaphore):
    async with sem:
        try:
            final = await agent.ainvoke(initial_state(item["question"]))
            result = final["result"]
            return {
                "preset": preset,
                "policy_version": policy_version,
                "id": item["id"],
                "category": item["category"],
                "question": item["question"],
                "kind": result.kind if result else "error",
                "answer_text": result.text if result else "",
                "citations": [c.chunk_id for c in result.citations] if result else [],
                "missing_aspects": result.missing_aspects if result else [],
                "retrieval_count": final["retrieval_count"],
                "searched_queries": final["searched_queries"],
                "pool_size": len(final["evidence"]),
                "rerank_scores": final["rerank_scores"],
                "trace": final["trace"],
            }
        except Exception as error:  # 한 문항의 실패가 전체 실행을 죽이면 안 된다
            return {
                "preset": preset,
                "id": item["id"],
                "category": item["category"],
                "kind": "error",
                "error": repr(error),
            }


async def run_preset(preset: str, dataset: list[dict], out_dir: Path) -> None:
    settings = get_settings()
    cfg = PRESETS[preset]
    pool = AsyncConnectionPool(settings.database_url, open=False, kwargs={"row_factory": dict_row})
    await pool.open()
    try:
        llm = make_llm(settings)
        retriever = make_retriever(cfg, pool, make_embeddings(settings), settings)
        agent = build_graph(cfg, make_default_nodes(cfg, llm, retriever, settings))
        # 검색 정책은 전부 cfg에서 넘긴다 — settings 폴백을 쓰던 동안 rerank(3.5)와
        # rerank_cut9(9.0)의 policy_version이 둘 다 rerank9.0으로 찍혀 결과 파일로 두 구성을
        # 구분할 수 없었다
        policy = settings.policy_version_for(
            top_k=cfg.top_k,
            rerank=cfg.enable_rerank,
            distance_cutoff=cfg.distance_cutoff,
            rerank_cutoff=cfg.rerank_score_cutoff if cfg.enable_rerank else None,
            fuse_before_rerank=cfg.fuse_before_rerank,
        )

        sem = asyncio.Semaphore(RERANK_CONCURRENCY if cfg.enable_rerank else CONCURRENCY)
        rows = await asyncio.gather(
            *(run_one(agent, item, preset, policy, sem) for item in dataset)
        )

        out_path = out_dir / f"{preset}.jsonl"
        with out_path.open("w") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        errors = sum(1 for r in rows if r["kind"] == "error")
        print(f"[{preset}] {len(rows)}문항 완료 (오류 {errors}) → {out_path}")
    finally:
        await pool.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("evals/dataset.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("results"))
    parser.add_argument(
        "--presets", default="baseline,decomposition,full",
        help=f"쉼표 구분. 표의 행과 1:1 대응한다. 사용 가능: {','.join(PRESETS)}",
    )
    # 반복 실행은 complex만 돌린다 — simple(0.92)·insufficient(1.00)은 포화라 더 돌려도
    # 정보가 안 늘고 비용만 3배가 된다. 데이터셋 파일을 쪼개지 않는 이유는 gold key point가
    # 두 파일에 살면서 갈라지는 걸 막기 위해서다.
    parser.add_argument(
        "--categories", default="",
        help="쉼표 구분(simple,complex,insufficient). 비우면 전체.",
    )
    args = parser.parse_args()

    dataset = load_dataset(args.dataset)
    if args.categories:
        wanted = {c.strip() for c in args.categories.split(",") if c.strip()}
        dataset = [row for row in dataset if row["category"] in wanted]
        assert dataset, f"카테고리 필터가 전부 걸러냈다: {args.categories}"
    args.out.mkdir(parents=True, exist_ok=True)
    for preset in args.presets.split(","):
        preset = preset.strip()
        assert preset in PRESETS, f"unknown preset: {preset}"
        await run_preset(preset, dataset, args.out)


if __name__ == "__main__":
    asyncio.run(main())
