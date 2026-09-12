"""명세 50의 AGENT 수용 기준을 HTTP 응답 계약과 기존 경로 회귀 가드로 검증한다."""

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

import httpx
import langsmith
import pytest
from fastapi.testclient import TestClient

from app.service.config import ServiceSettings
from app.service.main import create_app

if TYPE_CHECKING:
    # starlette 1.6의 TestClient는 타입 검사 시 httpx2 응답을 돌려준다.
    import httpx2


class RecordingBackend:
    """모든 BE 요청을 기록하고 테스트가 지정한 응답이나 전송 예외를 돌려준다."""

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)


@contextmanager
def _service_client(
    backend: RecordingBackend,
    *,
    be_origin: str = "http://backend.test:4312",
) -> Iterator[TestClient]:
    """추적을 끄고 가짜 BE를 주입한 앱의 lifespan을 실행한다."""
    app = create_app(
        ServiceSettings(be_origin=be_origin, agent_tracing_enabled=""),
        backend_transport=backend.transport,
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        # 앱이 바꾼 전역 추적 설정을 다음 테스트에 남기지 않는다.
        langsmith.configure(enabled=None)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """실제 네트워크 없이 각 테스트에 새 서비스 클라이언트를 제공한다."""
    backend = RecordingBackend(lambda _request: httpx.Response(200, json={}))
    with _service_client(backend) as test_client:
        yield test_client


def _json_object(response: "httpx2.Response") -> dict[str, object]:
    """응답의 JSON 파싱과 객체 형태를 확인하고 타입을 좁힌다."""
    payload: object = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def test_metrics_returns_ok(client: TestClient) -> None:
    """기준 1: 맨 /metrics의 GET 요청은 200이다."""
    response = client.get("/metrics")

    assert response.status_code == 200


def test_metrics_content_type_is_plain_text(client: TestClient) -> None:
    """기준 2: 성공한 메트릭 응답의 Content-Type은 text/plain으로 시작한다."""
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_metrics_contains_python_info_sample(client: TestClient) -> None:
    """기준 3: 주석이 아닌 python_info 샘플 줄이 존재한다."""
    response = client.get("/metrics")

    assert response.status_code == 200
    # 이름 경계와 샘플 값의 존재만 확인하며 라벨 값이나 지표 값은 고정하지 않는다.
    assert any(
        re.fullmatch(r"python_info(?:\{[^\n]*\})?\s+\S+(?:\s+\S+)?\s*", line)
        for line in response.text.splitlines()
        if not line.startswith("#")
    )


def test_metrics_body_is_not_json(client: TestClient) -> None:
    """기준 4: 비어 있지 않은 성공 메트릭 본문은 JSON으로 파싱되지 않는다."""
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.text.strip()
    with pytest.raises(json.JSONDecodeError):
        json.loads(response.text)


def test_metrics_body_has_no_success_key(client: TestClient) -> None:
    """기준 4: 비어 있지 않은 성공 메트릭 본문에 봉투의 success가 없다."""
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.text.strip()
    assert "success" not in response.text


def test_metrics_accepts_request_without_credentials(client: TestClient) -> None:
    """기준 5: Cookie와 CSRF 헤더를 싣지 않은 메트릭 요청도 200이다."""
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.request.method == "GET"
    assert "cookie" not in response.request.headers
    assert "x-csrf-protection" not in response.request.headers


def test_metrics_does_not_call_unreachable_backend() -> None:
    """기준 6: BE 연결 불가 설정에서도 메트릭은 200이며 BE 호출은 0건이다."""

    def raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic backend failure", request=request)

    backend = RecordingBackend(raise_connect_error)
    with _service_client(backend, be_origin="http://127.0.0.1:9") as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert len(backend.requests) == 0


def test_metrics_is_not_mounted_under_agent_prefix(client: TestClient) -> None:
    """기준 7: 프록시되는 에이전트 접두사 안의 메트릭 경로는 404다."""
    response = client.get("/api/v1/agent/metrics")

    assert response.status_code == 404


def test_healthz_preserves_ok_status(client: TestClient) -> None:
    """기준 9: 기존 healthz는 200과 data.status=ok를 유지한다."""
    response = client.get("/api/v1/agent/healthz")

    assert response.status_code == 200
    body = _json_object(response)
    data = body.get("data")
    assert isinstance(data, dict)
    assert data.get("status") == "ok"


def test_missing_route_preserves_not_found_envelope(client: TestClient) -> None:
    """기준 10: 없는 경로는 기존 404 NOT_FOUND 봉투를 유지한다."""
    response = client.get("/api/v1/agent/no-such-path")

    assert response.status_code == 404
    assert _json_object(response).get("code") == "NOT_FOUND"


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_documentation_routes_remain_closed(client: TestClient, path: str) -> None:
    """기준 11: /docs와 /openapi.json은 각각 404를 유지한다."""
    response = client.get(path)

    assert response.status_code == 404
