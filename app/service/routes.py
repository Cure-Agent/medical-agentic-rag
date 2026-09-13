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


@router.post("/conversations/{conversation_id}/messages/stream")
async def stream_message(conversation_id: str, request: Request) -> Response:
    """질문 하나를 네 갈래로 나눠 끝까지 흘린다 (BE docs/specs/51).

    **스트림을 열기 전에 셋을 거른다** — 실패는 §10.1 봉투다.

    1. 본문 검증 — `content` 1~4000자 · `clientRequestId` 1~100자 · `responseLang` `ko`|`en`(선택).
       어기면 422 `VALIDATION_FAILED`.
    2. access 토큰 선검사 — `access_token` 쿠키를 서명 검증 없이 JWT로 읽어 `exp`·`iat`(초)를 본다.
       남은 수명(`exp − 지금`)이 실행 상한(`run_deadline`) 미만이면 401 `AUTH_TOKEN_EXPIRED`이고
       BE를 부르지 않는다. 쿠키가 없거나, JWT로 읽히지 않거나, `exp`·`iat` 중 하나라도 없거나,
       전체 수명(`exp − iat`)이 상한 이하이면 검사하지 않는다 — 인증 판정은 수락에서 BE가 한다.
    3. 수락 — BE `POST /api/v1/internal/agent/conversations/{conversationId}/turns`에
       `{content, clientRequestId, responseLang}`(`responseLang`은 요청에 있을 때만)을 싣는다.
       헤더는 받은 Cookie 원문과, 받았을 때만 `X-CSRF-Protection`이다. BE가 201이 아닌 응답을
       주면 그 상태·본문을 그대로 돌려주고, 응답을 못 받으면 502 `AGENT_BACKEND_UNAVAILABLE`이다.

    수락 뒤는 SSE(`text/event-stream`)다 — 이벤트 흐름·실패·끊김은 `app/service/turn.py`.
    """
    raise NotImplementedError
