"""BE 클라이언트 — 받은 자격을 그대로 넘겨 BE의 인증 판정을 돌려받는다.

에이전트는 자격을 만들지 않는다. Cookie는 받은 원문 그대로, `X-CSRF-Protection`은
**받았을 때만** 싣고 그 밖의 요청 헤더는 넘기지 않는다. 에이전트가 CSRF 헤더를 스스로
붙이면 헤더 없는 교차 출처 요청이 에이전트를 경유해 BE CSRF 가드를 통과한다
(BE docs/specs/49 「자격 전달」). 내부 API(BE docs/specs/51)도 같은 자격으로 부른다 — 스코프·CSRF
판정은 BE에 남는다.
"""

import json
import re
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, cast

import httpx

from app.service.sse import SseEvent, read_events

AUTH_ME_PATH = "/api/v1/auth/me"
INTERNAL_AGENT_PATH = "/api/v1/internal/agent"
# BE 순단을 사용자 대기로 오래 끌지 않는다 — 넘기면 502로 끝낸다
BACKEND_TIMEOUT = httpx.Timeout(5.0)
# 내부 SSE는 LLM이 느린 동안에도 BE가 15초마다 `: ping`을 보낸다(BE §8) — 그 두 배 동안 아무것도
# 오지 않으면 BE가 응답하지 않는 것으로 본다
STREAM_TIMEOUT = httpx.Timeout(5.0, read=30.0)
# 내부 API 경로에 박히는 값의 모양 — BE id는 ULID다. 사용자가 보낸 대화 id가 경로 구분자·점
# 세그먼트로 다른 내부 경로를 가리키지 못하게 한다
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,100}")


class BackendUnavailableError(Exception):
    """BE에서 응답을 받지 못했다 — 연결 실패·시간 초과·응답 전 끊김."""


class BackendProtocolError(Exception):
    """BE가 응답은 했으나 계약 밖의 모양이다 — 에이전트와 BE의 계약이 어긋났다는 결함 신호."""


@dataclass(frozen=True)
class Credentials:
    """요청에서 받은 자격 원문 — BE 호출에 실을 것은 이 둘뿐이다."""

    cookie: str | None
    csrf: str | None

    def headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.cookie is not None:
            headers["Cookie"] = self.cookie
        if self.csrf is not None:
            headers["X-CSRF-Protection"] = self.csrf
        return headers


@dataclass(frozen=True)
class BackendResponse:
    status_code: int
    content: bytes
    content_type: str | None

    def data(self) -> dict[str, Any]:
        """§10.1 봉투의 `data` 객체."""
        try:
            body: object = json.loads(self.content)
        except ValueError as e:
            raise BackendProtocolError("봉투가 JSON이 아니다") from e
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise BackendProtocolError("봉투에 data 객체가 없다")
        return cast(dict[str, Any], data)


@dataclass(frozen=True)
class BackendStream:
    """내부 SSE 호출의 결과 — 스트림을 열었거나(`events`), 열기 전에 봉투로 답했다(`rejection`)."""

    rejection: BackendResponse | None
    events: AsyncIterator[SseEvent]


def is_path_segment(value: str) -> bool:
    return _PATH_SEGMENT.fullmatch(value) is not None


def make_http_client(
    be_origin: str, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=be_origin,
        transport=transport,
        timeout=BACKEND_TIMEOUT,
        follow_redirects=False,
        # 모든 사용자가 이 클라이언트 하나를 나눠 쓴다. 기본 쿠키 저장소는
        # BE의 Set-Cookie를 담아 두었다가 Cookie 없이 온 다음 요청(다른 사용자)에
        # 싣는다(실측) — 어떤 쿠키도 저장하지 않는다
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
    )


class BackendClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def get_me(self, credentials: Credentials) -> BackendResponse:
        return await self._send("GET", AUTH_ME_PATH, None, credentials)

    async def accept_turn(
        self, conversation_id: str, body: dict[str, object], credentials: Credentials
    ) -> BackendResponse:
        path = f"{INTERNAL_AGENT_PATH}/conversations/{conversation_id}/turns"
        return await self._send("POST", path, body, credentials)

    async def resolve_patient(
        self, assistant_message_id: str, case_label: str, credentials: Credentials
    ) -> BackendResponse:
        path = f"{INTERNAL_AGENT_PATH}/turns/{assistant_message_id}/patient"
        return await self._send("POST", path, {"caseLabel": case_label}, credentials)

    async def finish_turn(
        self, assistant_message_id: str, body: dict[str, object], credentials: Credentials
    ) -> BackendResponse:
        path = f"{INTERNAL_AGENT_PATH}/turns/{assistant_message_id}/finish"
        return await self._send("POST", path, body, credentials)

    def guideline_answer(
        self, assistant_message_id: str, classifier_version: str, credentials: Credentials
    ) -> AbstractAsyncContextManager[BackendStream]:
        path = f"{INTERNAL_AGENT_PATH}/turns/{assistant_message_id}/guideline-answer"
        return self._stream(path, {"classifierVersion": classifier_version}, credentials)

    def guideline_evidence(
        self, assistant_message_id: str, query: str, credentials: Credentials
    ) -> AbstractAsyncContextManager[BackendStream]:
        path = f"{INTERNAL_AGENT_PATH}/turns/{assistant_message_id}/guideline-evidence"
        return self._stream(path, {"query": query}, credentials)

    async def _send(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None,
        credentials: Credentials,
    ) -> BackendResponse:
        try:
            response = await self._http.request(
                method, path, json=body, headers=credentials.headers()
            )
        except httpx.TransportError as e:
            raise BackendUnavailableError(repr(e)) from e
        return BackendResponse(
            status_code=response.status_code,
            content=response.content,
            content_type=response.headers.get("content-type"),
        )

    @asynccontextmanager
    async def _stream(
        self, path: str, body: dict[str, object], credentials: Credentials
    ) -> AsyncIterator[BackendStream]:
        try:
            async with self._http.stream(
                "POST", path, json=body, headers=credentials.headers(), timeout=STREAM_TIMEOUT
            ) as response:
                content_type = response.headers.get("content-type")
                if response.status_code != 200 or not (content_type or "").startswith(
                    "text/event-stream"
                ):
                    # SSE를 열기 전의 실패는 봉투다(없는 턴 404·닫힌 턴 409 등)
                    content = await response.aread()
                    yield BackendStream(
                        rejection=BackendResponse(response.status_code, content, content_type),
                        events=_no_events(),
                    )
                    return
                yield BackendStream(rejection=None, events=_events(response))
        except httpx.TransportError as e:
            raise BackendUnavailableError(repr(e)) from e


async def _events(response: httpx.Response) -> AsyncIterator[SseEvent]:
    try:
        async for event in read_events(response.aiter_lines()):
            yield event
    except httpx.TransportError as e:
        raise BackendUnavailableError(repr(e)) from e


async def _no_events() -> AsyncIterator[SseEvent]:
    return
    yield
