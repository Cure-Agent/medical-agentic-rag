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
  기다리는 시간에는 상한(`FLUSH_TIMEOUT_SECONDS`)이 있다: 닿지 않는 엔드포인트에서 flush는 재시도로
  12초를 붙잡고(실측) 컨테이너 정지 유예(10초)를 넘긴다 — 추적을 잃는 것이 종료가 끌리는 것보다
  낫다.

SDK 환경변수(`LANGSMITH_TRACING`·`LANGCHAIN_TRACING_V2`)는 한쪽 네임스페이스의 `true`로 켜지고
`false`로 끌 수 없으므로, 켜고 끄는 결정은 기동 시 이 스위치 하나가 전역으로 고정한다.
"""

import logging
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import langsmith
from langsmith.run_helpers import tracing_context

logger = logging.getLogger(__name__)

TRACE_PROJECT = "cure-agent"
# 숨김 클라이언트가 남기는 메타데이터 — 토큰 수·모델·노드·traceId
# (BE docs/specs/51 「추적 숨김 범위」)
HIDDEN_METADATA_KEYS = frozenset(
    {"usage_metadata", "ls_provider", "ls_model_name", "ls_model_type", "agent_step", "traceId"}
)
FLUSH_TIMEOUT_SECONDS = 5.0
# LangChain이 싣는 오류는 `repr(예외)` + 트레이스백이다 — 맨 앞의 예외 클래스 이름만 남긴다
_EXCEPTION_NAME = re.compile(r"[A-Za-z_][\w.]*(?=[(:\n]|$)")


@dataclass(frozen=True)
class AgentTracing:
    """켜졌을 때만 클라이언트가 있다 — 보이는 쪽(분류기·지침)과 분기 뒤 숨김 쪽."""

    visible: langsmith.Client | None = None
    hidden: langsmith.Client | None = None


def configure_tracing(agent_tracing_enabled: str) -> AgentTracing:
    """기동 시 LangSmith SDK 전역 추적 스위치를 고정한다 — 정확히 `"true"`일 때만 켠다.

    끌 때도 `enabled=False`를 명시로 건다 — 걸지 않으면 SDK가 환경변수로 폴백해 `.env` 한 줄이
    조용히 추적을 켠다. 전역 스위치는 어느 스레드·태스크에서 걸어도 프로세스 전체에 적용된다
    (langsmith 0.11.0 실측). SDK를 올려 우선순위가 바뀌면 tests/test_agent_tracing.py가
    먼저 알린다.
    """
    if agent_tracing_enabled != "true":
        langsmith.configure(enabled=False)
        return AgentTracing()
    visible = langsmith.Client()
    langsmith.configure(enabled=True, client=visible, project_name=TRACE_PROJECT)
    # hide_*를 코드가 True로 고정한다 — 생략하면 LANGSMITH_HIDE_*=false가 숨김을 푼다.
    # 오류는 hide_*를 거치지 않고 anonymizer만 거친다(실측)
    hidden = langsmith.Client(hide_inputs=True, hide_outputs=True, anonymizer=hide_after_branch)
    return AgentTracing(visible=visible, hidden=hidden)


def hide_after_branch(value: dict[str, Any]) -> dict[str, Any]:
    """숨김 클라이언트의 anonymizer — 여기로 오는 것은 메타데이터와 오류다.

    입출력은 hide_*가 이미 비웠다. SDK는 오류를 `{"error": <문자열>}`로 감싸 넘긴다. anonymizer가
    있으면 함수형 `hide_metadata`는 무시되므로(실측) 메타데이터 허용목록도 여기서 건다.
    """
    error = value.get("error")
    if len(value) == 1 and isinstance(error, str):
        match = _EXCEPTION_NAME.match(error)
        return {"error": match.group(0) if match else "Exception"}
    return {key: item for key, item in value.items() if key in HIDDEN_METADATA_KEYS}


@contextmanager
def traced_turn(tracing: AgentTracing) -> Iterator[None]:
    """턴 실행 전체(분류기 포함)를 감싼다 — 이 안의 보이는 실행은 `cure-agent` 프로젝트로 간다.

    `langsmith.configure(project_name=...)`는 호출한 태스크의 contextvar와 전역에 쓰지만, LangChain
    트레이서는 프로젝트명을 **contextvar에서만** 읽고 전역 폴백이 없다(langsmith 0.11.0 ·
    langchain-core 1.5.6). lifespan 태스크에서 건 값이 uvicorn 요청 태스크로 이어지지 않아 분류기
    실행이 `default` 프로젝트로 갔다(2026-09-14 운영 관측). client·enabled는 전역 폴백이 있어
    멀쩡했다.
    추적이 꺼져 있으면 아무것도 하지 않는다.
    """
    raise NotImplementedError


@contextmanager
def hidden_branch(tracing: AgentTracing) -> Iterator[None]:
    """이 안의 LangChain 실행은 숨김 클라이언트로 간다 — 환자·복합 경로 전체를 감싼다."""
    if tracing.hidden is None:
        yield
        return
    # 프로젝트명을 함께 넘기지 않으면 숨김 실행만 기본 프로젝트(default)로 간다(실측)
    with tracing_context(client=tracing.hidden, project_name=TRACE_PROJECT):
        yield


def flush_tracing(tracing: AgentTracing) -> None:
    """두 클라이언트를 상한 안에서 flush한다.

    flush 스레드는 데몬이라 상한을 넘겨도 프로세스 종료를 붙잡지 않는다.
    """
    clients = [client for client in (tracing.visible, tracing.hidden) if client is not None]
    threads = [threading.Thread(target=client.flush, daemon=True) for client in clients]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + FLUSH_TIMEOUT_SECONDS
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    if any(thread.is_alive() for thread in threads):
        logger.warning("추적 flush가 %.0f초 안에 끝나지 않았다", FLUSH_TIMEOUT_SECONDS)
