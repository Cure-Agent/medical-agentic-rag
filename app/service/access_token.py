"""access 토큰 잔여 수명 선검사 (BE docs/specs/51 「실행 도중 access 만료」).

에이전트는 access JWT의 `exp`·`iat`만 읽는다 — **서명을 검증하지 않는다.** 서명 검증과 인증 판정은
수락에서 BE가 하므로, 조작된 `exp`로 선검사를 넘겨도 수락이 거절한다. 여기서 읽는 값은 판정이 아니라
「이 토큰으로 실행 상한 안에 끝까지 갈 수 있나」의 힌트다.
"""

import base64
import json
from dataclasses import dataclass
from typing import TypeGuard

ACCESS_COOKIE = "access_token"


@dataclass(frozen=True)
class TokenLifetime:
    issued_at: float
    expires_at: float


def expires_within_run(cookie_header: str | None, *, now: float, run_deadline: float) -> bool:
    """남은 수명이 실행 상한보다 짧아 수락 전에 돌려보낼 토큰인가.

    읽을 수 없는 토큰은 검사하지 않는다(인증 판정은 BE의 몫). **전체 수명이 상한 이하인 토큰도
    검사하지 않는다** — 새로 발급받아도 상한을 넘지 못하므로, 돌려보내면 FE가 refresh 후 재시도에서
    같은 401을 받고 두 번째 401에서 강제 로그아웃한다.
    """
    token = read_cookie(cookie_header, ACCESS_COOKIE)
    lifetime = read_lifetime(token) if token is not None else None
    if lifetime is None:
        return False
    if lifetime.expires_at - lifetime.issued_at <= run_deadline:
        return False
    return lifetime.expires_at - now < run_deadline


def read_cookie(cookie_header: str | None, name: str) -> str | None:
    """Cookie 헤더에서 이름이 같은 첫 쿠키 값.

    표준 파서는 모양이 어긋난 쿠키 하나에 헤더 전체를 버린다 — 다른 쿠키 때문에 선검사가 빠지지 않게
    직접 가른다.
    """
    if not cookie_header:
        return None
    for pair in cookie_header.split(";"):
        key, separator, value = pair.strip().partition("=")
        if separator and key.strip() == name:
            return value.strip().removeprefix('"').removesuffix('"')
    return None


def read_lifetime(token: str) -> TokenLifetime | None:
    segments = token.split(".")
    if len(segments) != 3:
        return None
    encoded = segments[1]
    try:
        payload: object = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    issued_at, expires_at = payload.get("iat"), payload.get("exp")
    if not _is_seconds(issued_at) or not _is_seconds(expires_at):
        return None
    return TokenLifetime(issued_at=float(issued_at), expires_at=float(expires_at))


def _is_seconds(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)
