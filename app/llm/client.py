from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr

from app.config import Settings


def _is_reasoning_model(model: str) -> bool:
    """gpt-5 계열·o-시리즈는 temperature 지정을 거부한다(400) — 전송 자체를 막는다."""
    name = model.split(":", 1)[-1]
    return name.startswith(("gpt-5", "o1", "o3", "o4"))


def _make(model: str, api_key: str) -> BaseChatModel:
    # api_key 명시 전달: pydantic-settings는 .env를 os.environ으로 내보내지 않는다.
    # temperature 0은 실험 재현성용이지만 추론 모델에는 보낼 수 없다 —
    # 운영 패리티(gpt-5.4-mini)를 택하면 temperature 재현성은 포기하는 트레이드오프다.
    kwargs: dict[str, Any] = {} if _is_reasoning_model(model) else {"temperature": 0}
    return init_chat_model(model, api_key=api_key or None, **kwargs)


def make_llm(settings: Settings) -> BaseChatModel:
    return _make(settings.agent_model, settings.openai_api_key)


def make_judge_llm(settings: Settings) -> BaseChatModel:
    return _make(settings.judge_model, settings.openai_api_key)


def make_embeddings(settings: Settings) -> OpenAIEmbeddings:
    # OpenAIEmbeddings는 문자열 키를 SecretStr로 검증·변환한다 — 미리 감싸도 결과가 같다
    api_key = SecretStr(settings.openai_api_key) if settings.openai_api_key else None
    return OpenAIEmbeddings(model=settings.embedding_model, api_key=api_key)
