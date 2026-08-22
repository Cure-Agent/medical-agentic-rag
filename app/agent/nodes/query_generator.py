"""Query Generator — Evaluator가 지목한 부족 정보를 채울 새 검색 질의를 만든다."""

from langchain_core.language_models import BaseChatModel

from app.agent.state import AgentState, GeneratedQueries, trace_event
from app.llm.prompts import QUERY_GENERATOR_SYSTEM


def make_generate_queries_node(llm: BaseChatModel):
    structured = llm.with_structured_output(GeneratedQueries)

    async def generate_queries(state: AgentState) -> dict:
        verdict = state["verdict"]
        missing = "\n".join(f"- {m}" for m in (verdict.missing_aspects if verdict else []))
        missing = missing or "- (명시되지 않음 — 원 질문 기준으로 판단)"
        tried = "\n".join(f"- {q}" for q in state["searched_queries"])
        user = (
            f"## 원 질문\n{state['question']}\n\n"
            f"## 부족하다고 판정된 정보\n{missing}\n\n"
            f"## 이미 시도한 질의 (중복 금지)\n{tried}"
        )
        try:
            result: GeneratedQueries = await structured.ainvoke(
                [("system", QUERY_GENERATOR_SYSTEM), ("user", user)]
            )
            queries = [q.strip() for q in result.queries if q.strip()]
        except Exception:
            queries = []
        # 이미 검색한 질의 제거 — 빈 리스트가 되면 이번 라운드 retrieve는 no-op이고
        # retrieval_count는 그대로 올라 예산이 소진된다 (무한 루프 방지)
        queries = [q for q in queries if q not in state["searched_queries"]]
        next_round = state["retrieval_count"] + 1
        return {
            "queries": queries,
            "retrieval_count": next_round,
            "trace": state["trace"]
            + [trace_event("generate_queries", queries=queries, round=next_round)],
        }

    return generate_queries
