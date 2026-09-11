"""§10.1 응답 봉투 — BE `ApiResponseDto`와 같은 모양으로 낸다.

FE에게 `/api/v1`은 하나의 API 표면이다. 코드·메시지의 단일 소스는 BE 레지스트리
(`error-code.registry.ts`·`success-code.registry.ts`)이고, 여기는 에이전트가 발신하는 것만
문자열로 미러링한다 (BE architecture.md §10.2).
"""

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class ResponseCode:
    code: str
    status: int
    message: str


SUCCESS = ResponseCode("SUCCESS", 200, "요청에 성공하였습니다.")
NOT_FOUND = ResponseCode("NOT_FOUND", 404, "대상을 찾을 수 없습니다.")
INTERNAL_ERROR = ResponseCode("INTERNAL_ERROR", 500, "서버 내부 오류가 발생했습니다.")
# 상류 실패라 502다(§10.1). BE는 던지지 않고, 에이전트가 BE에서 응답을 받지 못했을 때 발신한다
AGENT_BACKEND_UNAVAILABLE = ResponseCode(
    "AGENT_BACKEND_UNAVAILABLE", 502, "서버 응답을 받지 못했습니다. 잠시 후 다시 시도해주세요."
)

_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_trace_id() -> str:
    """요청당 traceId — ULID (BE §10.3).

    48비트 밀리초 시각 + 80비트 난수를 Crockford base32 26자로 쓴다.
    """
    value = (time.time_ns() // 1_000_000) << 80 | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD32[(value >> shift) & 0x1F] for shift in range(125, -1, -5))


def success(data: Any, trace_id: str) -> JSONResponse:
    return _envelope(SUCCESS, ok=True, data=data, trace_id=trace_id)


def failure(code: ResponseCode, trace_id: str) -> JSONResponse:
    return _envelope(code, ok=False, data=None, trace_id=trace_id)


def _envelope(code: ResponseCode, *, ok: bool, data: Any, trace_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=code.status,
        content={
            "success": ok,
            "code": code.code,
            "message": code.message,
            "data": data,
            "page": None,
            "timestamp": _now_iso(),
            "traceId": trace_id,
        },
    )


def _now_iso() -> str:
    # BE `new Date().toISOString()`과 같은 형식 — UTC·밀리초·Z
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
