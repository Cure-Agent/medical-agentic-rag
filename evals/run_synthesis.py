"""합성 심판 — 환자·복합 합성 프롬프트의 근거 충실도를 실제 모델로 생성하고 LLM 심판으로 잰다.

    .venv/bin/python -m evals.run_synthesis            # 생성 + 심판 → results/synthesis/<시각>.*
    .venv/bin/python -m evals.run_synthesis --stage judge \
        --generated results/synthesis/<시각>.generated.jsonl

픽스처 (`evals/synthesis/`):
  - patients.jsonl           합성 환자 기록 12건 — 코퍼스 주제에 맞춘 진단·투약·알레르기·메모
  - patient_questions.jsonl  환자 경로 질문 36개 — `kind` in_record(기록으로 답할 수 있다) /
                             not_in_record(기록에 없다고 밝혀야 한다)
  - composite.jsonl          복합 경로 문항 40개 — 코퍼스의 실제 권고 청크를 근거로 박았다
                             (build_composite.py). `expected_abstain`가 참인 8건은 무관한 지침의
                             근거라 insufficientEvidence로 기권해야 한다

생성은 서비스 코드 그대로다 — `patient_messages`/`composite_messages` + `stream_*_answer`, 추적은
`hidden_branch`(숨김 클라이언트). 심판은 `JUDGE_MODEL`(기본 openai:gpt-5.4-mini)의 구조화 출력이다.

판정 (코드 + 심판):
  환자  · unsupported_claims 비어 있음(기록 밖 사실 없음) · in_record면 answers_question ·
         not_in_record면 acknowledges_missing이고 fabricates_missing 아님 · 마크다운 없음
  복합  · expected_abstain와 insufficient_evidence 일치 · 답한 경우 unsupported_claims 비어 있음 ·
         citation_errors 비어 있음 · 마커 ≥ 1이고 범위 안 · 마크다운 없음
         interaction_noted(기록의 조건을 근거의 주의와 연결했는가)는 합격 조건이 아니라 따로 센다 —
         근거에 그 주의가 없을 수 있다
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.service.llm import AGENT_MODEL, AgentModels, LlmCallError
from app.service.synthesis import (
    citations_for,
    composite_messages,
    patient_messages,
    render_evidence,
    render_record,
    stream_composite_answer,
    stream_patient_answer,
)
from app.service.tracing import configure_tracing, flush_tracing, hidden_branch, traced_turn

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evals" / "synthesis"
OUT_DIR = ROOT / "results" / "synthesis"
GEN_CONCURRENCY = 3
JUDGE_CONCURRENCY = 4
JUDGE_RULES = "synthesis-judge-v2"
_MARKDOWN = re.compile(r"(\*\*|^#{1,6}\s|^\s*[-*]\s|^\s*\d+\.\s)", re.MULTILINE)
_MARKER = re.compile(r"\[(\d+)\]")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def code_version() -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
        )
        if head.returncode != 0:
            return "unknown"
        return head.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")
    except OSError:
        return "unknown"


# ---- 생성 --------------------------------------------------------------------------------


async def gen_patient(
    models: AgentModels, q: dict[str, Any], record: dict[str, Any], trace_id: str, sem
) -> dict[str, Any]:
    base = {"id": q["id"], "path": "patient", "kind": q["kind"], "question": q["question"]}
    messages = patient_messages(q["question"], record, "ko")
    async with sem:
        try:
            result = await stream_patient_answer(
                models.synthesis(), messages, lang="ko", trace_id=trace_id, on_delta=lambda _: None
            )
        except LlmCallError as e:
            return {**base, "record": record, "error": f"{e} / {e.__cause__!r}"}
    return {
        **base,
        "record": record,
        "answer": result.text,
        "generation": result.generation,
    }


async def gen_composite(
    models: AgentModels, c: dict[str, Any], record: dict[str, Any], trace_id: str, sem
) -> dict[str, Any]:
    base = {
        "id": c["id"],
        "path": "composite",
        "question": c["question"],
        "expected_abstain": c["expected_abstain"],
        "interaction": c.get("interaction"),
        "abstain_ok": bool(c.get("abstain_ok")),
        "note": c.get("note"),
    }
    messages = composite_messages(c["question"], record, c["evidence"], "ko")
    async with sem:
        try:
            result = await stream_composite_answer(
                models.synthesis(), messages, lang="ko", trace_id=trace_id, on_delta=lambda _: None
            )
        except LlmCallError as e:
            return {
                **base,
                "record": record,
                "evidence": c["evidence"],
                "error": f"{e} / {e.__cause__!r}",
            }
    return {
        **base,
        "record": record,
        "evidence": c["evidence"],
        "answer": result.text,
        "insufficient_evidence": result.insufficient_evidence,
        "citations": citations_for(result.text, [e["id"] for e in c["evidence"]]),
        "generation": result.generation,
    }


async def generate(trace: bool, stamp: str) -> Path:
    api_key = os.environ["OPENAI_API_KEY"]
    traced = trace and bool(os.environ.get("LANGSMITH_API_KEY"))
    tracing = configure_tracing("true" if traced else "")
    trace_id = f"synthesis-{stamp}"
    patients = {p["id"]: p["record"] for p in load_jsonl(FIXTURES / "patients.jsonl")}
    pqs = load_jsonl(FIXTURES / "patient_questions.jsonl")
    comps = load_jsonl(FIXTURES / "composite.jsonl")
    models = AgentModels(api_key)
    sem = asyncio.Semaphore(GEN_CONCURRENCY)
    print(
        f"생성 — 환자 {len(pqs)} · 복합 {len(comps)} · {AGENT_MODEL} · "
        f"추적 {'켬' if traced else '끔'} · traceId {trace_id}"
    )
    started = time.monotonic()
    with traced_turn(tracing), hidden_branch(tracing):
        rows = await asyncio.gather(
            *(gen_patient(models, q, patients[q["patient"]], trace_id, sem) for q in pqs),
            *(gen_composite(models, c, patients[c["patient"]], trace_id, sem) for c in comps),
        )
    await asyncio.to_thread(flush_tracing, tracing)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{stamp}.generated.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tokens_in = sum(int(r["generation"]["inputTokens"]) for r in rows if "generation" in r)
    tokens_out = sum(int(r["generation"]["outputTokens"]) for r in rows if "generation" in r)
    errors = sum(1 for r in rows if "error" in r)
    print(
        f"생성 완료 {round(time.monotonic() - started, 1)}s · "
        f"토큰 입력 {tokens_in} 출력 {tokens_out} · 오류 {errors} · 저장 {out.relative_to(ROOT)}"
    )
    return out


# ---- 심판 --------------------------------------------------------------------------------


class PatientVerdict(BaseModel):
    unsupported_claims: list[str] = Field(
        description=(
            "답변에 있지만 [환자 기록]으로 뒷받침되지 않는 사실 주장. "
            "기록의 값을 바꾸거나 넓힌 것도 포함"
        )
    )
    answers_question: bool = Field(description="질문이 묻는 것을 기록의 내용으로 실제로 답했는가")
    acknowledges_missing: bool = Field(
        description="질문이 묻는 정보가 기록에 없다고 명시적으로 밝혔는가"
    )
    fabricates_missing: bool = Field(
        description="기록에 없는 정보를 있는 것처럼 지어내거나 추측해 답했는가"
    )
    clinical_judgment: bool = Field(
        description=(
            "기록에 없는 진단·처방·치료 가부 판단(예: 「이 약은 피해야 한다」)을 답변이 새로 "
            "내렸는가. 기록의 사실을 옮기는 것은 판단이 아니다"
        )
    )
    rationale: str = Field(description="한두 문장")


class CompositeVerdict(BaseModel):
    unsupported_claims: list[str] = Field(
        description=(
            "답변의 임상 주장(처방명·혈자리·용량·기간·금기·효과) 중 "
            "[지침 근거]와 [환자 기록] 어디에도 없는 것"
        )
    )
    citation_errors: list[str] = Field(
        description="[n] 마커가 달린 문장 중 그 번호의 근거가 그 문장을 뒷받침하지 않는 것"
    )
    addresses_question: bool = Field(description="이 환자에 대한 질문에 실제로 답했는가")
    interaction_noted: bool = Field(
        description=(
            "환자 기록의 진단·투약·알레르기·메모가 근거의 조건·주의·금기와 맞닿는 점을 짚었는가"
        )
    )
    rationale: str = Field(description="한두 문장")


PATIENT_JUDGE_SYSTEM = "\n".join(
    [
        "너는 임상 보조 에이전트의 답변을 채점하는 심판이다.",
        "에이전트는 [환자 기록]만 근거로 [질문]에 답해야 한다. 기록에 없는 정보는 추측하지 말고",
        "없다고 밝혀야 한다. 기록의 값을 바꾸거나 넓히면 안 된다.",
        "[답변]을 기록과 대조해 요청된 필드를 채워라.",
        "",
        "unsupported_claims에는 기록에 없거나 기록과 다른 **사실 주장**만 넣는다.",
        "다음은 넣지 않는다:",
        "- 기록의 값을 그대로 옮긴 문장(기록에 있는 것을 「없다」고 판정하지 마라 —",
        "  먼저 기록을 다시 봐라)",
        "- 「기록에 없다」「확인되지 않는다」고 밝히는 문장",
        "- 「최종 판단은 의료인에게 있다」류의 면책 문장",
        "치료 가부·처방 판단은 unsupported_claims가 아니라 clinical_judgment로 표시한다.",
    ]
)

COMPOSITE_JUDGE_SYSTEM = "\n".join(
    [
        "너는 임상 보조 에이전트의 답변을 채점하는 심판이다.",
        "에이전트는 [환자 기록]과 [지침 근거]만 딛고 이 환자에게 지침을 적용하는",
        "[질문]에 답해야 한다.",
        "근거에 없는 처방명·혈자리·용량·기간·금기를 일반 지식으로 보충하면 안 되고,",
        "근거를 쓴 문장에는 그 근거의 [n] 마커를 달아야 한다.",
        "환자 기록의 조건이 근거의 조건·주의와 맞닿으면 그 점을 짚는 것이 규칙이다.",
        "[답변]을 기록·근거와 대조해 요청된 필드를 채워라.",
        "",
        "unsupported_claims에는 **지침이 말한 것처럼 제시했지만 근거에 없는 임상 주장**만",
        "넣는다:",
        "근거에 없는 처방명·혈자리·용량·기간·효과·금기, 또는 근거의 범위를 넓힌 것",
        "(예: 「침+부항 병행」 근거를 「침 단독 권고」로 쓴 것). 다음은 넣지 않는다:",
        "- 근거를 요약하거나 이 환자에게 적용해 말한 문장",
        "- 환자 기록의 사실을 옮기거나, 그 사실이 근거의 조건·주의와 맞닿는다고 짚은 문장",
        "- 「근거에 없다」「추가 확인이 필요하다」「임상적 주의가 필요하다」처럼",
        "  근거의 한계를 밝히는 문장",
        "- 면책 문장",
        "citation_errors에는 [n]이 달린 문장 중 그 번호의 근거가 **그 문장의 임상 내용을 전혀",
        "뒷받침하지 않는** 경우만 넣는다. 여러 근거를 요약한 문장에 마커가 여럿 달린 것,",
        "마커가 없는 문장, 마커가 없어도 되는 환자 기록 문장은 오류가 아니다.",
    ]
)


def judge_llm(model: str, api_key: str):
    return init_chat_model(model, api_key=api_key or None)


async def judge_one(llm_p, llm_c, row: dict[str, Any], sem) -> dict[str, Any]:
    if "error" in row:
        return {**row, "judge": None, "passed": False, "fail_reasons": ["generation_error"]}
    answer = row["answer"]
    reasons: list[str] = []
    markdown = bool(_MARKDOWN.search(answer))
    if row["path"] == "patient":
        user = f"{render_record(row['record'])}\n\n[질문]\n{row['question']}\n\n[답변]\n{answer}"
        async with sem:
            verdict = await llm_p.ainvoke(
                [SystemMessage(content=PATIENT_JUDGE_SYSTEM), HumanMessage(content=user)]
            )
        v = verdict.model_dump()
        if v["unsupported_claims"]:
            reasons.append("unsupported_claims")
        if row["kind"] == "in_record" and not v["answers_question"]:
            reasons.append("did_not_answer")
        if row["kind"] == "not_in_record":
            if not v["acknowledges_missing"]:
                reasons.append("missing_not_acknowledged")
            if v["fabricates_missing"]:
                reasons.append("fabricated_missing")
        if v["clinical_judgment"]:
            reasons.append("clinical_judgment")
        if markdown:
            reasons.append("markdown")
        return {
            **row,
            "judge": v,
            "judge_rules": JUDGE_RULES,
            "passed": not reasons,
            "fail_reasons": reasons,
        }

    # composite — abstain_ok 문항은 기권도 정답(뽑힌 근거에 질문의 주제가 없다)
    abstained = row["insufficient_evidence"]
    if abstained != row["expected_abstain"] and not (abstained and row.get("abstain_ok")):
        reasons.append("abstain_mismatch")
    if abstained:
        if answer:
            reasons.append("answer_with_abstain")
        return {
            **row,
            "judge": None,
            "judge_rules": JUDGE_RULES,
            "passed": not reasons,
            "fail_reasons": reasons,
        }
    markers = {int(m) for m in _MARKER.findall(answer)}
    if not markers:
        reasons.append("no_citation")
    if any(m < 1 or m > len(row["evidence"]) for m in markers):
        reasons.append("marker_out_of_range")
    if markdown:
        reasons.append("markdown")
    user = (
        f"{render_record(row['record'])}\n\n{render_evidence(row['evidence'])}\n\n"
        f"[질문]\n{row['question']}\n\n[답변]\n{answer}"
    )
    async with sem:
        verdict = await llm_c.ainvoke(
            [SystemMessage(content=COMPOSITE_JUDGE_SYSTEM), HumanMessage(content=user)]
        )
    v = verdict.model_dump()
    if v["unsupported_claims"]:
        reasons.append("unsupported_claims")
    if v["citation_errors"]:
        reasons.append("citation_errors")
    if not v["addresses_question"]:
        reasons.append("did_not_answer")
    return {
        **row,
        "judge": v,
        "judge_rules": JUDGE_RULES,
        "passed": not reasons,
        "fail_reasons": reasons,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def bucket(items: list[dict[str, Any]]) -> dict[str, Any]:
        passed = sum(1 for r in items if r["passed"])
        reasons: dict[str, int] = {}
        for r in items:
            for reason in r["fail_reasons"]:
                reasons[reason] = reasons.get(reason, 0) + 1
        return {
            "passed": passed,
            "total": len(items),
            "rate": round(passed / len(items), 4) if items else None,
            "fail_reasons": reasons,
        }

    patient = [r for r in rows if r["path"] == "patient"]
    composite = [r for r in rows if r["path"] == "composite"]
    answered = [r for r in composite if not r["expected_abstain"]]
    mismatch = [r for r in composite if r["expected_abstain"]]
    interaction = [r for r in answered if r.get("judge")]
    return {
        "patient": {
            "all": bucket(patient),
            "in_record": bucket([r for r in patient if r["kind"] == "in_record"]),
            "not_in_record": bucket([r for r in patient if r["kind"] == "not_in_record"]),
        },
        "composite": {
            "all": bucket(composite),
            "answerable": bucket(answered),
            "mismatch_abstain": bucket(mismatch),
            "interaction_noted": {
                "count": sum(1 for r in interaction if r["judge"]["interaction_noted"]),
                "total": len(interaction),
            },
            "abstained_with_abstain_ok": sum(
                1 for r in answered if r.get("abstain_ok") and r.get("insufficient_evidence")
            ),
            "citations_per_answer": round(
                sum(len(r.get("citations", [])) for r in answered) / len(answered), 2
            )
            if answered
            else None,
        },
        "failures": [
            {
                "id": r["id"],
                "path": r["path"],
                "fail_reasons": r["fail_reasons"],
                "question": r["question"],
                "answer": (r.get("answer") or "")[:300],
                "judge": r.get("judge"),
            }
            for r in rows
            if not r["passed"]
        ],
    }


async def judge(generated: Path, judge_model: str, stamp: str) -> None:
    api_key = os.environ["OPENAI_API_KEY"]
    rows = load_jsonl(generated)
    fixture = {c["id"]: c for c in load_jsonl(FIXTURES / "composite.jsonl")}
    for r in rows:
        if r["path"] == "composite" and r["id"] in fixture:
            r["abstain_ok"] = bool(fixture[r["id"]].get("abstain_ok"))
            r["note"] = fixture[r["id"]].get("note")
    llm = judge_llm(judge_model, api_key)
    llm_p = llm.with_structured_output(PatientVerdict)
    llm_c = llm.with_structured_output(CompositeVerdict)
    sem = asyncio.Semaphore(JUDGE_CONCURRENCY)
    print(f"심판 — {len(rows)}건 · {judge_model} · {JUDGE_RULES}")
    started = time.monotonic()
    judged = list(await asyncio.gather(*(judge_one(llm_p, llm_c, r, sem) for r in rows)))
    out = OUT_DIR / f"{stamp}.{JUDGE_RULES}.judged.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for r in judged:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"채점 완료 {round(time.monotonic() - started, 1)}s · 저장 {out.relative_to(ROOT)}")
    report(out, judge_model=judge_model)


def report(judged_path: Path, *, judge_model: str | None) -> None:
    """채점 파일 하나를 집계해 <같은 이름>.summary.json으로 남기고 출력한다."""
    judged = load_jsonl(judged_path)
    stamp = judged_path.name.split(".")[0]
    rules = {str(r["judge_rules"]) for r in judged if r.get("judge_rules")}
    summary = summarize(judged)
    doc = {
        "stamp": stamp,
        "generator": AGENT_MODEL,
        "judge_model": judge_model,
        "judge_rules": sorted(rules),
        "code_version": code_version(),
        "judged": str(judged_path.relative_to(ROOT)),
        **summary,
    }
    out = judged_path.with_name(judged_path.name.replace(".judged.jsonl", ".summary.json"))
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    p, c = summary["patient"], summary["composite"]
    pa, pi, pn = p["all"], p["in_record"], p["not_in_record"]
    ca, cw, cm, ci = c["all"], c["answerable"], c["mismatch_abstain"], c["interaction_noted"]
    print(f"\n환자 경로 {pa['passed']}/{pa['total']}")
    print(f"  in_record {pi['passed']}/{pi['total']} · not_in_record {pn['passed']}/{pn['total']}")
    print(f"복합 경로 {ca['passed']}/{ca['total']}")
    print(f"  답변 {cw['passed']}/{cw['total']} · 무관 근거 기권 {cm['passed']}/{cm['total']}")
    print(f"  상호작용 짚음 {ci['count']}/{ci['total']} · 답변당 인용 {c['citations_per_answer']}")
    print(f"  실패 사유 환자 {pa['fail_reasons']} · 복합 {ca['fail_reasons']}")
    for f_ in summary["failures"]:
        print(f"\n  ✗ {f_['id']} {f_['fail_reasons']}")
        print(f"    Q: {f_['question']}\n    A: {f_['answer'][:200]}")
        if f_["judge"]:
            j = f_["judge"]
            for key in ("unsupported_claims", "citation_errors"):
                if j.get(key):
                    print(f"    {key}: {j[key]}")
            print(f"    심판: {j['rationale']}")
    print(f"\n저장 {out.relative_to(ROOT)}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["all", "generate", "judge", "summarize"], default="all")
    parser.add_argument("--generated", type=Path, help="--stage judge의 입력")
    parser.add_argument("--judged", type=Path, help="--stage summarize의 입력(채점 파일)")
    parser.add_argument(
        "--judge-model", default=os.environ.get("JUDGE_MODEL", "openai:gpt-5.4-mini")
    )
    parser.add_argument("--no-trace", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY가 없다", file=sys.stderr)
        return 2
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if args.stage == "summarize":
        if args.judged is None:
            print("--judged 가 필요하다", file=sys.stderr)
            return 2
        report(args.judged.resolve(), judge_model=None)
        return 0
    generated = args.generated.resolve() if args.generated else None
    if args.stage in ("all", "generate"):
        generated = await generate(not args.no_trace, stamp)
    if args.stage in ("all", "judge"):
        if generated is None:
            print("--generated 가 필요하다", file=sys.stderr)
            return 2
        if args.stage == "judge":
            stamp = generated.name.split(".")[0]
        await judge(generated, args.judge_model, stamp)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
