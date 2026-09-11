"""BE 호출이 받은 자격만 싣는다 — 구현측 회귀 가드 (spec 49 수용 기준 밖, 동결 대상 아님).

스펙 49 범위표는 BE 클라이언트가 「받은 Cookie는 그대로, X-CSRF-Protection은 받았을 때만
싣고 그 밖의 요청 헤더는 넘기지 않는다」고 정한다. 수용 기준은 요청 하나 안의 전달만
단언하므로, 여기서는 그 문장이 **요청 사이에서도**, **그 밖의 헤더에 대해서도** 성립하는지를
잠근다.

에이전트는 httpx 클라이언트 하나를 모든 사용자가 나눠 쓴다. httpx 기본 쿠키 저장소는 BE의
Set-Cookie를 담아 두었다가 Cookie 없이 온 다음 요청에 싣는다(2026-09-11 실측) — 다른 사용자의
자격으로 BE를 부르게 된다.
"""

from collections.abc import Iterator

import httpx
import langsmith
import pytest
from fastapi.testclient import TestClient

from app.service.config import ServiceSettings
from app.service.main import create_app

BE_ME_SUCCESS = {
    "success": True,
    "code": "SUCCESS",
    "message": "ok",
    "data": {
        "id": "clinician-1",
        "email": "a@clinic.test",
        "displayName": "A",
        "clinic": {"id": "clinic-1", "name": "Clinic"},
        "verificationStatus": "VERIFIED",
    },
    "page": None,
    "timestamp": "2026-09-11T00:00:00.000Z",
    "traceId": "01J00000000000000000000000",
}


@pytest.fixture
def recorded() -> list[httpx.Request]:
    return []


@pytest.fixture
def client(recorded: list[httpx.Request]) -> Iterator[TestClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        # 다음 요청으로 새는지 보려고 BE가 쿠키를 발급하는 상황을 만든다
        return httpx.Response(
            200, headers={"set-cookie": "access_token=LEAKED; Path=/"}, json=BE_ME_SUCCESS
        )

    app = create_app(
        ServiceSettings(be_origin="http://be.test", agent_tracing_enabled=""),
        backend_transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as test_client:
        yield test_client
    langsmith.configure(enabled=None)


def test_backend_set_cookie_does_not_reach_another_request(
    client: TestClient, recorded: list[httpx.Request]
):
    """앞 사용자 요청의 BE Set-Cookie가 Cookie 없이 온 다음 요청의 BE 호출에 실리지 않는다."""
    client.get("/api/v1/agent/me", headers={"Cookie": "access_token=AAA"})
    client.get("/api/v1/agent/me")

    assert len(recorded) == 2
    assert recorded[0].headers.get("cookie") == "access_token=AAA"
    assert "cookie" not in recorded[1].headers


def test_other_request_headers_are_not_forwarded(
    client: TestClient, recorded: list[httpx.Request]
):
    """Cookie·X-CSRF-Protection 밖의 요청 헤더는 BE 호출에 넘기지 않는다."""
    client.get(
        "/api/v1/agent/me",
        headers={
            "Cookie": "access_token=AAA",
            "Authorization": "Bearer forwarded-probe",
            "X-Forwarded-For": "203.0.113.7",
            "X-Probe": "forwarded-probe",
        },
    )

    assert len(recorded) == 1
    sent = recorded[0].headers
    assert "authorization" not in sent
    assert "x-forwarded-for" not in sent
    assert "x-probe" not in sent
