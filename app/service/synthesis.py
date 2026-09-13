"""환자·복합 경로의 답변 합성 (BE docs/specs/51 「복합의 지침 근거」·「복합 판정 방식」).

**합성 입력** — 두 경로 모두 질문 원문과 환자 도구가 돌려준 기록 필드 중 값이 있는 것을 싣는다:
`caseLabel`·`age`·`sex`·`bmi`·`heightCm`·`weightKg`·`waistCm`·`diagnoses`·`medications`·
`allergies`(배열은 항목마다)·`clinicalNotes`. 복합은 여기에 `retrieval.evidence` 프레임마다 마커
`[n]`(프레임 순서, 1부터)과 그 근거의 `excerpt`(원문)·`guidelineTitle`·`sectionPath`를 더한다.

**환자** — `synthesis_model.astream(messages)`(바인딩 인자 없음). 텍스트가 빈 조각은 건너뛰고,
텍스트가 있는 조각마다 순서대로 `answer.delta` 하나를 보낸다.

**복합** — `synthesis_model.bind(response_format=COMPOSITE_RESPONSE_FORMAT).astream(messages)`.
조각을 이은 텍스트가 `{"insufficientEvidence": <bool>, "answer": <문자열>}`이다(필드 순서가 계약).
판정 선행 증분 파싱(BE §40 이식): `insufficientEvidence`가 false로 확정되기 전에는 델타를 내지 않고,
확정 뒤에는 해석된 `answer` 문자열의 증분을 델타로 흘린다. true면 델타 없이 `insufficient_evidence`
기권이다. 판정 전에 스트림이 끝나거나 JSON이 깨지면 LLM 실패이고, 판정 뒤에 깨지면 그때까지의
답으로 완결한다.

**인용** — 복합 COMPLETED의 `citations`는 최종 답변에 등장한 마커 `[n]` 중 1 ≤ n ≤ 받은
`retrieval.evidence` 프레임 수인 것을 n 오름차순으로 한 번씩, `{marker: n, evidenceId: n번째
프레임의 evidence.id}`로 싣는다.

**generation** — 합성 LLM 호출 1회의 기록:
`provider` "openai" · `model` 스트림 응답 메타데이터의 `model_name`(없으면 `AGENT_MODEL`) ·
`promptVersion` · `latencyMs` · `inputTokens`·`outputTokens` 조각들의 `usage_metadata`
합계(없으면 텍스트 길이 / 3 올림 추정). 복합은 `evidence.gated`의 `retrievalPolicyVersion`·
`searchQuestion`을 더하고, 환자는 둘 다 싣지 않는다(검색하지 않은 생성).
"""

import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.service.llm import AGENT_MODEL, PROVIDER, LlmCallError, as_chunk

COMPOSITE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "agent_composite_answer",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "insufficientEvidence": {"type": "boolean"},
                "answer": {"type": "string"},
            },
            "required": ["insufficientEvidence", "answer"],
            "additionalProperties": False,
        },
    },
}

ResponseLang = Literal["ko", "en"]

# GenerationRun.promptVersion 기록값 — 답변 언어가 다르면 규칙 한 줄이 달라 따로 잰다
# (BE §42와 같은 이유)
PATIENT_PROMPT_VERSION = "agent-patient-v1"
COMPOSITE_PROMPT_VERSION = "agent-composite-v1"

_LANGUAGE_RULE: dict[ResponseLang, str] = {
    "ko": "한국어로 쓴다.",
    "en": "영어로 쓴다. 진단명·약물명·지침 제목은 원문 표기를 함께 적어도 된다.",
}

_RECORD_FIELDS: tuple[tuple[str, str], ...] = (
    ("caseLabel", "케이스 라벨"),
    ("age", "나이"),
    ("sex", "성별"),
    ("bmi", "BMI"),
    ("heightCm", "키(cm)"),
    ("weightKg", "체중(kg)"),
    ("waistCm", "허리둘레(cm)"),
    ("diagnoses", "진단"),
    ("medications", "투약"),
    ("allergies", "알레르기"),
    ("clinicalNotes", "임상 메모"),
)

_MARKER = re.compile(r"\[(\d+)\]")


@dataclass(frozen=True)
class SynthesisResult:
    text: str
    insufficient_evidence: bool
    generation: dict[str, object]


def prompt_version(base: str, lang: ResponseLang) -> str:
    return f"{base}-en" if lang == "en" else base


def patient_messages(
    question: str, patient: Mapping[str, Any], lang: ResponseLang
) -> list[BaseMessage]:
    rules = "\n".join(
        [
            "너는 한의원 의료인을 돕는 임상 보조 에이전트다.",
            "[환자 기록]만 근거로 [질문]에 답한다.",
            "",
            "규칙",
            "1. 기록에 적힌 내용만 전한다. 기록에 없는 정보는 추측하거나",
            "   지어내지 말고, 기록에 없다고 밝힌다.",
            "2. 기록의 값을 옮길 때 바꾸거나 넓히지 않는다. 기록에 없는",
            "   진단·처방 판단을 새로 만들지 않는다.",
            "3. 최종 판단과 책임은 의료인에게 있다 — 확정적 지시가 아니라",
            "   참고 정보로 서술한다.",
            "4. 마크다운을 쓰지 않는다 — 굵게(**), 제목(#), 목록(-) 기호 없이",
            "   평문으로만 답한다.",
            f"5. 답은 {_LANGUAGE_RULE[lang]}",
        ]
    )
    user = f"{render_record(patient)}\n\n[질문]\n{question}"
    return [SystemMessage(content=rules), HumanMessage(content=user)]


def composite_messages(
    question: str,
    patient: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    lang: ResponseLang,
) -> list[BaseMessage]:
    rules = "\n".join(
        [
            "너는 한의원 의료인을 돕는 임상 보조 에이전트다.",
            "[환자 기록]과 [지침 근거]를 함께 딛고, 이 환자에게 지침을 적용하는",
            "[질문]에 답한다.",
            "",
            "규칙",
            "1. 제공된 기록과 근거만 사용한다. 근거에 없는 처방명·혈자리·용량·",
            "   기간·금기를 일반 지식으로 보충하지 않는다.",
            "2. 근거를 사용한 문장에는 그 근거의 마커를 [n] 형식으로 표기한다",
            "   (예: 침 치료가 권고된다 [1]). 질문과 무관한 근거에는 마커를 달지",
            "   않는다 — 마커를 단 근거는 모두 인용으로 저장된다.",
            "3. 환자 기록의 진단·투약·알레르기·임상 메모가 근거의 조건·금기와",
            "   맞닿으면 그 점을 짚는다.",
            "4. 기록과 근거로 이 환자에 대한 질문에 답할 수 없으면",
            "   insufficientEvidence를 true로 두고 answer는 비운다. 답할 수 있으면",
            "   false로 두고 answer에 답을 쓴다. answer에 거부 산문을 쓰지 않는다.",
            "5. 최종 판단과 책임은 의료인에게 있다 — 확정적 처방 지시가 아니라",
            "   참고 정보로 서술한다.",
            "6. 마크다운을 쓰지 않는다 — 평문으로만 쓴다.",
            f"7. answer는 {_LANGUAGE_RULE[lang]}",
        ]
    )
    user = f"{render_record(patient)}\n\n{render_evidence(evidence)}\n\n[질문]\n{question}"
    return [SystemMessage(content=rules), HumanMessage(content=user)]


def render_record(patient: Mapping[str, Any]) -> str:
    lines = ["[환자 기록]"]
    for key, label in _RECORD_FIELDS:
        value = patient.get(key)
        if isinstance(value, list):
            items = [str(item) for item in value if str(item).strip()]
            if items:
                lines.append(f"{label}: {', '.join(items)}")
        elif value is not None and str(value).strip():
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def render_evidence(evidence: Sequence[Mapping[str, Any]]) -> str:
    blocks = ["[지침 근거]"]
    for marker, item in enumerate(evidence, start=1):
        section = " > ".join(str(part) for part in item.get("sectionPath") or [])
        header = f"[{marker}] {item.get('guidelineTitle', '')}"
        if section:
            header = f"{header} — {section}"
        # 발췌가 아니라 청크 원문이다 — 조건·금기는 발췌 밖에 있는 경우가 많다(BE §33)
        blocks.append(f"{header}\n{item.get('excerpt', '')}")
    return "\n\n".join(blocks)


def citations_for(answer: str, evidence_ids: Sequence[str]) -> list[dict[str, object]]:
    """답변에 실제로 등장한 마커만, 받은 근거 수 안의 것만 인용한다."""
    used = sorted({int(marker) for marker in _MARKER.findall(answer)})
    return [
        {"marker": marker, "evidenceId": evidence_ids[marker - 1]}
        for marker in used
        if 1 <= marker <= len(evidence_ids)
    ]


async def stream_patient_answer(
    model: BaseChatModel,
    messages: list[BaseMessage],
    *,
    lang: ResponseLang,
    trace_id: str,
    on_delta: Callable[[str], None],
) -> SynthesisResult:
    started = time.monotonic()
    parts: list[str] = []
    total: AIMessageChunk | None = None
    try:
        async for chunk in model.astream(
            messages, config=_run_config("patient_synthesis", trace_id)
        ):
            total = chunk if total is None else total + chunk
            text = str(chunk.text)
            if text:
                parts.append(text)
                on_delta(text)
    except Exception as e:
        raise LlmCallError(f"환자 합성 실패: {type(e).__name__}") from e
    answer = "".join(parts)
    return SynthesisResult(
        text=answer,
        insufficient_evidence=False,
        generation=_generation(
            total,
            prompt_version=prompt_version(PATIENT_PROMPT_VERSION, lang),
            started=started,
            messages=messages,
            answer=answer,
        ),
    )


async def stream_composite_answer(
    model: BaseChatModel,
    messages: list[BaseMessage],
    *,
    lang: ResponseLang,
    trace_id: str,
    on_delta: Callable[[str], None],
) -> SynthesisResult:
    started = time.monotonic()
    parser = VerdictFirstParser()
    parts: list[str] = []
    total: AIMessageChunk | None = None
    try:
        bound = model.bind(response_format=COMPOSITE_RESPONSE_FORMAT)
        async for message in bound.astream(
            messages, config=_run_config("composite_synthesis", trace_id)
        ):
            chunk = as_chunk(message)
            total = chunk if total is None else total + chunk
            # 기권 판정 뒤에도 끝까지 받는다 — 스트림 끝의 usage가 generation의 토큰 수다
            for text in parser.push(str(chunk.text)):
                parts.append(text)
                on_delta(text)
        parser.finish()
    except Exception as e:
        raise LlmCallError(f"복합 합성 실패: {type(e).__name__}") from e
    answer = "".join(parts)
    return SynthesisResult(
        text=answer,
        insufficient_evidence=parser.insufficient_evidence,
        generation=_generation(
            total,
            prompt_version=prompt_version(COMPOSITE_PROMPT_VERSION, lang),
            started=started,
            messages=messages,
            answer=answer,
        ),
    )


def _run_config(step: str, trace_id: str) -> RunnableConfig:
    # 메타데이터는 숨김 클라이언트의 허용목록(agent_step·traceId)에 드는 값만 싣는다
    return {"run_name": f"agent_{step}", "metadata": {"agent_step": step, "traceId": trace_id}}


def _generation(
    total: AIMessageChunk | None,
    *,
    prompt_version: str,
    started: float,
    messages: Sequence[BaseMessage],
    answer: str,
) -> dict[str, object]:
    usage = total.usage_metadata if total is not None else None
    model_name = total.response_metadata.get("model_name") if total is not None else None
    if usage is not None:
        input_tokens, output_tokens = usage["input_tokens"], usage["output_tokens"]
    else:
        # 프로바이더가 usage를 주지 않으면 BE와 같은 추정(글자 수 / 3 올림)으로 남긴다
        prompt = "".join(str(message.text) for message in messages)
        input_tokens, output_tokens = math.ceil(len(prompt) / 3), math.ceil(len(answer) / 3)
    return {
        "provider": PROVIDER,
        "model": model_name if isinstance(model_name, str) and model_name else AGENT_MODEL,
        "promptVersion": prompt_version,
        "latencyMs": round((time.monotonic() - started) * 1000),
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
    }


class MalformedAnswerError(Exception):
    """구조화 답변을 해석하지 못했다."""


_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_HEX4 = re.compile(r"[0-9a-fA-F]{4}")
_LITERAL = re.compile(r"(true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)")
_PARTIAL_LITERAL = re.compile(r"(t|tr|tru|f|fa|fal|fals|n|nu|nul|-|\d+\.?|\d+[eE][+-]?)")
_JSON_SPACE = " \t\n\r"


class VerdictFirstParser:
    """`{"insufficientEvidence": <bool>, "answer": <문자열>}`의 판정 선행 증분 파서 (BE §40 이식).

    **전체 버퍼링을 하지 않는다.** strict 구조화 출력은 스키마 순서로 생성하므로 판정이 답변 첫
    글자보다 먼저 오고, 그 뒤 `answer` 문자열은 오는 대로 해석해 흘린다.

    - 판정이 서기 전에 도착한 답변(스키마 순서 위반)은 보류한다 — 흘리면 기권을 되돌릴 수 없다.
    - 기권 판정(true)이면 여기서 멈춘다. 답변을 해석하지도 흘리지도 않는다.
    - 해석 실패는 판정 확정 시점으로 등급이 갈린다: 확정 **전**이면 실패(`MalformedAnswerError`),
      확정 **후**면 조용히 멈추고 그때까지 흘린 답으로 끝난다(출력 상한 잘림과 같다).
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._cursor = 0
        self._state = "object"
        self._key = ""
        self._flag: bool | None = None
        self._verdict_sent = False
        self._held = ""
        self._stopped = False

    @property
    def insufficient_evidence(self) -> bool:
        return self._flag is True

    def push(self, text: str) -> list[str]:
        """새 조각을 먹이고, 흘려도 되는 답변 증분을 돌려준다."""
        if self._stopped or self._state == "done" or not text:
            return []
        self._buffer += text
        out: list[str] = []
        try:
            while self._step(out):
                pass
        except MalformedAnswerError:
            if not self._verdict_sent:
                raise
            self._stopped = True
        self._buffer = self._buffer[self._cursor :]
        self._cursor = 0
        return out

    def finish(self) -> None:
        """스트림 종료 — 판정을 받기 전에 끝났으면 판정도 답도 못 받은 응답이다."""
        if not self._verdict_sent:
            raise MalformedAnswerError("구조화 답변이 판정 전에 끝났다")

    def _step(self, out: list[str]) -> bool:
        if self._stopped:
            return False
        match self._state:
            case "object":
                at = self._skip_space()
                if at is None:
                    return False
                if self._buffer[at] != "{":
                    raise MalformedAnswerError("객체가 아니다")
                self._cursor = at + 1
                self._state = "key"
                return True
            case "key":
                at = self._skip_space()
                if at is None:
                    return False
                if self._buffer[at] == "}":
                    return self._close_object(at, out)
                parsed = self._read_string(at)
                if parsed is None:
                    return False
                self._key, self._cursor = parsed
                self._state = "colon"
                return True
            case "colon":
                at = self._skip_space()
                if at is None:
                    return False
                if self._buffer[at] != ":":
                    raise MalformedAnswerError("콜론이 없다")
                self._cursor = at + 1
                self._state = "answer-open" if self._key == "answer" else "value"
                return True
            case "value":
                at = self._skip_space()
                if at is None:
                    return False
                value = self._read_value(at)
                if value is None:
                    return False
                self._assign(self._key, value[0])
                self._cursor = value[1]
                self._state = "next"
                self._emit_verdict(out)
                return True
            case "answer-open":
                at = self._skip_space()
                if at is None:
                    return False
                if self._buffer[at] != '"':
                    raise MalformedAnswerError("answer가 문자열이 아니다")
                self._cursor = at + 1
                self._state = "answer"
                return True
            case "answer":
                return self._read_answer_run(out)
            case "next":
                at = self._skip_space()
                if at is None:
                    return False
                if self._buffer[at] == ",":
                    self._cursor = at + 1
                    self._state = "key"
                    return True
                if self._buffer[at] == "}":
                    return self._close_object(at, out)
                raise MalformedAnswerError("필드 구분자가 없다")
            case _:
                return False

    def _close_object(self, at: int, out: list[str]) -> bool:
        self._cursor = at + 1
        self._state = "done"
        # 판정 없이 닫힌 객체의 보류분은 흘리지 않는다 — finish()가 실패로 판정한다
        if self._verdict_sent:
            self._flush_held(out)
        return True

    def _emit_verdict(self, out: list[str]) -> None:
        if self._verdict_sent or self._flag is None:
            return
        self._verdict_sent = True
        if self._flag:
            self._held = ""
            self._stopped = True
            return
        self._flush_held(out)

    def _flush_held(self, out: list[str]) -> None:
        if self._held:
            out.append(self._held)
            self._held = ""

    def _emit_answer(self, text: str, out: list[str]) -> None:
        if not text:
            return
        if not self._verdict_sent:
            self._held += text
            return
        out.append(text)

    def _read_answer_run(self, out: list[str]) -> bool:
        """닫는 따옴표까지, 또는 도착한 만큼 답변을 해석해 흘린다."""
        index = self._cursor
        decoded: list[str] = []
        while index < len(self._buffer):
            char = self._buffer[index]
            if char == '"':
                self._cursor = index + 1
                self._state = "next"
                self._emit_answer("".join(decoded), out)
                return True
            if char == "\\":
                escape = self._read_escape(index)
                if escape is None:
                    break  # 이스케이프가 아직 다 오지 않았다
                decoded.append(escape[0])
                index = escape[1]
                continue
            decoded.append(char)
            index += 1
        self._cursor = index
        self._emit_answer("".join(decoded), out)
        return False

    def _read_escape(self, at: int) -> tuple[str, int] | None:
        if at + 1 >= len(self._buffer):
            return None
        code = self._buffer[at + 1]
        if code != "u":
            simple = _SIMPLE_ESCAPES.get(code)
            if simple is None:
                raise MalformedAnswerError("알 수 없는 이스케이프")
            return simple, at + 2
        unit = self._read_hex4(at + 2)
        if unit is None:
            return None
        if 0xD800 <= unit <= 0xDBFF:
            # 서로게이트 쌍은 두 이스케이프가 다 와야 한 글자다 — 반쪽을 흘리면 UTF-8로 쓸 수 없다
            if at + 12 > len(self._buffer):
                return None
            low = self._read_hex4(at + 8) if self._buffer[at + 6 : at + 8] == "\\u" else None
            if low is not None and 0xDC00 <= low <= 0xDFFF:
                return chr(0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00)), at + 12
            return "�", at + 6
        if 0xDC00 <= unit <= 0xDFFF:
            return "�", at + 6
        return chr(unit), at + 6

    def _read_hex4(self, at: int) -> int | None:
        digits = self._buffer[at : at + 4]
        if len(digits) < 4:
            return None
        if _HEX4.fullmatch(digits) is None:
            raise MalformedAnswerError("\\u 이스케이프가 16진수가 아니다")
        return int(digits, 16)

    def _read_value(self, at: int) -> tuple[object, int] | None:
        """완결된 JSON 값 하나 — 미완이면 None(커서를 움직이지 않는다)."""
        char = self._buffer[at]
        if char == '"':
            return self._read_string(at)
        if char in "[{":
            end = self._find_structure_end(at)
            if end is None:
                return None
            return self._loads(self._buffer[at:end]), end
        rest = self._buffer[at:]
        literal = _LITERAL.match(rest)
        if literal is None:
            if _PARTIAL_LITERAL.fullmatch(rest):
                return None
            raise MalformedAnswerError("값이 아니다")
        token = literal.group(1)
        end = at + len(token)
        # 숫자는 구분자가 와야 끝난 것이 확실하다 (12가 123의 앞부분일 수 있다)
        if end == len(self._buffer) and token not in ("true", "false", "null"):
            return None
        return self._loads(token), end

    def _read_string(self, at: int) -> tuple[str, int] | None:
        if self._buffer[at] != '"':
            raise MalformedAnswerError("문자열이 아니다")
        index = at + 1
        while index < len(self._buffer):
            char = self._buffer[index]
            if char == "\\":
                index += 2
                continue
            if char == '"':
                value = self._loads(self._buffer[at : index + 1])
                if not isinstance(value, str):
                    raise MalformedAnswerError("문자열이 아니다")
                return value, index + 1
            index += 1
        return None

    def _find_structure_end(self, at: int) -> int | None:
        depth = 0
        in_string = False
        index = at
        while index < len(self._buffer):
            char = self._buffer[index]
            if in_string:
                if char == "\\":
                    index += 1
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        return None

    def _skip_space(self) -> int | None:
        index = self._cursor
        while index < len(self._buffer) and self._buffer[index] in _JSON_SPACE:
            index += 1
        if index >= len(self._buffer):
            self._cursor = index
            return None
        return index

    def _assign(self, key: str, value: object) -> None:
        # 모델이 불리언을 문자열로 내는 것은 BE 리랭커에서 실측됐다 — 판정 자체가 서지 않을 때만
        # 실패다
        if key != "insufficientEvidence":
            return
        if isinstance(value, bool):
            self._flag = value
        elif value in ("true", "false"):
            self._flag = value == "true"
        else:
            raise MalformedAnswerError("판정이 불리언이 아니다")

    @staticmethod
    def _loads(text: str) -> object:
        try:
            return json.loads(text)
        except ValueError as e:
            raise MalformedAnswerError("JSON 값을 해석하지 못했다") from e
