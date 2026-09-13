"""에이전트 LLM — 분류기와 합성 모두 BE와 같은 기본 모델이다 (BE docs/specs/51 「에이전트 LLM」).

외부 유료 경계라 LangChain `BaseChatModel` 뒤에 둔다 — 테스트는 `create_app`에 가짜 채팅 모델을
꽂아 실키 없이 돈다.
"""

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

AGENT_MODEL = "gpt-5.4-mini"
PROVIDER = "openai"
# BE llm.config.ts와 같은 출력 상한 — 추론 모델은 사고 토큰을 이 예산에서 함께 쓴다
MAX_OUTPUT_TOKENS = 4096
# BE §11의 LLM 첫 응답 상한과 같다. 실행 전체는 턴의 실행 상한(150초)이 따로 끊는다
REQUEST_TIMEOUT_SECONDS = 45.0


class LlmCallError(Exception):
    """분류·합성 LLM 호출이 실패했거나 응답을 해석하지 못했다 — 스트림에서 `LLM_UNAVAILABLE`이다."""


class AgentModels:
    """분류기·합성 채팅 모델의 출처.

    주입이 없으면 첫 사용 때 `AGENT_MODEL` 하나를 만들어 둘이 나눠 쓴다. 키가 없어도 앱은 뜬다 —
    healthz·metrics는 LLM과 무관하고, 스트림은 수락 뒤 `LLM_UNAVAILABLE`로 끝난다.
    """

    def __init__(
        self,
        api_key: str,
        *,
        classifier: BaseChatModel | None = None,
        synthesis: BaseChatModel | None = None,
    ) -> None:
        self._api_key = api_key
        self._classifier = classifier
        self._synthesis = synthesis
        self._default: BaseChatModel | None = None

    def classifier(self) -> BaseChatModel:
        return self._classifier if self._classifier is not None else self._default_model()

    def synthesis(self) -> BaseChatModel:
        return self._synthesis if self._synthesis is not None else self._default_model()

    def _default_model(self) -> BaseChatModel:
        if not self._api_key:
            raise LlmCallError("OPENAI_API_KEY가 설정되지 않았다")
        if self._default is None:
            self._default = ChatOpenAI(
                model=AGENT_MODEL,
                api_key=SecretStr(self._api_key),
                # 스트림 끝에 usage를 싣게 한다(stream_options.include_usage) — generation 토큰 수의
                # 원천이다
                stream_usage=True,
                max_completion_tokens=MAX_OUTPUT_TOKENS,
                timeout=REQUEST_TIMEOUT_SECONDS,
                max_retries=1,
            )
        return self._default


def message_text(message: AIMessage) -> str:
    return str(message.text)


def as_chunk(message: AIMessage) -> AIMessageChunk:
    """`bind()` 뒤 `astream`의 조각은 정적 타입이 `AIMessage`다 — 누적하려면 조각으로 좁힌다."""
    if isinstance(message, AIMessageChunk):
        return message
    return AIMessageChunk(
        content=message.content,
        usage_metadata=message.usage_metadata,
        response_metadata=message.response_metadata,
    )
