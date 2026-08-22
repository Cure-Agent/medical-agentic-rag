from contextlib import asynccontextmanager

from fastapi import FastAPI
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.api.routes import router
from app.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    # open=False: DB가 없어도 서버는 뜬다 — /ask가 접속 시점에 명확한 오류를 낸다
    app.state.pool = AsyncConnectionPool(
        settings.database_url,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    await app.state.pool.open(wait=False)
    app.state.agents = {}  # preset 이름 → 컴파일된 그래프 (요청 시 lazy 조립)
    yield
    await app.state.pool.close()


app = FastAPI(title="medical-agentic-rag", lifespan=lifespan)
app.include_router(router)
