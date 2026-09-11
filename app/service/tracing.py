def configure_tracing(agent_tracing_enabled: str) -> None:
    """기동 시 LangSmith SDK 전역 추적 스위치를 고정한다 — 정확히 `"true"`일 때만 켠다.

    SDK 환경변수(`LANGSMITH_TRACING`·`LANGCHAIN_TRACING_V2`)는 한쪽 네임스페이스의 `true`로
    켜지고 `false`로 끌 수 없으므로, 켜고 끄는 결정을 이 스위치 하나가 쥔다
    (BE docs/specs/49 「추적 스위치」).
    """
