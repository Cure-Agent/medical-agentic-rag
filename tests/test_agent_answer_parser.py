"""복합 합성의 판정 선행 파서·인용 규칙 — 구현측 회귀 가드 (spec 51 수용 기준 밖, 동결 대상 아님).

동결 테스트는 파서를 스트림 흐름으로만 본다(판정 전 델타 없음 · 기권 시 델타 없음 · 인용). 여기서는
조각 경계가 토큰·이스케이프·서로게이트 쌍 한가운데에 떨어지는 경우와, 판정 확정 전후로 등급이
갈리는 해석 실패를 잠근다 — 실제 모델은 조각을 어디서든 자른다.
"""

import json

import pytest

from app.service.synthesis import MalformedAnswerError, VerdictFirstParser, citations_for


def _feed(pieces: list[str]) -> tuple[VerdictFirstParser, list[list[str]]]:
    parser = VerdictFirstParser()
    return parser, [parser.push(piece) for piece in pieces]


def _every_split(text: str) -> list[list[str]]:
    return [[text[:index], text[index:]] for index in range(1, len(text))]


ANSWER = '첫 줄\n"따옴표" \\ 역슬래시 😀 끝 [1]'
DOCUMENT = json.dumps({"insufficientEvidence": False, "answer": ANSWER}, ensure_ascii=True)


@pytest.mark.parametrize("pieces", _every_split(DOCUMENT))
def test_any_two_way_split_reassembles_the_answer(pieces: list[str]) -> None:
    """어느 경계에서 잘려도 델타를 이으면 원래 답변이다 — `\\uXXXX`·서로게이트 쌍 포함."""
    parser, outputs = _feed(pieces)
    parser.finish()
    assert "".join(text for output in outputs for text in output) == ANSWER
    assert parser.insufficient_evidence is False


def test_no_delta_until_verdict_value_is_complete() -> None:
    parser = VerdictFirstParser()
    assert parser.push('{"insufficientEvidence": fal') == []
    assert parser.push('se, "answer": "가') == ["가"]
    assert parser.push('나"}') == ["나"]
    parser.finish()


def test_answer_before_verdict_is_held_then_released() -> None:
    """스키마 순서 위반 — 판정이 서기 전에 온 답변은 흘리지 않고 보류한다."""
    parser = VerdictFirstParser()
    assert parser.push('{"answer": "먼저 온 답", ') == []
    assert parser.push('"insufficientEvidence": false}') == ["먼저 온 답"]
    parser.finish()


def test_answer_before_abstain_verdict_is_dropped() -> None:
    parser = VerdictFirstParser()
    assert parser.push('{"answer": "버려질 답", "insufficientEvidence": true}') == []
    assert parser.insufficient_evidence is True
    parser.finish()


def test_abstain_verdict_ignores_the_rest() -> None:
    parser = VerdictFirstParser()
    assert parser.push('{"insufficientEvidence": true, "answer": "근거가') == []
    assert parser.push(' 부족합니다"}') == []
    assert parser.insufficient_evidence is True
    parser.finish()


def test_string_boolean_verdict_is_accepted() -> None:
    parser = VerdictFirstParser()
    assert parser.push('{"insufficientEvidence": "false", "answer": "답"}') == ["답"]
    parser.finish()


@pytest.mark.parametrize(
    "document",
    ['["배열"]', '{"insufficientEvidence": maybe, "answer": ""}', '{"insufficientEvidence" false}'],
)
def test_malformed_before_verdict_fails(document: str) -> None:
    with pytest.raises(MalformedAnswerError):
        VerdictFirstParser().push(document)


def test_stream_ending_before_verdict_fails() -> None:
    parser = VerdictFirstParser()
    parser.push('{"insufficientEvidence": ')
    with pytest.raises(MalformedAnswerError):
        parser.finish()


def test_object_closed_without_verdict_fails_and_never_releases_held_answer() -> None:
    parser = VerdictFirstParser()
    assert parser.push('{"answer": "판정 없는 답"}') == []
    with pytest.raises(MalformedAnswerError):
        parser.finish()


def test_malformed_after_verdict_stops_quietly_with_answer_so_far() -> None:
    """판정 뒤의 깨짐은 출력 상한 잘림과 같다 — 흘린 만큼으로 끝난다."""
    parser = VerdictFirstParser()
    assert parser.push('{"insufficientEvidence": false, "answer": "여기까지') == ["여기까지"]
    assert parser.push('\\q 이후"}') == []
    assert parser.push("더 오는 조각") == []
    parser.finish()


def test_lone_surrogate_escape_becomes_replacement_character() -> None:
    parser = VerdictFirstParser()
    output = parser.push('{"insufficientEvidence": false, "answer": "a\\ud83dbcdefgh"}')
    assert "".join(output) == "a�bcdefgh"
    "".join(output).encode()  # UTF-8로 쓸 수 있어야 SSE 프레임이 된다


def test_citations_follow_used_markers_within_evidence_range() -> None:
    evidence_ids = ["E-first", "E-second", "E-third"]
    answer = "가 [3] 나 [1] 다 [3] 라 [0] 마 [4] 바 [01]"
    assert citations_for(answer, evidence_ids) == [
        {"marker": 1, "evidenceId": "E-first"},
        {"marker": 3, "evidenceId": "E-third"},
    ]
