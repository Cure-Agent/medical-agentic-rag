from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/v1/agent")


@router.get("/healthz")
async def healthz() -> JSONResponse:
    """프로세스 생존만 본다 — BE를 부르지 않는다."""
    raise NotImplementedError


@router.get("/me")
async def me(request: Request) -> JSONResponse:
    """받은 Cookie·CSRF 헤더를 그대로 BE `GET /api/v1/auth/me`에 넘겨 인증 판정을 돌려받는다."""
    raise NotImplementedError
