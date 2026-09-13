"""복합 경로의 근거·합성·인용과 스트림 실패·끊김의 완결을 검증한다."""

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
from pydantic import ConfigDict, Field
from starlette.types import Message, Scope

from app.service.config import ServiceSettings
from app.service.main import create_app

STREAM_PATH = "/api/v1/agent/conversations/conversation-c/messages/stream"
ACCEPT_PATH = "/api/v1/internal/agent/conversations/conversation-c/turns"
TURN_PATH = "/api/v1/internal/agent/turns/assistant-c"
PATIENT_PATH = f"{TURN_PATH}/patient"
EVIDENCE_PATH = f"{TURN_PATH}/guideline-evidence"
GUIDELINE_PATH = f"{TURN_PATH}/guideline-answer"
FINISH_PATH = f"{TURN_PATH}/finish"
QUESTION = "CASE-731 합성 처치의 지침을 검토해 줘"
REQUEST_ID = "request-composite-c"
POLICY = "POLICY_QUARTZ_83"
SEARCH = "SEARCH_COBALT_29"
STAMP = "2026-09-13T00:00:00.000Z"


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _objects(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return [_object(item) for item in value]


def _events(text: str) -> list[dict[str, object]]:
    return [
        _object(json.loads(line[6:])) for line in text.splitlines() if line.startswith("data: ")
    ]


def _of_type(events: list[dict[str, object]], kind: str) -> list[dict[str, object]]:
    return [event for event in events if event.get("eventType") == kind]


def _event(events: list[dict[str, object]], kind: str) -> dict[str, object]:
    found = _of_type(events, kind)
    assert found, f"기대 이벤트가 없다: {kind}"
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
        "traceId": "trace-envelope-c",
    }


def _patient() -> dict[str, object]:
    return {
        "id": "patient-c",
        "caseLabel": "CASE-731",
        "status": "ACTIVE",
        "age": 47,
        "sex": "FEMALE",
        "diagnoses": ["DIAG_IVORY_17", "DIAG_AMBER_42"],
        "medications": ["MED_ONYX_63", "MED_TOPAZ_91"],
        "allergies": ["ALLERGY_JADE_28", "ALLERGY_PEARL_54"],
        "clinicalNotes": "NOTE_GARNET_76",
        "version": 1,
    }


def _evidence() -> list[dict[str, object]]:
    return [
        {
            "id": evidence_id,
            "guidelineId": f"guideline-{index}",
            "guidelineVersionId": f"version-{index}",
            "guidelineTitle": f"합성 지침 {index}",
            "version": "1.0",
            "sectionPath": ["합성 절", f"항목 {index}"],
            "excerpt": excerpt,
            "sourceUrl": f"https://source.invalid/evidence/{index}",
        }
        for index, (evidence_id, excerpt) in enumerate(
            [
                ("EID_ZULU_61", "EXCERPT_RUBY_32"),
                ("EID_ALPHA_24", "EXCERPT_OPAL_58"),
                ("EID_MIKE_87", "EXCERPT_BERYL_94"),
            ]
        )
    ]


class ScriptedChatModel(BaseChatModel):
    """메시지를 기록하고 합성 조각·실패·대기 지점을 주입하는 로컬 모델이다."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    chunks: list[str] = Field(default_factory=list)
    calls: list[list[BaseMessage]] = Field(default_factory=list)
    fail: bool = False
    pause_after: int | None = None
    delay: float = 0.0
    paused: asyncio.Event = Field(default_factory=asyncio.Event)
    gate: asyncio.Event = Field(default_factory=asyncio.Event)

    @property
    def _llm_type(self) -> str:
        return "synthetic-composite-c"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        if self.fail:
            raise RuntimeError("synthetic classifier failure")
        message = AIMessage(content="".join(self.chunks))
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        self.calls.append(list(messages))
        for index, piece in enumerate(self.chunks):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
            if index == self.pause_after:
                self.paused.set()
                if self.delay:
                    await asyncio.sleep(self.delay)
                else:
                    await self.gate.wait()
        usage: UsageMetadata = {"input_tokens": 37, "output_tokens": 19, "total_tokens": 56}
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                usage_metadata=usage,
                response_metadata={"model_name": "synthetic-model-c"},
            )
        )


def _classifier(route: str = "COMPOSITE", *, fail: bool = False) -> ScriptedChatModel:
    labels = ["CASE-731"] if route in {"PATIENT", "COMPOSITE"} else []
    return ScriptedChatModel(
        chunks=[json.dumps({"route": route, "patient_labels": labels})], fail=fail
    )


def _synthesis(
    answer: str = "합성 답변 [1] [2] [3]", *, insufficient: bool = False
) -> ScriptedChatModel:
    verdict = "true" if insufficient else "false"
    return ScriptedChatModel(
        chunks=['{"insufficientEvidence": ', verdict, ', "answer": ', json.dumps(answer), "}"]
    )


class RecordingBackend:
    """BE 계약 모양의 응답만 합성하고 요청·발신 이벤트를 보존한다."""

    def __init__(
        self,
        *,
        count: int = 3,
        abstain_reason: str | None = None,
        unavailable_path: str | None = None,
        evidence_error: bool = False,
        delay_first_abstention: bool = False,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.sent_events: list[dict[str, object]] = []
        self.patient = _patient()
        self.evidence = _evidence()[:count]
        self.gated: dict[str, object] = {
            "eventType": "evidence.gated",
            "abstainReason": abstain_reason,
            "evidenceCount": 0 if abstain_reason else count,
            "retrievalPolicyVersion": POLICY,
            "searchQuestion": SEARCH,
        }
        self.error: dict[str, object] = {
            "eventType": "error",
            "code": "LLM_UNAVAILABLE",
            "message": "합성 근거 도구 실패",
            "retryable": True,
            "traceId": "TRACE_BE_TUNGSTEN_46",
        }
        self.unavailable_path = unavailable_path
        self.evidence_error = evidence_error
        self.delay_first_abstention = delay_first_abstention
        self.abstention_delayed = False
        self.transport = httpx.MockTransport(self._handle)

    def bodies(self, path: str) -> list[dict[str, object]]:
        return [
            _object(json.loads(request.content))
            for request in self.requests
            if request.url.path == path
        ]

    def finish(self) -> dict[str, object]:
        bodies = self.bodies(FINISH_PATH)
        assert bodies, "완결 요청이 없다"
        return bodies[-1]

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == self.unavailable_path:
            raise httpx.ConnectError("synthetic backend failure", request=request)
        if path == ACCEPT_PATH:
            return httpx.Response(
                201,
                json=_envelope(
                    {"userMessageId": "user-c", "assistantMessageId": "assistant-c"}, created=True
                ),
            )
        if path == PATIENT_PATH:
            return httpx.Response(
                200, json=_envelope({"outcome": "RESOLVED", "patient": self.patient})
            )
        if path == EVIDENCE_PATH:
            events: list[dict[str, object]] = [
                {"eventType": "retrieval.started", "requestId": REQUEST_ID}
            ]
            if self.evidence_error:
                events.append(self.error)
            else:
                events.extend(
                    [{"eventType": "retrieval.progress", "stage": "embedded"}, self.gated]
                )
                if self.gated["abstainReason"] is None:
                    events.extend(
                        {
                            "eventType": "retrieval.evidence",
                            "index": index,
                            "total": len(self.evidence),
                            "evidence": evidence,
                        }
                        for index, evidence in enumerate(self.evidence)
                    )
                events.append({"eventType": "retrieval.completed"})
            self.sent_events.extend(events)
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
        if path == FINISH_PATH:
            body = _object(json.loads(request.content))
            if (
                self.delay_first_abstention
                and body.get("status") == "ABSTAINED"
                and not self.abstention_delayed
            ):
                self.abstention_delayed = True
                await asyncio.sleep(30)
            message: dict[str, object] = {
                "id": "assistant-c",
                "role": "ASSISTANT",
                "content": body.get("content", "") if body.get("status") == "COMPLETED" else "",
                "status": body.get("status"),
                "responseLang": "ko",
                "citations": [],
                "createdAt": STAMP,
            }
            if body.get("status") == "ABSTAINED":
                message["abstainReason"] = {
                    "beyond_cutoff": "질문과 충분히 관련된 지침 근거를 찾지 못했습니다.",
                    "no_candidates": "검색 조건에 해당하는 지침 근거를 찾지 못했습니다.",
                    "insufficient_evidence": "찾은 지침 근거만으로는 이 질문에 답하기 어렵습니다.",
                    "out_of_scope": (
                        "임상 지침이나 환자 한 명의 기록에 관한 질문에만 답할 수 있습니다."
                    ),
                }.get(str(body.get("abstainReason")), "합성 기권 안내")
            return httpx.Response(200, json=_envelope(message))
        raise AssertionError(f"준비하지 않은 BE 요청: {request.method} {path}")


def _app(
    backend: RecordingBackend,
    classifier: ScriptedChatModel,
    synthesis: ScriptedChatModel,
    *,
    run_deadline: float = 150.0,
) -> FastAPI:
    return create_app(
        ServiceSettings(
            be_origin="http://backend.test", agent_tracing_enabled="", openai_api_key=""
        ),
        backend_transport=backend.transport,
        classifier_model=classifier,
        synthesis_model=synthesis,
        heartbeat_interval=60.0,
        run_deadline=run_deadline,
    )


@contextmanager
def _client(app: FastAPI) -> Iterator[TestClient]:
    try:
        with TestClient(app) as client:
            yield client
    finally:
        langsmith.configure(enabled=None)


def _run(
    backend: RecordingBackend,
    *,
    classifier: ScriptedChatModel | None = None,
    synthesis: ScriptedChatModel | None = None,
    question: str = QUESTION,
    run_deadline: float = 150.0,
) -> list[dict[str, object]]:
    app = _app(
        backend,
        classifier if classifier is not None else _classifier(),
        synthesis if synthesis is not None else _synthesis(),
        run_deadline=run_deadline,
    )
    with _client(app) as client:
        response = client.post(
            STREAM_PATH,
            json={"content": question, "clientRequestId": REQUEST_ID},
            headers={"Cookie": _cookie(), "X-CSRF-Protection": "csrf-c"},
        )
    assert response.status_code == 200
    return _events(response.text)


def test_patient_tool_precedes_evidence_tool() -> None:
    """기준 37: 환자 도구와 근거 도구가 모두 호출되고 환자 도구가 앞선다."""
    backend = RecordingBackend()
    _run(backend)
    paths = [request.url.path for request in backend.requests]
    assert PATIENT_PATH in paths
    assert EVIDENCE_PATH in paths
    assert paths.index(PATIENT_PATH) < paths.index(EVIDENCE_PATH)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("CASE-731   합성 처치 지침을 검토해 줘  ", "합성 처치 지침을 검토해 줘"),
        ("합성 처치를   CASE-731   에게 적용할 지침은?", "합성 처치를 에게 적용할 지침은?"),
        ("  case-731 합성 처치와 CaSe-731   기록을 비교해 줘", "합성 처치와 기록을 비교해 줘"),
    ],
)
def test_evidence_query_removes_labels(question: str, expected: str) -> None:
    """기준 38: 앞·중간·대소문자 혼합 라벨을 전부 지우고 공백을 정규화한다."""
    backend = RecordingBackend()
    _run(backend, question=question)
    bodies = backend.bodies(EVIDENCE_PATH)
    assert bodies
    assert bodies[0]["query"] == expected


def test_answer_started_precedes_first_evidence_with_gate_count() -> None:
    """기준 39: 게이트의 근거 수를 가진 답변 시작이 첫 근거보다 앞선다."""
    backend = RecordingBackend()
    events = _run(backend)
    started = _event(events, "answer.started")
    first_evidence = _event(events, "retrieval.evidence")
    assert started["evidenceCount"] == backend.gated["evidenceCount"]
    assert events.index(started) < events.index(first_evidence)


def test_internal_gate_is_not_forwarded() -> None:
    """기준 40: BE가 게이트를 보내고 근거가 흘러도 내부 게이트는 브라우저에 없다."""
    backend = RecordingBackend()
    events = _run(backend)
    assert backend.gated in backend.sent_events
    assert _of_type(events, "retrieval.evidence")
    assert not _of_type(events, "evidence.gated")


@pytest.mark.parametrize("reason", ["beyond_cutoff", "no_candidates"])
def test_retrieval_abstention_skips_synthesis(reason: str) -> None:
    """기준 41: 근거 게이트 기권은 도구·완결에 도달하고 합성을 부르지 않는다."""
    backend = RecordingBackend(abstain_reason=reason)
    synthesis = _synthesis()
    _run(backend, synthesis=synthesis)
    assert backend.bodies(EVIDENCE_PATH)
    assert backend.bodies(FINISH_PATH)
    assert len(synthesis.calls) == 0


@pytest.mark.parametrize("reason", ["beyond_cutoff", "no_candidates"])
def test_retrieval_abstention_finishes_with_gate_reason(reason: str) -> None:
    """기준 42: 검색 기권은 각 게이트 사유 그대로 ABSTAINED 완결한다."""
    backend = RecordingBackend(abstain_reason=reason)
    _run(backend)
    finish = backend.finish()
    assert finish["status"] == "ABSTAINED"
    assert finish["abstainReason"] == backend.gated["abstainReason"]


def test_synthesis_receives_each_evidence_excerpt() -> None:
    """기준 43: 복합 합성 입력에 각 근거의 원문 표지가 들어간다."""
    backend = RecordingBackend()
    synthesis = _synthesis()
    _run(backend, synthesis=synthesis)
    assert synthesis.calls
    prompt = "\n".join(str(message.content) for message in synthesis.calls[0])
    for evidence in backend.evidence:
        excerpt = evidence["excerpt"]
        assert isinstance(excerpt, str)
        assert excerpt in prompt


@pytest.mark.parametrize("field", ["diagnoses", "medications", "allergies", "clinicalNotes"])
def test_synthesis_receives_each_patient_field(field: str) -> None:
    """기준 43: 복합 합성 입력에 기록 필드별 모든 항목의 표지가 들어간다."""
    backend = RecordingBackend()
    synthesis = _synthesis()
    _run(backend, synthesis=synthesis)
    assert synthesis.calls
    prompt = "\n".join(str(message.content) for message in synthesis.calls[0])
    value = backend.patient[field]
    markers = [value] if isinstance(value, str) else cast(list[str], value)
    for marker in markers:
        assert marker in prompt


class ASGIProbe:
    """같은 루프의 ASGI 전송을 관측하고 원하는 시점에 연결을 끊는다."""

    def __init__(self) -> None:
        self.disconnect = asyncio.Event()
        self.delta_sent = asyncio.Event()
        self.messages: list[Message] = []
        self.request_sent = False
        self.scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": STREAM_PATH,
            "raw_path": STREAM_PATH.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"content-type", b"application/json"),
                (b"cookie", _cookie().encode()),
                (b"x-csrf-protection", b"csrf-c"),
            ],
            "client": ("127.0.0.1", 5000),
            "server": ("testserver", 80),
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
        if _of_type(self.events(), "answer.delta"):
            self.delta_sent.set()

    def events(self) -> list[dict[str, object]]:
        body = b"".join(
            cast(bytes, message.get("body", b""))
            for message in self.messages
            if message["type"] == "http.response.body"
        )
        # 아직 전송 중인 마지막 프레임은 다음 본문 조각까지 기다린다.
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


async def test_no_delta_before_verdict_closes() -> None:
    """기준 44: 판정 값 전에는 델타가 없고 게이트를 연 뒤에는 델타가 나간다."""
    backend = RecordingBackend()
    synthesis = ScriptedChatModel(
        chunks=['{"insufficientEvidence": ', 'false, "answer": "합성 답변 [1]"}'],
        pause_after=0,
    )
    app = _app(backend, _classifier(), synthesis)
    probe = ASGIProbe()

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(app(probe.scope, probe.receive, probe.send))
            try:
                await _signal_or_failure(synthesis.paused, task)
                await asyncio.sleep(0.2)
                observed = probe.events()
                assert _of_type(observed, "retrieval.completed")
                assert not _of_type(observed, "answer.delta")
                synthesis.gate.set()
                await task
                assert _of_type(probe.events(), "answer.delta")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    try:
        await asyncio.wait_for(scenario(), timeout=30)
    finally:
        langsmith.configure(enabled=None)


def test_generation_abstention_emits_no_delta() -> None:
    """기준 45: 생성 판정이 true면 합성·기권 이벤트는 있지만 델타는 없다."""
    backend = RecordingBackend()
    synthesis = _synthesis("ANSWER_SAPPHIRE_65", insufficient=True)
    events = _run(backend, synthesis=synthesis)
    assert synthesis.calls
    assert _of_type(events, "answer.abstained")
    assert not _of_type(events, "answer.delta")


def test_generation_abstention_finishes_with_insufficient_evidence() -> None:
    """기준 46: 생성 판정 기권은 ABSTAINED·insufficient_evidence로 완결한다."""
    backend = RecordingBackend()
    _run(backend, synthesis=_synthesis(insufficient=True))
    finish = backend.finish()
    assert finish["status"] == "ABSTAINED"
    assert finish["abstainReason"] == "insufficient_evidence"


def test_generation_abstention_keeps_generation() -> None:
    """기준 47: 생성 기권 완결에도 모델·프롬프트 버전·입출력 토큰 필드가 있다."""
    backend = RecordingBackend()
    _run(backend, synthesis=_synthesis(insufficient=True))
    finish = backend.finish()
    assert finish["status"] == "ABSTAINED"
    assert "generation" in finish
    generation = _object(finish["generation"])
    for field in ("model", "promptVersion", "inputTokens", "outputTokens"):
        assert field in generation


def test_citations_exclude_unused_marker() -> None:
    """기준 48: 답변이 쓴 1·3은 인용에 있고 쓰지 않은 2는 없다."""
    backend = RecordingBackend(count=3)
    _run(backend, synthesis=_synthesis("합성 주장 [1] 및 [3]"))
    markers = {citation["marker"] for citation in _objects(backend.finish()["citations"])}
    assert 1 in markers
    assert 3 in markers
    assert 2 not in markers


def test_citations_exclude_out_of_range_marker() -> None:
    """기준 49: 근거 두 건에 답변이 쓴 1은 인용하고 범위를 넘는 5는 제외한다."""
    backend = RecordingBackend(count=2)
    _run(backend, synthesis=_synthesis("합성 주장 [1] 및 [5]"))
    markers = {citation["marker"] for citation in _objects(backend.finish()["citations"])}
    assert 1 in markers
    assert 5 not in markers


def test_citation_ids_follow_emitted_evidence_order() -> None:
    """기준 50: 각 마커는 id 사전순이 아니라 브라우저 근거 프레임 순서에 대응한다."""
    backend = RecordingBackend(count=3)
    events = _run(backend, synthesis=_synthesis("합성 주장 [1] [2] [3]"))
    frames = _of_type(events, "retrieval.evidence")
    assert len(frames) == 3
    citations = _objects(backend.finish()["citations"])
    for marker, frame in enumerate(frames, start=1):
        matches = [citation for citation in citations if citation["marker"] == marker]
        assert matches
        assert matches[0]["evidenceId"] == _object(frame["evidence"])["id"]


def test_generation_preserves_gate_search_metadata() -> None:
    """기준 51: 완결 generation의 검색 정책·검색 질문은 게이트가 준 값이다."""
    backend = RecordingBackend()
    _run(backend)
    generation = _object(backend.finish()["generation"])
    assert generation["retrievalPolicyVersion"] == backend.gated["retrievalPolicyVersion"]
    assert generation["searchQuestion"] == backend.gated["searchQuestion"]


def test_classifier_failure_emits_retryable_llm_error() -> None:
    """기준 52: 수락 이벤트 뒤 분류 실패를 재시도 가능한 LLM_UNAVAILABLE로 알린다."""
    events = _run(RecordingBackend(), classifier=_classifier(fail=True))
    accepted = _event(events, "message.accepted")
    error = _event(events, "error")
    assert events.index(accepted) < events.index(error)
    assert error["code"] == "LLM_UNAVAILABLE"
    assert error["retryable"] is True


def test_classifier_failure_finishes_failed() -> None:
    """기준 53: 분류 LLM 실패 시 수락된 턴을 FAILED 완결한다."""
    backend = RecordingBackend()
    _run(backend, classifier=_classifier(fail=True))
    assert backend.finish()["status"] == "FAILED"


def test_run_deadline_emits_llm_timeout() -> None:
    """기준 54: 주입한 실행 상한이 긴 합성 대기를 끊고 LLM_TIMEOUT을 보낸다."""
    synthesis = ScriptedChatModel(
        chunks=['{"insufficientEvidence": ', 'false, "answer": "합성 답변"}'],
        pause_after=0,
        delay=30.0,
    )
    started = time.monotonic()
    events = _run(RecordingBackend(), synthesis=synthesis, run_deadline=0.5)
    elapsed = time.monotonic() - started
    assert synthesis.calls
    assert synthesis.paused.is_set()
    assert _event(events, "error")["code"] == "LLM_TIMEOUT"
    assert elapsed < 5.0


@pytest.mark.parametrize("route", ["PATIENT", "COMPOSITE", "OTHER"])
def test_run_deadline_finishes_failed_for_each_route(route: str) -> None:
    """기준 55: 환자·복합 합성 및 기타 첫 기권 완결의 상한 초과는 FAILED 완결한다."""
    backend = RecordingBackend(delay_first_abstention=route == "OTHER")
    synthesis = ScriptedChatModel(
        chunks=["합성 조각" if route == "PATIENT" else '{"insufficientEvidence": '],
        pause_after=0,
        delay=30.0,
    )
    _run(backend, classifier=_classifier(route), synthesis=synthesis, run_deadline=0.5)
    if route == "OTHER":
        assert backend.abstention_delayed
        assert "ABSTAINED" in [body["status"] for body in backend.bodies(FINISH_PATH)]
    else:
        assert synthesis.calls
        assert synthesis.paused.is_set()
    assert "FAILED" in [body["status"] for body in backend.bodies(FINISH_PATH)]


async def test_disconnect_finishes_cancelled_after_delta() -> None:
    """기준 56: 환자 답변 델타를 실제 전송한 뒤 클라이언트가 끊으면 CANCELLED다."""
    backend = RecordingBackend()
    synthesis = ScriptedChatModel(chunks=["ANSWER_EMERALD_39"], pause_after=0)
    app = _app(backend, _classifier("PATIENT"), synthesis)
    probe = ASGIProbe()

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(app(probe.scope, probe.receive, probe.send))
            try:
                await _signal_or_failure(probe.delta_sent, task)
                await _signal_or_failure(synthesis.paused, task)
                assert _of_type(probe.events(), "answer.delta")
                probe.disconnect.set()
                await task
                assert "CANCELLED" in [body["status"] for body in backend.bodies(FINISH_PATH)]
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    try:
        await asyncio.wait_for(scenario(), timeout=30)
    finally:
        langsmith.configure(enabled=None)


@pytest.mark.parametrize(
    ("route", "path"),
    [("PATIENT", PATIENT_PATH), ("COMPOSITE", EVIDENCE_PATH), ("GUIDELINE", GUIDELINE_PATH)],
)
def test_backend_tool_connection_failure_emits_retryable_error(route: str, path: str) -> None:
    """기준 57: 각 BE 도구 연결 실패를 수락 뒤 재시도 가능한 BE 장애로 알린다."""
    backend = RecordingBackend(unavailable_path=path)
    events = _run(backend, classifier=_classifier(route))
    assert backend.bodies(path)
    accepted = _event(events, "message.accepted")
    error = _event(events, "error")
    assert events.index(accepted) < events.index(error)
    assert error["code"] == "AGENT_BACKEND_UNAVAILABLE"
    assert error["retryable"] is True


def test_evidence_tool_error_is_forwarded_unchanged() -> None:
    """기준 58: 근거 도구의 오류 이벤트를 BE traceId까지 JSON 동일하게 흘린다."""
    backend = RecordingBackend(evidence_error=True)
    events = _run(backend)
    assert backend.error in backend.sent_events
    assert _event(events, "error") == backend.error


def test_evidence_tool_error_finishes_failed() -> None:
    """기준 59: 턴을 닫지 않는 근거 도구의 오류를 받으면 FAILED 완결한다."""
    backend = RecordingBackend(evidence_error=True)
    _run(backend)
    assert backend.error in backend.sent_events
    assert backend.finish()["status"] == "FAILED"
