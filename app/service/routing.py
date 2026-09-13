"""질문 분류와 실행 경로 표 (BE docs/specs/51 「분류 경계」).

**라벨 차원은 코드가, 의미 차원은 LLM이 쥔다.** 분류기는 `{route, patient_labels}`만 내고 실제
경로는 실행 경로 표(순수 함수)가 정한다 — 특정 환자는 질문에 적힌 케이스 라벨로만 가리킨다.

분류기 호출 — 턴마다 한 번, `classifier_model.bind(response_format=ROUTE_RESPONSE_FORMAT)`의
`ainvoke`다. 입력에는 질문 원문을 그대로 담은 HumanMessage가 있다. 응답 텍스트는 JSON
`{"route": "GUIDELINE"|"PATIENT"|"COMPOSITE"|"OTHER", "patient_labels": [문자열, ...]}`이며,
해석하지 못하면 LLM 실패와 같다.

실행 경로 표 — 라벨은 앞뒤 공백을 지우고 빈 값을 버린 뒤 **대소문자를 무시하고 서로 다른
것만** 센다.

    분류기 route   라벨   실행 경로   결말
    GUIDELINE      0      지침        지침 도구
    GUIDELINE      1      복합
    PATIENT        1      환자        환자 도구가 NOT_FOUND·AMBIGUOUS면 ABSTAINED patient_unresolved
    PATIENT        0      환자        도구 없이 ABSTAINED patient_unresolved
    COMPOSITE      1      복합        환자 해석 실패는 위와 같다
    COMPOSITE      0      지침
    무엇이든       2 이상 기타        도구 없이 ABSTAINED out_of_scope
    OTHER          —      기타        도구 없이 ABSTAINED out_of_scope

- 환자 도구의 `caseLabel`은 그 라벨이다 — 공백을 지운 분류기의 표기이고, 대소문자만 다른 중복은
  처음 것을 쓴다.
- 근거 도구의 `query`는 질문에서 그 라벨을 **대소문자 무시로 전부 지운** 뒤 연속 공백을 한 칸으로
  줄이고 앞뒤 공백을 지운 문자열이다. 지우고 나서 비면 질문 원문을 쓴다.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from app.service.llm import AGENT_MODEL, LlmCallError, message_text

Route = Literal["GUIDELINE", "PATIENT", "COMPOSITE", "OTHER"]
# 에이전트만 기록하는 기권 사유 — BE `abstain_reason` enum의 값이다
RouteAbstainReason = Literal["out_of_scope", "patient_unresolved"]

# 턴의 분류기 버전 — 지침 도구 요청과 모든 완결에 실려 「왜 이 경로였나」를 재현한다(BE §5.7)
CLASSIFIER_VERSION = f"{AGENT_MODEL}/agent-route-v1"

# 정의만 준다(v1). 「라벨이 없으면 환자·복합이 아니다」 같은 라벨 규칙은 프롬프트가 아니라 실행 경로
# 표가 쥔다 — 규칙을 프롬프트로 옮긴 구성(v2)은 조건이 붙은 환자군 질문을 OTHER로 밀어 표로도
# 되돌릴 수 없었다(BE docs/specs/51 분류 모사: 실행 경로 131/132 → 123/132)
CLASSIFIER_PROMPT = "\n".join(
    [
        "너는 한의원 의료인이 보낸 질문 하나를 네 갈래 중 하나로 분류한다.",
        "질문은 한국어나 영어다.",
        "",
        "GUIDELINE — 임상 지침에 관한 질문이다. 질환·증상·환자군·치료법 일반에 대해",
        "  지침이 무엇을 권고하는지 묻는다.",
        "PATIENT — 특정 환자 한 명의 기록 자체를 묻는다(진단·투약·알레르기·신체 계측·",
        "  임상 메모 등).",
        "COMPOSITE — 특정 환자 한 명에게 지침을 적용한 판단을 묻는다(이 환자에게 이 치료가",
        "  적절한가, 주의할 점은 무엇인가 등).",
        "OTHER — 위 셋이 아니다. 인사·잡담·운영 문의, 약재 재고·발주, 여러 환자의 목록·집계·",
        "  비교, 환자 기록의 등록·수정·삭제 요청이 여기에 든다.",
        "",
        "patient_labels에는 질문에 적힌 환자 케이스 라벨(예: CASE-001)을 질문에 적힌 표기",
        "그대로 모두 담는다. 라벨이 없으면 빈 배열이다.",
    ]
)

ROUTE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "agent_route",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "route": {
                    "type": "string",
                    "enum": ["GUIDELINE", "PATIENT", "COMPOSITE", "OTHER"],
                },
                "patient_labels": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["route", "patient_labels"],
            "additionalProperties": False,
        },
    },
}


class RouteDecision(BaseModel):
    """분류기 판정 원문 — 실행 경로가 아니다."""

    route: Route
    patient_labels: list[str]


@dataclass(frozen=True)
class RoutePlan:
    """실행 경로 표의 결과. `abstain_reason`이 있으면 도구 없이 그 사유로 기권한다."""

    route: Route
    case_label: str | None = None
    abstain_reason: RouteAbstainReason | None = None


def plan_route(decision: RouteDecision) -> RoutePlan:
    """실행 경로 표 — 판정과 라벨 수로 실제 경로를 정한다(순수 함수)."""
    labels = distinct_labels(decision.patient_labels)
    if decision.route == "OTHER" or len(labels) >= 2:
        return RoutePlan("OTHER", abstain_reason="out_of_scope")
    label = labels[0] if labels else None
    if decision.route == "PATIENT":
        if label is None:
            return RoutePlan("PATIENT", abstain_reason="patient_unresolved")
        return RoutePlan("PATIENT", case_label=label)
    # GUIDELINE·COMPOSITE는 라벨이 경로를 정한다 — 라벨이 있으면 특정 환자고, 없으면 조건이 붙은
    # 환자군 질문이다(분류기가 COMPOSITE로 기울던 유형, BE docs/specs/51 실측)
    if label is None:
        return RoutePlan("GUIDELINE")
    return RoutePlan("COMPOSITE", case_label=label)


def distinct_labels(labels: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    distinct: list[str] = []
    for raw in labels:
        label = raw.strip()
        key = label.casefold()
        if label and key not in seen:
            seen.add(key)
            distinct.append(label)
    return distinct


def strip_label(question: str, label: str) -> str:
    """근거 도구의 검색 입력 — 병명은 BE가 턴 스냅샷에서 붙이므로 여기서는 라벨만 지운다."""
    removed = re.sub(re.escape(label), "", question, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", removed).strip()
    return normalized or question


async def classify(model: BaseChatModel, question: str, *, trace_id: str) -> RouteDecision:
    try:
        reply = await model.bind(response_format=ROUTE_RESPONSE_FORMAT).ainvoke(
            [SystemMessage(content=CLASSIFIER_PROMPT), HumanMessage(content=question)],
            config={
                "run_name": "agent_route_classifier",
                "metadata": {"agent_step": "classify", "traceId": trace_id},
            },
        )
        return RouteDecision.model_validate_json(message_text(reply))
    except Exception as e:
        raise LlmCallError(f"분류 실패: {type(e).__name__}") from e
