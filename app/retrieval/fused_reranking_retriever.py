"""분해 변형 B — 하위 질의는 검색만 넓히고, 재정렬·게이트는 원 질문으로 한 번만.

변형 A(`RerankingRetriever`)는 하위 질의마다 리랭크한다. 축별 정밀도는 보존되지만
① 리랭크 호출이 질의 수만큼 늘고 ② 점수 게이트가 최댓값을 보게 되어 약해진다
(실측: 컷 9에서 구조화 기권 0.92 → 0.67).

B는 후보만 합치고 리랭크는 원 질문으로 1회다:
- 리랭크 비용·지연이 분해와 무관하게 일정하다
- 점수가 하나뿐이라 최댓값 문제가 사라진다 — 게이트가 분해 여부에 영향받지 않는다
- **대신 리랭커가 다시 다면 질문을 마주한다** — 원 질문 하나로 재정렬하므로 한 축의
  청크가 상위를 채우면 분해로 넓힌 이득이 되돌려질 수 있다. 그게 이 실험의 질문이다.

프로토콜상 질의별 결과를 돌려줘야 하므로 병합 결과를 전 질의에 같게 싣는다 — retrieve
노드가 chunk_id로 dedup하므로 근거 풀은 정확하고, 점수도 전부 같아 max가 그 값이 된다.
"""

import logging

from app.retrieval.base import Evidence, QueryResult
from app.retrieval.reranker import OpenAiReranker, RerankCandidate

logger = logging.getLogger(__name__)


class FusedRerankingRetriever:
    def __init__(self, hybrid, reranker: OpenAiReranker, top_k: int, distance_cutoff: float):
        self._hybrid = hybrid
        self._reranker = reranker
        self._top_k = top_k
        self._distance_cutoff = distance_cutoff

    async def search(self, query: str) -> QueryResult:
        results = await self.search_many([query], question=query)
        return results.get(query, QueryResult())

    async def search_many(
        self, queries: list[str], *, question: str | None = None
    ) -> dict[str, QueryResult]:
        if not queries:
            return {}
        per_query = await self._hybrid.search_many(queries)

        # 후보 병합 — 하위 질의들이 같은 지침을 보면 겹친다. 거리순으로 세워 사전 게이트가
        # 운영과 같은 "후보 최소 거리" 의미를 갖게 한다.
        merged: dict[str, Evidence] = {}
        for result in per_query.values():
            for item in result.evidence:
                merged.setdefault(item.chunk_id, item)
        candidates = sorted(merged.values(), key=lambda e: e.distance)

        fused = await self._rerank(question or queries[0], candidates)
        return dict.fromkeys(queries, fused)

    async def _rerank(self, question: str, candidates: list[Evidence]) -> QueryResult:
        if not candidates:
            return QueryResult(evidence=[], top1_relevance=0.0)
        if candidates[0].distance > self._distance_cutoff:
            return QueryResult(evidence=candidates[: self._top_k], top1_relevance=0.0)

        try:
            result = await self._reranker.rerank(
                question,
                [
                    RerankCandidate(
                        chunk_id=c.chunk_id,
                        content=c.content,
                        guideline_title=c.guideline_title,
                    )
                    for c in candidates
                ],
            )
        except Exception as error:
            logger.warning("리랭크 실패 — 코사인 순위 폴백: %r", error)
            return QueryResult(evidence=candidates[: self._top_k], top1_relevance=None)

        by_id = {c.chunk_id: c for c in candidates}
        ordered = [by_id[cid] for cid in result.order if cid in by_id]
        return QueryResult(
            evidence=(ordered or candidates)[: self._top_k],
            top1_relevance=result.top1_relevance,
        )
