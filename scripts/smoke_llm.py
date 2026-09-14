"""실제 OpenAI 스모크 — 분류기·환자 합성·복합 합성을 운영 코드 경로 그대로 한 번씩 부른다.

검증 중 가짜 채팅 모델만 썼기 때문에 다음이 실제 API에서 미확인이었다. 이 스크립트가 그것만 본다.

  1. 분류기 — `response_format`(json_schema strict) 바인딩을 gpt-5.4-mini가 받는가, 응답이
     `RouteDecision`으로 해석되는가, 실행 경로 표가 기대 경로를 내는가.
  2. 환자 합성 — 스트림 조각의 text 누적, 스트림 끝 usage(`stream_usage=True`)가 실제로 오는가,
     `model_name` 메타데이터가 generation에 박히는가.
  3. 복합 합성 — strict 구조화 출력이 스키마 순서(판정 → answer)로 오는가, 손으로 짠 증분 파서
     `VerdictFirstParser`가 실제 청크 경계에서 버티는가, 마커가 인용으로 이어지는가.
  4. 복합 합성(근거 없음) — 근거가 비면 `insufficientEvidence: true`로 기권하는가.

추적 — 서비스와 같은 `configure_tracing("true")` → `traced_turn` / `hidden_branch`를 쓴다. 그래서
LangSmith `cure-agent` 프로젝트에 운영과 같은 모양으로 남고, 환자·복합 합성은 숨김 클라이언트로 가
입출력이 비어 있어야 한다(그것도 이 스모크가 눈으로 확인할 대상이다). traceId는
`smoke-<UTC 시각>`이라 운영 턴과 섞이지 않는다. `LANGSMITH_API_KEY`가 없으면 추적 없이 돈다.

환경 — `.env`의 `OPENAI_API_KEY`·`LANGSMITH_API_KEY`를 읽는다(서비스 설정과 달리 개발 스크립트라
python-dotenv를 쓴다). 프로세스 환경변수가 있으면 그것이 우선한다.

실행:
    .venv/bin/python scripts/smoke_llm.py             # 4회 호출, results/smoke/<시각>.json에 남긴다
    .venv/bin/python scripts/smoke_llm.py --no-trace  # 키가 있어도 추적을 끈다

종료 코드 — 검사 하나라도 실패하면 1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from app.service.llm import AGENT_MODEL, AgentModels  # noqa: E402
from app.service.routing import (  # noqa: E402
    CLASSIFIER_VERSION,
    RouteDecision,
    classify,
    plan_route,
    strip_label,
)
from app.service.synthesis import (  # noqa: E402
    citations_for,
    composite_messages,
    patient_messages,
    stream_composite_answer,
    stream_patient_answer,
)
from app.service.tracing import (  # noqa: E402
    TRACE_PROJECT,
    AgentTracing,
    configure_tracing,
    flush_tracing,
    hidden_branch,
    traced_turn,
)

# ---- 입력 — 운영 데이터가 아니라 합성 텍스트다 ------------------------------------------

CASE_LABEL = "CASE-777"
COMPOSITE_QUESTION = f"{CASE_LABEL} 환자에게 만성 요통으로 침 치료를 해도 괜찮을까요?"
PATIENT_QUESTION = f"{CASE_LABEL} 환자가 복용 중인 약과 알레르기를 알려줘"

PATIENT_RECORD: dict[str, Any] = {
    "caseLabel": CASE_LABEL,
    "age": 58,
    "sex": "F",
    "bmi": 27.1,
    "diagnoses": ["만성 비특이적 요통", "고혈압"],
    "medications": ["암로디핀 5mg 1일 1회", "와파린 3mg 1일 1회"],
    "allergies": ["페니실린"],
    "clinicalNotes": "6개월 이상 지속된 요통. 최근 2주 통증 악화(NRS 6).",
}

EVIDENCE: list[dict[str, Any]] = [
    {
        "id": "ev-smoke-1",
        "guidelineTitle": "만성 요통 한의표준임상진료지침",
        "sectionPath": ["권고안", "R2"],
        "excerpt": (
            "성인 만성 비특이적 요통 환자에게 통증 완화와 기능 개선을 위해 침 치료를 시행할 것을 "
            "권고한다(권고등급 B, 근거수준 Moderate)."
        ),
    },
    {
        "id": "ev-smoke-2",
        "guidelineTitle": "만성 요통 한의표준임상진료지침",
        "sectionPath": ["안전성", "주의사항"],
        "excerpt": (
            "항응고제를 복용 중인 환자에게 침 치료를 시행할 때는 출혈·혈종 위험을 고려하여 "
            "자침 깊이와 부위를 조절하고 시술 후 지혈을 확인한다."
        ),
    },
]

# ---- 검사 결과 ------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class StepResult:
    step: str
    latency_ms: int
    checks: list[Check] = field(default_factory=list)
    output: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def expect(result: StepResult, name: str, ok: bool, detail: str = "") -> None:
    result.checks.append(Check(name, ok, detail))


def real_usage(generation: dict[str, object], messages_len: int) -> bool:
    """usage가 실제 API 값인지 — 추정치는 글자 수/3 올림이라 그 값과 일치하면 추정으로 본다."""
    import math

    return generation["inputTokens"] != math.ceil(messages_len / 3)


# ---- 단계 ------------------------------------------------------------------------------


async def step_classify(models: AgentModels, trace_id: str) -> StepResult:
    started = time.monotonic()
    result = StepResult("classify", 0)
    try:
        decision = await classify(models.classifier(), COMPOSITE_QUESTION, trace_id=trace_id)
    except Exception as e:
        result.latency_ms = round((time.monotonic() - started) * 1000)
        expect(result, "호출·해석", False, f"{type(e).__name__}: {e}")
        return result
    result.latency_ms = round((time.monotonic() - started) * 1000)
    plan = plan_route(decision)
    result.output = {
        "decision": decision.model_dump(),
        "plan": {
            "route": plan.route,
            "case_label": plan.case_label,
            "abstain_reason": plan.abstain_reason,
        },
        "evidence_query": strip_label(COMPOSITE_QUESTION, CASE_LABEL),
        "classifier_version": CLASSIFIER_VERSION,
    }
    expect(result, "RouteDecision 해석", isinstance(decision, RouteDecision))
    expect(
        result,
        "라벨 추출",
        decision.patient_labels == [CASE_LABEL],
        f"patient_labels={decision.patient_labels!r}",
    )
    expect(
        result,
        "실행 경로 COMPOSITE",
        plan.route == "COMPOSITE" and plan.case_label == CASE_LABEL,
        f"route={plan.route} (분류기 원문 {decision.route})",
    )
    return result


async def step_patient(models: AgentModels, trace_id: str) -> StepResult:
    started = time.monotonic()
    result = StepResult("patient_synthesis", 0)
    messages = patient_messages(PATIENT_QUESTION, PATIENT_RECORD, "ko")
    deltas: list[str] = []
    try:
        synthesis = await stream_patient_answer(
            models.synthesis(), messages, lang="ko", trace_id=trace_id, on_delta=deltas.append
        )
    except Exception as e:
        result.latency_ms = round((time.monotonic() - started) * 1000)
        expect(result, "호출", False, f"{type(e).__name__}: {e} / cause={e.__cause__!r}")
        return result
    result.latency_ms = round((time.monotonic() - started) * 1000)
    gen = synthesis.generation
    result.output = {"text": synthesis.text, "delta_count": len(deltas), "generation": gen}
    expect(result, "답변 비어 있지 않음", bool(synthesis.text.strip()))
    expect(result, "델타가 여러 조각", len(deltas) > 1, f"{len(deltas)}조각")
    expect(
        result,
        "델타 합 == 답변",
        "".join(deltas) == synthesis.text,
    )
    prompt_len = sum(len(str(message.text)) for message in messages)
    expect(
        result,
        "스트림 끝 usage 실제값",
        real_usage(gen, prompt_len),
        f"inputTokens={gen['inputTokens']} outputTokens={gen['outputTokens']}",
    )
    expect(
        result,
        "model_name 메타데이터",
        str(gen["model"]).startswith(AGENT_MODEL),
        f"model={gen['model']!r}",
    )
    expect(
        result,
        "기록 밖 값 없음(와파린·페니실린 언급)",
        "와파린" in synthesis.text and "페니실린" in synthesis.text,
        "느슨한 검사 — 정량 평가는 합성 심판이 한다",
    )
    return result


async def step_composite(
    models: AgentModels, trace_id: str, evidence: list[dict[str, Any]], *, expect_abstain: bool
) -> StepResult:
    started = time.monotonic()
    name = "composite_synthesis" + ("_no_evidence" if expect_abstain else "")
    result = StepResult(name, 0)
    messages = composite_messages(COMPOSITE_QUESTION, PATIENT_RECORD, evidence, "ko")
    deltas: list[str] = []
    try:
        synthesis = await stream_composite_answer(
            models.synthesis(), messages, lang="ko", trace_id=trace_id, on_delta=deltas.append
        )
    except Exception as e:
        result.latency_ms = round((time.monotonic() - started) * 1000)
        expect(result, "호출·파서", False, f"{type(e).__name__}: {e} / cause={e.__cause__!r}")
        return result
    result.latency_ms = round((time.monotonic() - started) * 1000)
    gen = synthesis.generation
    citations = citations_for(synthesis.text, [str(item["id"]) for item in evidence])
    result.output = {
        "insufficient_evidence": synthesis.insufficient_evidence,
        "text": synthesis.text,
        "delta_count": len(deltas),
        "citations": citations,
        "generation": gen,
    }
    prompt_len = sum(len(str(message.text)) for message in messages)
    expect(
        result,
        "스트림 끝 usage 실제값",
        real_usage(gen, prompt_len),
        f"inputTokens={gen['inputTokens']} outputTokens={gen['outputTokens']}",
    )
    if expect_abstain:
        expect(
            result,
            "근거 없음 → 기권",
            synthesis.insufficient_evidence and synthesis.text == "",
            f"insufficient={synthesis.insufficient_evidence} text={synthesis.text[:60]!r}",
        )
        return result
    expect(result, "판정 false", not synthesis.insufficient_evidence)
    expect(result, "답변 비어 있지 않음", bool(synthesis.text.strip()))
    expect(result, "파서가 answer를 조각으로 흘림", len(deltas) > 1, f"{len(deltas)}조각")
    expect(result, "델타 합 == 답변", "".join(deltas) == synthesis.text)
    expect(
        result,
        "마커 → 인용",
        len(citations) >= 1,
        f"citations={citations}",
    )
    expect(
        result,
        "항응고제 주의를 짚음(와파린 ↔ 근거 [2])",
        any(c["marker"] == 2 for c in citations),
        "느슨한 검사 — 정량 평가는 합성 심판이 한다",
    )
    return result


# ---- 실행 ------------------------------------------------------------------------------


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


async def run(trace: bool) -> int:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("OPENAI_API_KEY가 없다 — .env 또는 환경변수에 넣어라", file=sys.stderr)
        return 2
    tracing: AgentTracing
    if trace and os.environ.get("LANGSMITH_API_KEY"):
        tracing = configure_tracing("true")
        trace_note = f"LangSmith 프로젝트 {TRACE_PROJECT} (합성 2건은 숨김 클라이언트)"
    else:
        tracing = configure_tracing("")
        trace_note = "추적 없음" + ("" if trace else " (--no-trace)")
        if trace:
            trace_note += " — LANGSMITH_API_KEY 없음"

    trace_id = "smoke-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    models = AgentModels(api_key)
    print(f"모델 {AGENT_MODEL} · traceId {trace_id} · {trace_note}")

    steps: list[StepResult] = []
    with traced_turn(tracing):
        steps.append(await step_classify(models, trace_id))
        with hidden_branch(tracing):
            steps.append(await step_patient(models, trace_id))
            steps.append(await step_composite(models, trace_id, EVIDENCE, expect_abstain=False))
            steps.append(await step_composite(models, trace_id, [], expect_abstain=True))
    await asyncio.to_thread(flush_tracing, tracing)

    total_in = total_out = 0
    for step in steps:
        mark = "PASS" if step.ok else "FAIL"
        print(f"\n[{mark}] {step.step}  {step.latency_ms}ms")
        for check in step.checks:
            suffix = f"  — {check.detail}" if check.detail else ""
            print(f"  {'✓' if check.ok else '✗'} {check.name}{suffix}")
        gen = step.output.get("generation")
        if isinstance(gen, dict):
            total_in += int(gen["inputTokens"])
            total_out += int(gen["outputTokens"])
        text = step.output.get("text")
        if isinstance(text, str) and text:
            print("  답변: " + text[:200].replace("\n", " ") + ("…" if len(text) > 200 else ""))

    print(f"\n토큰 합계 — 입력 {total_in} · 출력 {total_out} (분류기는 usage를 남기지 않아 제외)")

    out_dir = ROOT / "results" / "smoke"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{trace_id}.json"
    out_path.write_text(
        json.dumps(
            {
                "trace_id": trace_id,
                "model": AGENT_MODEL,
                "code_version": code_version(),
                "tracing": trace_note,
                "steps": [
                    {
                        "step": s.step,
                        "ok": s.ok,
                        "latency_ms": s.latency_ms,
                        "checks": [c.__dict__ for c in s.checks],
                        "output": s.output,
                    }
                    for s in steps
                ],
                "tokens": {"input": total_in, "output": total_out},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"결과 저장 {out_path.relative_to(ROOT)}")
    return 0 if all(step.ok for step in steps) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--no-trace", action="store_true", help="LANGSMITH_API_KEY가 있어도 추적을 끈다"
    )
    args = parser.parse_args()
    return asyncio.run(run(trace=not args.no_trace))


if __name__ == "__main__":
    sys.exit(main())
