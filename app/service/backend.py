"""BE 클라이언트 — 받은 자격을 그대로 넘겨 BE의 인증 판정을 돌려받는다.

에이전트는 자격을 만들지 않는다. Cookie는 받은 원문 그대로, `X-CSRF-Protection`은
**받았을 때만** 싣고 그 밖의 요청 헤더는 넘기지 않는다. 에이전트가 CSRF 헤더를 스스로
붙이면 헤더 없는 교차 출처 요청이 에이전트를 경유해 BE CSRF 가드를 통과한다
(BE docs/specs/49 「자격 전달」).
"""

from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

AUTH_ME_PATH = "/api/v1/auth/me"
# BE 순단을 사용자 대기로 오래 끌지 않는다 — 넘기면 502로 끝낸다
BACKEND_TIMEOUT = httpx.Timeout(5.0)


class BackendUnavailableError(Exception):
    """BE에서 응답을 받지 못했다 — 연결 실패·시간 초과·응답 전 끊김."""


@dataclass(frozen=True)
class BackendResponse:
    status_code: int
    content: bytes
    content_type: str | None


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

    async def get_me(self, *, cookie: str | None, csrf: str | None) -> BackendResponse:
        headers: dict[str, str] = {}
        if cookie is not None:
            headers["Cookie"] = cookie
        if csrf is not None:
            headers["X-CSRF-Protection"] = csrf
        try:
            response = await self._http.get(AUTH_ME_PATH, headers=headers)
        except httpx.TransportError as e:
            raise BackendUnavailableError(repr(e)) from e
        return BackendResponse(
            status_code=response.status_code,
            content=response.content,
            content_type=response.headers.get("content-type"),
        )
