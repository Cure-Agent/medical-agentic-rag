import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.service.backend import BackendClient, BackendUnavailableError
from app.service.envelope import AGENT_BACKEND_UNAVAILABLE, failure, new_trace_id, success

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/agent")


@router.get("/healthz")
async def healthz() -> JSONResponse:
    """프로세스 생존만 본다 — BE를 부르지 않는다.

    BE를 보게 하면 BE 장애가 에이전트 재시작·배포 롤백으로 번진다
    (liveness와 readiness를 가르는 이유와 같다).
    """
    return success({"status": "ok"}, new_trace_id())


@router.get("/me")
async def me(request: Request) -> Response:
    """받은 Cookie·CSRF 헤더를 그대로 BE `GET /api/v1/auth/me`에 넘겨 인증 판정을 돌려받는다.

    - BE가 응답하면 그 판정(상태·봉투)을 그대로 돌려준다 — FE의 refresh → 재시도가
      401 하나에 걸려 있다.
    - 응답을 못 받으면 502 `AGENT_BACKEND_UNAVAILABLE`이다. 401로 뭉개면 BE 순단이
      FE의 refresh 실패 → 강제 로그아웃이 된다.
    - 성공 응답에는 `clinicianId`·`clinicId`만 싣는다 — `email`·`displayName`을
      에이전트 응답·추적 표면에 올릴 이유가 없다.
    """
    trace_id = new_trace_id()
    backend: BackendClient = request.app.state.backend
    try:
        result = await backend.get_me(
            cookie=request.headers.get("cookie"),
            csrf=request.headers.get("x-csrf-protection"),
        )
    except BackendUnavailableError as e:
        logger.warning("[%s] BE 응답 없음 — %s", trace_id, e)
        return failure(AGENT_BACKEND_UNAVAILABLE, trace_id)

    if result.status_code != 200:
        return Response(
            content=result.content, status_code=result.status_code, media_type=result.content_type
        )
    clinician = json.loads(result.content)["data"]
    return success(
        {"clinicianId": clinician["id"], "clinicId": clinician["clinic"]["id"]}, trace_id
    )
