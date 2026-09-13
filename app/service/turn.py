"""에이전트 턴 — 수락된 질문 하나를 네 갈래 중 하나로 끝까지 흘린다 (BE docs/specs/51).

스트림을 열기 전의 판정(본문 검증·선검사·수락)은 `routes.stream_message`가 하고, 이 모듈은
수락이 끝난 뒤의 SSE를 맡는다. 프레임은 `data: <JSON>\\n\\n`, 하트비트는 `: ping\\n\\n`이다.

**이벤트 흐름** (BE §8 이벤트 계약 + `agent.progress`)

    공통  message.accepted{requestId, userMessageId, assistantMessageId}
          → 분류 → agent.progress{stage: "routed", route}
    지침  지침 도구 SSE의 이벤트를 받은 순서·내용 그대로 흘린다 — 완결을 부르지 않는다
    환자  환자 도구 → agent.progress{stage: "patient_loaded"} → answer.delta*
          → [완결 COMPLETED] → answer.completed
    복합  환자 도구 → agent.progress{stage: "patient_loaded"} → 근거 도구 SSE:
          retrieval.started → retrieval.progress* → (evidence.gated가 통과면 answer.started)
          → retrieval.evidence×N → retrieval.completed → answer.delta*(판정 뒤)
          → [완결] → answer.completed | answer.abstained
    기타  [완결 ABSTAINED] → answer.abstained          (환자·복합의 라벨 해석 실패도 같다)

- `route`는 실행 경로(`GUIDELINE`·`PATIENT`·`COMPOSITE`·`OTHER`)다 — 분류기 판정이 아니라
  `routing`의 실행 경로 표를 거친 값이다.
- `answer.delta`는 `{messageId: assistantMessageId, seq, delta}`이고 `seq`는 턴마다 0부터 연속이다.
- `evidence.gated`는 브라우저로 흘리지 않는다. `abstainReason`이 null이면 `answer.started
  {evidenceCount}`(그 이벤트의 `evidenceCount`)로 바꿔 첫 `retrieval.evidence`보다 앞에 보내고,
  사유가 있으면 합성 없이 그 사유로 기권 완결한다.
- 종결 이벤트는 **완결 응답을 받은 뒤에** 보낸다. `answer.completed{message}`의 `message`는 완결
  응답 봉투의 `data`이고, `answer.abstained{message, reason, missingInformation: []}`의 `reason`은
  그 메시지의 `abstainReason` 문장이다.

**BE 내부 API** (`/api/v1/internal/agent/…`, 헤더는 전부 받은 Cookie 원문 + 받았을 때만 CSRF)

    지침 도구  POST turns/{assistantMessageId}/guideline-answer   {classifierVersion}
    환자 도구  POST turns/{assistantMessageId}/patient            {caseLabel}
    근거 도구  POST turns/{assistantMessageId}/guideline-evidence {query}
    완결       POST turns/{assistantMessageId}/finish             아래

완결 본문 — `classifierVersion`은 모든 완결에 싣고, `route`는 경로가 정해진 뒤에만 싣는다.

    COMPLETED(환자)   {status, route, classifierVersion, content, generation}
    COMPLETED(복합)   {status, route, classifierVersion, content, citations, generation}
    ABSTAINED         {status, route, classifierVersion, abstainReason}
                      + generation — 합성 LLM이 판정으로 기권했을 때(insufficient_evidence)만
    FAILED·CANCELLED  {status, route?, classifierVersion}

- `content`는 그 턴에 흘린 `answer.delta`를 이은 전체 텍스트다.
- 기권 사유: 기타·라벨 2개 이상 `out_of_scope` · 환자 라벨 0개와 환자 도구 `NOT_FOUND`·`AMBIGUOUS`
  `patient_unresolved` · 근거 게이트 기권은 `evidence.gated.abstainReason` 그대로 · 합성 판정
  `insufficient_evidence`.

**실패는 턴을 닫는다** — 지침 경로는 지침 도구가 턴을 닫으므로 어떤 경우에도 완결을 부르지 않는다.
나머지(경로가 아직 정해지지 않은 경우 포함)는 FAILED 완결을 먼저 부른 뒤 `error{code, message,
retryable, traceId}`를 보내고 스트림을 닫는다. `traceId`는 이 요청의 traceId다.

- 분류·합성 LLM의 예외, 분류 응답을 해석하지 못함 → `LLM_UNAVAILABLE`, retryable true
- 실행 상한(`run_deadline`, 요청 도착부터 잰다) 초과 → `LLM_TIMEOUT`, retryable true.
  지침 경로는 지침 도구 스트림을 끊는다(BE가 CANCELLED로 정리한다)
- BE 도구·완결에서 응답을 받지 못함(연결 실패·시간 초과·종결 이벤트 전에 끝난 SSE)
  → `AGENT_BACKEND_UNAVAILABLE`, retryable true
- BE 도구가 2xx가 아닌 봉투로 답함 → 그 봉투의 code·message, retryable은 code가
  `LLM_UNAVAILABLE`·`LLM_TIMEOUT`일 때만 true
- 지침·근거 도구가 보낸 `error` 이벤트는 받은 그대로 흘린다(근거 도구면 그 전에 FAILED 완결)

**하트비트** — SSE가 열린 뒤 `heartbeat_interval`초 동안 보낼 프레임이 없으면 `: ping`을 보낸다.

**끊김** — 클라이언트가 끊으면 실행을 멈춘다. 지침 경로는 지침 도구 스트림을 끊고, 나머지는
CANCELLED로 완결한다. 정리가 끝나야 ASGI 호출이 반환된다.
"""

# 실행 상한이자 선검사 기준 (BE docs/specs/51 「실행 도중 access 만료」) — BE 스트림 LLM 상한
# 120초에 분류·도구 여유를 더한 값. 남은 수명이 이보다 짧은 토큰은 수락 전에 401로 돌려보낸다
RUN_DEADLINE_SECONDS = 150.0
# BE §8 전송 규약과 같은 주기 — 프록시 idle timeout으로 스트림이 끊기지 않게 한다
HEARTBEAT_INTERVAL_SECONDS = 15.0
