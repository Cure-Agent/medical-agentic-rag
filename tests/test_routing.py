"""루프 종료 결정은 순수 함수다 — LLM 없이 전 분기를 검증한다."""

from app.agent.graph import decide_after_evaluate, decide_gate


def test_sufficient_goes_to_answer():
    assert (
        decide_after_evaluate(sufficient=True, retrieval_count=0, max_retrieval=2) == "answer"
    )


def test_insufficient_with_budget_goes_to_generate():
    assert (
        decide_after_evaluate(sufficient=False, retrieval_count=0, max_retrieval=2)
        == "generate_queries"
    )
    assert (
        decide_after_evaluate(sufficient=False, retrieval_count=1, max_retrieval=2)
        == "generate_queries"
    )


def test_insufficient_with_exhausted_budget_abstains():
    assert (
        decide_after_evaluate(sufficient=False, retrieval_count=2, max_retrieval=2) == "abstain"
    )


def test_zero_budget_never_regenerates():
    assert (
        decide_after_evaluate(sufficient=False, retrieval_count=0, max_retrieval=0) == "abstain"
    )


def test_gate_passes_below_cutoff():
    assert decide_gate(min_distance=0.30, cutoff=0.48) == "answer"


def test_gate_abstains_above_cutoff():
    assert decide_gate(min_distance=0.49, cutoff=0.48) == "abstain"


def test_gate_abstains_on_empty_pool():
    assert decide_gate(min_distance=None, cutoff=0.48) == "abstain"
