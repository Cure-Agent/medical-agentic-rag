"""기준 60~67의 경로별 추적 숨김과 남겨야 할 분류 입력·실행·토큰 수를 검증한다.

spec 54 기준 47(복합 완결 응답의 참고안 값이 추적 전송에 없다)도 같은 캡처로 판정한다.
"""

import base64
import json
import os
import subprocess
import sys
from collections.abc import Callable
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORD_FIELDS = ("diagnoses", "medications", "allergies", "clinicalNotes")
Probe = dict[str, Any]


@pytest.fixture(scope="module")
def probe() -> Callable[[str], Probe]:
    """같은 시나리오는 한 번만 실행하고 서로 다른 환경은 새 프로세스로 격리한다."""
    cache: dict[str, Probe] = {}

    def collect(scenario: str) -> Probe:
        if scenario in cache:
            return cache[scenario]
        environment = os.environ.copy()
        for key in tuple(environment):
            if key == "AGENT_TRACING_ENABLED" or key.startswith(("LANGSMITH_", "LANGCHAIN_")):
                del environment[key]
        if scenario == "env_false":
            environment.update(LANGSMITH_HIDE_INPUTS="false", LANGSMITH_HIDE_OUTPUTS="false")
        # 부모의 프록시 설정이 로컬 캡처 요청을 외부로 보내지 못하게 한다.
        environment["NO_PROXY"] = "127.0.0.1,localhost"
        environment["no_proxy"] = "127.0.0.1,localhost"
        inherited = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(REPO_ROOT) + (
            os.pathsep + inherited if inherited else ""
        )
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tests/agent_trace_probe.py"), scenario],
            cwd=REPO_ROOT, env=environment, capture_output=True, text=True,
            timeout=120, check=False,
        )
        tail = completed.stderr[-3000:]
        assert completed.returncode == 0, (
            f"추적 probe 종료코드: {completed.returncode}\nstderr 꼬리:\n{tail}"
        )
        lines = completed.stdout.splitlines()
        assert lines, f"추적 probe 출력이 없다.\nstderr 꼬리:\n{tail}"
        try:
            payload: object = json.loads(lines[-1])
        except json.JSONDecodeError:
            pytest.fail(f"추적 probe JSON 파싱 실패.\nstderr 꼬리:\n{tail}", pytrace=False)
        assert isinstance(payload, dict), f"추적 probe 결과가 객체가 아니다.\n{tail}"
        result = cast(Probe, payload)
        cache[scenario] = result
        return result

    return collect


def _bodies(result: Probe) -> bytes:
    """모든 요청 본문을 디코딩하되 압축 해제나 내용 가공은 하지 않는다."""
    return b"".join(base64.b64decode(item["body"]) for item in result["captures"])


def _runs(result: Probe) -> dict[str, dict[str, Any]]:
    """multipart의 post·patch 파트를 실행 id로 합치고 JSON 런 전송도 받는다."""
    runs: dict[str, dict[str, Any]] = {}

    def merge(run_id: str, value: object, field: str | None = None) -> None:
        target = runs.setdefault(run_id, {})
        if field is not None:
            target[field] = value
        elif isinstance(value, dict):
            target.update(value)

    for capture in result["captures"]:
        if capture["method"] not in {"POST", "PATCH"}:
            continue
        if not capture["path"].startswith("/runs"):
            continue
        body = base64.b64decode(capture["body"])
        content_type = capture["content_type"]
        if "multipart/" in content_type:
            document = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
            )
            for part in document.walk():
                name = part.get_param("name", header="content-disposition")
                if not isinstance(name, str):
                    continue
                components = name.split(".", 2)
                if len(components) < 2 or components[0] not in {"post", "patch"}:
                    continue
                raw = part.get_payload(decode=True)
                assert isinstance(raw, bytes)
                value: object = json.loads(raw)
                merge(components[1], value, components[2] if len(components) == 3 else None)
        else:
            value = json.loads(body)
            assert isinstance(value, dict)
            if capture["path"] == "/runs/batch":
                for operation in ("post", "patch"):
                    for run in value.get(operation, []):
                        merge(str(run["id"]), run)
            else:
                run_id = value.get("id", capture["path"].removeprefix("/runs/"))
                merge(str(run_id), value)
    return runs


def _classifier_ids(result: Probe, runs: dict[str, dict[str, Any]]) -> set[str]:
    """원문 질문이 입력에 실린 LLM 실행으로 분류기를 식별한다."""
    return {
        run_id for run_id, run in runs.items()
        if run.get("run_type") == "llm"
        and result["question"] in json.dumps(run.get("inputs"), ensure_ascii=False)
    }


def _synthesis_runs(result: Probe) -> list[dict[str, Any]]:
    """이름이나 내부 노드명 대신 분류기와 다른 LLM 실행을 찾는다."""
    runs = _runs(result)
    classifier_ids = _classifier_ids(result, runs)
    assert classifier_ids, "분류기 LLM 입력에서 원문 질문을 찾지 못했다."
    return [
        run for run_id, run in runs.items()
        if run.get("run_type") == "llm" and run_id not in classifier_ids
    ]


def _guard(result: Probe, scenario: str) -> None:
    """미전송·압축·중도 실패로 부정 단언이 공허하게 통과하지 못하게 한다."""
    assert result["status"] == 200
    expected = "error" if scenario == "exception" else (
        "answer.abstained" if scenario == "other" else "answer.completed"
    )
    assert result["events"] and result["events"][-1] == expected
    assert any(
        item["method"] in {"POST", "PATCH"} and item["path"].startswith("/runs")
        and base64.b64decode(item["body"])
        for item in result["captures"]
    ), "캡처에 런 전송이 없다."
    assert result["question_marker"].encode() in _bodies(result)
    if scenario == "guideline":
        assert any(
            item["path"].endswith("/guideline-answer") for item in result["backend_requests"]
        )
    else:
        assert result["finishes"], "SSE 종결까지 갔지만 완결 요청이 없다."
    if scenario in {"patient", "composite", "exception", "env_false"}:
        assert _synthesis_runs(result), "분기 뒤 합성 LLM 실행이 없다."


@pytest.mark.parametrize("scenario", ["guideline", "patient", "composite", "other"])
def test_access_token_is_absent(probe: Callable[[str], Probe], scenario: str) -> None:
    """기준 60: 네 경로 각각의 추적 전송 전체에 access JWT 전체 값이 없다."""
    result = probe(scenario)
    _guard(result, scenario)
    assert _bodies(result).count(result["token"].encode()) == 0


def test_classifier_input_contains_question(probe: Callable[[str], Probe]) -> None:
    """기준 61: 분류기 LLM 실행의 입력 파트에 질문 원문이 남는다."""
    result = probe("other")
    _guard(result, "other")
    assert _classifier_ids(result, _runs(result))


@pytest.mark.parametrize("scenario", ["patient", "composite"])
@pytest.mark.parametrize("field", RECORD_FIELDS)
def test_patient_fields_are_hidden(
    probe: Callable[[str], Probe], scenario: str, field: str
) -> None:
    """기준 62: 환자·복합 각각에서 모든 기록 필드의 각 항목 표지가 숨겨진다."""
    result = probe(scenario)
    _guard(result, scenario)
    for marker in result["record_markers"][field]:
        assert _bodies(result).count(marker.encode()) == 0, field


@pytest.mark.parametrize("scenario", ["patient", "composite"])
def test_synthesized_answer_is_hidden(probe: Callable[[str], Probe], scenario: str) -> None:
    """기준 63: 실제 완결 content에 들어간 합성 답변 표지가 추적에는 없다."""
    result = probe(scenario)
    _guard(result, scenario)
    assert any(
        result["answer_marker"] in finish.get("content", "") for finish in result["finishes"]
    )
    assert _bodies(result).count(result["answer_marker"].encode()) == 0


def test_exception_record_value_is_hidden(probe: Callable[[str], Probe]) -> None:
    """기준 64: 합성 스트림에서 던진 예외 메시지의 기록 값도 추적에 없다."""
    result = probe("exception")
    _guard(result, "exception")
    assert result["exception_raised"] is True
    marker = result["record_markers"]["clinicalNotes"][0]
    assert _bodies(result).count(marker.encode()) == 0


def test_synthesis_llm_run_remains(probe: Callable[[str], Probe]) -> None:
    """기준 65: 환자 분기 뒤 합성은 추적을 끄지 않고 별도 LLM 실행을 남긴다."""
    result = probe("patient")
    _guard(result, "patient")
    assert _synthesis_runs(result)


def test_synthesis_input_usage_remains(probe: Callable[[str], Probe]) -> None:
    """기준 66: 합성 LLM 실행 extra의 입력 토큰 수는 가짜 usage 값 그대로 남는다."""
    result = probe("patient")
    _guard(result, "patient")
    assert any(
        run.get("extra", {}).get("metadata", {}).get("usage_metadata", {}).get("input_tokens")
        == 8675309
        for run in _synthesis_runs(result)
    )


@pytest.mark.parametrize("field", RECORD_FIELDS)
def test_environment_false_cannot_unhide_patient_fields(
    probe: Callable[[str], Probe], field: str
) -> None:
    """기준 67: 두 HIDE 환경변수가 false여도 환자 기록의 각 필드 값이 숨겨진다."""
    result = probe("env_false")
    _guard(result, "env_false")
    for marker in result["record_markers"][field]:
        assert _bodies(result).count(marker.encode()) == 0, field


def test_composite_guidance_unique_values_are_hidden(probe: Callable[[str], Probe]) -> None:
    """기준 47: 완결과 종결 이벤트에 실린 참고안의 고유 표지는 추적 전송에 없다."""
    result = probe("composite")
    _guard(result, "composite")
    sent = result["finish_guidances"]
    assert len(sent) == 1, "참고안을 실은 완결 응답이 없다."
    assert result["completed_guidances"] == sent, "종결 이벤트가 참고안을 그대로 싣지 않았다."
    markers = result["guidance_markers"]
    assert len(markers) == 4
    guidance_text = json.dumps(sent, ensure_ascii=False)
    transmitted = _bodies(result)
    for marker in markers:
        assert marker in guidance_text, "고유 표지가 실제 참고안에 없다."
        assert transmitted.count(marker.encode()) == 0, marker


def test_composite_guidance_allergy_values_are_hidden(probe: Callable[[str], Probe]) -> None:
    """기준 47: 참고안에도 실린 환자 기록의 각 알레르기 표지는 추적 전송에 없다."""
    result = probe("composite")
    _guard(result, "composite")
    sent = result["finish_guidances"]
    assert len(sent) == 1, "참고안을 실은 완결 응답이 없다."
    assert result["completed_guidances"] == sent, "종결 이벤트가 참고안을 그대로 싣지 않았다."
    markers = result["guidance_allergy_markers"]
    assert markers
    assert markers == result["record_markers"]["allergies"]
    alerts = json.dumps(sent[0]["safetyAlerts"], ensure_ascii=False)
    transmitted = _bodies(result)
    for marker in markers:
        assert marker in alerts, "기록의 알레르기 표지가 참고안 경고에 없다."
        assert transmitted.count(marker.encode()) == 0, marker
