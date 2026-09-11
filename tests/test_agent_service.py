"""에이전트 서비스의 생존 확인·자격 전달·BE 판정 매핑을 가짜 전송으로 검증한다."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, cast

import httpx
import langsmith
import pytest
from fastapi.testclient import TestClient

from app.service.config import ServiceSettings
from app.service.main import create_app

if TYPE_CHECKING:
    # starlette 1.6의 TestClient는 타입 검사 시 httpx2 응답을 돌려준다(starlette/testclient.py)
    import httpx2

BE_ORIGIN = "http://backend.test:4312"
RAW_COOKIE = (
    'access_token=AAA.bbb-CCC_ddd; refresh_token=RRR;other=1==; theme="dark mode"'
)
CSRF_VALUE = "csrf-probe-5e1"
BE_CLINICIAN_ID = "clinician-id-from-be"
BE_CLINIC_ID = "clinic-id-from-be"
BE_EMAIL = "email-value-marker@example.invalid"
BE_DISPLAY_NAME = "display-name-value-marker"


class RecordingBackend:
    """받은 요청을 모두 보존한 뒤 지정된 합성 응답이나 예외를 돌려준다."""

    def __init__(
        self,
        responder: Callable[[httpx.Request], httpx.Response],
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)


def _success_envelope() -> dict[str, object]:
    """BE ClinicianResponseDto를 닮은 식별 가능한 합성 성공 봉투를 만든다."""
    return {
        "success": True,
        "code": "SUCCESS",
        "message": "Synthetic success",
        "data": {
            "id": BE_CLINICIAN_ID,
            "email": BE_EMAIL,
            "displayName": BE_DISPLAY_NAME,
            "clinic": {
                "id": BE_CLINIC_ID,
                "name": "clinic-name-marker",
            },
            "verificationStatus": "VERIFIED",
        },
        "page": None,
        "timestamp": "2026-07-23T14:00:00.000Z",
        "traceId": "trace-be-success",
    }


def _failure_envelope(code: str) -> dict[str, object]:
    """BE의 인증 실패 봉투와 같은 필드 구조를 만든다."""
    return {
        "success": False,
        "code": code,
        "message": "Synthetic authentication failure",
        "data": None,
        "page": None,
        "timestamp": "2026-07-23T14:00:00.000Z",
        "traceId": "trace-be-failure",
    }


def _json_backend(status_code: int, payload: dict[str, object]) -> RecordingBackend:
    """고정 JSON을 반환하는 기록형 MockTransport를 만든다."""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status_code, json=payload)

    return RecordingBackend(respond)


def _successful_backend() -> RecordingBackend:
    return _json_backend(200, _success_envelope())


def _error_backend(error_type: type[httpx.RequestError]) -> RecordingBackend:
    """요청 객체가 결합된 httpx 전송 예외를 던지는 가짜 BE를 만든다."""

    def raise_error(request: httpx.Request) -> httpx.Response:
        raise error_type("synthetic backend failure", request=request)

    return RecordingBackend(raise_error)


@contextmanager
def _service_client(
    backend: RecordingBackend,
    *,
    be_origin: str = BE_ORIGIN,
) -> Iterator[TestClient]:
    """명시적 설정과 가짜 BE로 lifespan이 실행되는 클라이언트를 연다."""
    app = create_app(
        ServiceSettings(
            be_origin=be_origin,
            agent_tracing_enabled="",
        ),
        backend_transport=backend.transport,
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        # lifespan이 바꾼 프로세스 전역 추적 설정을 다음 테스트에 남기지 않는다.
        langsmith.configure(enabled=None)


@contextmanager
def _environment_service_client(backend: RecordingBackend) -> Iterator[TestClient]:
    """설정을 생략해 프로세스 환경을 읽는 앱 클라이언트를 연다."""
    app = create_app(backend_transport=backend.transport)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        langsmith.configure(enabled=None)


def _json_object(response: "httpx2.Response") -> dict[str, object]:
    """응답이 파싱 가능한 JSON 객체인지 확인하고 타입을 좁힌다."""
    payload: object = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _contains_marker(value: object, marker: str) -> bool:
    """JSON의 모든 문자열 키와 값을 재귀적으로 검사한다."""
    if isinstance(value, str):
        return marker in value
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and marker in key:
                return True
            if _contains_marker(item, marker):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_marker(item, marker) for item in value)
    return False


def test_healthz_returns_ok_without_cookie() -> None:
    """기준 1: 쿠키 없는 healthz는 200과 data.status=ok를 반환한다."""
    backend = _successful_backend()

    with _service_client(backend) as client:
        response = client.get("/api/v1/agent/healthz")

    assert response.status_code == 200
    body = _json_object(response)
    data = body.get("data")
    assert isinstance(data, dict)
    assert data.get("status") == "ok"


def test_healthz_does_not_call_unreachable_backend() -> None:
    """기준 2: 닿지 않는 BE와 무관하게 healthz가 살고 BE 호출은 없다."""
    backend = _error_backend(httpx.ConnectError)

    with _service_client(backend, be_origin="http://127.0.0.1:9") as client:
        response = client.get("/api/v1/agent/healthz")

    assert response.status_code == 200
    assert len(backend.requests) == 0


def test_me_forwards_original_cookie_to_backend() -> None:
    """기준 3: 받은 Cookie 원문을 BE의 인증 조회 GET에 그대로 전달한다."""
    backend = _successful_backend()

    with _service_client(backend) as client:
        client.get("/api/v1/agent/me", headers={"Cookie": RAW_COOKIE})

    assert len(backend.requests) == 1
    request = backend.requests[0]
    assert request.method == "GET"
    assert request.url.path == "/api/v1/auth/me"
    assert request.headers["Cookie"] == RAW_COOKIE


def test_me_uses_be_origin_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """기준 4: BE_ORIGIN 환경변수가 BE 호출의 origin을 정한다."""
    backend = _successful_backend()
    monkeypatch.setenv("BE_ORIGIN", "http://be-origin.test:4321")
    monkeypatch.delenv("AGENT_TRACING_ENABLED", raising=False)

    with _environment_service_client(backend) as client:
        client.get("/api/v1/agent/me", headers={"Cookie": RAW_COOKIE})

    assert len(backend.requests) == 1
    url = backend.requests[0].url
    assert (url.scheme, url.host, url.port) == ("http", "be-origin.test", 4321)


def test_me_forwards_present_csrf_header() -> None:
    """기준 5: 받은 X-CSRF-Protection 값을 BE에 그대로 전달한다."""
    backend = _successful_backend()
    headers = {
        "Cookie": RAW_COOKIE,
        "X-CSRF-Protection": CSRF_VALUE,
    }

    with _service_client(backend) as client:
        client.get("/api/v1/agent/me", headers=headers)

    assert len(backend.requests) == 1
    assert backend.requests[0].headers["X-CSRF-Protection"] == CSRF_VALUE


def test_me_does_not_invent_missing_csrf_header() -> None:
    """기준 6: 요청에 없는 X-CSRF-Protection을 에이전트가 만들지 않는다."""
    backend = _successful_backend()

    with _service_client(backend) as client:
        client.get("/api/v1/agent/me", headers={"Cookie": RAW_COOKIE})

    assert len(backend.requests) == 1
    assert "X-CSRF-Protection" not in backend.requests[0].headers


def test_me_maps_backend_clinician_id() -> None:
    """기준 7: BE 200의 clinician id를 성공 응답의 clinicianId로 옮긴다."""
    backend = _successful_backend()

    with _service_client(backend) as client:
        response = client.get("/api/v1/agent/me", headers={"Cookie": RAW_COOKIE})

    assert response.status_code == 200
    body = _json_object(response)
    data = body.get("data")
    assert isinstance(data, dict)
    assert data.get("clinicianId") == BE_CLINICIAN_ID


def test_me_maps_backend_clinic_id() -> None:
    """기준 8: BE 200의 clinic.id를 성공 응답의 clinicId로 옮긴다."""
    backend = _successful_backend()

    with _service_client(backend) as client:
        response = client.get("/api/v1/agent/me", headers={"Cookie": RAW_COOKIE})

    assert response.status_code == 200
    body = _json_object(response)
    data = body.get("data")
    assert isinstance(data, dict)
    assert data.get("clinicId") == BE_CLINIC_ID


def test_me_omits_private_clinician_fields_everywhere() -> None:
    """기준 9: 성공 응답의 원문과 전체 JSON에 email·displayName 값이 없다."""
    backend = _successful_backend()

    with _service_client(backend) as client:
        response = client.get("/api/v1/agent/me", headers={"Cookie": RAW_COOKIE})

    assert response.status_code == 200
    parsed = _json_object(response)
    assert BE_EMAIL not in response.text
    assert not _contains_marker(parsed, BE_EMAIL)
    assert BE_DISPLAY_NAME not in response.text
    assert not _contains_marker(parsed, BE_DISPLAY_NAME)


def test_me_success_uses_api_envelope() -> None:
    """기준 10: 성공 응답은 §10.1의 일곱 필드 봉투와 값 제약을 지킨다."""
    backend = _successful_backend()

    with _service_client(backend) as client:
        response = client.get("/api/v1/agent/me", headers={"Cookie": RAW_COOKIE})

    assert response.status_code == 200
    body = _json_object(response)
    assert set(body) == {
        "success",
        "code",
        "message",
        "data",
        "page",
        "timestamp",
        "traceId",
    }
    assert body["success"] is True
    code = body["code"]
    message = body["message"]
    assert isinstance(code, str) and bool(code)
    assert isinstance(message, str)
    assert isinstance(body["data"], dict)
    assert body["page"] is None
    timestamp = body["timestamp"]
    assert isinstance(timestamp, str)
    datetime.fromisoformat(timestamp)
    trace_id = body["traceId"]
    assert isinstance(trace_id, str) and bool(trace_id)


def test_me_preserves_backend_unauthorized_status() -> None:
    """기준 11: BE가 반환한 401 상태를 에이전트도 반환한다."""
    backend = _json_backend(401, _failure_envelope("UNAUTHORIZED"))

    with _service_client(backend) as client:
        response = client.get("/api/v1/agent/me", headers={"Cookie": RAW_COOKIE})

    assert response.status_code == 401


@pytest.mark.parametrize("code", ["UNAUTHORIZED", "AUTH_TOKEN_EXPIRED"])
def test_me_preserves_backend_unauthorized_code(code: str) -> None:
    """기준 12: 두 인증 실패 code를 각각 BE 봉투 그대로 반환한다."""
    backend = _json_backend(401, _failure_envelope(code))

    with _service_client(backend) as client:
        response = client.get("/api/v1/agent/me", headers={"Cookie": RAW_COOKIE})

    assert response.status_code == 401
    assert _json_object(response).get("code") == code


def test_me_maps_connect_error_to_backend_unavailable() -> None:
    """기준 13: BE 연결 실패를 502 AGENT_BACKEND_UNAVAILABLE로 바꾼다."""
    backend = _error_backend(httpx.ConnectError)

    with _service_client(backend) as client:
        response = client.get("/api/v1/agent/me", headers={"Cookie": RAW_COOKIE})

    assert response.status_code == 502
    assert _json_object(response).get("code") == "AGENT_BACKEND_UNAVAILABLE"


TIMEOUT_TYPES: tuple[type[httpx.TimeoutException], ...] = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)


@pytest.mark.parametrize("timeout_type", TIMEOUT_TYPES)
def test_me_maps_timeout_to_backend_unavailable(
    timeout_type: type[httpx.TimeoutException],
) -> None:
    """기준 14: 모든 httpx 시간 초과를 같은 502 상류 장애로 바꾼다."""
    backend = _error_backend(timeout_type)

    with _service_client(backend) as client:
        response = client.get("/api/v1/agent/me", headers={"Cookie": RAW_COOKIE})

    assert response.status_code == 502
    assert _json_object(response).get("code") == "AGENT_BACKEND_UNAVAILABLE"
