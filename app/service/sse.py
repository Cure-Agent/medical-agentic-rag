"""SSE 프레임 — 브라우저로 쓰는 쪽과 BE 내부 SSE를 읽는 쪽 (BE §8 전송 규약).

봉투를 씌우지 않고 이벤트 하나를 `data: <JSON>` 프레임 하나로 쓴다. 하트비트는 SSE 주석이다.
"""

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, cast

PING_FRAME = b": ping\n\n"


class SseProtocolError(Exception):
    """BE SSE가 이벤트 계약(JSON 객체 · `eventType`)을 지키지 않았다."""


@dataclass(frozen=True)
class SseEvent:
    """BE가 보낸 이벤트 하나 — 원문 `data`와 해석한 JSON을 함께 든다.

    중계는 원문을 그대로 다시 쓴다. 다시 직렬화하면 키 순서·이스케이프가 바뀌어 「내용 그대로」가
    JSON 동등성으로만 남는다.
    """

    data: str
    payload: dict[str, Any]

    @property
    def event_type(self) -> str:
        return cast(str, self.payload["eventType"])


def event_frame(event: Mapping[str, Any]) -> bytes:
    body = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"data: {body}\n\n".encode()


def relay_frame(event: SseEvent) -> bytes:
    # data 안의 줄바꿈은 줄마다 data: 필드로 나눠야 한 이벤트로 재조립된다
    lines = "".join(f"data: {line}\n" for line in event.data.split("\n"))
    return f"{lines}\n".encode()


async def read_events(lines: AsyncIterator[str]) -> AsyncIterator[SseEvent]:
    """줄 단위 SSE를 이벤트로 묶는다 — 주석(`:`)과 `data` 밖의 필드는 버린다."""
    data: list[str] = []
    async for line in lines:
        if line == "":
            if data:
                yield _parse("\n".join(data))
                data = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data.append(value.removeprefix(" "))
    if data:
        yield _parse("\n".join(data))


def _parse(data: str) -> SseEvent:
    try:
        payload: object = json.loads(data)
    except ValueError as e:
        raise SseProtocolError("이벤트 data가 JSON이 아니다") from e
    if not isinstance(payload, dict) or not isinstance(payload.get("eventType"), str):
        raise SseProtocolError("이벤트에 eventType이 없다")
    return SseEvent(data=data, payload=cast(dict[str, Any], payload))
