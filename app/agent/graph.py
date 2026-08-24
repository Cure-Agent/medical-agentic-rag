"""오케스트레이터 — 노드 배선과 루프 종료 결정. LLM 호출이 없는 층이다.

재검색 예산은 LLM이 아니라 이 모듈의 라우팅 함수가 쥔다. 라우팅은 순수 함수라
LLM 없이 단위 테스트된다.

ablation은 build_graph(cfg)로 통제한다 — 표의 각 행 = 같은 노드 코드 + 다른 배선:
  baseline       : (passthrough) → retrieve → 거리 게이트 → answer | abstain
  decomposition  : decompose → retrieve → 거리 게이트 → answer | abstain
  full           : decompose → retrieve → evaluate ⇄ generate_queries (≤N) → answer | abstain
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from app.agent.state import AgentState

NodeFn = Callable[[AgentState], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class AgentConfig:
    enable_decomposition: bool = True
    enable_evaluator: bool = True
    max_retrieval: int = 2
    distance_cutoff: float = 0.48
    # 리랭커 구성 — enable_rerank=True면 검색 경로가 RerankingRetriever로 바뀌고
    # 기권 판정이 거리 게이트에서 점수 게이트로 넘어간다 (운영 §29와 같은 분업)
    enable_rerank: bool = False
    # True면 하위 질의 후보를 병합해 **원 질문으로 1회** 재정렬한다 (분해 변형 B).
    # False면 하위 질의마다 재정렬한다 (변형 A). enable_rerank=True일 때만 의미가 있다.
    fuse_before_rerank: bool = False
    # 컷 3.5: 안전을 answerer 게이트로 옮긴 뒤의 값이다. 9는 이 게이트가 안전을 혼자
    # 책임지던 시절의 값인데, LLM 자가보고 정수라 재표현에 7↔10으로 흔들려(실측) 컷 근처가
    # 불안정했다. 무관 문항은 0~2에 몰려 있어 낮은 컷이 오히려 안정적이다 — 역할은 비용 절감.
    rerank_score_cutoff: float = 3.5
    # answerer가 받는 근거 수. 질의당 상한이므로 분해 구성에서는 질의 수만큼 곱해진다
    top_k: int = 5
    # True면 answerer가 질문의 축을 먼저 열거하고 축마다 답한다. **검색 경로는 그대로다** —
    # 「근거는 풀에 있는데 답변이 놓친다」(실측 26/165)를 프롬프트만으로 회수할 수 있는지
    # 재는 축이라, 이 플래그와 검색 변경을 같은 구성에 섞으면 원인이 갈리지 않는다.
    enumerate_facets: bool = False


PRESETS: dict[str, AgentConfig] = {
    # ─ 리랭커 없는 구성 (2026-08-22 1차 실험) ─
    "baseline": AgentConfig(enable_decomposition=False, enable_evaluator=False),
    "decomposition": AgentConfig(enable_decomposition=True, enable_evaluator=False),
    "full": AgentConfig(enable_decomposition=True, enable_evaluator=True, max_retrieval=2),
    # ─ 운영 패리티 구성 — 이식 판단은 이 축에서 해야 한다 ─
    # rerank: cure-agent-be 운영 재현(하이브리드 → 리랭크 → top-5 → 점수 게이트)
    "rerank": AgentConfig(enable_decomposition=False, enable_evaluator=False, enable_rerank=True),
    # rerank_topk11: **근거 개수 통제군.** decomposition은 질의별 top-5를 합쳐 complex에서
    # 평균 11개를 answerer에 넘긴다(실측). 분해 없이 top_k만 11로 올린 이 구성이 따라잡으면
    # 이득의 정체는 "분해"가 아니라 "컨텍스트 증가"다. LLM 호출은 늘지 않는다.
    "rerank_topk11": AgentConfig(
        enable_decomposition=False, enable_evaluator=False, enable_rerank=True, top_k=11
    ),
    "rerank_decomp": AgentConfig(
        enable_decomposition=True, enable_evaluator=False, enable_rerank=True
    ),
    # 변형 B — 후보를 병합해 원 질문으로 1회 재정렬. A와 리랭크 호출 수·게이트 거동이 다르다
    "rerank_decomp_fused": AgentConfig(
        enable_decomposition=True,
        enable_evaluator=False,
        enable_rerank=True,
        fuse_before_rerank=True,
    ),
    # B는 top-5만 남기므로 A(근거 12.1개)와 근거 수가 다르다 — 그 차이를 통제한 B
    "rerank_decomp_fused_topk11": AgentConfig(
        enable_decomposition=True,
        enable_evaluator=False,
        enable_rerank=True,
        fuse_before_rerank=True,
        top_k=11,
    ),
}

# 반복 검색 루프 실험(4차) — `rerank`(운영 재현)에 **충분성 루프만** 얹은 짝이다.
# 통제군은 `rerank` 자신이고, 분해는 켜지 않는다 — 분해는 이미 기각됐으므로(2026-08-24)
# 여기서 재는 것은 「원 질문 1개로 검색한 뒤, 부족하면 질의를 새로 만들어 다시 검색하는 것이
# 운영 패리티 위에서 이득인가」 하나다.
#
# **주의: 이 구성에는 리랭크 점수 게이트가 없다.** enable_evaluator=True면 `build_graph`가
# `route_after_evaluate`만 배선하고 `route_gate`를 지나지 않는다 — 기권 판정이 점수 게이트에서
# evaluator 판정 + 재검색 예산으로 넘어간다. 따라서 `rerank`와의 차이는 「루프」 단독이 아니라
# **「루프 + 점수 게이트 제거」의 합**이고, 결과 문서에 그렇게 적어야 원인이 갈린다.
#
# v1의 `full`은 리랭커 없는 축에서 k=1로 잰 것이라 이 축의 근거가 못 된다(그 표는 폐기됨).
PRESETS["rerank_full"] = replace(PRESETS["rerank"], enable_evaluator=True, max_retrieval=2)

# 축 열거 실험(3차) — `rerank`(운영 재현)에서 **answerer 프롬프트만** 바꾼 짝이다.
# 분해와 달리 검색 경로·LLM 호출 수가 통제군과 완전히 같아, 차이가 나면 원인이 프롬프트 하나로
# 특정된다. 통제군은 `rerank` 자신이다.
PRESETS["rerank_facets"] = replace(PRESETS["rerank"], enumerate_facets=True)

# 2차 실험에서 쓴 공격적 컷 — v1 결과 재현·비교용으로 남긴다
LEGACY_RERANK_CUTOFF = 9.0
for _name in ("rerank", "rerank_topk11", "rerank_decomp"):
    PRESETS[f"{_name}_cut9"] = replace(PRESETS[_name], rerank_score_cutoff=LEGACY_RERANK_CUTOFF)

# 컷 스윕용 최저 컷 실행 — **컷을 재려는 것이 아니라 데이터를 넓히려는 구성이다.**
# 컷은 라우팅에만 쓰이므로(`route_gate`) 컷 c로 돌린 결과에서 c 이상의 임의의 컷을
# 사후 재구성할 수 있다(`evals/cut_sweep.py`). 거꾸로 c 미만은 재구성할 수 없다 —
# 그 점수대 문항은 answerer를 거치지 않아 생성 게이트 판정이 비어 있다. 그래서
# 컷 후보 전 구간을 한 번의 실행으로 덮으려면 관심 구간의 **하한 아래**에서 돌려야 한다.
# 0.0이 아니라 0.5인 이유: 거리 게이트에 걸린 질의는 top1_relevance=0.0으로 오는데
# (`RerankingRetriever`), 컷 0.0은 그것까지 통과시켜 운영에 없는 경로를 만든다.
SWEEP_RERANK_CUTOFF = 0.5
PRESETS["rerank_cut05"] = replace(PRESETS["rerank"], rerank_score_cutoff=SWEEP_RERANK_CUTOFF)


@dataclass
class AgentNodes:
    """그래프에 배선할 노드 구현체 — 테스트는 여기에 가짜를 꽂는다."""

    decompose: NodeFn
    retrieve: NodeFn
    answer: NodeFn
    abstain: NodeFn
    evaluate: NodeFn | None = None
    generate_queries: NodeFn | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def decide_after_evaluate(
    *, sufficient: bool, retrieval_count: int, max_retrieval: int
) -> Literal["answer", "generate_queries", "abstain"]:
    """③ 판정 후 분기 — GPT식 if/elif/else가 정확히 이 함수다."""
    if sufficient:
        return "answer"
    if retrieval_count >= max_retrieval:
        return "abstain"
    return "generate_queries"


def decide_gate(
    *, min_distance: float | None, cutoff: float
) -> Literal["answer", "abstain"]:
    """evaluator 없는 구성의 기권 게이트 — cure-agent-be의 거리 게이트(§28)와 같은 의미다.
    근거 0건(min_distance=None)은 기권한다."""
    if min_distance is None or min_distance > cutoff:
        return "abstain"
    return "answer"


def decide_rerank_gate(
    *, scores: list[float], cutoff: float
) -> Literal["answer", "abstain"]:
    """리랭크 점수 게이트 — cure-agent-be §29. top1 관련도가 컷 미만이면 기권한다.

    **최댓값을 본다.** 분해 구성에서는 하위 질의마다 점수가 하나씩 나오는데, 한 축의
    근거가 없다고 전체를 기권시키면 부분적으로 답할 수 있는 다면 질문을 통째로 버린다.
    점수가 하나도 없으면(전 질의 리랭크 실패) 호출측이 거리 게이트로 되돌아간다.
    """
    if not scores:
        raise ValueError("점수가 없다 — 호출측이 거리 게이트로 폴백해야 한다")
    return "answer" if max(scores) >= cutoff else "abstain"


def build_graph(cfg: AgentConfig, nodes: AgentNodes):
    builder = StateGraph(AgentState)
    builder.add_node("decompose", nodes.decompose)
    builder.add_node("retrieve", nodes.retrieve)
    builder.add_node("answer", nodes.answer)
    builder.add_node("abstain", nodes.abstain)

    builder.add_edge(START, "decompose")
    builder.add_edge("decompose", "retrieve")

    if cfg.enable_evaluator:
        if nodes.evaluate is None or nodes.generate_queries is None:
            raise ValueError("enable_evaluator=True인데 evaluate/generate_queries 노드가 없다")
        builder.add_node("evaluate", nodes.evaluate)
        builder.add_node("generate_queries", nodes.generate_queries)
        builder.add_edge("retrieve", "evaluate")

        def route_after_evaluate(state: AgentState) -> str:
            verdict = state["verdict"]
            return decide_after_evaluate(
                sufficient=bool(verdict and verdict.sufficient),
                retrieval_count=state["retrieval_count"],
                max_retrieval=cfg.max_retrieval,
            )

        builder.add_conditional_edges(
            "evaluate",
            route_after_evaluate,
            {"answer": "answer", "generate_queries": "generate_queries", "abstain": "abstain"},
        )
        builder.add_edge("generate_queries", "retrieve")  # ← 루프
    else:

        def route_gate(state: AgentState) -> str:
            # 리랭크 구성이면 점수 게이트가 기권을 판정한다 — 거리 게이트는 이미
            # RerankingRetriever 안에서 리랭크 앞단으로 옮겨갔다(운영과 같은 순서).
            scores = list(state["rerank_scores"].values())
            if cfg.enable_rerank and scores:
                return decide_rerank_gate(scores=scores, cutoff=cfg.rerank_score_cutoff)
            distances = [e.distance for e in state["evidence"].values()]
            return decide_gate(
                min_distance=min(distances) if distances else None,
                cutoff=cfg.distance_cutoff,
            )

        builder.add_conditional_edges(
            "retrieve", route_gate, {"answer": "answer", "abstain": "abstain"}
        )

    builder.add_edge("answer", END)
    builder.add_edge("abstain", END)
    return builder.compile()
