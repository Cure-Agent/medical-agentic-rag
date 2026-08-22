"""② Hybrid Retriever 노드 — state["queries"]를 소비해 근거 풀에 누적한다."""

from app.agent.state import AgentState, trace_event
from app.retrieval.base import Retriever


def make_retrieve_node(retriever: Retriever):
    async def retrieve(state: AgentState) -> dict:
        # 이미 검색한 질의는 다시 던지지 않는다 — Query Generator가 놓친 중복의 최종 방어선
        pending = [q for q in state["queries"] if q not in state["searched_queries"]]

        results = await retriever.search_many(pending, question=state["question"])

        evidence = dict(state["evidence"])
        query_hits = dict(state["query_hits"])
        rerank_scores = dict(state["rerank_scores"])
        new_chunk_ids: list[str] = []
        for query, result in results.items():
            query_hits[query] = [e.chunk_id for e in result.evidence]
            if result.top1_relevance is not None:
                rerank_scores[query] = result.top1_relevance
            for item in result.evidence:
                if item.chunk_id not in evidence:
                    evidence[item.chunk_id] = item
                    new_chunk_ids.append(item.chunk_id)

        return {
            "queries": [],
            "searched_queries": state["searched_queries"] + pending,
            "evidence": evidence,
            "query_hits": query_hits,
            "rerank_scores": rerank_scores,
            "trace": state["trace"]
            + [
                trace_event(
                    "retrieve",
                    queries=pending,
                    new_chunks=new_chunk_ids,
                    pool_size=len(evidence),
                    rerank_scores={q: rerank_scores[q] for q in pending if q in rerank_scores},
                )
            ],
        }

    return retrieve
