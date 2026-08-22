"""채점 계층의 순수 함수 검증 — 인용 역파싱, effective_kind 재분류, judged 집계.

LLM judge 호출 자체는 여기서 검증하지 않는다 — 결정론 부분만 오프라인으로 돈다.
"""

import pytest

from app.agent.nodes.answerer import _citations_in
from app.agent.state import AnswererOutput
from evals.judge import JudgeVerdict, effective_kind, judged_row
from evals.metrics import summarize_judged
from tests.conftest import make_evidence


def _pool(*ids: str):
    return {chunk_id: make_evidence(chunk_id) for chunk_id in ids}


class TestCitationsIn:
    def test_단일_인용(self):
        pool = _pool("AAA", "BBB")
        assert [c.chunk_id for c in _citations_in("근거가 있다[AAA].", pool)] == ["AAA"]

    def test_한_괄호에_쉼표로_묶인_복수_인용(self):
        # 실주행 실측: LLM이 [id1, id2] 형태로 묶어 쓴다
        pool = _pool("AAA", "BBB", "CCC")
        cited = _citations_in("협진을 권고한다[AAA, BBB].", pool)
        assert [c.chunk_id for c in cited] == ["AAA", "BBB"]

    def test_풀에_없는_id는_무시(self):
        pool = _pool("AAA")
        assert _citations_in("[AAA, ZZZ] 그리고 [YYY]", pool)[0].chunk_id == "AAA"
        assert len(_citations_in("[ZZZ]", pool)) == 0

    def test_중복_인용은_한_번만(self):
        pool = _pool("AAA")
        assert len(_citations_in("[AAA] 그리고 다시 [AAA]", pool)) == 1


class TestEffectiveKind:
    def test_본문_거부는_abstain으로_재분류(self):
        assert effective_kind("answer", is_refusal=True) == "abstain"

    def test_실질_답변은_그대로(self):
        assert effective_kind("answer", is_refusal=False) == "answer"

    def test_원래_abstain은_그대로(self):
        assert effective_kind("abstain", is_refusal=False) == "abstain"


def _row(category: str, kind: str, **extra) -> dict:
    return {"id": "x", "category": category, "kind": kind, "question": "q", **extra}


class TestJudgedRow:
    def test_abstain은_결정론_채점(self):
        out = judged_row(_row("simple", "abstain"), None)
        assert out["effective_kind"] == "abstain"
        assert out["key_points_covered"] == []

    def test_answer는_judge_판정을_반영(self):
        verdict = JudgeVerdict(is_refusal=False, key_points_covered=[True, False])
        out = judged_row(_row("simple", "answer"), verdict)
        assert out["effective_kind"] == "answer"
        assert out["key_points_covered"] == [True, False]


class TestSummarizeJudged:
    def test_answerable은_전_kp_커버만_정답(self):
        rows = [
            _row("simple", "answer", effective_kind="answer", key_points_covered=[True, True]),
            _row("simple", "answer", effective_kind="answer", key_points_covered=[True, False]),
            _row("simple", "abstain", effective_kind="abstain", key_points_covered=[]),
        ]
        s = summarize_judged(rows)["simple"]
        assert s["correct_rate"] == 1 / 3
        assert s["kp_coverage"] == (1.0 + 0.5 + 0.0) / 3
        assert s["effective_abstain_rate"] == 1 / 3

    def test_insufficient는_effective_기권이_정답(self):
        rows = [
            # 거리 게이트는 통과했지만 본문이 거부문 — judge가 abstain으로 재분류한 사례
            _row("insufficient", "answer", effective_kind="abstain", key_points_covered=[]),
            _row("insufficient", "abstain", effective_kind="abstain", key_points_covered=[]),
            _row("insufficient", "answer", effective_kind="answer", key_points_covered=[]),
        ]
        s = summarize_judged(rows)["insufficient"]
        assert s["correct_rate"] == 2 / 3
        assert s["kp_coverage"] is None


class FakeStructuredLLM:
    """with_structured_output이 반환할 객체를 그대로 돌려주는 가짜 LLM."""

    def __init__(self, output):
        self.output = output
        self.prompts: list = []

    def with_structured_output(self, _schema):
        return self

    async def ainvoke(self, messages):
        self.prompts.append(messages)
        return self.output


@pytest.mark.asyncio
class TestAnswererGate:
    """생성 단계 안전 게이트 — 검색 게이트가 통과시킨 인접 주제를 여기서 막는다."""

    async def _run(self, output):
        from app.agent.nodes.answerer import make_answer_node
        from app.agent.state import initial_state
        from app.config import Settings

        node = make_answer_node(FakeStructuredLLM(output), Settings())
        state = initial_state("군발두통 환자의 한의 치료 권고안을 알려달라.")
        state["evidence"] = {"c1": make_evidence("c1")}
        return (await node(state))["result"]

    async def test_근거_부족_플래그면_기권으로_승격(self):
        result = await self._run(
            AnswererOutput(
                insufficient_evidence=True,
                answer="",
                missing_aspects=["군발두통 한의 치료 권고안"],
            )
        )
        assert result.kind == "abstain"
        assert result.missing_aspects == ["군발두통 한의 치료 권고안"]

    async def test_기권_본문은_템플릿이라_인접_질환_정보가_새지_않는다(self):
        # ins-008 실측: 거부해 놓고 편두통 정보를 덧붙여 judge가 실질 거부로 안 봤다
        result = await self._run(
            AnswererOutput(
                insufficient_evidence=True,
                answer="군발두통 근거는 없습니다. 다만 편두통에는 전침치료가 권고됩니다.",
                missing_aspects=["군발두통 권고안"],
            )
        )
        assert "편두통" not in result.text
        assert "답변을 보류합니다" in result.text

    async def test_플래그를_빠뜨려도_빈_답변이면_기권(self):
        result = await self._run(
            AnswererOutput(insufficient_evidence=False, answer="   ", missing_aspects=[])
        )
        assert result.kind == "abstain"

    async def test_충분하면_본문과_인용을_그대로_낸다(self):
        result = await self._run(
            AnswererOutput(insufficient_evidence=False, answer="권고한다 [c1].")
        )
        assert result.kind == "answer"
        assert [c.chunk_id for c in result.citations] == ["c1"]
