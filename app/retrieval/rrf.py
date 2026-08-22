"""Reciprocal Rank Fusion — cure-agent-be retrieval.service.ts fuseByRrf의 포팅.

점수는 arm마다 1/(RRF_K + 순위)의 합이다. **순위만 쓰므로** 코사인 거리와
트라이그램 유사도처럼 척도가 다른 두 신호를 정규화 없이 합칠 수 있다.

동점이 흔하다(두 arm에서 교차 순위면 점수가 정확히 같다). 벡터 arm을 앞에 두는
이유는 거리 게이트가 그 위에서 정의된 1차 신호이기 때문이고, id는 최종 결정성이다.
"""

from app.retrieval.base import Evidence

RRF_K = 60


def fuse_by_rrf(vector_rows: list[Evidence], keyword_rows: list[Evidence]) -> list[Evidence]:
    fused: dict[str, Evidence] = {}

    def merge(rows: list[Evidence], arm: str) -> None:
        for index, row in enumerate(rows):
            existing = fused.get(row.chunk_id)
            target = existing if existing is not None else row.model_copy(
                update={"vector_rank": None, "keyword_rank": None}
            )
            fused[row.chunk_id] = target.model_copy(update={arm: index + 1})

    merge(vector_rows, "vector_rank")
    merge(keyword_rows, "keyword_rank")

    def score_of(row: Evidence) -> float:
        vector = 0.0 if row.vector_rank is None else 1.0 / (RRF_K + row.vector_rank)
        keyword = 0.0 if row.keyword_rank is None else 1.0 / (RRF_K + row.keyword_rank)
        return vector + keyword

    def sort_key(row: Evidence) -> tuple[float, float, str]:
        vector_rank = float("inf") if row.vector_rank is None else float(row.vector_rank)
        return (-score_of(row), vector_rank, row.chunk_id)

    return sorted(fused.values(), key=sort_key)
