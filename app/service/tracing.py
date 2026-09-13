"""LangSmith 추적 — 스위치와 분기 뒤 숨김 (BE docs/specs/49·51).

**추적은 `AGENT_TRACING_ENABLED`가 정확히 `"true"`일 때만 켜진다** — 아래는 켜졌을 때의 계약이다.
엔드포인트·키는 SDK 환경변수(`LANGSMITH_ENDPOINT`·`LANGSMITH_API_KEY`)를 쓰고, 프로젝트명은
`cure-agent`로 고정한다.

- **분류기와 지침 경로는 보인다.** 분류기 실행의 입력에 질문 원문이 남는 것은 BE §14 「프롬프트
  원문 로그 금지」의 명시적 예외다 — 경로가 정해지기 전이라 숨길 기준이 없고, 오분류를 트레이스에서
  바로 본다. 지침·기타 경로는 분기 뒤 에이전트 LLM 실행이 없다.
- **경로가 환자·복합으로 정해진 뒤의 LangChain 실행은 전부 숨김 클라이언트로 돈다.** 숨길 실행을
  목록으로 고르면 빠진다 — 환자 도구 출력만 숨기면 같은 기록이 합성 프롬프트로 다시 실린다.
  숨김 클라이언트는 입력·출력을 비우고(환경변수 `LANGSMITH_HIDE_*`와 무관하게 코드가 고정),
  메타데이터는 허용목록(`usage_metadata`·`ls_provider`·`ls_model_name`·`ls_model_type`·
  `agent_step`·`traceId`)만, 오류는 예외 클래스 이름만 남긴다. 실행 자체는 끄지 않는다 — 토큰 수와
  모델은 남는다.
- **자격은 추적 표면에 싣지 않는다.** Cookie·CSRF는 어떤 LangChain 입력·`configurable`·
  메타데이터에도 넣지 않는다 — `configurable`은 숨김과 무관하게 메타데이터로 샌다(langsmith 0.11.0
  실측).
- 앱 종료(lifespan 종료) 시 두 클라이언트를 flush한다 — 종료 뒤에는 보낸 페이로드가 모두 나가 있다.

SDK 환경변수(`LANGSMITH_TRACING`·`LANGCHAIN_TRACING_V2`)는 한쪽 네임스페이스의 `true`로 켜지고
`false`로 끌 수 없으므로, 켜고 끄는 결정은 기동 시 이 스위치 하나가 전역으로 고정한다.
"""

import langsmith


def configure_tracing(agent_tracing_enabled: str) -> None:
    """기동 시 LangSmith SDK 전역 추적 스위치를 고정한다 — 정확히 `"true"`일 때만 켠다.

    끌 때도 `enabled=False`를 명시로 건다 — 걸지 않으면 SDK가 환경변수로 폴백해 `.env` 한 줄이
    조용히 추적을 켠다. 전역 스위치는 어느 스레드·태스크에서 걸어도 프로세스 전체에 적용된다
    (langsmith 0.11.0 실측). SDK를 올려 우선순위가 바뀌면 tests/test_agent_tracing.py가
    먼저 알린다.
    """
    langsmith.configure(enabled=agent_tracing_enabled == "true")
