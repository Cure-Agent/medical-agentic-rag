"""턴 전체의 프로젝트명과 추적 비활성화 시 정상 종결을 경로별로 검증한다."""

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
SCENARIOS = ("guideline", "patient", "composite", "other")
Probe = dict[str, Any]
Collect = Callable[[str, bool], Probe]

# 기존 가짜 모델·BE·TestClient를 재사용하고 꺼짐 사례의 설정값만 바꾼다.
# main이 SDK import 전에 캡처 엔드포인트를 설치하도록 래퍼 안에서 가져온다.
CHILD_SCRIPT = """
import sys
from unittest.mock import patch

sys.path.insert(0, sys.argv[3])
import agent_trace_probe

original_run_turn = agent_trace_probe.run_turn

def run_turn(scenario):
    if sys.argv[2] == "true":
        return original_run_turn(scenario)

    from app.service.config import ServiceSettings

    def disabled_settings(*args, **kwargs):
        kwargs["agent_tracing_enabled"] = ""
        return ServiceSettings(*args, **kwargs)

    with patch("app.service.config.ServiceSettings", side_effect=disabled_settings):
        return original_run_turn(scenario)

agent_trace_probe.run_turn = run_turn
agent_trace_probe.main()
"""


@pytest.fixture(scope="module")
def probe() -> Collect:
    """경로·스위치 조합마다 새 프로세스를 쓰고 같은 조합의 결과만 재사용한다."""
    cache: dict[tuple[str, bool], Probe] = {}

    def collect(scenario: str, enabled: bool) -> Probe:
        key = (scenario, enabled)
        if key in cache:
            return cache[key]
        environment = os.environ.copy()
        for name in tuple(environment):
            if name == "AGENT_TRACING_ENABLED" or name.startswith(("LANGSMITH_", "LANGCHAIN_")):
                del environment[name]
        environment["NO_PROXY"] = "127.0.0.1,localhost"
        environment["no_proxy"] = "127.0.0.1,localhost"
        inherited = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(REPO_ROOT) + (
            os.pathsep + inherited if inherited else ""
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                CHILD_SCRIPT,
                scenario,
                "true" if enabled else "",
                str(REPO_ROOT / "tests"),
            ],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        diagnostic = (
            f"경로={scenario}, 추적={enabled}, 종료코드={completed.returncode}"
            f"\nstderr 꼬리:\n{completed.stderr[-3000:]}"
        )
        assert completed.returncode == 0, diagnostic
        lines = completed.stdout.splitlines()
        assert lines, f"자식 프로세스 출력이 없다.\n{diagnostic}"
        try:
            payload: object = json.loads(lines[-1])
        except json.JSONDecodeError:
            pytest.fail(f"자식 프로세스 JSON 파싱 실패.\n{diagnostic}", pytrace=False)
        assert isinstance(payload, dict), diagnostic
        result = cast(Probe, payload)
        cache[key] = result
        return result

    return collect


def _runs(result: Probe) -> dict[str, dict[str, Any]]:
    """캡처된 multipart 본체와 필드 파트를 런 id로 합친다."""
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


def _guard_terminal(result: Probe, scenario: str) -> None:
    """스텁의 error 종결이 정상 종결로 인정되지 않게 한다."""
    assert result["status"] == 200
    # 복합 가짜는 insufficientEvidence=false이므로 답변 완결이 정상이다.
    expected = "answer.abstained" if scenario == "other" else "answer.completed"
    assert result["events"] and result["events"][-1] == expected, result["events"]


def _guard_traced(
    result: Probe, scenario: str
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """정상 종결·도구 또는 완결 요청·분류기 캡처를 함께 확인한다."""
    _guard_terminal(result, scenario)
    if scenario == "guideline":
        assert any(
            item["path"].endswith("/guideline-answer") for item in result["backend_requests"]
        ), "가짜 지침 도구 호출이 없다."
    else:
        assert result["finishes"], "가짜 BE 완결 요청이 없다."
    runs = _runs(result)
    classifier_ids = {
        run_id
        for run_id, run in runs.items()
        if run.get("run_type") == "llm"
        and result["question"] in json.dumps(run.get("inputs"), ensure_ascii=False)
    }
    assert classifier_ids, "원문 질문이 입력에 실린 분류기 LLM 런이 없다."
    return runs, classifier_ids


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_classifier_project_is_cure_agent(probe: Collect, scenario: str) -> None:
    """기준 1: 네 경로 각각의 분류기 런 본체에 지정 프로젝트가 기록된다."""
    result = probe(scenario, True)
    runs, classifier_ids = _guard_traced(result, scenario)
    for run_id in classifier_ids:
        assert runs[run_id].get("session_name") == "cure-agent", runs[run_id]


@pytest.mark.parametrize("scenario", ["patient", "composite"])
def test_synthesis_project_is_cure_agent(probe: Collect, scenario: str) -> None:
    """기준 2: 환자·복합 분기 뒤 합성 런의 지정 프로젝트를 유지한다."""
    result = probe(scenario, True)
    runs, classifier_ids = _guard_traced(result, scenario)
    synthesis = [
        run
        for run_id, run in runs.items()
        if run.get("run_type") == "llm" and run_id not in classifier_ids
    ]
    assert synthesis, "분류기와 다른 합성 LLM 런이 없다."
    for run in synthesis:
        assert run.get("session_name") == "cure-agent", run


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_no_run_uses_default_project(probe: Collect, scenario: str) -> None:
    """기준 3: 정상 실행과 분류기 캡처를 확인한 뒤 모든 런의 default를 배제한다."""
    result = probe(scenario, True)
    runs, _ = _guard_traced(result, scenario)
    assert not {
        run_id: run for run_id, run in runs.items() if run.get("session_name") == "default"
    }, "default 프로젝트로 전송된 런이 있다."


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_stream_terminates_with_tracing_disabled(probe: Collect, scenario: str) -> None:
    """기준 4: 추적 스위치가 빈 문자열이어도 네 경로의 SSE가 정상 종결한다."""
    result = probe(scenario, False)
    _guard_terminal(result, scenario)
