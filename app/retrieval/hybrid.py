"""하이브리드 검색 — cure-agent-be retrieval.service.ts searchHybrid의 SQL 포팅.

벡터 arm(pgvector cosine)과 키워드 arm(pg_trgm word_similarity)의 합집합을 RRF로
융합한다. 키워드 arm이 BM25가 아닌 이유: 이 코퍼스는 어절 경계 공백이 소실돼
tsvector 어절 매칭이 성립하지 않는다 — 문자 n-gram은 조사·붙임에 강건하다.

word_similarity는 **비대칭**이다 — 짧은 질문이 1번 인자, 긴 본문이 2번이다.
동점 평원이 넓어(top-30 경계에 같은 값 72건 실측) id 2차 정렬 없이는
같은 질의가 실행마다 다른 후보를 낸다.
"""

import asyncio

from langchain_openai import OpenAIEmbeddings
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.retrieval.base import Evidence, QueryResult
from app.retrieval.rrf import fuse_by_rrf

# 근거 1행의 모양은 두 arm이 같다 — 조인·선택 목록을 한 곳에 둔다.
# embedding_model 조건: 좌표계가 다른 벡터는 코사인 거리가 무의미하다.
# ACTIVE 조건: 폐기된 판본은 새 답변에 인용되지 않는다.
_EVIDENCE_SELECT = """
SELECT
    ec.id AS chunk_id,
    ec.content,
    g.title AS guideline_title,
    gs.title AS section_title,
    gs.path AS section_path,
    gv.version,
    ec.recommendation_grade->>'code' AS recommendation_grade,
    ec.evidence_level->>'code' AS evidence_level,
    (ec.embedding <=> %(embedding)s::vector)::float8 AS distance
FROM evidence_chunks ec
JOIN guideline_sections gs ON ec.section_id = gs.id
JOIN guideline_versions gv ON ec.guideline_version_id = gv.id
JOIN guidelines g ON gv.guideline_id = g.id
WHERE ec.embedding_model = %(embedding_model)s
  AND gv.status = 'ACTIVE'
"""

_VECTOR_ARM = _EVIDENCE_SELECT + "ORDER BY distance ASC LIMIT %(arm_k)s"

_KEYWORD_ARM = _EVIDENCE_SELECT + (
    "ORDER BY word_similarity(%(query)s, ec.content) DESC, ec.id ASC LIMIT %(arm_k)s"
)


def _to_vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(value) for value in embedding) + "]"


class HybridRetriever:
    """하이브리드 검색. `limit`이 절단 지점이다.

    운영(cure-agent-be)은 RRF 융합 결과를 **자르지 않고** 리랭커에 통째로 넘긴 뒤
    리랭크 순위에서 top-5를 뽑는다(`retrieval.service.ts:247` → `conversation-stream.ts:287`).
    융합 직후 top-5로 자르면 리랭커가 재정렬할 후보가 5개뿐이라 §29의 Recall@5
    0.780→0.983이 재현되지 않는다. 그래서 리랭커로 감쌀 때는 limit=None으로 만든다.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        embeddings: OpenAIEmbeddings,
        settings: Settings,
        limit: int | None = -1,
    ):
        self._pool = pool
        self._embeddings = embeddings
        self._settings = settings
        # -1 센티넬: 명시하지 않으면 설정의 top_k를 쓴다 (None은 "자르지 않음"이라 구분이 필요)
        self._limit = settings.top_k if limit == -1 else limit

    async def search(self, query: str) -> QueryResult:
        [embedding] = await self._embeddings.aembed_documents([query])
        return QueryResult(evidence=await self._search_with_embedding(query, embedding))

    async def search_many(
        self, queries: list[str], *, question: str | None = None
    ) -> dict[str, QueryResult]:
        if not queries:
            return {}
        # 질의 임베딩은 한 번의 배치 호출로
        embeddings = await self._embeddings.aembed_documents(queries)
        results = await asyncio.gather(
            *(self._search_with_embedding(q, e) for q, e in zip(queries, embeddings, strict=True))
        )
        return {q: QueryResult(evidence=r) for q, r in zip(queries, results, strict=True)}

    async def _search_with_embedding(self, query: str, embedding: list[float]) -> list[Evidence]:
        params = {
            "embedding": _to_vector_literal(embedding),
            "embedding_model": self._settings.embedding_model,
            "query": query,
            "arm_k": self._settings.arm_k,
        }
        # 두 arm을 병렬로 던진다 — 키워드 arm은 전수 스캔이라 순차면 그대로 지연에 더해진다
        vector_rows, keyword_rows = await asyncio.gather(
            self._fetch(_VECTOR_ARM, params),
            self._fetch(_KEYWORD_ARM, params),
        )
        fused = fuse_by_rrf(vector_rows, keyword_rows)
        return fused if self._limit is None else fused[: self._limit]

    async def _fetch(self, sql: str, params: dict) -> list[Evidence]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
        return [
            Evidence(
                chunk_id=row["chunk_id"],
                content=row["content"],
                guideline_title=row["guideline_title"],
                section_title=row["section_title"],
                section_path=row["section_path"] or [],
                version=row["version"],
                distance=row["distance"],
                recommendation_grade=row["recommendation_grade"],
                evidence_level=row["evidence_level"],
            )
            for row in rows
        ]
