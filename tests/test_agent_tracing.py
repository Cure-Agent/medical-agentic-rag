"""추적 환경 조합을 격리된 새 프로세스에서 판정한다."""

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEAD_ENDPOINT = "http://127.0.0.1:9"
PROBE = """
import json

from fastapi.testclient import TestClient
from langchain_core.callbacks import CallbackManager
from langchain_core.tracers.langchain import LangChainTracer

from app.service.main import create_app

with TestClient(create_app()):
    manager = CallbackManager.configure()
    tracer = any(isinstance(handler, LangChainTracer) for handler in manager.handlers)

print(json.dumps({"tracer": tracer}))
"""


def _probe_tracer(extra_environment: Mapping[str, str]) -> bool:
    """추적 캐시와 전역 설정이 없는 새 인터프리터에서 한 조합을 판정한다."""
    environment = os.environ.copy()
    for key in tuple(environment):
        if key == "AGENT_TRACING_ENABLED" or key.startswith(
            ("LANGSMITH_", "LANGCHAIN_")
        ):
            del environment[key]
    environment.update(extra_environment)

    repository_path = str(REPO_ROOT)
    inherited_pythonpath = environment.get("PYTHONPATH")
    if inherited_pythonpath:
        environment["PYTHONPATH"] = (
            f"{repository_path}{os.pathsep}{inherited_pythonpath}"
        )
    else:
        environment["PYTHONPATH"] = repository_path

    completed = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    stderr_tail = completed.stderr[-2000:]
    assert completed.returncode == 0, (
        f"추적 probe 종료코드: {completed.returncode}\n"
        f"stderr 꼬리:\n{stderr_tail}"
    )

    output_lines = completed.stdout.splitlines()
    assert output_lines, f"추적 probe 출력이 없다.\nstderr 꼬리:\n{stderr_tail}"
    try:
        payload: object = json.loads(output_lines[-1])
    except json.JSONDecodeError:
        pytest.fail(
            f"추적 probe 마지막 줄이 JSON이 아니다: {output_lines[-1]!r}\n"
            f"stderr 꼬리:\n{stderr_tail}",
            pytrace=False,
        )

    assert isinstance(payload, dict)
    tracer = payload.get("tracer")
    assert isinstance(tracer, bool)
    return tracer


def test_tracing_default_is_off_and_exact_true_turns_it_on() -> None:
    """기준 15·18: 무설정은 꺼지고 AGENT의 정확한 true는 켜진다."""
    assert _probe_tracer({}) is False
    assert (
        _probe_tracer(
            {
                "AGENT_TRACING_ENABLED": "true",
                "LANGSMITH_ENDPOINT": DEAD_ENDPOINT,
            }
        )
        is True
    )


def test_langsmith_environment_cannot_enable_tracing_alone() -> None:
    """기준 16: LANGSMITH_TRACING만 true여도 AGENT 스위치가 추적을 끈다."""
    assert (
        _probe_tracer(
            {
                "LANGSMITH_TRACING": "true",
                "LANGSMITH_ENDPOINT": DEAD_ENDPOINT,
            }
        )
        is False
    )


def test_langchain_environment_cannot_enable_tracing_alone() -> None:
    """기준 17: LANGCHAIN_TRACING_V2만 true여도 AGENT 스위치가 추적을 끈다."""
    assert (
        _probe_tracer(
            {
                "LANGCHAIN_TRACING_V2": "true",
                "LANGSMITH_ENDPOINT": DEAD_ENDPOINT,
            }
        )
        is False
    )


@pytest.mark.parametrize(
    "agent_value",
    ["", "1", "True"],
    ids=["empty", "one", "capitalized_true"],
)
def test_only_lowercase_true_enables_tracing(agent_value: str) -> None:
    """기준 19: 빈 값·1·대문자 True는 SDK 변수가 켜져 있어도 추적을 끈다."""
    assert (
        _probe_tracer(
            {
                "AGENT_TRACING_ENABLED": agent_value,
                "LANGSMITH_TRACING": "true",
                "LANGCHAIN_TRACING_V2": "true",
                "LANGSMITH_ENDPOINT": DEAD_ENDPOINT,
            }
        )
        is False
    )
