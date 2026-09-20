"""완결 대기 상한·참고안 분리 전달·대기 중 하트비트와 끊김 정리를 검증한다."""

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any, cast

import httpx
import langsmith
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field
from starlette.types import Message, Scope

from app.service.config import ServiceSettings
from app.service.main import create_app

STREAM_PATH = "/api/v1/agent/conversations/conversation-c/messages/stream"
ACCEPT_PATH = "/api/v1/internal/agent/conversations/conversation-c/turns"
TURN_PATH = "/api/v1/internal/agent/turns/assistant-c"
PATIENT_PATH = f"{TURN_PATH}/patient"
EVIDENCE_PATH = f"{TURN_PATH}/guideline-evidence"
FINISH_PATH = f"{TURN_PATH}/finish"
QUESTION = "CASE-731 합성 처치의 지침을 검토해 줘"
REQUEST_ID = "request-guidance-c"
STAMP = "2026-09-21T00:00:00.000Z"

def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _events(text: str) -> list[dict[str, object]]:
    return [
        _object(json.loads(line[6:])) for line in text.splitlines() if line.startswith("data: ")
    ]


def _event(events: list[dict[str, object]], kind: str) -> dict[str, object]:
    found = [event for event in events if event.get("eventType") == kind]
    assert len(found) == 1, f"기대 이벤트 수가 다르다: {kind}"
    return found[0]


def _cookie() -> str:
    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    now = int(time.time())
    header = encode(b'{"alg":"none","typ":"JWT"}')
    payload = encode(json.dumps({"sub": "synthetic-c", "iat": now, "exp": now + 3600}).encode())
    return f"access_token={header}.{payload}.{encode(b'sig')}"


def _envelope(data: object, *, created: bool = False) -> dict[str, object]:
    return {
        "success": True,
        "code": "CREATED" if created else "SUCCESS",
        "message": "생성되었습니다." if created else "요청에 성공하였습니다.",
        "data": data,
        "page": None,
        "timestamp": STAMP,
        "traceId": "trace-guidance-c",
    }


def _guidance() -> dict[str, object]:
    return {
        "id": "guidance-c",
        "patientId": "patient-c",
        "patientProfileSnapshotId": "snapshot-c",
        "summary": "GUIDANCE_SUMMARY_QUARTZ_54",
        "considerations": [{
            "title": "합성 검토 항목",
            "rationale": "GUIDANCE_RATIONALE_OPAL_54",
            "citations": [{"marker": 1, "evidenceId": "EID_ZULU_61"}],
            "applicability": "CAUTION",
            "patientFactors": ["diagnoses", "allergies"],
        }],
        "safetyAlerts": [{
            "severity": "WARNING",
            "description": "ALLERGY_JADE_28 합성 알레르기 확인",
            "citations": [],
        }],
        "missingInformation": ["GUIDANCE_MISSING_BERYL_54"],
        "reviewStatus": "DRAFT",
        "generatedAt": STAMP,
    }


class ScriptedChatModel(BaseChatModel):
    """정상 LangChain 경로에서 합성 응답 조각과 사용량만 반환한다."""

    chunks: list[str] = Field(default_factory=list, repr=False)
    calls: list[list[BaseMessage]] = Field(default_factory=list, repr=False)

    @property
    def _llm_type(self) -> str:
        return "synthetic-guidance-c"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        return ChatResult(generations=[
            ChatGeneration(message=AIMessage(content="".join(self.chunks)))
        ])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        self.calls.append(list(messages))
        for piece in self.chunks:
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
        usage: UsageMetadata = {"input_tokens": 37, "output_tokens": 19, "total_tokens": 56}
        yield ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata=usage))


def _classifier(route: str = "COMPOSITE") -> ScriptedChatModel:
    return ScriptedChatModel(chunks=[json.dumps({
        "route": route, "patient_labels": ["CASE-731"],
    })])


def _synthesis(route: str = "COMPOSITE") -> ScriptedChatModel:
    answer = "합성 답변 ANSWER_SAPPHIRE_54 [1]"
    return ScriptedChatModel(chunks=[
        json.dumps({"insufficientEvidence": False, "answer": answer})
        if route == "COMPOSITE" else answer
    ])


class RecordingBackend:
    """요청과 응답을 보존하고 완결 응답의 참고안·지연·대기를 주입한다."""

    def __init__(
        self, *, guidance: bool = True, finish_delay: float = 0.0, pause_finish: bool = False
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.finish_responses: list[dict[str, object]] = []
        self.guidance = _guidance() if guidance else None
        self.finish_delay = finish_delay
        self.pause_finish = pause_finish
        self.finish_started = asyncio.Event()
        self.finish_gate = asyncio.Event()
        self.transport = httpx.MockTransport(self._handle)

    def bodies(self, path: str) -> list[dict[str, object]]:
        return [
            _object(json.loads(request.content))
            for request in self.requests if request.url.path == path
        ]

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == ACCEPT_PATH:
            return httpx.Response(201, json=_envelope({
                "userMessageId": "user-c", "assistantMessageId": "assistant-c",
            }, created=True))
        if path == PATIENT_PATH:
            return httpx.Response(200, json=_envelope({
                "outcome": "RESOLVED",
                "patient": {
                    "id": "patient-c", "caseLabel": "CASE-731", "status": "ACTIVE",
                    "age": 47, "sex": "FEMALE", "diagnoses": ["DIAG_IVORY_17"],
                    "medications": ["MED_ONYX_63"], "allergies": ["ALLERGY_JADE_28"],
                    "clinicalNotes": "NOTE_GARNET_76", "version": 1,
                },
            }))
        if path == EVIDENCE_PATH:
            events = [
                {"eventType": "retrieval.started", "requestId": REQUEST_ID},
                {"eventType": "retrieval.progress", "stage": "embedded"},
                {"eventType": "evidence.gated", "abstainReason": None, "evidenceCount": 1,
                 "retrievalPolicyVersion": "POLICY_QUARTZ_83", "searchQuestion": "합성 검색"},
                {"eventType": "retrieval.evidence", "index": 0, "total": 1, "evidence": {
                    "id": "EID_ZULU_61", "guidelineId": "guideline-c",
                    "guidelineVersionId": "version-c", "guidelineTitle": "합성 지침",
                    "version": "1.0", "sectionPath": ["합성 절"],
                    "excerpt": "EXCERPT_RUBY_32", "sourceUrl": "https://source.invalid/c",
                }},
                {"eventType": "retrieval.completed"},
            ]
            content = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=content
            )
        if path == FINISH_PATH:
            body = _object(json.loads(request.content))
            if body.get("status") == "COMPLETED":
                self.finish_started.set()
                if self.pause_finish:
                    await self.finish_gate.wait()
                if self.finish_delay:
                    await asyncio.sleep(self.finish_delay)
            message: dict[str, object] = {
                "id": "assistant-c", "role": "ASSISTANT", "content": body.get("content", ""),
                "status": body["status"], "responseLang": "ko",
                "citations": body.get("citations", []), "createdAt": STAMP,
            }
            if body.get("status") == "COMPLETED" and self.guidance is not None:
                message["answerKind"] = "CLINICAL_GUIDANCE"
                message["guidance"] = self.guidance
            self.finish_responses.append(message)
            return httpx.Response(200, json=_envelope(message))
        raise AssertionError(f"준비하지 않은 BE 요청: {request.method} {path}")


def _app(
    backend: RecordingBackend, route: str = "COMPOSITE", *, heartbeat_interval: float = 60.0
) -> FastAPI:
    return create_app(
        ServiceSettings(
            be_origin="http://backend.test", agent_tracing_enabled="", openai_api_key=""
        ),
        backend_transport=backend.transport,
        classifier_model=_classifier(route),
        synthesis_model=_synthesis(route),
        heartbeat_interval=heartbeat_interval,
    )


@contextmanager
def _client(app: FastAPI) -> Iterator[TestClient]:
    try:
        with TestClient(app) as client:
            yield client
    finally:
        langsmith.configure(enabled=None)


def _run(backend: RecordingBackend, route: str = "COMPOSITE") -> list[dict[str, object]]:
    with _client(_app(backend, route)) as client:
        response = client.post(
            STREAM_PATH, json={"content": QUESTION, "clientRequestId": REQUEST_ID},
            headers={"Cookie": _cookie(), "X-CSRF-Protection": "csrf-c"},
        )
    assert response.status_code == 200
    events = _events(response.text)
    assert _event(events, "answer.completed")["message"]
    assert backend.bodies(FINISH_PATH)[0]["route"] == route
    assert backend.bodies(FINISH_PATH)[0]["status"] == "COMPLETED"
    return events


def _timeout(backend: RecordingBackend, path: str) -> dict[str, object]:
    requests = [request for request in backend.requests if request.url.path == path]
    assert len(requests) == 1
    assert requests[0].method == "POST"
    timeout: object = requests[0].extensions["timeout"]
    assert isinstance(timeout, dict)
    return cast(dict[str, object], timeout)


def test_finish_read_timeout_is_thirty_seconds() -> None:
    """기준 39: 복합 완료 완결 요청의 읽기 상한은 30초다."""
    backend = RecordingBackend()
    _run(backend)
    assert _timeout(backend, FINISH_PATH)["read"] == 30.0


def test_finish_connect_timeout_remains_five_seconds() -> None:
    """기준 40: 복합 완료 완결 요청의 연결 상한은 5초다."""
    backend = RecordingBackend()
    _run(backend)
    assert _timeout(backend, FINISH_PATH)["connect"] == 5.0


def test_patient_read_timeout_remains_five_seconds() -> None:
    """기준 41: 복합 완료에 사용한 환자 도구의 읽기 상한은 5초다."""
    backend = RecordingBackend()
    _run(backend)
    assert _timeout(backend, PATIENT_PATH)["read"] == 5.0


def test_completed_event_preserves_guidance() -> None:
    """기준 42: 완료 이벤트의 참고안은 완결 응답의 중첩 객체와 같다."""
    backend = RecordingBackend()
    completed = _event(_run(backend), "answer.completed")
    assert backend.finish_responses[0]["guidance"] == backend.guidance
    assert completed["guidance"] == backend.guidance


def test_completed_message_excludes_guidance() -> None:
    """기준 43: 참고안이 온 완료 이벤트의 메시지에서 참고안 키를 떼어 낸다."""
    backend = RecordingBackend()
    completed = _event(_run(backend), "answer.completed")
    sent = backend.finish_responses[0]
    assert sent["guidance"] == backend.guidance
    message = _object(completed["message"])
    assert message["id"] == sent["id"]
    assert message["content"] == sent["content"]
    assert "guidance" not in message
    assert message == {key: value for key, value in sent.items() if key != "guidance"}


@pytest.mark.parametrize("route", ["PATIENT", "COMPOSITE"], ids=["환자", "옛_BE_복합"])
def test_completed_without_guidance_omits_key(route: str) -> None:
    """기준 44: 환자 경로와 옛 BE 복합 응답 각각에서 없는 참고안 키를 만들지 않는다."""
    backend = RecordingBackend(guidance=False)
    completed = _event(_run(backend, route), "answer.completed")
    sent = backend.finish_responses[0]
    assert sent["status"] == "COMPLETED"
    assert sent["content"]
    assert "guidance" not in sent
    assert completed["message"] == sent
    assert "guidance" not in completed


class ASGIProbe:
    """같은 루프의 ASGI 전송을 관측하고 원하는 시점에 연결을 끊는다."""

    def __init__(self, backend: RecordingBackend) -> None:
        self.backend = backend
        self.disconnect = asyncio.Event()
        self.ping_during_finish = asyncio.Event()
        self.messages: list[Message] = []
        self.request_sent = False
        self.scope: Scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": STREAM_PATH, "raw_path": STREAM_PATH.encode(), "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json"),
                        (b"cookie", _cookie().encode()), (b"x-csrf-protection", b"csrf-c")],
            "client": ("127.0.0.1", 5000), "server": ("testserver", 80),
        }

    async def receive(self) -> Message:
        if not self.request_sent:
            self.request_sent = True
            return {
                "type": "http.request",
                "body": json.dumps({"content": QUESTION, "clientRequestId": REQUEST_ID}).encode(),
                "more_body": False,
            }
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        self.messages.append(dict(message))
        body = cast(bytes, message.get("body", b""))
        if (
            b": ping\n\n" in body and self.backend.finish_started.is_set()
            and not self.backend.finish_responses
        ):
            self.ping_during_finish.set()

    def events(self) -> list[dict[str, object]]:
        body = b"".join(
            cast(bytes, message.get("body", b"")) for message in self.messages
            if message["type"] == "http.response.body"
        )
        complete = body.rsplit(b"\n\n", 1)[0] if b"\n\n" in body else b""
        return _events(complete.decode())


async def _signal_or_failure(signal: asyncio.Event, task: asyncio.Task[None]) -> None:
    """앱의 조기 실패를 관측 신호 대기로 가리지 않는다."""
    waiter = asyncio.create_task(signal.wait())
    try:
        await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            await task
        assert signal.is_set(), "관측 지점 전에 ASGI 호출이 끝났다"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def test_finish_wait_emits_ping_and_completes() -> None:
    """기준 45: 완결 대기 중 하트비트가 나가고 이후 정상 완료 이벤트가 도착한다."""
    backend = RecordingBackend(guidance=False, finish_delay=0.2, pause_finish=True)
    app = _app(backend, heartbeat_interval=0.02)
    probe = ASGIProbe(backend)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(app(probe.scope, probe.receive, probe.send))
            try:
                await _signal_or_failure(backend.finish_started, task)
                await _signal_or_failure(probe.ping_during_finish, task)
                assert backend.bodies(FINISH_PATH)[0]["status"] == "COMPLETED"
                backend.finish_gate.set()
                await task
                assert probe.ping_during_finish.is_set()
                completed = _event(probe.events(), "answer.completed")
                assert completed["message"] == backend.finish_responses[0]
                assert _object(completed["message"])["status"] == "COMPLETED"
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    try:
        await asyncio.wait_for(scenario(), timeout=30)
    finally:
        langsmith.configure(enabled=None)


async def test_disconnect_during_finish_sends_cancelled() -> None:
    """기준 46: 완료 완결이 대기하는 지점에서 끊으면 두 번째 완결은 취소다."""
    backend = RecordingBackend(pause_finish=True)
    app = _app(backend)
    probe = ASGIProbe(backend)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(app(probe.scope, probe.receive, probe.send))
            try:
                await _signal_or_failure(backend.finish_started, task)
                first = backend.bodies(FINISH_PATH)
                assert len(first) == 1
                assert first[0]["status"] == "COMPLETED"
                assert first[0]["route"] == "COMPOSITE"
                assert not backend.finish_responses
                probe.disconnect.set()
                await task
                finishes = backend.bodies(FINISH_PATH)
                assert len(finishes) == 2
                assert [body["status"] for body in finishes] == ["COMPLETED", "CANCELLED"]
                assert backend.finish_responses[0]["status"] == "CANCELLED"
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    try:
        await asyncio.wait_for(scenario(), timeout=30)
    finally:
        langsmith.configure(enabled=None)
