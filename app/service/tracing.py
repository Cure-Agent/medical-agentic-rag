import langsmith


def configure_tracing(agent_tracing_enabled: str) -> None:
    """기동 시 LangSmith SDK 전역 추적 스위치를 고정한다 — 정확히 `"true"`일 때만 켠다.

    SDK 환경변수(`LANGSMITH_TRACING`·`LANGCHAIN_TRACING_V2`)는 한쪽 네임스페이스의 `true`로
    켜지고 `false`로 끌 수 없으므로, 켜고 끄는 결정을 이 스위치 하나가 쥔다
    (BE docs/specs/49 「추적 스위치」).

    끌 때도 `enabled=False`를 명시로 건다 — 걸지 않으면 SDK가 환경변수로 폴백해 `.env` 한 줄이
    조용히 추적을 켠다. 전역 스위치는 어느 스레드·태스크에서 걸어도 프로세스 전체에 적용된다
    (langsmith 0.11.0 실측). SDK를 올려 우선순위가 바뀌면 tests/test_agent_tracing.py가
    먼저 알린다.
    """
    langsmith.configure(enabled=agent_tracing_enabled == "true")
