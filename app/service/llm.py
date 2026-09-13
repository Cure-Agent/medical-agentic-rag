"""에이전트 LLM — 분류기와 합성 모두 BE와 같은 기본 모델이다 (BE docs/specs/51 「에이전트 LLM」).

외부 유료 경계라 LangChain `BaseChatModel` 뒤에 둔다 — 테스트는 `create_app`에 가짜 채팅 모델을
꽂아 실키 없이 돈다.
"""

AGENT_MODEL = "gpt-5.4-mini"
