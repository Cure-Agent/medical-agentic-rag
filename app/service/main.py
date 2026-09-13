import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from langchain_core.language_models import BaseChatModel
from prometheus_client import make_asgi_app
from starlette.exceptions import HTTPException
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from app.service.backend import BackendClient, make_http_client
from app.service.config import ServiceSettings
from app.service.envelope import INTERNAL_ERROR, NOT_FOUND, failure, new_trace_id
from app.service.llm import AgentModels
from app.service.routes import router
from app.service.tracing import configure_tracing, flush_tracing
from app.service.turn import HEARTBEAT_INTERVAL_SECONDS, RUN_DEADLINE_SECONDS, TurnSettings

logger = logging.getLogger(__name__)


class _ASGIEndpoint:
    """ASGI 앱을 **정확 경로** Route에 붙이기 위한 래퍼.

    `app.mount("/metrics", ...)`는 `/metrics/<하위>`만 매칭해 `/metrics` 자체를 307로
    `/metrics/`에 리디렉션한다(실측) — 수집기의 `metrics_path`는 `/metrics`이고 스펙은 그
    경로의 200을 요구한다. Route는 **함수** endpoint를 `func(request) -> response`로 감싸므로,
    ASGI로 취급되려면 함수가 아닌 콜러블이어야 한다. 정확 경로라 `/metrics/...` 하위가
    열리지 않는 이득도 따라온다 — 노출 표면은 좁을수록 좋다.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app(scope, receive, send)


def create_app(
    settings: ServiceSettings | None = None,
    *,
    backend_transport: httpx.AsyncBaseTransport | None = None,
    classifier_model: BaseChatModel | None = None,
    synthesis_model: BaseChatModel | None = None,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    run_deadline: float = RUN_DEADLINE_SECONDS,
) -> FastAPI:
    """에이전트 서비스 앱을 만든다. 운영은 `uvicorn app.service.main:create_app --factory`로 띄운다.

    - `settings`: 생략하면 프로세스 환경변수에서 읽는다.
    - `backend_transport`: BE 호출의 HTTP 전송 계층. 생략하면 실제 네트워크를 쓴다 —
      테스트는 요청을 기록하는 가짜 전송을 꽂아 실제 BE 없이 돈다.
    - `classifier_model`·`synthesis_model`: 분류기와 환자·복합 합성의 채팅 모델. 생략하면
      `settings.openai_api_key`로 `AGENT_MODEL`을 만든다 — 테스트는 가짜 채팅 모델을 꽂는다.
      호출 방식은 `app/service/routing.py`·`app/service/synthesis.py`가 정한다.
    - `heartbeat_interval`: 보낼 프레임이 없을 때 `: ping`을 보내는 주기(초).
    - `run_deadline`: 실행 상한(초)이자 access 토큰 선검사 기준 — 요청 도착부터 잰다.

    기동(lifespan 진입) 시 추적 스위치를 고정하고 BE 클라이언트를 연다. 종료 시 추적을 flush한다.
    """
    resolved = settings if settings is not None else ServiceSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tracing = configure_tracing(resolved.agent_tracing_enabled)
        try:
            async with make_http_client(resolved.be_origin, backend_transport) as http:
                backend = BackendClient(http)
                app.state.backend = backend
                app.state.turns = TurnSettings(
                    backend=backend,
                    models=AgentModels(
                        resolved.openai_api_key,
                        classifier=classifier_model,
                        synthesis=synthesis_model,
                    ),
                    tracing=tracing,
                    heartbeat_interval=heartbeat_interval,
                    run_deadline=run_deadline,
                )
                yield
        finally:
            await asyncio.to_thread(flush_tracing, tracing)

    # 사용자 API는 라우터의 것뿐이다 — 문서 경로(/docs·/redoc·/openapi.json)를 열지 않는다
    app = FastAPI(
        title="cure-agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.include_router(router)
    # 수집 표면 — 라우터 접두사(/api/v1/agent) **밖**의 맨 /metrics다. nginx는 그 접두사만
    # 에이전트로 보내므로 접두사 밖에 두면 차단 규칙을 더하지 않아도 외부에서 닿지 않는다
    # (BE docs/specs/50 「메트릭 경로」). 기본 레지스트리라 도메인 라벨이 0개다 —
    # 무엇을 셀지는 기능이 붙은 뒤에 정한다. 자격을 요구하지 않는다(수집기는 자격이 없다).
    app.router.routes.append(Route("/metrics", _ASGIEndpoint(make_asgi_app()), methods=["GET"]))
    app.add_exception_handler(HTTPException, _routing_error)
    app.add_exception_handler(Exception, _unexpected_error)
    return app


async def _routing_error(_request: Request, exc: Exception) -> JSONResponse:
    """라우팅 실패도 §10.1 봉투로 낸다.

    없는 경로·메서드는 BE(Express)처럼 404 `NOT_FOUND`로 수렴한다. 서비스 코드는
    HTTPException을 던지지 않으므로(실패 봉투를 직접 만든다) 그 밖의 상태는 결함으로 본다.
    """
    status = exc.status_code if isinstance(exc, HTTPException) else 500
    return failure(NOT_FOUND if status in (404, 405) else INTERNAL_ERROR, new_trace_id())


async def _unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    trace_id = new_trace_id()
    logger.error("[%s] 예상 밖 예외", trace_id, exc_info=exc)
    return failure(INTERNAL_ERROR, trace_id)
