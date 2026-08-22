"""① Query Decomposer — 질문을 독립 검색 가능한 하위 질의로 분해한다."""

from langchain_core.language_models import BaseChatModel

from app.agent.state import AgentState, DecomposedQueries, trace_event
from app.llm.prompts import DECOMPOSER_SYSTEM


def make_decompose_node(llm: BaseChatModel):
    structured = llm.with_structured_output(DecomposedQueries)

    async def decompose(state: AgentState) -> dict:
        try:
            result: DecomposedQueries = await structured.ainvoke(
                [("system", DECOMPOSER_SYSTEM), ("user", state["question"])]
            )
            queries = [q.strip() for q in result.queries if q.strip()] or [state["question"]]
            fallback = False
        except Exception:
            # 분해 실패가 파이프라인을 죽여선 안 된다 — 원 질문 그대로 검색한다
            queries = [state["question"]]
            fallback = True
        return {
            "queries": queries,
            "trace": state["trace"]
            + [trace_event("decompose", queries=queries, fallback=fallback)],
        }

    return decompose


def make_passthrough_decompose():
    """ablation용 — decomposition 꺼진 구성은 LLM을 부르지 않고 원 질문 그대로 검색한다."""

    async def decompose(state: AgentState) -> dict:
        return {
            "queries": [state["question"]],
            "trace": state["trace"] + [trace_event("decompose", passthrough=True)],
        }

    return decompose
