from typing import Protocol

from pydantic import BaseModel


class Evidence(BaseModel):
    """검색된 근거 청크 1건. cure-agent-be의 HybridEvidence와 같은 모양이다 —
    arm별 순위를 부기해 평가와 운영이 같은 축을 보게 한다."""

    chunk_id: str
    content: str
    guideline_title: str
    section_title: str
    section_path: list[str] = []
    version: str = ""
    distance: float  # 코사인 거리 (0에 가까울수록 유사) — 기권 게이트가 소비한다
    vector_rank: int | None = None
    keyword_rank: int | None = None
    recommendation_grade: str | None = None
    evidence_level: str | None = None


class QueryResult(BaseModel):
    """질의 1건의 검색 결과.

    top1_relevance는 리랭커가 매긴 1위 후보의 관련도(0~10)다. cure-agent-be §29의
    점수 게이트가 소비하는 값과 같다. 리랭커를 쓰지 않는 구성에서는 None이고,
    그때는 거리 게이트가 기권을 판정한다.
    """

    evidence: list[Evidence] = []
    top1_relevance: float | None = None


class Retriever(Protocol):
    """노드가 아는 검색 인터페이스의 전부. 테스트는 이 프로토콜의 가짜 구현을 꽂는다."""

    async def search(self, query: str) -> QueryResult: ...

    async def search_many(
        self, queries: list[str], *, question: str | None = None
    ) -> dict[str, QueryResult]:
        """질의별 결과를 따로 돌려준다 — 서브쿼리 커버리지 평가에 필요하다.

        `question`은 분해 이전의 원 질문이다. 병합 후 원 질문으로 한 번만 재정렬하는
        구현(FusedRerankingRetriever)만 쓰고, 나머지는 무시한다.
        """
        ...
