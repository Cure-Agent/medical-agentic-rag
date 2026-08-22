"""리랭크를 포함한 검색 경로 — cure-agent-be `conversation-stream.service.ts`의 순서를 따른다.

    hybrid(자르지 않음) → 거리 사전 게이트 → 리랭크 → top_k 절단

거리 사전 게이트가 리랭크 **앞**에 있는 이유는 운영과 같다: 확실히 먼 질문에 리랭크
비용을 쓰지 않는다. 게이트에 걸리면 리랭커를 부르지 않고 top1_relevance=0.0을 돌려주며,
그래프의 점수 게이트가 이를 기권으로 처리한다.

리랭크 실패는 예외로 새어나가지 않는다 — 운영과 같이 코사인 순위로 폴백한다.
그때 top1_relevance는 None이라 점수 게이트가 거리 판정으로 되돌아간다.
"""

import asyncio
import logging

from app.retrieval.base import Evidence, QueryResult
from app.retrieval.reranker import OpenAiReranker, RerankCandidate

logger = logging.getLogger(__name__)


class RerankingRetriever:
    def __init__(
        self,
        hybrid,
        reranker: OpenAiReranker,
        top_k: int,
        distance_cutoff: float,
    ):
        self._hybrid = hybrid
        self._reranker = reranker
        self._top_k = top_k
        self._distance_cutoff = distance_cutoff

    async def search(self, query: str) -> QueryResult:
        results = await self.search_many([query])
        return results.get(query, QueryResult())

    async def search_many(
        self, queries: list[str], *, question: str | None = None
    ) -> dict[str, QueryResult]:
        if not queries:
            return {}
        candidates = await self._hybrid.search_many(queries)
        reranked = await asyncio.gather(
            *(self._rerank_one(q, candidates[q].evidence) for q in queries)
        )
        return dict(zip(queries, reranked, strict=True))

    async def _rerank_one(self, query: str, candidates: list[Evidence]) -> QueryResult:
        if not candidates:
            return QueryResult(evidence=[], top1_relevance=0.0)

        # 게이트 ① 거리 — 후보 최소 거리만 본다(운영 §28: per-chunk 필터는 정답 청크를 잘랐다)
        if min(c.distance for c in candidates) > self._distance_cutoff:
            return QueryResult(evidence=candidates[: self._top_k], top1_relevance=0.0)

        try:
            result = await self._reranker.rerank(
                query,
                [
                    RerankCandidate(
                        chunk_id=c.chunk_id,
                        content=c.content,
                        guideline_title=c.guideline_title,
                    )
                    for c in candidates
                ],
            )
        except Exception as error:  # 폴백이 재시도보다 싸다 — 운영과 같은 판단
            logger.warning("리랭크 실패 — 코사인 순위 폴백: %r", error)
            return QueryResult(evidence=candidates[: self._top_k], top1_relevance=None)

        by_id = {c.chunk_id: c for c in candidates}
        ordered = [by_id[cid] for cid in result.order if cid in by_id]
        # 빈 순위 = 관련 후보 없음 판정. 근거를 비워 두면 답변 노드가 인용할 게 없으므로
        # 코사인 순위를 유지하고, 기권 판단은 점수 게이트에 맡긴다 (운영과 같은 분업).
        evidence = (ordered or candidates)[: self._top_k]
        return QueryResult(evidence=evidence, top1_relevance=result.top1_relevance)
