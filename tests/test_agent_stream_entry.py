"""에이전트 스트림의 수락 전 선검사·수락 전달·공통 이벤트를 검증한다."""

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

import httpx
import langsmith
import pytest
from fastapi.testclient import TestClient
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

from app.service.config import ServiceSettings
from app.service.main import create_app

if TYPE_CHECKING:
    # starlette 1.6 TestClient의 응답 타입이다.
    import httpx2

CONVERSATION_ID = "conversation-entry-cedar"
CLIENT_REQUEST_ID = "request-entry-maple"
USER_MESSAGE_ID = "user-entry-birch"
ASSISTANT_MESSAGE_ID = "assistant-entry-willow"
CASE_LABEL = "CASE-731"
STREAM_PATH = f"/api/v1/agent/conversations/{CONVERSATION_ID}/messages/stream"
ACCEPT_PATH = f"/api/v1/internal/agent/conversations/{CONVERSATION_ID}/turns"
TURN_PATH = f"/api/v1/internal/agent/turns/{ASSISTANT_MESSAGE_ID}"
STAMP = "2026-09-13T00:00:00.000Z"

ERROR_CASES = [
    (401, "UNAUTHORIZED"),
    (403, "CSRF_REJECTED"),
    (404, "NOT_FOUND"),
    (409, "DUPLICATE_CLIENT_REQUEST"),
]
ERROR_MESSAGES = {
    "UNAUTHORIZED": "인증이 필요합니다.",
    "CSRF_REJECTED": "요청 출처를 확인할 수 없습니다. 새로고침 후 다시 시도해주세요.",
    "NOT_FOUND": "대상을 찾을 수 없습니다.",
    "DUPLICATE_CLIENT_REQUEST": "이미 처리 중인 요청입니다.",
}


class ScriptedChatModel(BaseChatModel):
    """호출을 기록하고 합성 텍스트와 사용량만 돌려주는 로컬 채팅 모델이다."""

    chunks: list[str] = Field(default_factory=list)
    calls: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "entry-scripted-fake"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        usage: UsageMetadata = {"input_tokens": 17, "output_tokens": 9, "total_tokens": 26}
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(content="".join(self.chunks), usage_metadata=usage)
                )
            ]
        )

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        for piece in self.chunks:
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
        usage: UsageMetadata = {"input_tokens": 17, "output_tokens": 9, "total_tokens": 26}
        yield ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata=usage))


def _classifier(route: str = "OTHER", labels: list[str] | None = None) -> ScriptedChatModel:
    return ScriptedChatModel(
        chunks=[json.dumps({"route": route, "patient_labels": labels or []})]
    )


def _envelope(data: object, *, code: str = "SUCCESS") -> dict[str, object]:
    return {
        "success": True,
        "code": code,
        "message": "생성되었습니다." if code == "CREATED" else "요청에 성공하였습니다.",
        "data": data,
        "page": None,
        "timestamp": STAMP,
        "traceId": "trace-entry-spruce",
    }


def _failure_envelope(code: str) -> dict[str, object]:
    return {
        "success": False,
        "code": code,
        "message": ERROR_MESSAGES[code],
        "data": {"probe": "failure-data-juniper"},
        "page": None,
        "timestamp": STAMP,
        "traceId": "trace-entry-larch",
    }


def _message(*, abstained: bool) -> dict[str, object]:
    """완결 응답을 에이전트가 재조립하는지 구분할 수 있는 합성 DTO다."""
    message: dict[str, object] = {
        "id": ASSISTANT_MESSAGE_ID,
        "role": "ASSISTANT",
        "content": "" if abstained else "stored-answer-sequoia",
        "status": "ABSTAINED" if abstained else "COMPLETED",
        "responseLang": "ko",
        "citations": [],
        "createdAt": STAMP,
    }
    if abstained:
        message["abstainReason"] = "범위 밖 합성 질문입니다. reason-marker-cypress"
    return message


def _patient() -> dict[str, object]:
    return {
        "id": "patient-entry-aspen",
        "caseLabel": CASE_LABEL,
        "age": 47,
        "sex": "FEMALE",
        "status": "ACTIVE",
        "diagnoses": ["diagnosis-marker-poplar"],
        "medications": ["medication-marker-hemlock"],
        "allergies": ["allergy-marker-hazel"],
        "clinicalNotes": "합성 기록입니다. notes-marker-alder",
        "version": 1,
    }


def _evidence_event() -> dict[str, object]:
    return {
        "eventType": "retrieval.evidence",
        "index": 0,
        "total": 1,
        "evidence": {
            "id": "evidence-entry-rowan",
            "guidelineId": "guideline-entry-elm",
            "guidelineVersionId": "version-entry-oak",
            "guidelineTitle": "합성 지침",
            "version": "1.0",
            "sectionPath": ["합성 절"],
            "excerpt": "합성 근거 원문입니다. excerpt-marker-beech",
            "sourceUrl": "https://evidence.invalid/synthetic",
        },
    }


def _sse_response(events: list[dict[str, object]]) -> httpx.Response:
    content = "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream; charset=utf-8"},
        content=content.encode(),
    )


class RecordingBackend:
    """내부 경로별 JSON·SSE와 완결 지연을 합성하며 모든 요청을 기록한다."""

    def __init__(
        self,
        *,
        accept_status: int = 201,
        accept_code: str = "CREATED",
        accept_error: type[httpx.RequestError] | None = None,
        abstained: bool = True,
        finish_delay: float = 0.0,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.accept_status = accept_status
        self.accept_error = accept_error
        self.accepted = {
            "userMessageId": USER_MESSAGE_ID,
            "assistantMessageId": ASSISTANT_MESSAGE_ID,
        }
        self.accept_envelope = (
            _envelope(self.accepted, code="CREATED")
            if accept_status == 201
            else _failure_envelope(accept_code)
        )
        self.finish_envelope = _envelope(_message(abstained=abstained))
        self.finish_delay = finish_delay
        self.transport = httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == ACCEPT_PATH:
            if self.accept_error is not None:
                raise self.accept_error("synthetic transport failure", request=request)
            return httpx.Response(self.accept_status, json=self.accept_envelope)
        if path == f"{TURN_PATH}/patient":
            return httpx.Response(
                200, json=_envelope({"outcome": "RESOLVED", "patient": _patient()})
            )
        if path == f"{TURN_PATH}/guideline-evidence":
            return _sse_response(
                [
                    {"eventType": "retrieval.started", "requestId": CLIENT_REQUEST_ID},
                    {"eventType": "retrieval.progress", "stage": "embedded"},
                    {
                        "eventType": "evidence.gated",
                        "abstainReason": None,
                        "evidenceCount": 1,
                        "retrievalPolicyVersion": "policy-entry-ash",
                        "searchQuestion": "합성 검색 질문",
                    },
                    _evidence_event(),
                    {"eventType": "retrieval.completed"},
                ]
            )
        if path == f"{TURN_PATH}/guideline-answer":
            message = _message(abstained=False)
            message["answerKind"] = "GUIDELINE_ANSWER"
            return _sse_response(
                [
                    {"eventType": "retrieval.started", "requestId": CLIENT_REQUEST_ID},
                    {"eventType": "retrieval.progress", "stage": "embedded"},
                    {"eventType": "answer.started", "evidenceCount": 1},
                    _evidence_event(),
                    {"eventType": "retrieval.completed"},
                    {
                        "eventType": "answer.delta",
                        "messageId": ASSISTANT_MESSAGE_ID,
                        "seq": 0,
                        "delta": message["content"],
                    },
                    {"eventType": "answer.completed", "message": message},
                ]
            )
        if path == f"{TURN_PATH}/finish":
            if self.finish_delay:
                await asyncio.sleep(self.finish_delay)
            return httpx.Response(200, json=self.finish_envelope)
        raise AssertionError(f"준비하지 않은 BE 경로: {path}")


@contextmanager
def _service_client(
    backend: RecordingBackend,
    *,
    classifier: ScriptedChatModel | None = None,
    synthesis: ScriptedChatModel | None = None,
    heartbeat_interval: float = 15.0,
) -> Iterator[TestClient]:
    """모든 외부 경계를 가짜로 주입하고 추적 전역을 종료 시 되돌린다."""
    app = create_app(
        ServiceSettings(
            be_origin="http://backend.test:4312",
            agent_tracing_enabled="",
            openai_api_key="",
        ),
        backend_transport=backend.transport,
        classifier_model=classifier if classifier is not None else _classifier(),
        synthesis_model=(
            synthesis
            if synthesis is not None
            else ScriptedChatModel(chunks=["draft-answer-magnolia"])
        ),
        heartbeat_interval=heartbeat_interval,
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        langsmith.configure(enabled=None)


def _jwt(*, iat: int, exp: int) -> str:
    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    header = encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = encode(json.dumps({"sub": "clinician-entry-fir", "iat": iat, "exp": exp}).encode())
    return f"{header}.{payload}.{encode(b'sig')}"


def _valid_cookie() -> str:
    now = int(time.time())
    return f"access_token={_jwt(iat=now, exp=now + 3600)}"


def _body(content: str = "합성 범위 밖 질문입니다.") -> dict[str, str]:
    return {"content": content, "clientRequestId": CLIENT_REQUEST_ID}


def _json_object(response: "httpx2.Response") -> dict[str, object]:
    payload: object = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _events(text: str) -> list[dict[str, object]]:
    """주석은 건너뛰고 완료된 SSE 본문의 데이터 프레임만 읽는다."""
    events: list[dict[str, object]] = []
    for line in text.splitlines():
        if line.startswith("data: "):
            payload: object = json.loads(line.removeprefix("data: "))
            assert isinstance(payload, dict)
            events.append(cast(dict[str, object], payload))
    return events


def _accept_requests(backend: RecordingBackend) -> list[httpx.Request]:
    return [
        request
        for request in backend.requests
        if request.method == "POST" and request.url.path == ACCEPT_PATH
    ]


def test_expiring_access_returns_envelope_without_backend_call() -> None:
    """기준 1·2: 잔여 수명이 짧은 장수명 토큰은 401 봉투로 거르고 BE를 부르지 않는다."""
    backend = RecordingBackend()
    now = int(time.time())
    cookie = f"access_token={_jwt(iat=now - 3000, exp=now + 60)}"

    with _service_client(backend) as client:
        response = client.post(STREAM_PATH, json=_body(), headers={"Cookie": cookie})

    assert response.status_code == 401
    body = _json_object(response)
    assert set(body) == {"success", "code", "message", "data", "page", "timestamp", "traceId"}
    assert body["success"] is False
    assert body["code"] == "AUTH_TOKEN_EXPIRED"
    assert isinstance(body["message"], str)
    assert body["data"] is None
    assert body["page"] is None
    timestamp = body["timestamp"]
    assert isinstance(timestamp, str)
    datetime.fromisoformat(timestamp)
    trace_id = body["traceId"]
    assert isinstance(trace_id, str) and bool(trace_id)
    assert len(backend.requests) == 0


@pytest.mark.parametrize("total_lifetime", [130, 150], ids=["below-limit", "exact-limit"])
def test_short_total_lifetime_reaches_acceptance(total_lifetime: int) -> None:
    """기준 3: 전체 수명이 150초 이하이면 잔여 수명이 짧아도 수락을 호출한다."""
    backend = RecordingBackend()
    now = int(time.time())
    exp = now + 60
    cookie = f"access_token={_jwt(iat=exp - total_lifetime, exp=exp)}"

    with _service_client(backend) as client:
        client.post(STREAM_PATH, json=_body(), headers={"Cookie": cookie})

    assert _accept_requests(backend)


@pytest.mark.parametrize("cookie", [None, "refresh_token=refresh-marker-dogwood"])
def test_missing_access_preserves_acceptance_unauthorized(cookie: str | None) -> None:
    """기준 4: 헤더 없음·refresh만 있는 경우 모두 BE 수락의 401 봉투를 보존한다."""
    backend = RecordingBackend(accept_status=401, accept_code="UNAUTHORIZED")
    headers = {} if cookie is None else {"Cookie": cookie}

    with _service_client(backend) as client:
        response = client.post(STREAM_PATH, json=_body(), headers=headers)

    assert _accept_requests(backend)
    assert response.status_code == 401
    assert _json_object(response) == backend.accept_envelope


def test_acceptance_receives_original_cookie() -> None:
    """기준 5: 수락에 공백·등호·따옴표를 섞은 Cookie 원문을 그대로 싣는다."""
    backend = RecordingBackend()
    cookie = (
        f'{_valid_cookie()}; refresh_token=refresh-marker-acacia;other=1==; theme="dark mode"'
    )

    with _service_client(backend) as client:
        client.post(STREAM_PATH, json=_body(), headers={"Cookie": cookie})

    accepted = _accept_requests(backend)
    assert accepted
    assert accepted[0].headers["Cookie"] == cookie


def test_acceptance_omits_absent_csrf_header() -> None:
    """기준 6: 요청에 없는 CSRF 헤더를 수락 요청에 새로 만들지 않는다."""
    backend = RecordingBackend()

    with _service_client(backend) as client:
        client.post(STREAM_PATH, json=_body(), headers={"Cookie": _valid_cookie()})

    accepted = _accept_requests(backend)
    assert accepted
    assert "X-CSRF-Protection" not in accepted[0].headers


@pytest.mark.parametrize(("status", "code"), ERROR_CASES)
def test_acceptance_4xx_preserves_envelope_without_llm(status: int, code: str) -> None:
    """기준 7·8: 수락의 각 4xx 상태·봉투를 보존하고 분류·합성 모델을 부르지 않는다."""
    backend = RecordingBackend(accept_status=status, accept_code=code)
    classifier = _classifier("PATIENT", [CASE_LABEL])
    synthesis = ScriptedChatModel(chunks=["unused-answer-palm"])

    with _service_client(backend, classifier=classifier, synthesis=synthesis) as client:
        response = client.post(
            STREAM_PATH, json=_body(), headers={"Cookie": _valid_cookie()}
        )

    assert _accept_requests(backend)
    assert response.status_code == status
    assert _json_object(response) == backend.accept_envelope
    assert len(classifier.calls) == 0
    assert len(synthesis.calls) == 0


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_acceptance_transport_failure_returns_502(error_type: type[httpx.RequestError]) -> None:
    """기준 9: 수락의 연결 실패·읽기 시간 초과는 502 상류 장애 봉투다."""
    backend = RecordingBackend(accept_error=error_type)

    with _service_client(backend) as client:
        response = client.post(
            STREAM_PATH, json=_body(), headers={"Cookie": _valid_cookie()}
        )

    assert _accept_requests(backend)
    assert response.status_code == 502
    assert _json_object(response)["code"] == "AGENT_BACKEND_UNAVAILABLE"


def test_first_event_contains_accepted_ids_and_request_id() -> None:
    """기준 10: 첫 이벤트가 수락의 두 메시지 id와 요청의 clientRequestId를 싣는다."""
    backend = RecordingBackend()
    body = _body()

    with _service_client(backend) as client:
        response = client.post(STREAM_PATH, json=body, headers={"Cookie": _valid_cookie()})

    events = _events(response.text)
    assert events
    first = events[0]
    assert first["eventType"] == "message.accepted"
    assert first["userMessageId"] == backend.accepted["userMessageId"]
    assert first["assistantMessageId"] == backend.accepted["assistantMessageId"]
    assert first["requestId"] == body["clientRequestId"]


@pytest.mark.parametrize(
    ("classifier_route", "labels", "execution_route"),
    [("COMPOSITE", [], "GUIDELINE"), ("GUIDELINE", [CASE_LABEL], "COMPOSITE")],
)
def test_second_event_reports_execution_route(
    classifier_route: str, labels: list[str], execution_route: str
) -> None:
    """기준 11: 두 번째 routed 진행 이벤트는 분류기 판정과 다른 실제 실행 경로를 싣는다."""
    backend = RecordingBackend(abstained=False)
    synthesis = ScriptedChatModel(
        chunks=['{"insufficientEvidence": false, "answer": "합성 답변 [1]"}']
    )
    question = f"{CASE_LABEL}에 관한 합성 질문" if labels else "합성 환자군 지침 질문"

    with _service_client(
        backend, classifier=_classifier(classifier_route, labels), synthesis=synthesis
    ) as client:
        response = client.post(
            STREAM_PATH, json=_body(question), headers={"Cookie": _valid_cookie()}
        )

    events = _events(response.text)
    assert len(events) >= 2
    second = events[1]
    assert second["eventType"] == "agent.progress"
    assert second["stage"] == "routed"
    assert second["route"] == execution_route


def test_idle_stream_emits_ping_comment() -> None:
    """기준 12: 수락 뒤 완결을 기다리는 공백에 주입한 주기의 ping 주석을 보낸다."""
    backend = RecordingBackend(finish_delay=0.6)

    with _service_client(backend, heartbeat_interval=0.05) as client:
        response = client.post(
            STREAM_PATH, json=_body(), headers={"Cookie": _valid_cookie()}
        )

    events = _events(response.text)
    assert any(event.get("eventType") == "message.accepted" for event in events)
    assert ": ping" in response.text.splitlines()


@pytest.mark.parametrize(
    ("route", "abstained", "terminal_type"),
    [("PATIENT", False, "answer.completed"), ("OTHER", True, "answer.abstained")],
)
def test_terminal_message_equals_finish_response(
    route: str, abstained: bool, terminal_type: str
) -> None:
    """기준 13: 완료·기권 종결 이벤트의 message는 완결 응답 봉투의 data와 같다."""
    backend = RecordingBackend(abstained=abstained)
    labels = [CASE_LABEL] if route == "PATIENT" else []
    question = f"{CASE_LABEL}의 합성 기록 질문" if labels else "합성 범위 밖 질문"

    with _service_client(backend, classifier=_classifier(route, labels)) as client:
        response = client.post(
            STREAM_PATH, json=_body(question), headers={"Cookie": _valid_cookie()}
        )

    assert any(
        request.method == "POST" and request.url.path == f"{TURN_PATH}/finish"
        for request in backend.requests
    )
    events = _events(response.text)
    assert events
    assert events[-1]["eventType"] == terminal_type
    assert events[-1]["message"] == backend.finish_envelope["data"]
