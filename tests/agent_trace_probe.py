"""격리된 자식 프로세스에서 합성 턴 하나의 추적 전송과 종결을 수집한다."""

import base64
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast


def main() -> None:
    """SDK를 가져오기 전에 로컬 캡처 서버와 환경을 준비한다."""
    captures: list[dict[str, object]] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        """압축을 광고하지 않고 수신한 원본 바이트를 보존한다."""

        def capture(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            with lock:
                captures.append({
                    "method": self.command,
                    "path": self.path,
                    "content_type": self.headers.get("Content-Type", ""),
                    "body": base64.b64encode(body).decode("ascii"),
                })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        do_GET = capture
        do_POST = capture
        do_PATCH = capture

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    os.environ["LANGSMITH_ENDPOINT"] = f"http://127.0.0.1:{server.server_port}"
    os.environ["LANGSMITH_API_KEY"] = "synthetic-local-capture-key"
    try:
        result = run_turn(sys.argv[1])
        with lock:
            result["captures"] = list(captures)
        print(json.dumps(result, ensure_ascii=True))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_turn(scenario: str) -> dict[str, Any]:
    """실제 외부 경계는 가짜 모델과 MockTransport로 모두 치환한다."""
    from collections.abc import AsyncIterator

    import httpx
    import langsmith
    from fastapi.testclient import TestClient
    from langchain_core.callbacks import (
        AsyncCallbackManagerForLLMRun,
        CallbackManagerForLLMRun,
    )
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
    from langchain_core.messages.ai import UsageMetadata
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
    from pydantic import Field

    from app.service.config import ServiceSettings
    from app.service.main import create_app

    class ScriptedChatModel(BaseChatModel):
        """표지는 repr에 노출하지 않고 정상 LangChain 콜백 경로를 탄다."""

        chunks: list[str] = Field(default_factory=list, repr=False)
        error_text: str | None = Field(default=None, repr=False)
        raised: bool = False
        input_tokens: int = 0

        @property
        def _llm_type(self) -> str:
            return "synthetic-trace-chat"

        def usage(self) -> UsageMetadata:
            return {
                "input_tokens": self.input_tokens,
                "output_tokens": 17,
                "total_tokens": self.input_tokens + 17,
            }

        def _generate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: CallbackManagerForLLMRun | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            message = AIMessage(content="".join(self.chunks), usage_metadata=self.usage())
            return ChatResult(generations=[ChatGeneration(message=message)])

        async def _astream(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: AsyncCallbackManagerForLLMRun | None = None,
            **kwargs: Any,
        ) -> AsyncIterator[ChatGenerationChunk]:
            for piece in self.chunks:
                yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
            if self.error_text is not None:
                self.raised = True
                raise RuntimeError(self.error_text)
            yield ChatGenerationChunk(
                message=AIMessageChunk(content="", usage_metadata=self.usage())
            )

    route = "PATIENT" if scenario in {"exception", "env_false"} else scenario.upper()
    question_marker = "QMARK7431"
    question = {
        "GUIDELINE": f"{question_marker} 합성 질환의 일반 지침은?",
        "PATIENT": f"{question_marker} CASE-701 기록을 설명해 주세요.",
        "COMPOSITE": f"{question_marker} CASE-701에게 합성 중재를 적용해도 되나요?",
        "OTHER": f"{question_marker} 합성 점심 메뉴를 골라 주세요.",
    }[route]
    markers = {
        "diagnoses": ["DXMARK2847", "DXMARK9632"],
        "medications": ["RXMARK3158", "RXMARK6429"],
        "allergies": ["ALMARK4269", "ALMARK7530"],
        "clinicalNotes": ["NOTEMARK5370"],
    }
    answer_marker = "ANSWERMARK6481"
    patient: dict[str, object] = {
        "id": "patient-synthetic-701", "caseLabel": "CASE-701", "age": 44,
        "sex": "FEMALE", "bmi": 22.1, "status": "ACTIVE", "birthYear": 1982,
        "heightCm": 164, "weightKg": 59.4, "waistCm": 73, "version": 1,
        "diagnoses": markers["diagnoses"], "medications": markers["medications"],
        "allergies": markers["allergies"], "clinicalNotes": markers["clinicalNotes"][0],
    }
    labels = ["CASE-701"] if route in {"PATIENT", "COMPOSITE"} else []
    classifier = ScriptedChatModel(
        chunks=[json.dumps({"route": route, "patient_labels": labels})], input_tokens=1237
    )
    pieces = [f"합성 답변 {answer_marker}"]
    if route == "COMPOSITE":
        pieces = ['{"insufficientEvidence": false, "answer": "', *pieces, '"}']
    synthesis = ScriptedChatModel(
        chunks=pieces,
        input_tokens=8675309,
        error_text=(
            f"synthetic failure {markers['clinicalNotes'][0]}"
            if scenario == "exception" else None
        ),
    )
    finishes: list[dict[str, Any]] = []
    backend_requests: list[dict[str, str]] = []
    stamp = "2026-09-13T00:00:00.000Z"
    guidance_markers = [
        "GUIDANCE_SUMMARY_QUARTZ_5401",
        "GUIDANCE_RATIONALE_OPAL_5402",
        "GUIDANCE_ALERT_BERYL_5403",
        "GUIDANCE_MISSING_TOPAZ_5404",
    ]
    guidance_allergy_markers = list(markers["allergies"])
    finish_guidances: list[dict[str, object]] = []
    completed_guidances: list[object] = []

    def guidance() -> dict[str, object]:
        """완결 응답에만 고유 표지를 넣고 기록의 알레르기 값을 함께 싣는다."""
        return {
            "id": "guidance-synthetic-5401",
            "patientId": patient["id"],
            "patientProfileSnapshotId": "snapshot-synthetic-5401",
            "summary": guidance_markers[0],
            "considerations": [{
                "title": "합성 검토 항목",
                "rationale": guidance_markers[1],
                "citations": [],
                "applicability": "CAUTION",
                "patientFactors": ["allergies"],
            }],
            "safetyAlerts": [{
                "severity": "WARNING",
                "description": " ".join([guidance_markers[2], *guidance_allergy_markers]),
                "citations": [],
            }],
            "missingInformation": [guidance_markers[3]],
            "reviewStatus": "DRAFT",
            "generatedAt": stamp,
        }

    def envelope(data: object, created: bool = False) -> dict[str, object]:
        return {
            "success": True, "code": "CREATED" if created else "SUCCESS",
            "message": "생성되었습니다." if created else "요청에 성공하였습니다.",
            "data": data, "page": None, "timestamp": stamp, "traceId": "synthetic-be-trace",
        }

    def message(status: str, content: str) -> dict[str, object]:
        result: dict[str, object] = {
            "id": "assistant-701", "role": "ASSISTANT", "content": content,
            "status": status, "responseLang": "ko", "citations": [], "createdAt": stamp,
        }
        if status == "ABSTAINED":
            result["abstainReason"] = (
                "임상 지침이나 환자 한 명의 기록에 관한 질문에만 답할 수 있습니다."
            )
        return result

    def sse(events: list[dict[str, object]]) -> httpx.Response:
        content = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    def backend(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        backend_requests.append({"method": request.method, "path": path})
        if path == "/api/v1/internal/agent/conversations/conversation-701/turns":
            return httpx.Response(201, json=envelope({
                "userMessageId": "user-701", "assistantMessageId": "assistant-701",
            }, created=True))
        prefix = "/api/v1/internal/agent/turns/assistant-701"
        if path == f"{prefix}/patient":
            return httpx.Response(
                200, json=envelope({"outcome": "RESOLVED", "patient": patient})
            )
        if path == f"{prefix}/finish":
            payload = cast(dict[str, Any], json.loads(request.content))
            finishes.append(payload)
            completed = message(payload["status"], payload.get("content", ""))
            if payload.get("route") == "COMPOSITE" and payload["status"] == "COMPLETED":
                completed["guidance"] = guidance()
            response = httpx.Response(200, json=envelope(completed))
            # 전송할 응답의 직렬화된 값으로 양성 대조를 보존한다.
            sent = response.json()["data"]
            if "guidance" in sent:
                finish_guidances.append(cast(dict[str, object], sent["guidance"]))
            return response
        if path == f"{prefix}/guideline-answer":
            completed = message("COMPLETED", "합성 지침 답변")
            completed["answerKind"] = "GUIDELINE_ANSWER"
            return sse([
                {"eventType": "retrieval.started", "requestId": "request-701"},
                {"eventType": "answer.started", "evidenceCount": 0},
                {"eventType": "retrieval.completed"},
                {"eventType": "answer.delta", "messageId": "assistant-701",
                 "seq": 0, "delta": "합성 지침 답변"},
                {"eventType": "answer.completed", "message": completed},
            ])
        if path == f"{prefix}/guideline-evidence":
            return sse([
                {"eventType": "retrieval.started", "requestId": "request-701"},
                {"eventType": "retrieval.progress", "stage": "embedded"},
                {"eventType": "retrieval.progress", "stage": "searched", "candidates": 1},
                {"eventType": "retrieval.progress", "stage": "reranked"},
                {"eventType": "evidence.gated", "abstainReason": None, "evidenceCount": 1,
                 "retrievalPolicyVersion": "synthetic-policy",
                 "searchQuestion": "합성 근거 질의"},
                {"eventType": "retrieval.evidence", "index": 0, "total": 1, "evidence": {
                    "id": "evidence-701", "guidelineId": "guideline-701",
                    "guidelineVersionId": "version-701", "guidelineTitle": "합성 지침",
                    "version": "1.0", "sectionPath": ["합성 절"],
                    "excerpt": "합성 중재의 조건과 적용 범위에 관한 합성 근거.",
                    "sourceUrl": "https://example.invalid/synthetic-evidence",
                }},
                {"eventType": "retrieval.completed"},
            ])
        raise AssertionError(f"준비하지 않은 가짜 BE 요청: {request.method} {path}")

    def b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    now = int(time.time())
    token = ".".join([
        b64(b'{"alg":"none","typ":"JWT"}'),
        b64(json.dumps({"sub": "clinician-1", "iat": now, "exp": now + 3600}).encode()),
        b64(b"sig"),
    ])
    app = create_app(
        ServiceSettings(be_origin="http://backend.test", agent_tracing_enabled="true"),
        backend_transport=httpx.MockTransport(backend),
        classifier_model=classifier,
        synthesis_model=synthesis,
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/agent/conversations/conversation-701/messages/stream",
                json={"content": question, "clientRequestId": "request-701"},
                headers={
                    "Cookie": f"access_token={token}; refresh_token=REFRESHMARK7592",
                    "X-CSRF-Protection": "CSRFMARK8603",
                },
            )
        events: list[str] = []
        for line in response.text.splitlines():
            if line.startswith("data: "):
                event = cast(dict[str, Any], json.loads(line[6:]))
                events.append(str(event["eventType"]))
                if event["eventType"] == "answer.completed" and "guidance" in event:
                    completed_guidances.append(event["guidance"])
        return {
            "status": response.status_code, "events": events, "finishes": finishes,
            "backend_requests": backend_requests, "question": question,
            "question_marker": question_marker, "token": token, "record_markers": markers,
            "answer_marker": answer_marker, "exception_raised": synthesis.raised,
            "guidance_markers": guidance_markers,
            "guidance_allergy_markers": guidance_allergy_markers,
            "finish_guidances": finish_guidances,
            "completed_guidances": completed_guidances,
        }
    finally:
        langsmith.configure(enabled=None)


if __name__ == "__main__":
    main()
