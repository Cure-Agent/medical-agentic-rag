"""환자·복합 경로의 답변 합성 (BE docs/specs/51 「복합의 지침 근거」·「복합 판정 방식」).

**합성 입력** — 두 경로 모두 질문 원문과 환자 도구가 돌려준 기록 필드 중 값이 있는 것을 싣는다:
`caseLabel`·`age`·`sex`·`bmi`·`heightCm`·`weightKg`·`waistCm`·`diagnoses`·`medications`·
`allergies`(배열은 항목마다)·`clinicalNotes`. 복합은 여기에 `retrieval.evidence` 프레임마다 마커
`[n]`(프레임 순서, 1부터)과 그 근거의 `excerpt`(원문)·`guidelineTitle`·`sectionPath`를 더한다.

**환자** — `synthesis_model.astream(messages)`(바인딩 인자 없음). 텍스트가 빈 조각은 건너뛰고,
텍스트가 있는 조각마다 순서대로 `answer.delta` 하나를 보낸다.

**복합** — `synthesis_model.bind(response_format=COMPOSITE_RESPONSE_FORMAT).astream(messages)`.
조각을 이은 텍스트가 `{"insufficientEvidence": <bool>, "answer": <문자열>}`이다(필드 순서가 계약).
판정 선행 증분 파싱(BE §40 이식): `insufficientEvidence`가 false로 확정되기 전에는 델타를 내지 않고,
확정 뒤에는 해석된 `answer` 문자열의 증분을 델타로 흘린다. true면 델타 없이 `insufficient_evidence`
기권이다. 판정 전에 스트림이 끝나거나 JSON이 깨지면 LLM 실패이고, 판정 뒤에 깨지면 그때까지의
답으로 완결한다.

**인용** — 복합 COMPLETED의 `citations`는 최종 답변에 등장한 마커 `[n]` 중 1 ≤ n ≤ 받은
`retrieval.evidence` 프레임 수인 것을 n 오름차순으로 한 번씩, `{marker: n, evidenceId: n번째
프레임의 evidence.id}`로 싣는다.

**generation** — 합성 LLM 호출 1회의 기록:
`provider` "openai" · `model` 스트림 응답 메타데이터의 `model_name`(없으면 `AGENT_MODEL`) ·
`promptVersion` · `latencyMs` · `inputTokens`·`outputTokens` 조각들의 `usage_metadata`
합계(없으면 텍스트 길이 / 3 올림 추정). 복합은 `evidence.gated`의 `retrievalPolicyVersion`·
`searchQuestion`을 더하고, 환자는 둘 다 싣지 않는다(검색하지 않은 생성).
"""

COMPOSITE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "agent_composite_answer",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "insufficientEvidence": {"type": "boolean"},
                "answer": {"type": "string"},
            },
            "required": ["insufficientEvidence", "answer"],
            "additionalProperties": False,
        },
    },
}
