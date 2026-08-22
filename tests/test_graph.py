"""그래프 배선 테스트 — LLM 노드만 가짜로 꽂고, retrieve·abstain·라우팅은 실물을 쓴다.

루프가 실제로 돌고(재검색), 예산에서 멈추고(기권), 근거가 chunk_id로 중복 제거되는지를
LLM·DB 없이 검증한다.
"""

from app.agent.graph import AgentConfig, AgentNodes, build_graph
from app.agent.nodes.answerer import make_abstain_node
from app.agent.nodes.decomposer import make_passthrough_decompose
from app.agent.nodes.retrieve import make_retrieve_node
from app.agent.state import AgentAnswer, EvaluatorVerdict, initial_state, trace_event
from tests.conftest import FakeRetriever, make_evidence


def fake_decompose(queries: list[str]):
    async def decompose(state):
        return {"queries": queries, "trace": state["trace"] + [trace_event("decompose")]}

    return decompose


def fake_answer():
    async def answer(state):
        return {
            "result": AgentAnswer(kind="answer", text="답변 [chunk-1]"),
            "trace": state["trace"] + [trace_event("answer")],
        }

    return answer


def scripted_evaluate(verdicts: list[EvaluatorVerdict]):
    """호출될 때마다 다음 판정을 내는 Evaluator 대역."""
    remaining = list(verdicts)

    async def evaluate(state):
        verdict = remaining.pop(0)
        return {"verdict": verdict, "trace": state["trace"] + [trace_event("evaluate")]}

    evaluate.remaining = remaining
    return evaluate


def fake_generate(queries: list[str]):
    async def generate_queries(state):
        new = [q for q in queries if q not in state["searched_queries"]]
        return {
            "queries": new,
            "retrieval_count": state["retrieval_count"] + 1,
            "trace": state["trace"] + [trace_event("generate_queries")],
        }

    return generate_queries


E1 = make_evidence("chunk-1", distance=0.20)
E2 = make_evidence("chunk-2", distance=0.30)
E3 = make_evidence("chunk-3", distance=0.40)


async def test_full_loop_re_retrieves_then_answers():
    retriever = FakeRetriever({"q-a": [E1], "q-b": [E1, E2], "q-c": [E3]})
    insufficient = EvaluatorVerdict(sufficient=False, missing_aspects=["보충 정보"])
    sufficient = EvaluatorVerdict(sufficient=True)

    cfg = AgentConfig(max_retrieval=2)
    graph = build_graph(
        cfg,
        AgentNodes(
            decompose=fake_decompose(["q-a", "q-b"]),
            retrieve=make_retrieve_node(retriever),
            answer=fake_answer(),
            abstain=make_abstain_node(),
            evaluate=scripted_evaluate([insufficient, sufficient]),
            generate_queries=fake_generate(["q-c"]),
        ),
    )
    final = await graph.ainvoke(initial_state("질문"))

    assert final["result"].kind == "answer"
    assert retriever.calls == [["q-a", "q-b"], ["q-c"]]  # 정확히 1회 재검색
    assert set(final["evidence"]) == {"chunk-1", "chunk-2", "chunk-3"}  # dedup 누적
    assert final["retrieval_count"] == 1
    assert final["searched_queries"] == ["q-a", "q-b", "q-c"]


async def test_full_loop_exhausts_budget_and_abstains():
    retriever = FakeRetriever({"q": [E1], "q-c": [E3]})
    insufficient = EvaluatorVerdict(sufficient=False, missing_aspects=["끝내 못 찾은 정보"])

    cfg = AgentConfig(max_retrieval=1)
    graph = build_graph(
        cfg,
        AgentNodes(
            decompose=fake_decompose(["q"]),
            retrieve=make_retrieve_node(retriever),
            answer=fake_answer(),
            abstain=make_abstain_node(),
            evaluate=scripted_evaluate([insufficient, insufficient]),
            generate_queries=fake_generate(["q-c"]),
        ),
    )
    final = await graph.ainvoke(initial_state("질문"))

    assert final["result"].kind == "abstain"
    assert final["result"].missing_aspects == ["끝내 못 찾은 정보"]
    assert final["retrieval_count"] == 1
    assert len(retriever.calls) == 2  # 최초 + 재검색 1회에서 멈췄다


async def test_baseline_gate_answers_below_cutoff():
    retriever = FakeRetriever({"질문": [E1]})  # distance 0.20 < cutoff 0.48
    cfg = AgentConfig(enable_decomposition=False, enable_evaluator=False)
    graph = build_graph(
        cfg,
        AgentNodes(
            decompose=make_passthrough_decompose(),
            retrieve=make_retrieve_node(retriever),
            answer=fake_answer(),
            abstain=make_abstain_node(),
        ),
    )
    final = await graph.ainvoke(initial_state("질문"))
    assert final["result"].kind == "answer"
    assert final["searched_queries"] == ["질문"]  # 분해 없이 원 질문 그대로


async def test_baseline_gate_abstains_above_cutoff_and_on_empty():
    cfg = AgentConfig(enable_decomposition=False, enable_evaluator=False)

    far = FakeRetriever({"질문": [make_evidence("chunk-9", distance=0.90)]})
    graph = build_graph(
        cfg,
        AgentNodes(
            decompose=make_passthrough_decompose(),
            retrieve=make_retrieve_node(far),
            answer=fake_answer(),
            abstain=make_abstain_node(),
        ),
    )
    final = await graph.ainvoke(initial_state("질문"))
    assert final["result"].kind == "abstain"

    empty = FakeRetriever({})
    graph = build_graph(
        cfg,
        AgentNodes(
            decompose=make_passthrough_decompose(),
            retrieve=make_retrieve_node(empty),
            answer=fake_answer(),
            abstain=make_abstain_node(),
        ),
    )
    final = await graph.ainvoke(initial_state("질문"))
    assert final["result"].kind == "abstain"
