"""에이전트 턴 — 수락된 질문 하나를 네 갈래 중 하나로 끝까지 흘린다 (BE docs/specs/51).

스트림을 열기 전의 판정(본문 검증·선검사·수락)은 `routes.stream_message`가 하고, 이 모듈은
수락이 끝난 뒤의 SSE를 맡는다. 프레임은 `data: <JSON>\\n\\n`, 하트비트는 `: ping\\n\\n`이다.

**이벤트 흐름** (BE §8 이벤트 계약 + `agent.progress`)

    공통  message.accepted{requestId, userMessageId, assistantMessageId}
          → 분류 → agent.progress{stage: "routed", route}
    지침  지침 도구 SSE의 이벤트를 받은 순서·내용 그대로 흘린다 — 완결을 부르지 않는다
    환자  환자 도구 → agent.progress{stage: "patient_loaded"} → answer.delta*
          → [완결 COMPLETED] → answer.completed
    복합  환자 도구 → agent.progress{stage: "patient_loaded"} → 근거 도구 SSE:
          retrieval.started → retrieval.progress* → (evidence.gated가 통과면 answer.started)
          → retrieval.evidence×N → retrieval.completed → answer.delta*(판정 뒤)
          → [완결] → answer.completed | answer.abstained
    기타  [완결 ABSTAINED] → answer.abstained          (환자·복합의 라벨 해석 실패도 같다)

- `route`는 실행 경로(`GUIDELINE`·`PATIENT`·`COMPOSITE`·`OTHER`)다 — 분류기 판정이 아니라
  `routing`의 실행 경로 표를 거친 값이다.
- `answer.delta`는 `{messageId: assistantMessageId, seq, delta}`이고 `seq`는 턴마다 0부터 연속이다.
- `evidence.gated`는 브라우저로 흘리지 않는다. `abstainReason`이 null이면 `answer.started
  {evidenceCount}`(그 이벤트의 `evidenceCount`)로 바꿔 첫 `retrieval.evidence`보다 앞에 보내고,
  사유가 있으면 합성 없이 그 사유로 기권 완결한다.
- 종결 이벤트는 **완결 응답을 받은 뒤에** 보낸다. `answer.completed{message}`의 `message`는 완결
  응답 봉투의 `data`이고, `answer.abstained{message, reason, missingInformation: []}`의 `reason`은
  그 메시지의 `abstainReason` 문장이다.

**BE 내부 API** (`/api/v1/internal/agent/…`, 헤더는 전부 받은 Cookie 원문 + 받았을 때만 CSRF)

    지침 도구  POST turns/{assistantMessageId}/guideline-answer   {classifierVersion}
    환자 도구  POST turns/{assistantMessageId}/patient            {caseLabel}
    근거 도구  POST turns/{assistantMessageId}/guideline-evidence {query}
    완결       POST turns/{assistantMessageId}/finish             아래

완결 본문 — `classifierVersion`은 모든 완결에 싣고, `route`는 경로가 정해진 뒤에만 싣는다.

    COMPLETED(환자)   {status, route, classifierVersion, content, generation}
    COMPLETED(복합)   {status, route, classifierVersion, content, citations, generation}
    ABSTAINED         {status, route, classifierVersion, abstainReason}
                      + generation — 합성 LLM이 판정으로 기권했을 때(insufficient_evidence)만
    FAILED·CANCELLED  {status, route?, classifierVersion}

- `content`는 그 턴에 흘린 `answer.delta`를 이은 전체 텍스트다.
- 기권 사유: 기타·라벨 2개 이상 `out_of_scope` · 환자 라벨 0개와 환자 도구 `NOT_FOUND`·`AMBIGUOUS`
  `patient_unresolved` · 근거 게이트 기권은 `evidence.gated.abstainReason` 그대로 · 합성 판정
  `insufficient_evidence`.

**실패는 턴을 닫는다** — 지침 경로는 지침 도구가 턴을 닫으므로 어떤 경우에도 완결을 부르지 않는다.
나머지(경로가 아직 정해지지 않은 경우 포함)는 FAILED 완결을 먼저 부른 뒤 `error{code, message,
retryable, traceId}`를 보내고 스트림을 닫는다. `traceId`는 이 요청의 traceId다.

- 분류·합성 LLM의 예외, 분류 응답을 해석하지 못함 → `LLM_UNAVAILABLE`, retryable true
- 실행 상한(`run_deadline`, 요청 도착부터 잰다) 초과 → `LLM_TIMEOUT`, retryable true.
  지침 경로는 지침 도구 스트림을 끊는다(BE가 CANCELLED로 정리한다)
- BE 도구·완결에서 응답을 받지 못함(연결 실패·시간 초과·종결 이벤트 전에 끝난 SSE)
  → `AGENT_BACKEND_UNAVAILABLE`, retryable true
- BE 도구가 2xx가 아닌 봉투로 답함 → 그 봉투의 code·message, retryable은 code가
  `LLM_UNAVAILABLE`·`LLM_TIMEOUT`일 때만 true
- 지침·근거 도구가 보낸 `error` 이벤트는 받은 그대로 흘린다(근거 도구면 그 전에 FAILED 완결)
- BE가 계약 밖의 모양으로 답하는 등 그 밖의 결함 → `INTERNAL_ERROR`, retryable false

로그에는 예외 클래스 이름만 남긴다 — 분기 뒤 예외 메시지에는 환자 기록이 섞일 수 있다(BE §14).

**하트비트** — SSE가 열린 뒤 `heartbeat_interval`초 동안 보낼 프레임이 없으면 `: ping`을 보낸다.

**끊김** — 클라이언트가 끊으면 실행을 멈춘다. 지침 경로는 지침 도구 스트림을 끊고, 나머지는
CANCELLED로 완결한다. 정리가 끝나야 ASGI 호출이 반환된다.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import anyio
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.service.backend import (
    BackendClient,
    BackendProtocolError,
    BackendResponse,
    BackendUnavailableError,
    Credentials,
)
from app.service.envelope import (
    AGENT_BACKEND_UNAVAILABLE,
    INTERNAL_ERROR,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
)
from app.service.llm import AgentModels, LlmCallError
from app.service.routing import CLASSIFIER_VERSION, Route, classify, plan_route, strip_label
from app.service.sse import PING_FRAME, SseEvent, event_frame, relay_frame
from app.service.synthesis import (
    ResponseLang,
    citations_for,
    composite_messages,
    patient_messages,
    stream_composite_answer,
    stream_patient_answer,
)
from app.service.tracing import AgentTracing, hidden_branch, traced_turn

logger = logging.getLogger(__name__)

# 실행 상한이자 선검사 기준 (BE docs/specs/51 「실행 도중 access 만료」) — BE 스트림 LLM 상한
# 120초에 분류·도구 여유를 더한 값. 남은 수명이 이보다 짧은 토큰은 수락 전에 401로 돌려보낸다
RUN_DEADLINE_SECONDS = 150.0
# BE §8 전송 규약과 같은 주기 — 프록시 idle timeout으로 스트림이 끊기지 않게 한다
HEARTBEAT_INTERVAL_SECONDS = 15.0

# 지침 도구 스트림의 종결 이벤트 — 받으면 흘리고 턴을 끝낸다
_TERMINAL_EVENTS = frozenset({"answer.completed", "answer.abstained", "error"})
# BE §8 끊김 복구 6 — 상류 장애만 재시도하면 달라질 수 있다
_RETRYABLE_CODES = frozenset({LLM_UNAVAILABLE.code, LLM_TIMEOUT.code})


@dataclass(frozen=True)
class TurnSettings:
    """앱 수명 동안 턴들이 나눠 쓰는 것 — lifespan이 만든다."""

    backend: BackendClient
    models: AgentModels
    tracing: AgentTracing
    heartbeat_interval: float
    run_deadline: float


@dataclass(frozen=True)
class AcceptedTurn:
    """수락이 만든 턴과 그 요청의 문맥."""

    user_message_id: str
    assistant_message_id: str
    client_request_id: str
    question: str
    response_lang: ResponseLang
    credentials: Credentials
    trace_id: str
    # 실행 상한 시각(이벤트 루프 시계) — 요청이 도착한 순간부터 잰다
    deadline: float


class ToolRejectedError(Exception):
    """BE 도구·완결이 2xx가 아닌 봉투로 답했다 — 그 code·message를 error 이벤트로 옮긴다."""

    def __init__(self, response: BackendResponse) -> None:
        super().__init__(f"BE {response.status_code}")
        try:
            body: object = json.loads(response.content)
        except ValueError:
            body = None
        code = body.get("code") if isinstance(body, dict) else None
        message = body.get("message") if isinstance(body, dict) else None
        self.code = code if isinstance(code, str) and code else INTERNAL_ERROR.code
        self.message = message if isinstance(message, str) else INTERNAL_ERROR.message


class ToolErrorEventError(Exception):
    """근거 도구가 error 이벤트를 보냈다 — 받은 그대로 흘린다."""

    def __init__(self, event: SseEvent) -> None:
        super().__init__(event.event_type)
        self.event = event


class AgentTurn:
    """수락된 턴 하나의 실행.

    실행은 생산자 태스크가 하고 프레임을 큐에 넣는다. 응답 본문은 큐를 읽기만 한다 — 그래서 끊김으로
    본문 읽기가 취소돼도 실행을 이어서 정리(CANCELLED 완결)할 수 있다.
    """

    def __init__(self, settings: TurnSettings, turn: AcceptedTurn) -> None:
        self._settings = settings
        self._turn = turn
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._route: Route | None = None
        # 완결이 BE 턴을 닫았다 — 닫힌 턴에 FAILED·CANCELLED를 다시 보내지 않는다
        self._closed = False
        self._seq = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._produce())

    async def frames(self) -> AsyncIterator[bytes]:
        self.start()
        while True:
            try:
                frame = await asyncio.wait_for(
                    self._queue.get(), timeout=self._settings.heartbeat_interval
                )
            except TimeoutError:
                yield PING_FRAME
                continue
            if frame is None:
                return
            yield frame

    async def close(self) -> None:
        """응답이 끝났다. 실행이 아직 돌면 클라이언트가 끊은 것이다 — 멈추고 정리를 기다린다."""
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.wait({task})

    async def _produce(self) -> None:
        try:
            with traced_turn(self._settings.tracing):
                await self._run()
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                # 받은 취소를 정리 호출이 다시 받지 않게 한다 — 정리가 끝나면 취소를 다시 올린다
                current.uncancel()
            # 지침 경로는 도구 스트림이 닫히며 BE가 CANCELLED로 정리한다(§8-4)
            if self._route != "GUIDELINE" and not self._closed:
                await self._finish_quietly("CANCELLED")
            raise
        finally:
            self._queue.put_nowait(None)

    async def _run(self) -> None:
        turn = self._turn
        self._emit(
            {
                "eventType": "message.accepted",
                "requestId": turn.client_request_id,
                "userMessageId": turn.user_message_id,
                "assistantMessageId": turn.assistant_message_id,
            }
        )
        deadline = asyncio.timeout_at(turn.deadline)
        try:
            async with deadline:
                await self._execute()
            return
        except Exception as e:
            failure = self._failure_frame(e, deadline_expired=deadline.expired())
            logger.warning(
                "[%s] 에이전트 턴 실패 — %s (경로 %s)", turn.trace_id, type(e).__name__, self._route
            )
        if self._route != "GUIDELINE" and not self._closed:
            await self._finish_quietly("FAILED")
        self._queue.put_nowait(failure)

    def _failure_frame(self, error: Exception, *, deadline_expired: bool) -> bytes:
        if isinstance(error, ToolErrorEventError):
            return relay_frame(error.event)
        if isinstance(error, TimeoutError) and deadline_expired:
            return self._error_frame(LLM_TIMEOUT.code, LLM_TIMEOUT.message)
        if isinstance(error, LlmCallError):
            return self._error_frame(LLM_UNAVAILABLE.code, LLM_UNAVAILABLE.message)
        if isinstance(error, BackendUnavailableError):
            return self._error_frame(
                AGENT_BACKEND_UNAVAILABLE.code, AGENT_BACKEND_UNAVAILABLE.message, retryable=True
            )
        if isinstance(error, ToolRejectedError):
            return self._error_frame(error.code, error.message)
        return self._error_frame(INTERNAL_ERROR.code, INTERNAL_ERROR.message)

    def _error_frame(self, code: str, message: str, *, retryable: bool | None = None) -> bytes:
        return event_frame(
            {
                "eventType": "error",
                "code": code,
                "message": message,
                "retryable": code in _RETRYABLE_CODES if retryable is None else retryable,
                "traceId": self._turn.trace_id,
            }
        )

    async def _execute(self) -> None:
        turn = self._turn
        decision = await classify(
            self._settings.models.classifier(), turn.question, trace_id=turn.trace_id
        )
        plan = plan_route(decision)
        self._route = plan.route
        self._emit({"eventType": "agent.progress", "stage": "routed", "route": plan.route})
        if plan.abstain_reason is not None:
            await self._abstain(plan.abstain_reason)
            return
        if plan.route == "GUIDELINE":
            await self._run_guideline()
            return
        assert plan.case_label is not None
        # 경로가 환자·복합으로 정해진 뒤의 LangChain 실행은 전부 숨김 클라이언트로 돈다
        with hidden_branch(self._settings.tracing):
            if plan.route == "PATIENT":
                await self._run_patient(plan.case_label)
            else:
                await self._run_composite(plan.case_label)

    async def _run_guideline(self) -> None:
        turn = self._turn
        async with self._settings.backend.guideline_answer(
            turn.assistant_message_id, CLASSIFIER_VERSION, turn.credentials
        ) as stream:
            if stream.rejection is not None:
                raise ToolRejectedError(stream.rejection)
            async for event in stream.events:
                self._relay(event)
                if event.event_type in _TERMINAL_EVENTS:
                    return
        raise BackendUnavailableError("지침 도구 스트림이 종결 이벤트 없이 끝났다")

    async def _run_patient(self, case_label: str) -> None:
        turn = self._turn
        patient = await self._resolve_patient(case_label)
        if patient is None:
            await self._abstain("patient_unresolved")
            return
        self._emit({"eventType": "agent.progress", "stage": "patient_loaded"})
        result = await stream_patient_answer(
            self._settings.models.synthesis(),
            patient_messages(turn.question, patient, turn.response_lang),
            lang=turn.response_lang,
            trace_id=turn.trace_id,
            on_delta=self._delta,
        )
        await self._complete(
            {
                "status": "COMPLETED",
                "route": "PATIENT",
                "classifierVersion": CLASSIFIER_VERSION,
                "content": result.text,
                "generation": result.generation,
            }
        )

    async def _run_composite(self, case_label: str) -> None:
        turn = self._turn
        patient = await self._resolve_patient(case_label)
        if patient is None:
            await self._abstain("patient_unresolved")
            return
        self._emit({"eventType": "agent.progress", "stage": "patient_loaded"})
        gated, evidence = await self._collect_evidence(strip_label(turn.question, case_label))
        abstain_reason = gated.get("abstainReason")
        if abstain_reason is not None:
            # 검색 게이트 기권은 LLM을 부르지 않았으므로 generation이 없다(BE §8 불변식)
            await self._abstain(str(abstain_reason))
            return
        result = await stream_composite_answer(
            self._settings.models.synthesis(),
            composite_messages(turn.question, patient, evidence, turn.response_lang),
            lang=turn.response_lang,
            trace_id=turn.trace_id,
            on_delta=self._delta,
        )
        generation = {
            **result.generation,
            "retrievalPolicyVersion": gated["retrievalPolicyVersion"],
            "searchQuestion": gated["searchQuestion"],
        }
        if result.insufficient_evidence:
            await self._abstain("insufficient_evidence", generation=generation)
            return
        await self._complete(
            {
                "status": "COMPLETED",
                "route": "COMPOSITE",
                "classifierVersion": CLASSIFIER_VERSION,
                "content": result.text,
                "citations": citations_for(result.text, [str(item["id"]) for item in evidence]),
                "generation": generation,
            }
        )

    async def _resolve_patient(self, case_label: str) -> dict[str, Any] | None:
        turn = self._turn
        response = await self._settings.backend.resolve_patient(
            turn.assistant_message_id, case_label, turn.credentials
        )
        if response.status_code != 200:
            raise ToolRejectedError(response)
        data = response.data()
        outcome = data.get("outcome")
        if outcome in ("NOT_FOUND", "AMBIGUOUS"):
            return None
        patient = data.get("patient")
        if outcome != "RESOLVED" or not isinstance(patient, dict):
            raise BackendProtocolError("환자 도구 결과의 모양이 계약과 다르다")
        return patient

    async def _collect_evidence(
        self, query: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """근거 도구를 흘린다 — `evidence.gated`는 삼키고 통과면 `answer.started`로 바꿔 보낸다."""
        turn = self._turn
        gated: dict[str, Any] | None = None
        evidence: list[dict[str, Any]] = []
        async with self._settings.backend.guideline_evidence(
            turn.assistant_message_id, query, turn.credentials
        ) as stream:
            if stream.rejection is not None:
                raise ToolRejectedError(stream.rejection)
            async for event in stream.events:
                match event.event_type:
                    case "evidence.gated":
                        gated = event.payload
                        if gated.get("abstainReason") is None:
                            # 첫 근거 프레임보다 앞에 선다(§47)
                            count = gated["evidenceCount"]
                            self._emit({"eventType": "answer.started", "evidenceCount": count})
                    case "error":
                        # 근거 도구는 턴을 닫지 않는다 — FAILED 완결 뒤 이 이벤트를 그대로 흘린다
                        raise ToolErrorEventError(event)
                    case "retrieval.evidence":
                        item = event.payload.get("evidence")
                        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                            raise BackendProtocolError("근거 프레임의 모양이 계약과 다르다")
                        evidence.append(item)
                        self._relay(event)
                    case "retrieval.completed":
                        self._relay(event)
                        if gated is None:
                            raise BackendProtocolError("evidence.gated 없이 근거 구간이 끝났다")
                        return gated, evidence
                    case _:
                        self._relay(event)
        raise BackendUnavailableError("근거 도구 스트림이 retrieval.completed 없이 끝났다")

    async def _complete(self, body: dict[str, object]) -> None:
        message = await self._finish(body)
        self._emit({"eventType": "answer.completed", "message": message})

    async def _abstain(self, reason: str, *, generation: dict[str, object] | None = None) -> None:
        body: dict[str, object] = {
            "status": "ABSTAINED",
            "route": self._route,
            "classifierVersion": CLASSIFIER_VERSION,
            "abstainReason": reason,
        }
        if generation is not None:
            body["generation"] = generation
        message = await self._finish(body)
        self._emit(
            {
                "eventType": "answer.abstained",
                "message": message,
                "reason": message.get("abstainReason") or "",
                "missingInformation": [],
            }
        )

    async def _finish(self, body: dict[str, object]) -> dict[str, Any]:
        turn = self._turn
        response = await self._settings.backend.finish_turn(
            turn.assistant_message_id, body, turn.credentials
        )
        if response.status_code != 200:
            raise ToolRejectedError(response)
        self._closed = True
        return response.data()

    async def _finish_quietly(self, status: str) -> None:
        """실패·끊김의 완결 — 이것마저 실패하면 턴은 STREAMING으로 남는다(스펙 위험 ⑹)."""
        turn = self._turn
        body: dict[str, object] = {"status": status, "classifierVersion": CLASSIFIER_VERSION}
        if self._route is not None:
            body["route"] = self._route
        try:
            response = await self._settings.backend.finish_turn(
                turn.assistant_message_id, body, turn.credentials
            )
        except BackendUnavailableError:
            logger.warning("[%s] %s 완결에 BE 응답이 없다", turn.trace_id, status)
            return
        if response.status_code == 200:
            self._closed = True
        else:
            logger.warning(
                "[%s] %s 완결이 %d로 거절됐다", turn.trace_id, status, response.status_code
            )

    def _emit(self, event: dict[str, object]) -> None:
        self._queue.put_nowait(event_frame(event))

    def _relay(self, event: SseEvent) -> None:
        self._queue.put_nowait(relay_frame(event))

    def _delta(self, text: str) -> None:
        self._emit(
            {
                "eventType": "answer.delta",
                "messageId": self._turn.assistant_message_id,
                "seq": self._seq,
                "delta": text,
            }
        )
        self._seq += 1


class TurnStreamResponse(StreamingResponse):
    """턴의 SSE 응답 — 응답이 어떻게 끝나든(정상·끊김) 턴의 정리를 기다린 뒤 반환한다."""

    def __init__(self, turn: AgentTurn) -> None:
        super().__init__(
            turn.frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self._turn = turn

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self._turn.start()
        try:
            await super().__call__(scope, receive, send)
        finally:
            # 끊김이면 이 자리의 스코프가 이미 취소됐을 수 있다 — 정리는 취소에서 떼어 끝까지 한다
            with anyio.CancelScope(shield=True):
                await self._turn.close()

