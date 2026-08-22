"""LLM-as-judge — results/<preset>.jsonl을 채점해 <preset>.judged.jsonl을 만든다.

    python -m evals.judge --results results --dataset evals/dataset.jsonl

채점 축은 스모크 실주행에서 확정했다:

- **effective_kind**: kind=answer라도 본문이 실질적 거부문이면 abstain으로 센다.
  baseline이 거리 게이트(0.48)를 통과시킨 뒤 본문에서 "답변할 수 없습니다"라고
  거부하는 사례가 실측됐다 — kind만 보면 이 행동이 오답(누출)으로 잘못 집계된다.
- **key_points_covered**: answerable 문항의 key_point별 포함 여부. 표현이 달라도
  같은 사실이면 인정하되, key_point가 권고등급·근거수준을 명시하면 그 값까지
  일치해야 한다.

LLM 호출은 kind=answer 행에만 한다 — abstain은 결정론적으로 채점된다(비용·재현성).
동시성 2: run_eval과 같은 이유 (워커 8은 429 폭탄 실측).
"""

import argparse
import asyncio
import json
from pathlib import Path

from pydantic import BaseModel, Field

from app.config import get_settings
from app.llm.client import make_judge_llm

CONCURRENCY = 2

JUDGE_SYSTEM = """너는 의료 RAG 시스템의 답변을 채점하는 평가자다.
주어진 질문·답변·채점 기준(key points)을 보고 정확히 판정한다.

판정 규칙:
1. is_refusal — 답변 본문이 실질적으로 답변을 거부하는가?
   - "근거가 없다", "답변할 수 없다", "제공된 청크에 없다" 등으로 실질 정보 없이 끝나면 true.
   - 일부 측면만 답하고 나머지를 모른다고 하면 false (부분 답변은 거부가 아니다).
2. key_points_covered — 제시된 key point 각각에 대해, 답변이 그 사실을 담고 있으면 true.
   - 표현이 달라도 같은 사실이면 인정한다 (예: "강한 권고" ≈ "권고등급 A").
   - key point가 권고등급이나 근거수준을 명시하면 답변의 값과 일치해야 true다.
   - 답변에 없는 내용을 추측으로 인정하지 않는다. 순서는 제시된 key point 순서 그대로다.
"""


class JudgeVerdict(BaseModel):
    is_refusal: bool = Field(description="본문이 실질적 거부문이면 true")
    key_points_covered: list[bool] = Field(
        default_factory=list,
        description="제시된 key point 순서대로, 답변이 해당 사실을 담으면 true",
    )


def effective_kind(kind: str, is_refusal: bool) -> str:
    """채점용 결과 종류 — kind=answer라도 본문 거부면 abstain으로 재분류한다."""
    if kind == "answer" and is_refusal:
        return "abstain"
    return kind


def judged_row(row: dict, verdict: JudgeVerdict | None) -> dict:
    """원 결과 행에 채점 결과를 붙인다. verdict=None이면 결정론 채점(abstain/error)."""
    if verdict is None:
        is_refusal = row["kind"] == "abstain"
        covered: list[bool] = []
    else:
        is_refusal = verdict.is_refusal
        covered = verdict.key_points_covered
    return {
        **row,
        "effective_kind": effective_kind(row["kind"], is_refusal),
        "key_points_covered": covered,
    }


async def judge_one(llm, row: dict, key_points: list[str], sem: asyncio.Semaphore) -> dict:
    if row["kind"] != "answer":
        return judged_row(row, None)
    async with sem:
        points = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(key_points)) or "(없음)"
        user = (
            f"## 질문\n{row['question']}\n\n"
            f"## 시스템의 답변\n{row['answer_text']}\n\n"
            f"## Key points ({len(key_points)}개)\n{points}"
        )
        verdict = await llm.ainvoke([("system", JUDGE_SYSTEM), ("user", user)])
        # 길이 불일치는 judge 실수 — 부족분은 미커버로, 초과분은 절단으로 보정한다
        covered = (verdict.key_points_covered + [False] * len(key_points))[: len(key_points)]
        fixed = JudgeVerdict(is_refusal=verdict.is_refusal, key_points_covered=covered)
        return judged_row(row, fixed)


async def judge_file(path: Path, dataset_by_id: dict[str, dict], llm) -> Path:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    sem = asyncio.Semaphore(CONCURRENCY)
    judged = await asyncio.gather(
        *(
            judge_one(llm, row, dataset_by_id.get(row["id"], {}).get("key_points", []), sem)
            for row in rows
        )
    )
    out_path = path.with_suffix(".judged.jsonl")
    with out_path.open("w") as f:
        for row in judged:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return out_path


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=Path("results"))
    parser.add_argument("--dataset", type=Path, default=Path("evals/dataset.jsonl"))
    args = parser.parse_args()

    dataset_by_id = {
        row["id"]: row
        for row in (
            json.loads(line) for line in args.dataset.read_text().splitlines() if line.strip()
        )
    }
    settings = get_settings()
    llm = make_judge_llm(settings).with_structured_output(JudgeVerdict)

    targets = sorted(
        p for p in args.results.glob("*.jsonl") if not p.name.endswith(".judged.jsonl")
    )
    for path in targets:
        out_path = await judge_file(path, dataset_by_id, llm)
        print(f"[judge] {path.name} → {out_path.name}")


if __name__ == "__main__":
    asyncio.run(main())
