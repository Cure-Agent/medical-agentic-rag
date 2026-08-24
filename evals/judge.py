"""LLM-as-judge — results/<preset>.jsonl을 채점해 <preset>.judged.jsonl을 만든다.

    python -m evals.judge --results results --dataset evals/dataset.jsonl

채점 축은 스모크 실주행에서 확정했다:

- **effective_kind**: kind=answer라도 본문이 실질적 거부문이면 abstain으로 센다.
  baseline이 거리 게이트(0.48)를 통과시킨 뒤 본문에서 "답변할 수 없습니다"라고
  거부하는 사례가 실측됐다 — kind만 보면 이 행동이 오답(누출)으로 잘못 집계된다.
- **key_points_covered**: answerable 문항의 key_point별 포함 여부. 표현이 달라도
  같은 사실이면 인정하되, key_point가 권고등급·근거수준을 명시하면 그 값까지
  일치해야 한다.

**등급 규칙은 프롬프트만으로는 지켜지지 않는다(2026-08-24 실측).** cpx-009 kp3에서
key point는 「경과관찰보다 한약치료 고려**해야** 함(R3-1, 중등도=B)」인데 답변 10건이
전부 R3 전체를 인용해 「고려할 **수 있다**, 권고등급 **C**」로 답했고, 10건 모두
covered=true로 채점됐다. 규칙은 이미 프롬프트에 있었다 — 모델이 안 지킨 것이다.

그래서 판정을 두 단계로 쪼갠다:

1. LLM은 **답변이 명시한 등급·근거수준을 key point별로 뽑아 적는다**(A/B/C/GPP,
   High/Moderate/Low/Very Low). 판정 전에 값을 적게 하면 대충 넘어갈 수가 없다.
2. 비교는 **코드가 한다** — key point가 등급을 명시하는데 답변의 값이 다르거나
   없으면 `covered`를 False로 내린다. 한 방향으로만 내리므로 채점이 관대해질 일은 없다.

`judge_rules` 필드로 채점 판을 결과 행에 박는다 — 재채점 전후 파일이 섞이면
같은 결과에서 다른 정답률이 나온다.

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
JUDGE_RULES = "grade-strict-v1"  # 채점 판. 재채점 전후 파일을 가르는 표식이다.

JUDGE_SYSTEM = """너는 의료 RAG 시스템의 답변을 채점하는 평가자다.
주어진 질문·답변·채점 기준(key points)을 보고 정확히 판정한다.

판정 규칙:
1. is_refusal — 답변 본문이 실질적으로 답변을 거부하는가?
   - "근거가 없다", "답변할 수 없다", "제공된 청크에 없다" 등으로 실질 정보 없이 끝나면 true.
   - 일부 측면만 답하고 나머지를 모른다고 하면 false (부분 답변은 거부가 아니다).
2. answer_grades / answer_levels — **판정하기 전에 먼저 적는다.** key point 각각에 대해,
   답변이 그 항목을 다루면서 명시한 권고등급과 근거수준을 뽑아 정규화한다.
   - 권고등급: A / B / C / GPP 중 하나. 답변이 말로 썼으면 옮긴다
     ("고려해야 한다"만으로는 등급을 추정하지 말 것 — 등급 표기가 있을 때만 적는다).
   - 근거수준: High / Moderate / Low / Very Low 중 하나. 한글이면 옮긴다
     (높음=High, 중등도=Moderate, 낮음=Low, 매우 낮음=Very Low).
   - 답변이 그 key point에 대해 등급·근거수준을 말하지 않았으면 빈 문자열 "".
   - **추측 금지.** 다른 key point의 등급을 끌어오지 않는다. 세 목록의 길이는 key point 수와 같다.
3. key_points_covered — 답변이 그 사실을 담고 있으면 true.
   - 표현이 달라도 같은 사실이면 인정한다.
   - 답변에 없는 내용을 추측으로 인정하지 않는다. 순서는 제시된 key point 순서 그대로다.
"""

RECOMMENDATION_GRADES = ("A", "B", "C", "GPP")
EVIDENCE_LEVELS = ("High", "Moderate", "Low", "Very Low")

# key point 문구 → 요구 등급. 33개 문구는 우리가 쓴 것이라 표기가 고정되어 있다.
# 긴 것부터 본다 — "근거수준 매우 낮음"이 "근거수준 낮음"에 먹히면 안 된다.
_KP_GRADE = (("강한 권고", "A"), ("중등도 권고", "B"), ("약한 권고", "C"), ("전문가 합의", "GPP"))
_KP_LEVEL = (
    ("근거수준 매우 낮음", "Very Low"),
    ("근거수준 중등도", "Moderate"),
    ("근거수준 높음", "High"),
    ("근거수준 낮음", "Low"),
)


class JudgeVerdict(BaseModel):
    is_refusal: bool = Field(description="본문이 실질적 거부문이면 true")
    answer_grades: list[str] = Field(
        default_factory=list,
        description="key point 순서대로, 답변이 그 항목에 대해 명시한 권고등급"
        "(A/B/C/GPP). 없으면 빈 문자열",
    )
    answer_levels: list[str] = Field(
        default_factory=list,
        description="key point 순서대로, 답변이 그 항목에 대해 명시한 근거수준"
        "(High/Moderate/Low/Very Low). 없으면 빈 문자열",
    )
    key_points_covered: list[bool] = Field(
        default_factory=list,
        description="제시된 key point 순서대로, 답변이 해당 사실을 담으면 true",
    )


def required_grade(key_point: str) -> tuple[str | None, str | None]:
    """key point가 요구하는 (권고등급, 근거수준). 명시가 없으면 None."""
    grade = next((g for token, g in _KP_GRADE if token in key_point), None)
    level = next((lv for token, lv in _KP_LEVEL if token in key_point), None)
    return grade, level


def _normalize(value: str, allowed: tuple[str, ...]) -> str | None:
    """LLM이 적어 낸 값을 허용 집합으로 접는다. 못 접으면 None(= 명시 없음)."""
    text = (value or "").strip()
    return next((a for a in allowed if a.casefold() == text.casefold()), None)


def reconcile_grade(key_point: str, covered: bool, grade: str, level: str) -> bool:
    """등급 규칙을 코드로 강제한다 — **내리기만 한다.**

    key point가 등급·근거수준을 명시하는데 답변이 그 값을 말하지 않았거나 다른 값을
    말했으면 담은 것으로 보지 않는다. 등급이 곧 임상 결론이라 "표현 차이"로 넘길 수 없다.
    """
    if not covered:
        return False
    want_grade, want_level = required_grade(key_point)
    if want_grade and _normalize(grade, RECOMMENDATION_GRADES) != want_grade:
        return False
    if want_level and _normalize(level, EVIDENCE_LEVELS) != want_level:
        return False
    return True


def effective_kind(kind: str, is_refusal: bool) -> str:
    """채점용 결과 종류 — kind=answer라도 본문 거부면 abstain으로 재분류한다."""
    if kind == "answer" and is_refusal:
        return "abstain"
    return kind


def judged_row(
    row: dict, verdict: JudgeVerdict | None, key_points: list[str] | None = None
) -> dict:
    """원 결과 행에 채점 결과를 붙인다. verdict=None이면 결정론 채점(abstain/error).

    key_points를 주면 등급 규칙을 코드로 강제한다. 무엇이 내려갔는지는 `grade_overrides`에
    남긴다 — 규칙이 조용히 작동하면 정답률이 왜 달라졌는지 나중에 못 따진다.
    """
    if verdict is None:
        return {
            **row,
            "effective_kind": effective_kind(row["kind"], row["kind"] == "abstain"),
            "key_points_covered": [],
            "answer_grades": [],
            "answer_levels": [],
            "grade_overrides": [],
            "judge_rules": JUDGE_RULES,
        }

    raw = verdict.key_points_covered
    covered = raw
    if key_points is not None:
        covered = [
            reconcile_grade(kp, raw[i], verdict.answer_grades[i], verdict.answer_levels[i])
            for i, kp in enumerate(key_points)
        ]
    return {
        **row,
        "effective_kind": effective_kind(row["kind"], verdict.is_refusal),
        "key_points_covered": covered,
        "answer_grades": verdict.answer_grades,
        "answer_levels": verdict.answer_levels,
        "grade_overrides": [
            i for i, (a, b) in enumerate(zip(raw, covered, strict=False)) if a != b
        ],
        "judge_rules": JUDGE_RULES,
    }


def _pad(values: list, n: int, filler):
    """길이 불일치는 judge 실수 — 부족분은 filler로, 초과분은 절단으로 보정한다."""
    return (list(values) + [filler] * n)[:n]


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
        n = len(key_points)
        fixed = JudgeVerdict(
            is_refusal=verdict.is_refusal,
            key_points_covered=_pad(verdict.key_points_covered, n, False),
            answer_grades=_pad(verdict.answer_grades, n, ""),
            answer_levels=_pad(verdict.answer_levels, n, ""),
        )
        return judged_row(row, fixed, key_points)


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
