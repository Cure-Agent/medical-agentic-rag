import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.service.backend import BackendClient, make_http_client
from app.service.config import ServiceSettings
from app.service.envelope import INTERNAL_ERROR, NOT_FOUND, failure, new_trace_id
from app.service.routes import router
from app.service.tracing import configure_tracing

logger = logging.getLogger(__name__)


def create_app(
    settings: ServiceSettings | None = None,
    *,
    backend_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """에이전트 서비스 앱을 만든다. 운영은 `uvicorn app.service.main:create_app --factory`로 띄운다.

    - `settings`: 생략하면 프로세스 환경변수에서 읽는다.
    - `backend_transport`: BE 호출의 HTTP 전송 계층. 생략하면 실제 네트워크를 쓴다 —
      테스트는 요청을 기록하는 가짜 전송을 꽂아 실제 BE 없이 돈다.

    기동(lifespan 진입) 시 추적 스위치를 고정하고 BE 클라이언트를 연다.
    """
    resolved = settings if settings is not None else ServiceSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_tracing(resolved.agent_tracing_enabled)
        async with make_http_client(resolved.be_origin, backend_transport) as http:
            app.state.backend = BackendClient(http)
            yield

    # 노출하는 경로는 라우터의 두 개뿐이다 — 문서 경로(/docs·/redoc·/openapi.json)를 열지 않는다
    app = FastAPI(
        title="cure-agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.include_router(router)
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
