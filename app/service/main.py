from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.service.config import ServiceSettings
from app.service.routes import router
from app.service.tracing import configure_tracing


def create_app(
    settings: ServiceSettings | None = None,
    *,
    backend_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """에이전트 서비스 앱을 만든다. 운영은 `uvicorn app.service.main:create_app --factory`로 띄운다.

    - `settings`: 생략하면 프로세스 환경변수에서 읽는다.
    - `backend_transport`: BE 호출의 HTTP 전송 계층. 생략하면 실제 네트워크를 쓴다 —
      테스트는 요청을 기록하는 가짜 전송을 꽂아 실제 BE 없이 돈다.

    기동(lifespan 진입) 시 추적 스위치를 고정한다.
    """
    resolved = settings if settings is not None else ServiceSettings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        configure_tracing(resolved.agent_tracing_enabled)
        yield

    app = FastAPI(title="cure-agent", lifespan=lifespan)
    app.include_router(router)
    return app
