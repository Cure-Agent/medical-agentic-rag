"""리랭크 계층 검증 — 순위 파싱, 게이트 분업, 절단 지점, preset 배선.

LLM·DB 없이 돈다. 리랭커는 가짜를 꽂고, 검증 대상은 운영(cure-agent-be §29)에서
이식한 규칙들이다.
"""

import pytest

from app.agent.graph import PRESETS, build_graph, decide_rerank_gate
from app.agent.nodes.answerer import make_abstain_node
from app.agent.state import initial_state
from app.retrieval.base import QueryResult
from app.retrieval.reranker import RerankCandidate, RerankerError, RerankResult, normalize_ranking
from app.retrieval.reranking_retriever import RerankingRetriever
from tests.conftest import FakeRetriever, make_evidence


class TestNormalizeRanking:
    """운영에서 관측된 모델 출력 변형을 그대로 가져왔다."""

    def test_정상_배열(self):
        assert normalize_ranking([3, 1, 2], 5) == [3, 1, 2]

    def test_문자열_번호도_받는다(self):
        # 2026-08-02 운영 관측 — 모델이 간헐적으로 "3"을 낸다
        assert normalize_ranking(["3", "1"], 5) == [3, 1]

    def test_범위_밖과_중복_제거(self):
        assert normalize_ranking([1, 1, 99, 0, -2, 2], 5) == [1, 2]

    def test_소수는_탈락(self):
        assert normalize_ranking([3.5, 2], 5) == [2]

    def test_bool은_번호가_아니다(self):
        # True는 파이썬에서 int의 하위형 — 1로 새어들면 안 된다
        assert normalize_ranking([True, 2], 5) == [2]

    def test_빈_순위는_오류가_아니라_빈_리스트(self):
        # {"ranking":[null],"top1Relevance":0} = 관련 후보 없음 판정
        assert normalize_ranking([None], 5) == []
        assert normalize_ranking("not-a-list", 5) == []


class TestRerankGate:
    def test_컷_이상이면_답변(self):
        assert decide_rerank_gate(scores=[9.0], cutoff=9.0) == "answer"

    def test_컷_미만이면_기권(self):
        assert decide_rerank_gate(scores=[8.9], cutoff=9.0) == "abstain"

    def test_하위_질의_하나만_통과해도_답변(self):
        # 다면 질문에서 한 축의 근거가 없다고 전체를 버리지 않는다
        assert decide_rerank_gate(scores=[0.0, 2.0, 9.5], cutoff=9.0) == "answer"

    def test_전부_미달이면_기권(self):
        assert decide_rerank_gate(scores=[0.0, 3.0, 8.0], cutoff=9.0) == "abstain"

    def test_점수가_없으면_호출측이_폴백하도록_예외(self):
        with pytest.raises(ValueError):
            decide_rerank_gate(scores=[], cutoff=9.0)


class FakeReranker:
    """order/top1을 지정하거나, fail=True로 폴백 경로를 만든다."""

    def __init__(self, order: list[str] | None = None, top1: float = 10.0, fail: bool = False):
        self.order = order
        self.top1 = top1
        self.fail = fail
        self.calls: list[int] = []  # 호출당 후보 수 — 절단 지점 검증용

    async def rerank(self, question: str, candidates: list[RerankCandidate]) -> RerankResult:
        self.calls.append(len(candidates))
        if self.fail:
            raise RerankerError("boom")
        order = self.order if self.order is not None else [c.chunk_id for c in candidates]
        return RerankResult(order=order, top1_relevance=self.top1)


class FakeHybrid:
    """limit=None인 HybridRetriever 자리 — 자르지 않은 후보를 낸다."""

    def __init__(self, by_query):
        self.by_query = by_query

    async def search_many(self, queries):
        return {q: QueryResult(evidence=self.by_query.get(q, [])) for q in queries}


@pytest.mark.asyncio
class TestRerankingRetriever:
    async def test_후보_전체를_리랭커에_넘기고_top_k만_남긴다(self):
        # 절단 지점 검증: 30개 후보가 리랭커로 가고, 결과는 5개다
        pool = [make_evidence(f"c{i}", distance=0.1) for i in range(30)]
        reranker = FakeReranker(order=[f"c{i}" for i in range(29, -1, -1)])
        retriever = RerankingRetriever(
            FakeHybrid({"q": pool}), reranker, top_k=5, distance_cutoff=0.48
        )

        result = (await retriever.search_many(["q"]))["q"]

        assert reranker.calls == [30], "융합 후보 전체가 리랭커로 가야 한다"
        assert [e.chunk_id for e in result.evidence] == ["c29", "c28", "c27", "c26", "c25"]
        assert result.top1_relevance == 10.0

    async def test_top_k를_올리면_더_많이_남는다(self):
        # rerank_topk11 통제군이 실제로 근거를 더 넘기는지 — 5로 하드캡되면 통제 변인이 사라진다
        pool = [make_evidence(f"c{i}", distance=0.1) for i in range(30)]
        retriever = RerankingRetriever(
            FakeHybrid({"q": pool}), FakeReranker(), top_k=11, distance_cutoff=0.48
        )
        result = (await retriever.search_many(["q"]))["q"]
        assert len(result.evidence) == 11

    async def test_거리_사전_게이트에_걸리면_리랭커를_부르지_않는다(self):
        far = [make_evidence("c1", distance=0.9)]
        reranker = FakeReranker()
        retriever = RerankingRetriever(
            FakeHybrid({"q": far}), reranker, top_k=5, distance_cutoff=0.48
        )

        result = (await retriever.search_many(["q"]))["q"]

        assert reranker.calls == [], "먼 질문에 리랭크 비용을 쓰지 않는다"
        assert result.top1_relevance == 0.0, "점수 게이트가 기권으로 처리해야 한다"

    async def test_리랭크_실패는_코사인_폴백이고_점수는_None(self):
        pool = [make_evidence("c1", distance=0.1), make_evidence("c2", distance=0.2)]
        retriever = RerankingRetriever(
            FakeHybrid({"q": pool}), FakeReranker(fail=True), top_k=5, distance_cutoff=0.48
        )

        result = (await retriever.search_many(["q"]))["q"]

        assert [e.chunk_id for e in result.evidence] == ["c1", "c2"]
        assert result.top1_relevance is None, "None이면 거리 게이트로 되돌아간다"

    async def test_빈_순위여도_근거는_남기고_점수로_기권시킨다(self):
        pool = [make_evidence("c1", distance=0.1)]
        retriever = RerankingRetriever(
            FakeHybrid({"q": pool}),
            FakeReranker(order=[], top1=0.0),
            top_k=5,
            distance_cutoff=0.48,
        )

        result = (await retriever.search_many(["q"]))["q"]

        assert result.top1_relevance == 0.0
        assert len(result.evidence) == 1


@pytest.mark.asyncio
class TestRerankPresetWiring:
    """그래프 배선: 점수 게이트가 거리 게이트를 대체하는지."""

    async def _run(self, preset: str, scores: dict[str, float]):
        evidence = [make_evidence("c1", distance=0.10)]  # 거리는 항상 통과시킨다
        retriever = FakeRetriever({"질문": evidence}, scores=scores)
        captured = {}

        async def fake_answer(state):
            captured["kind"] = "answer"
            return {"result": None, "trace": state["trace"]}

        from app.agent.graph import AgentNodes
        from app.agent.nodes.decomposer import make_passthrough_decompose
        from app.agent.nodes.retrieve import make_retrieve_node

        nodes = AgentNodes(
            decompose=make_passthrough_decompose(),
            retrieve=make_retrieve_node(retriever),
            answer=fake_answer,
            abstain=make_abstain_node(),
        )
        final = await build_graph(PRESETS[preset], nodes).ainvoke(initial_state("질문"))
        return captured.get("kind", final["result"].kind if final["result"] else None)

    async def test_점수가_컷_미만이면_거리가_통과해도_기권(self):
        # 컷 3.5 — 명백한 무관(실측 분포 0~2)만 여기서 걸린다
        assert await self._run("rerank", {"질문": 1.0}) == "abstain"

    async def test_점수가_컷_이상이면_답변(self):
        assert await self._run("rerank", {"질문": 4.0}) == "answer"

    async def test_컷_하향으로_경계_점수가_통과한다(self):
        # 7.0은 옛 컷 9에서 기권이었다(ins-008 실측). 이제 통과하고 안전은 answerer가 맡는다
        assert await self._run("rerank", {"질문": 7.0}) == "answer"
        assert await self._run("rerank_cut9", {"질문": 7.0}) == "abstain"

    async def test_리랭크_실패로_점수가_없으면_거리_게이트로_폴백(self):
        # 점수 dict가 비면 거리 0.10 < 0.48 이므로 답변
        assert await self._run("rerank", {}) == "answer"


class TestPresetDefinitions:
    def test_통제군은_분해_없이_top_k만_다르다(self):
        base, control = PRESETS["rerank"], PRESETS["rerank_topk11"]
        assert control.enable_decomposition is base.enable_decomposition is False
        assert control.enable_rerank is base.enable_rerank is True
        assert (base.top_k, control.top_k) == (5, 11), "통제 변인은 근거 개수뿐이어야 한다"

    def test_리랭크_구성은_evaluator를_쓰지_않는다(self):
        for name in ("rerank", "rerank_topk11", "rerank_decomp"):
            assert PRESETS[name].enable_evaluator is False


@pytest.mark.asyncio
class TestFusedRerankingRetriever:
    """변형 B — 병합 후 원 질문으로 1회 재정렬."""

    async def test_리랭크는_원_질문으로_한_번만_부른다(self):
        from app.retrieval.fused_reranking_retriever import FusedRerankingRetriever

        hybrid = FakeHybrid({"하위1": [make_evidence("a")], "하위2": [make_evidence("b")]})
        reranker = FakeReranker()
        seen: list[str] = []

        async def spy(question, candidates):
            seen.append(question)
            return await FakeReranker.rerank(reranker, question, candidates)

        retriever = FusedRerankingRetriever(hybrid, reranker, top_k=5, distance_cutoff=0.48)
        retriever._reranker = type("R", (), {"rerank": staticmethod(spy)})()

        await retriever.search_many(["하위1", "하위2"], question="원 질문")

        assert seen == ["원 질문"], "질의 수와 무관하게 1회, 원 질문으로"
        assert reranker.calls == [2], "두 하위 질의의 후보가 병합돼 들어간다"

    async def test_점수가_하나라_최댓값_문제가_없다(self):
        from app.retrieval.fused_reranking_retriever import FusedRerankingRetriever

        hybrid = FakeHybrid({"q1": [make_evidence("a")], "q2": [make_evidence("b")]})
        retriever = FusedRerankingRetriever(
            hybrid, FakeReranker(top1=4.0), top_k=5, distance_cutoff=0.48
        )

        results = await retriever.search_many(["q1", "q2"], question="원 질문")

        assert {r.top1_relevance for r in results.values()} == {4.0}

    async def test_후보는_chunk_id로_중복_제거된다(self):
        from app.retrieval.fused_reranking_retriever import FusedRerankingRetriever

        shared = make_evidence("dup")
        hybrid = FakeHybrid({"q1": [shared, make_evidence("a")], "q2": [shared]})
        reranker = FakeReranker()
        retriever = FusedRerankingRetriever(hybrid, reranker, top_k=5, distance_cutoff=0.48)

        await retriever.search_many(["q1", "q2"], question="원 질문")

        assert reranker.calls == [2], "dup은 한 번만 후보로 간다"
