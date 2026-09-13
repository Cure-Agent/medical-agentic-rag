"""기준 14~36의 실행 경로 표·지침 중계·환자 합성을 가짜 경계로 검증한다."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

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

ASSISTANT_ID = "01K00000000000000000000002"
USER_ID = "01K00000000000000000000001"
REQUEST_ID = "routing-b-request"
CLASSIFIER_VERSION = "gpt-5.4-mini/agent-route-v1"
PATIENT_PIECES = ("기록을 확인했습니다. ", "합성 설명입니다.\n", "마지막 안내입니다.")
TOOL_SUFFIXES = ("/guideline-answer", "/patient", "/guideline-evidence")


class ScriptedChatModel(BaseChatModel):
    """입력 메시지와 호출을 보존하고 합성 텍스트·사용량만 반환한다."""

    chunks: list[str] = Field(default_factory=list)
    input_tokens: int = 137
    output_tokens: int = 29
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "routing-scripted-fake"

    def _usage(self) -> UsageMetadata:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
        }

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        message = AIMessage(content="".join(self.chunks), usage_metadata=self._usage())
        return ChatResult(generations=[ChatGeneration(message=message)])

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
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                usage_metadata=self._usage(),
                response_metadata={"model_name": "synthetic-routing-model"},
            )
        )


def _object(value: object) -> dict[str, object]:
    """외부 JSON 객체의 타입을 좁힌다."""
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _envelope(data: dict[str, object], *, created: bool = False) -> dict[str, object]:
    return {
        "success": True,
        "code": "CREATED" if created else "SUCCESS",
        "message": "생성되었습니다." if created else "요청에 성공하였습니다.",
        "data": data,
        "page": None,
        "timestamp": "2026-09-13T00:00:00.000Z",
        "traceId": "01K00000000000000000000003",
    }


def _message(status: object, content: object = "") -> dict[str, object]:
    message: dict[str, object] = {
        "id": ASSISTANT_ID,
        "role": "ASSISTANT",
        "content": content,
        "status": status,
        "responseLang": "ko",
        "citations": [],
        "createdAt": "2026-09-13T00:00:00.000Z",
    }
    if status == "ABSTAINED":
        message["abstainReason"] = "합성 기권 안내입니다."
    return message


def _patient() -> dict[str, object]:
    return {
        "id": "synthetic-patient-id",
        "caseLabel": "CASE-303",
        "age": 47,
        "sex": "FEMALE",
        "bmi": 23.4,
        "heightCm": 164,
        "weightKg": 63,
        "waistCm": 78,
        "status": "ACTIVE",
        "version": 1,
        "diagnoses": ["DX_KAPPA_731", "DX_LAMBDA_842"],
        "medications": ["MED_SIGMA_953", "MED_TAU_164"],
        "allergies": ["ALG_UPSILON_275", "ALG_PHI_386"],
        "clinicalNotes": "NOTE_CHI_497",
    }


def _evidence(index: int) -> dict[str, object]:
    return {
        "id": f"synthetic-evidence-{index}",
        "guidelineId": "synthetic-guideline",
        "guidelineVersionId": "synthetic-version",
        "guidelineTitle": f"합성 지침 {index}",
        "version": "1.0",
        "sectionPath": ["합성 절", f"항목 {index}"],
        "excerpt": f"합성 근거 본문 {index}.\n조건을 설명합니다.",
        "sourceUrl": "https://example.invalid/synthetic-guideline",
    }


def _evidence_frames() -> list[dict[str, object]]:
    return [
        {
            "eventType": "retrieval.evidence",
            "index": index,
            "total": 2,
            "evidence": _evidence(index),
        }
        for index in range(2)
    ]


def _guideline_events(terminal: str) -> list[dict[str, object]]:
    """중첩 객체·줄바꿈·서로 다른 진행 단계를 포함한 지침 SSE를 만든다."""
    events: list[dict[str, object]] = [
        {"eventType": "retrieval.started", "requestId": REQUEST_ID},
        {"eventType": "retrieval.progress", "stage": "embedded"},
        {"eventType": "retrieval.progress", "stage": "searched", "candidates": 7},
        {"eventType": "answer.started", "evidenceCount": 2},
        *_evidence_frames(),
        {"eventType": "retrieval.completed"},
    ]
    # 생성 게이트 기권에는 델타가 없고, 실패는 첫 델타 이후에도 발생할 수 있다.
    if terminal != "answer.abstained":
        events.extend(
            [
                {
                    "eventType": "answer.delta",
                    "messageId": ASSISTANT_ID,
                    "seq": index,
                    "delta": piece,
                }
                for index, piece in enumerate(("합성 지침 답변. ", "다음 설명.\n"))
            ]
        )
    if terminal == "error":
        events.append(
            {
                "eventType": "error",
                "code": "LLM_UNAVAILABLE",
                "message": "합성 상류 실패입니다.",
                "retryable": True,
                "traceId": "01K00000000000000000000004",
            }
        )
    elif terminal == "answer.abstained":
        events.append(
            {
                "eventType": terminal,
                "message": _message("ABSTAINED"),
                "reason": "합성 기권 안내입니다.",
                "missingInformation": [],
            }
        )
    else:
        events.append(
            {
                "eventType": terminal,
                "message": {
                    **_message("COMPLETED", "합성 지침 답변. 다음 설명.\n"),
                    "answerKind": "GUIDELINE_ANSWER",
                },
            }
        )
    return events


def _sse(events: list[dict[str, object]]) -> httpx.Response:
    payload = ": ping\n\n".join(
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events
    )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream; charset=utf-8"},
        content=payload.encode(),
    )


class RecordingBackend:
    """저장 동작을 구현하지 않고 내부 API 계약의 응답만 합성한다."""

    def __init__(self, outcome: str, terminal: str) -> None:
        self.requests: list[httpx.Request] = []
        self.patient = _patient()
        self.outcome = outcome
        self.guideline_events = _guideline_events(terminal)
        self.transport = httpx.MockTransport(self._handle)

    def requests_to(self, suffix: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path.endswith(suffix)]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/turns"):
            return httpx.Response(
                201,
                json=_envelope(
                    {"userMessageId": USER_ID, "assistantMessageId": ASSISTANT_ID},
                    created=True,
                ),
            )
        if path.endswith("/patient"):
            data: dict[str, object] = {"outcome": self.outcome}
            if self.outcome == "RESOLVED":
                data["patient"] = self.patient
            return httpx.Response(200, json=_envelope(data))
        if path.endswith("/guideline-answer"):
            return _sse(self.guideline_events)
        if path.endswith("/guideline-evidence"):
            return _sse(
                [
                    {"eventType": "retrieval.started", "requestId": REQUEST_ID},
                    {
                        "eventType": "evidence.gated",
                        "abstainReason": None,
                        "evidenceCount": 2,
                        "retrievalPolicyVersion": "synthetic-retrieval-v1",
                        "searchQuestion": "합성 검색 질문",
                    },
                    *_evidence_frames(),
                    {"eventType": "retrieval.completed"},
                ]
            )
        if path.endswith("/finish"):
            body = _object(json.loads(request.content))
            return httpx.Response(
                200, json=_envelope(_message(body.get("status"), body.get("content", "")))
            )
        raise AssertionError(f"정의하지 않은 가짜 BE 경로: {path}")


@dataclass
class TurnResult:
    backend: RecordingBackend
    classifier: ScriptedChatModel
    synthesis: ScriptedChatModel
    events: list[dict[str, object]]

    def finish(self) -> dict[str, object]:
        requests = self.backend.requests_to("/finish")
        assert len(requests) == 1
        return _object(json.loads(requests[0].content))

    def deltas(self) -> list[dict[str, object]]:
        return [event for event in self.events if event.get("eventType") == "answer.delta"]


def _run(
    route: str = "PATIENT",
    labels: tuple[str, ...] = ("CASE-303",),
    *,
    outcome: str = "RESOLVED",
    terminal: str = "answer.completed",
) -> TurnResult:
    """각 호출에 독립된 앱·가짜를 만들고 끝난 SSE 본문만 파싱한다."""
    backend = RecordingBackend(outcome, terminal)
    classifier = ScriptedChatModel(
        chunks=[json.dumps({"route": route, "patient_labels": list(labels)})],
        cache=False,
    )
    composite = route in {"GUIDELINE", "COMPOSITE"} and len(labels) == 1
    chunks = (
        ['{"insufficientEvidence": false, "answer": "', "합성 복합 답변", '"}']
        if composite
        else ["", PATIENT_PIECES[0], "", PATIENT_PIECES[1], PATIENT_PIECES[2]]
    )
    synthesis = ScriptedChatModel(chunks=chunks, cache=False)
    try:
        langsmith.configure(enabled=False)
        app = create_app(
            ServiceSettings(
                be_origin="http://backend.test:4312",
                agent_tracing_enabled="",
                openai_api_key="",
            ),
            backend_transport=backend.transport,
            classifier_model=classifier,
            synthesis_model=synthesis,
            heartbeat_interval=60.0,
            run_deadline=150.0,
        )
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/agent/conversations/synthetic-conversation/messages/stream",
                json={
                    "content": f"{' '.join(labels)} 합성 질문을 설명해 주세요.",
                    "clientRequestId": REQUEST_ID,
                    "responseLang": "ko",
                },
                # JWT로 읽히지 않는 합성 자격은 선검사 없이 가짜 BE가 수락한다.
                headers={"Cookie": "access_token=synthetic-opaque", "X-CSRF-Protection": "1"},
            )
        assert response.status_code == 200
        events = [
            _object(json.loads(line[6:]))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        return TurnResult(backend, classifier, synthesis, events)
    finally:
        langsmith.configure(enabled=None)


@pytest.mark.parametrize(
    ("route", "labels", "counts"),
    [
        ("GUIDELINE", (), (1, 0, 0)),
        ("PATIENT", ("CASE-303",), (0, 1, 0)),
        ("COMPOSITE", ("CASE-303",), (0, 1, 1)),
    ],
)
def test_matching_route_calls_only_expected_tools(
    route: str, labels: tuple[str, ...], counts: tuple[int, int, int]
) -> None:
    """기준 14: 판정과 라벨이 맞물린 세 경우에 지정된 도구만 각각 한 번 호출한다."""
    result = _run(route, labels)
    for suffix, count in zip(TOOL_SUFFIXES, counts, strict=True):
        assert len(result.backend.requests_to(suffix)) == count


def test_guideline_with_label_executes_composite_tools() -> None:
    """기준 15: 라벨 한 개가 있는 지침 판정은 환자·근거 도구로 실행한다."""
    result = _run("GUIDELINE")
    assert len(result.backend.requests_to("/patient")) == 1
    assert len(result.backend.requests_to("/guideline-evidence")) == 1
    assert result.backend.requests_to("/guideline-answer") == []


def test_composite_without_label_executes_guideline_tool() -> None:
    """기준 16: 라벨 없는 복합 판정은 지침 도구로 실행한다."""
    result = _run("COMPOSITE", ())
    assert len(result.backend.requests_to("/guideline-answer")) == 1
    assert result.backend.requests_to("/patient") == []
    assert result.backend.requests_to("/guideline-evidence") == []


def test_patient_without_label_calls_no_tools_and_abstains() -> None:
    """기준 17·18: 라벨 없는 환자 판정은 도구 없이 patient_unresolved로 기권한다."""
    result = _run("PATIENT", ())
    finish = result.finish()
    for suffix in TOOL_SUFFIXES:
        assert result.backend.requests_to(suffix) == []
    assert finish["status"] == "ABSTAINED"
    assert finish["abstainReason"] == "patient_unresolved"


@pytest.mark.parametrize("route", ["GUIDELINE", "PATIENT", "COMPOSITE", "OTHER"])
def test_distinct_labels_call_no_tools_and_abstain(route: str) -> None:
    """기준 19·20: 서로 다른 두 라벨은 모든 판정에서 도구 없이 범위 밖으로 기권한다."""
    result = _run(route, ("CASE-101", "CASE-202"))
    finish = result.finish()
    for suffix in TOOL_SUFFIXES:
        assert result.backend.requests_to(suffix) == []
    assert finish["status"] == "ABSTAINED"
    assert finish["abstainReason"] == "out_of_scope"


def test_case_only_label_duplicates_count_as_one() -> None:
    """기준 21: 대소문자만 다른 두 라벨은 환자 한 명으로 세고 범위 밖 기권을 하지 않는다."""
    result = _run("PATIENT", ("CASE-303", "case-303"))
    assert len(result.backend.requests_to("/patient")) == 1
    finish = result.finish()
    assert not (
        finish.get("status") == "ABSTAINED" and finish.get("abstainReason") == "out_of_scope"
    )


@pytest.mark.parametrize("labels", [(), ("CASE-303",)])
def test_other_stops_after_classifier_and_abstains(labels: tuple[str, ...]) -> None:
    """기준 22·23: 기타 판정은 분류 한 번 뒤 합성 없이 범위 밖으로 기권한다."""
    result = _run("OTHER", labels)
    finish = result.finish()
    assert len(result.classifier.calls) == 1
    assert result.synthesis.calls == []
    assert finish["status"] == "ABSTAINED"
    assert finish["abstainReason"] == "out_of_scope"


def test_guideline_request_carries_classifier_version() -> None:
    """기준 24: 지침 도구 요청에 고정된 분류기 버전을 싣는다."""
    result = _run("GUIDELINE", ())
    requests = result.backend.requests_to("/guideline-answer")
    assert len(requests) == 1
    assert _object(json.loads(requests[0].content))["classifierVersion"] == CLASSIFIER_VERSION


@pytest.mark.parametrize("route", ["OTHER", "PATIENT"])
def test_finish_request_carries_classifier_version(route: str) -> None:
    """기준 24: 기타·환자 완결 요청에 각각 고정된 분류기 버전을 싣는다."""
    result = _run(route)
    assert result.finish()["classifierVersion"] == CLASSIFIER_VERSION


@pytest.mark.parametrize("terminal", ["answer.completed", "answer.abstained", "error"])
def test_guideline_preserves_event_sequence_and_payload(terminal: str) -> None:
    """기준 25: 세 종결의 지침 이벤트를 공통 진행 뒤 순서와 JSON 내용 그대로 중계한다."""
    result = _run("GUIDELINE", (), terminal=terminal)
    assert len(result.backend.requests_to("/guideline-answer")) == 1
    assert len(result.events) == len(result.backend.guideline_events) + 2
    assert result.events[0].get("eventType") == "message.accepted"
    assert result.events[1].get("eventType") == "agent.progress"
    assert result.events[1].get("stage") == "routed"
    assert result.events[2:] == result.backend.guideline_events


@pytest.mark.parametrize("terminal", ["answer.completed", "answer.abstained", "error"])
def test_guideline_does_not_call_finish(terminal: str) -> None:
    """기준 26: 지침 도구의 각 종결을 중계한 경로는 완결 API를 호출하지 않는다."""
    result = _run("GUIDELINE", (), terminal=terminal)
    assert len(result.backend.requests_to("/guideline-answer")) == 1
    assert result.backend.guideline_events[-1] in result.events
    assert result.backend.requests_to("/finish") == []


def test_resolved_patient_emits_loaded_progress() -> None:
    """기준 27: 환자 해석 성공 뒤 patient_loaded 진행 이벤트를 보낸다."""
    result = _run()
    assert any(
        event.get("eventType") == "agent.progress" and event.get("stage") == "patient_loaded"
        for event in result.events
    )


@pytest.mark.parametrize("field", ["diagnoses", "medications", "allergies", "clinicalNotes"])
def test_patient_record_fields_reach_synthesis(field: str) -> None:
    """기준 28: 환자 기록의 각 필드와 배열의 모든 항목이 합성 입력에 실린다."""
    result = _run()
    assert result.synthesis.calls
    value = result.backend.patient[field]
    markers = cast(list[str], value) if isinstance(value, list) else [cast(str, value)]
    # 메시지 위치나 프롬프트 문구를 고정하지 않고 기록 값의 전달만 확인한다.
    inputs = "\n".join(str(message.content) for message in result.synthesis.calls[0])
    for marker in markers:
        assert marker in inputs


def test_patient_stream_preserves_nonempty_pieces() -> None:
    """기준 29: 빈 조각을 제외한 합성 조각을 같은 순서·문자열의 델타로 보낸다."""
    result = _run()
    assert [event["delta"] for event in result.deltas()] == list(PATIENT_PIECES)


def test_patient_delta_sequence_and_message_id() -> None:
    """기준 30: 환자 델타의 seq는 0부터 연속이고 messageId는 수락된 답변 id다."""
    result = _run()
    deltas = result.deltas()
    assert deltas
    assert [event["seq"] for event in deltas] == list(range(len(deltas)))
    assert all(event["messageId"] == ASSISTANT_ID for event in deltas)


def test_patient_finish_route_and_status() -> None:
    """기준 31: 환자 답변 완결은 PATIENT 경로와 COMPLETED 상태를 싣는다."""
    finish = _run().finish()
    assert finish["route"] == "PATIENT"
    assert finish["status"] == "COMPLETED"


def test_patient_finish_content_joins_emitted_deltas() -> None:
    """기준 32: 완결 본문은 발신 델타 전체이자 합성 조각 전체를 이은 텍스트다."""
    result = _run()
    deltas = result.deltas()
    assert deltas
    texts: list[str] = []
    for event in deltas:
        delta = event["delta"]
        assert isinstance(delta, str)
        texts.append(delta)
    content = result.finish()["content"]
    assert content == "".join(texts)
    assert content == "".join(PATIENT_PIECES)


def test_patient_generation_identifies_model_prompt_and_usage() -> None:
    """기준 33: 환자 생성 기록에 모델·프롬프트 버전과 가짜의 입력·출력 토큰 수가 있다."""
    result = _run()
    generation = _object(result.finish()["generation"])
    model = generation["model"]
    prompt = generation["promptVersion"]
    assert isinstance(model, str) and bool(model)
    assert isinstance(prompt, str) and bool(prompt)
    assert generation["inputTokens"] == result.synthesis.input_tokens
    assert generation["outputTokens"] == result.synthesis.output_tokens


def test_patient_generation_omits_retrieval_policy() -> None:
    """기준 34: 환자 생성 기록은 존재하지만 retrievalPolicyVersion 키는 없다."""
    result = _run()
    generation = _object(result.finish()["generation"])
    assert generation
    assert "retrievalPolicyVersion" not in generation


@pytest.mark.parametrize("outcome", ["NOT_FOUND", "AMBIGUOUS"])
def test_unresolved_patient_skips_synthesis_and_abstains(outcome: str) -> None:
    """기준 35·36: 환자 해석 실패 두 경우는 각각 합성 없이 patient_unresolved로 기권한다."""
    result = _run(outcome=outcome)
    assert len(result.backend.requests_to("/patient")) == 1
    finish = result.finish()
    assert result.synthesis.calls == []
    assert finish["status"] == "ABSTAINED"
    assert finish["abstainReason"] == "patient_unresolved"
