"""그래프 상태와 노드 입출력 스키마.

노드는 전부 이 모듈의 타입으로만 말한다 — 노드끼리 직접 부르지 않고,
오케스트레이터(graph.py)만 노드를 배선한다.
"""

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

from app.retrieval.base import Evidence


class DecomposedQueries(BaseModel):
    """Query Decomposer 출력 — 독립적으로 검색 가능한 하위 질의 1~4개."""

    queries: list[str] = Field(min_length=1, max_length=4)


class QueryCoverage(BaseModel):
    query: str
    covered: bool
    supporting_chunk_ids: list[str] = []


class EvaluatorVerdict(BaseModel):
    """Evidence Evaluator 출력. missing_aspects가 Query Generator의 입력이 된다."""

    sufficient: bool
    per_query: list[QueryCoverage] = []
    missing_aspects: list[str] = []


class GeneratedQueries(BaseModel):
    """Query Generator 출력 — 부족 정보를 채울 새 검색 질의 1~3개."""

    queries: list[str] = Field(min_length=0, max_length=3)


class AnswererOutput(BaseModel):
    """Answerer 구조화 출력 — 답변가능성 판정을 산문이 아니라 신호로 받는다.

    검색 게이트(리랭크 점수)는 **관련도**를 재는데, 우리가 알고 싶은 건 **답변가능성**이다.
    둘은 다르다: 군발두통 질문에 편두통 청크는 관련도가 높지만(실측 10.0/10) 답할 수는 없다.
    답변가능성은 근거 전문을 보고 답을 써 보는 쪽만 판단할 수 있어 여기서 받는다.
    """

    insufficient_evidence: bool
    answer: str = ""
    missing_aspects: list[str] = []


class Citation(BaseModel):
    chunk_id: str
    guideline_title: str
    section_title: str


class AgentAnswer(BaseModel):
    kind: Literal["answer", "abstain"]
    text: str
    citations: list[Citation] = []
    missing_aspects: list[str] = []


class AgentState(TypedDict):
    """LangGraph 상태. evidence는 chunk_id 키 dict라 라운드 간 중복이 저절로 제거된다."""

    question: str
    queries: list[str]  # 이번 라운드에 검색할 질의 (retrieve가 소비하고 비운다)
    searched_queries: list[str]  # 이미 검색한 질의 — Query Generator의 중복 회피 입력
    evidence: dict[str, Evidence]
    query_hits: dict[str, list[str]]  # 질의 → 찾은 chunk_id — 서브쿼리별 커버리지 평가용
    rerank_scores: dict[str, float]  # 질의 → top1 관련도(0~10). 리랭커 없는 구성에서는 빈 dict
    verdict: EvaluatorVerdict | None
    retrieval_count: int  # 완료된 **추가** 검색 라운드 수 (최초 검색은 0)
    trace: list[dict[str, Any]]
    result: AgentAnswer | None


def initial_state(question: str) -> AgentState:
    return AgentState(
        question=question,
        queries=[],
        searched_queries=[],
        evidence={},
        query_hits={},
        rerank_scores={},
        verdict=None,
        retrieval_count=0,
        trace=[],
        result=None,
    )


def trace_event(node: str, **data: Any) -> dict[str, Any]:
    return {"node": node, **data}
