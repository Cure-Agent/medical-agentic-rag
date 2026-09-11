"""실제 빌드 이미지를 띄워 서비스 생존·비-root·추적 환경 불변식을 검증한다."""

import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HEALTH_PATH = "/api/v1/agent/healthz"


@dataclass(frozen=True)
class RunningContainer:
    """검증 중인 이미지·컨테이너와 호스트 접속 주소다."""

    image: str
    container_id: str
    base_url: str


def _docker(
    arguments: list[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Docker CLI를 실행하되 부재나 시간 초과를 조용히 건너뛰지 않는다."""
    command = ["docker", *arguments]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        pytest.fail("Docker CLI를 찾을 수 없다.", pytrace=False)
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"Docker 명령 시간이 초과됐다: {' '.join(command)}",
            pytrace=False,
        )


def _output_tail(result: subprocess.CompletedProcess[str]) -> str:
    """실패 원인을 남기되 긴 빌드·로그 출력은 꼬리만 보인다."""
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return combined[-4000:] or "(출력 없음)"


def _published_port(output: str) -> int:
    """docker port의 여러 줄 출력 중 첫 번째 유효한 호스트 포트를 읽는다."""
    for line in output.splitlines():
        _host, separator, port_text = line.strip().rpartition(":")
        if separator and port_text.isdecimal():
            return int(port_text)
    pytest.fail(f"호스트 포트를 파싱할 수 없다: {output!r}", pytrace=False)


def _http_status(url: str, *, timeout: float) -> int:
    """오류 상태도 HTTP 응답으로 인정해 상태 코드를 반환한다."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        return status


def _container_logs(container_id: str) -> str:
    result = _docker(["logs", container_id], timeout=60)
    return _output_tail(result)


def _wait_for_http(container_id: str, url: str) -> None:
    """응답 상태와 무관하게 HTTP가 열릴 때까지 컨테이너 생존을 함께 확인한다."""
    deadline = time.monotonic() + 60
    last_error = "아직 연결하지 않음"

    while (remaining := deadline - time.monotonic()) > 0:
        try:
            _http_status(url, timeout=min(2, remaining))
            return
        except OSError as error:
            last_error = repr(error)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        state = _docker(
            ["inspect", "--format", "{{.State.Running}}", container_id],
            timeout=min(5, remaining),
        )
        if state.returncode != 0 or state.stdout.strip() != "true":
            pytest.fail(
                "HTTP 준비 전에 컨테이너가 종료됐다.\n"
                f"상태 출력:\n{_output_tail(state)}\n"
                f"컨테이너 로그 꼬리:\n{_container_logs(container_id)}",
                pytrace=False,
            )

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.25, remaining))

    pytest.fail(
        "60초 안에 HTTP 응답을 받지 못했다.\n"
        f"마지막 연결 오류: {last_error}\n"
        f"컨테이너 로그 꼬리:\n{_container_logs(container_id)}",
        pytrace=False,
    )


@pytest.fixture(scope="module")
def agent_image() -> Iterator[str]:
    """레포 루트 Dockerfile로 고유 태그 이미지를 한 번 빌드한다."""
    info = _docker(["info"], timeout=60)
    if info.returncode != 0:
        pytest.fail(
            "docker info 실패 — Docker 데몬을 사용할 수 있어야 한다.\n"
            f"{_output_tail(info)}",
            pytrace=False,
        )

    image = f"medical-agentic-rag-spec49-{uuid.uuid4().hex}"
    try:
        build = _docker(
            ["build", "-t", image, str(REPO_ROOT)],
            timeout=1200,
        )
        if build.returncode != 0:
            pytest.fail(
                f"에이전트 이미지 빌드 실패:\n{_output_tail(build)}",
                pytrace=False,
            )
        yield image
    finally:
        _docker(["image", "rm", "-f", image], timeout=120)


@pytest.fixture(scope="module")
def running_container(agent_image: str) -> Iterator[RunningContainer]:
    """환경변수 주입 없이 8000 포트를 임의의 localhost 포트에 공개한다."""
    started = _docker(
        ["run", "-d", "-p", "127.0.0.1::8000", agent_image],
        timeout=120,
    )
    if started.returncode != 0:
        pytest.fail(
            f"에이전트 컨테이너 시작 실패:\n{_output_tail(started)}",
            pytrace=False,
        )
    container_id = started.stdout.strip()
    if not container_id:
        pytest.fail("docker run이 컨테이너 ID를 출력하지 않았다.", pytrace=False)

    try:
        port_result = _docker(
            ["port", container_id, "8000/tcp"],
            timeout=60,
        )
        if port_result.returncode != 0:
            pytest.fail(
                f"공개 포트 조회 실패:\n{_output_tail(port_result)}",
                pytrace=False,
            )
        host_port = _published_port(port_result.stdout)
        base_url = f"http://127.0.0.1:{host_port}"
        _wait_for_http(container_id, f"{base_url}{HEALTH_PATH}")
        yield RunningContainer(
            image=agent_image,
            container_id=container_id,
            base_url=base_url,
        )
    finally:
        _docker(["rm", "-f", container_id], timeout=120)


def _image_environment_keys(image: str) -> set[str]:
    """이미지 Config.Env의 KEY=VALUE 목록을 검증하고 키 집합을 만든다."""
    inspected = _docker(
        ["image", "inspect", "--format", "{{json .Config.Env}}", image],
        timeout=60,
    )
    if inspected.returncode != 0:
        pytest.fail(
            f"이미지 환경 조회 실패:\n{_output_tail(inspected)}",
            pytrace=False,
        )

    try:
        payload: object = json.loads(inspected.stdout.strip())
    except json.JSONDecodeError:
        pytest.fail(
            f"이미지 환경 출력이 JSON이 아니다: {inspected.stdout!r}",
            pytrace=False,
        )
    assert isinstance(payload, list)

    keys: set[str] = set()
    for entry in payload:
        assert isinstance(entry, str)
        key, separator, _value = entry.partition("=")
        assert separator == "=", f"KEY=VALUE 형식이 아니다: {entry!r}"
        keys.add(key)
    return keys


def test_container_healthz_returns_200(running_container: RunningContainer) -> None:
    """기준 20: 빌드 이미지의 컨테이너 healthz가 200을 반환한다."""
    status = _http_status(
        f"{running_container.base_url}{HEALTH_PATH}",
        timeout=5,
    )

    assert status == 200


def test_container_process_is_not_root(running_container: RunningContainer) -> None:
    """기준 21: 컨테이너 PID 1의 real·effective uid가 모두 0이 아니다."""
    status = _docker(
        ["exec", running_container.container_id, "cat", "/proc/1/status"],
        timeout=60,
    )
    assert status.returncode == 0, _output_tail(status)

    uid_lines = [line for line in status.stdout.splitlines() if line.startswith("Uid:")]
    assert uid_lines, "Uid: 줄이 없다."
    fields = uid_lines[0].split()
    assert len(fields) == 5 and fields[0] == "Uid:"
    try:
        uids = tuple(int(value) for value in fields[1:])
    except ValueError:
        pytest.fail(f"Uid 값을 정수로 파싱할 수 없다: {uid_lines[0]!r}", pytrace=False)

    assert uids[0] != 0
    assert uids[1] != 0


def test_image_has_no_tracing_environment(agent_image: str) -> None:
    """기준 22: 이미지 환경에는 AGENT·LangSmith·LangChain 추적 키가 없다."""
    keys = _image_environment_keys(agent_image)

    assert "PATH" in keys
    assert "AGENT_TRACING_ENABLED" not in keys
    assert not any(key.startswith("LANGSMITH_") for key in keys)
    assert not any(key.startswith("LANGCHAIN_") for key in keys)
