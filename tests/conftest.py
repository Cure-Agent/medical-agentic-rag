from app.retrieval.base import Evidence, QueryResult


def make_evidence(chunk_id: str, distance: float = 0.3, content: str = "근거 본문") -> Evidence:
    return Evidence(
        chunk_id=chunk_id,
        content=content,
        guideline_title="테스트 지침",
        section_title="테스트 섹션",
        distance=distance,
    )


class FakeRetriever:
    """Retriever 프로토콜의 결정론적 구현 — 그래프 배선 테스트가 LLM·DB 없이 돈다."""

    def __init__(
        self,
        by_query: dict[str, list[Evidence]],
        scores: dict[str, float] | None = None,
    ):
        self.by_query = by_query
        # 리랭크 구성 테스트용 — 지정한 질의만 top1 관련도를 갖는다
        self.scores = scores or {}
        self.calls: list[list[str]] = []

    def _result(self, query: str) -> QueryResult:
        return QueryResult(
            evidence=self.by_query.get(query, []), top1_relevance=self.scores.get(query)
        )

    async def search(self, query: str) -> QueryResult:
        return self._result(query)

    async def search_many(
        self, queries: list[str], *, question: str | None = None
    ) -> dict[str, QueryResult]:
        self.calls.append(list(queries))
        return {q: self._result(q) for q in queries}
