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

from app.service.llm import AGENT_MODEL

# 턴의 분류기 버전 — 지침 도구 요청과 모든 완결에 실려 「왜 이 경로였나」를 재현한다(BE §5.7)
CLASSIFIER_VERSION = f"{AGENT_MODEL}/agent-route-v1"

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
