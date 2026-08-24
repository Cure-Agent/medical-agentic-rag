"""컷 사후 스윕이 기대는 가정을 잠근다.

`evals/cut_sweep.py`는 「컷 c로 돌린 결과에서 c 이상의 임의의 컷을 재구성할 수 있다」에
기댄다. 그 전제는 **컷이 라우팅 밖으로 새지 않는다**는 것 하나다. 컷이 검색 경로에 닿는
순간 — 예컨대 리랭커 요청 개수나 후보 절단에 쓰이는 순간 — 사후 계산은 조용히 틀린다.
그때 표는 여전히 그럴듯한 숫자를 뱉으므로 실행 중에는 드러나지 않는다.

같은 종류의 실패를 이미 겪었다: 근거 개수 통제군이 리랭커 요청 개수 고정 때문에 운영
재현군과 같아졌고, 검색 정책 폴백 때문에 실행된 컷과 기록된 컷이 갈렸다. 둘 다
「돌아가긴 하는데 재는 것이 다른」 결함이었다.
"""

from dataclasses import asdict, replace

import pytest

from app.agent.graph import PRESETS, decide_rerank_gate
from app.config import Settings
from app.retrieval.factory import make_retriever
from evals.cut_sweep import gate_layer, rethreshold, top1_score, validate_grid
from evals.metrics import row_correct


def judged_row(
    item_id: str,
    category: str,
    scores: dict[str, float] | None,
    effective_kind: str,
    covered: list[bool] | None = None,
) -> dict:
    return {
        "id": item_id,
        "category": category,
        "kind": "answer" if effective_kind == "answer" else "abstain",
        "rerank_scores": scores,
        "effective_kind": effective_kind,
        "key_points_covered": covered or [],
    }


class TestCutDoesNotReachRetrieval:
    """사후 스윕의 유일한 전제 — 컷은 라우팅에만 쓰인다."""

    def test_컷만_다른_두_구성은_같은_리트리버를_만든다(self):
        settings = Settings(openai_api_key="test-key")
        base = PRESETS["rerank"]
        cut9 = PRESETS["rerank_cut9"]
        assert base.rerank_score_cutoff != cut9.rerank_score_cutoff, "전제: 컷만 다른 짝이다"

        made = [make_retriever(cfg, None, None, settings) for cfg in (base, cut9)]

        assert type(made[0]) is type(made[1])
        # 리랭크 경로에서 검색 결과를 결정하는 값 전부 — 하나라도 컷을 타면 스윕이 틀린다
        for attr in ("_top_k", "_distance_cutoff"):
            assert getattr(made[0], attr) == getattr(made[1], attr), attr
        # 요청 개수는 시스템 프롬프트에 박힌다 — 통제군을 무너뜨렸던 바로 그 값이다
        assert made[0]._reranker._system_prompt == made[1]._reranker._system_prompt
        assert made[0]._reranker._model == made[1]._reranker._model

    def test_컷을_바꿔도_config의_검색_필드가_그대로다(self):
        base = PRESETS["rerank"]
        for cut in (0.0, 3.5, 9.0, 11.0):
            variant = replace(base, rerank_score_cutoff=cut)
            for field in ("top_k", "distance_cutoff", "enable_rerank", "fuse_before_rerank"):
                assert getattr(variant, field) == getattr(base, field), field

    def test_스윕_프리셋은_컷만_내린_rerank다(self):
        """운영 재현군과 컷 하나만 달라야 한다 — 다른 게 섞이면 사후 스윕이 다른 구성을 잰다."""
        base, sweep = PRESETS["rerank"], PRESETS["rerank_cut05"]
        changed = {k for k, v in asdict(base).items() if v != asdict(sweep)[k]}
        assert changed == {"rerank_score_cutoff"}
        assert sweep.rerank_score_cutoff < base.rerank_score_cutoff
        # 0은 안 된다 — 거리 게이트가 돌려주는 top1=0.0까지 통과해 운영에 없는 경로가 된다
        assert sweep.rerank_score_cutoff > 0.0


class TestRethresholdMatchesRouting:
    """재구성이 실제 라우팅(`decide_rerank_gate`)과 같은 판정을 내야 한다."""

    @pytest.mark.parametrize("score", [0.0, 2.0, 3.5, 8.0, 9.0, 10.0])
    @pytest.mark.parametrize("cut", [3.5, 8.0, 9.0, 10.0])
    def test_모든_점수x컷_조합에서_일치한다(self, score, cut):
        row = judged_row("x", "complex", {"q": score}, "answer", [True])
        routed = decide_rerank_gate(scores=[score], cutoff=cut)
        assert rethreshold(row, cut)["effective_kind"] == (
            "answer" if routed == "answer" else "abstain"
        )

    def test_분해_구성은_최댓값을_본다(self):
        # 한 축의 근거가 없다고 전체를 기권시키지 않는다 — 라우팅과 같은 규칙이어야 한다
        row = judged_row("x", "complex", {"q1": 0.0, "q2": 2.0, "q3": 9.5}, "answer", [True])
        assert decide_rerank_gate(scores=[0.0, 2.0, 9.5], cutoff=9.0) == "answer"
        assert rethreshold(row, 9.0)["effective_kind"] == "answer"
        assert gate_layer(row, 9.0) == "answered"

    def test_검색_게이트에_걸리면_kp_커버리지가_사라진다(self):
        """answerer가 호출되지 않으므로 부분 점수도 없다 — 남겨두면 기권이 득점한다."""
        row = judged_row("x", "complex", {"q": 4.0}, "answer", [True, True, False])
        cut_off = rethreshold(row, 9.0)
        assert cut_off["key_points_covered"] == []
        assert not row_correct(cut_off)


class TestFallbackRows:
    def test_점수가_없는_행은_컷과_무관하다(self):
        """전 질의 리랭크 실패 — 실제 라우팅도 거리 게이트로 되돌아간다."""
        row = judged_row("x", "complex", {}, "answer", [True])
        assert top1_score(row) is None
        for cut in (3.5, 9.0, 99.0):
            assert rethreshold(row, cut) is row
            assert gate_layer(row, cut) == "fallback"

    def test_None_점수는_없는_것으로_본다(self):
        # retrieve 노드는 top1_relevance=None을 dict에 담지 않지만, 옛 결과 파일에는 있다
        row = judged_row("x", "complex", {"q": None}, "answer", [True])
        assert top1_score(row) is None


class TestGateLayer:
    def test_세_층이_구분된다(self):
        answered = judged_row("a", "complex", {"q": 10.0}, "answer", [True])
        generation = judged_row("b", "insufficient", {"q": 10.0}, "abstain")
        assert gate_layer(answered, 3.5) == "answered"
        assert gate_layer(generation, 3.5) == "generation", "검색 게이트를 통과한 뒤 잡힌 것"
        assert gate_layer(generation, 10.5) == "retrieval", "컷을 올리면 검색 게이트가 먼저 잡는다"

    def test_insufficient는_어느_층이_잡아도_정답이다(self):
        """스윕의 손익 판정이 여기 기댄다 — 층이 바뀐 것은 손해가 아니라 비용 절감이다."""
        row = judged_row("ins-008", "insufficient", {"q": 9.0}, "abstain")
        assert row_correct(row)
        assert row_correct(rethreshold(row, 10.0))


class TestValidateGrid:
    def test_실행컷보다_낮은_컷은_거부한다(self):
        with pytest.raises(ValueError, match="재구성할 수 없다"):
            validate_grid([2.0, 3.5, 9.0], run_cut=3.5)

    def test_실행컷_이상은_통과한다(self):
        validate_grid([3.5, 9.0, 11.0], run_cut=3.5)
