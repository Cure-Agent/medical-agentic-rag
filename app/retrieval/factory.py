"""구성별 검색 경로 조립 — run_eval과 API가 같은 배선을 쓰게 하는 단일 지점.

절단 지점이 구성마다 다르다는 게 요점이다:
- 리랭크 없음: RRF 융합 직후 top_k로 자른다.
- 리랭크 있음: 자르지 않고(limit=None) 후보 전체를 리랭커에 넘긴 뒤 top_k를 뽑는다.
  운영(cure-agent-be)의 절단 지점이 여기다 — §29 Recall@5 0.780→0.983이 이 구조에서 났다.
"""

from langchain_openai import OpenAIEmbeddings
from psycopg_pool import AsyncConnectionPool

from app.agent.graph import AgentConfig
from app.config import Settings
from app.retrieval.base import Retriever
from app.retrieval.fused_reranking_retriever import FusedRerankingRetriever
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.reranker import OpenAiReranker
from app.retrieval.reranking_retriever import RerankingRetriever


def make_retriever(
    cfg: AgentConfig,
    pool: AsyncConnectionPool,
    embeddings: OpenAIEmbeddings,
    settings: Settings,
) -> Retriever:
    if not cfg.enable_rerank:
        return HybridRetriever(pool, embeddings, settings, limit=cfg.top_k)

    hybrid = HybridRetriever(pool, embeddings, settings, limit=None)
    reranker = OpenAiReranker(
        api_key=settings.openai_api_key,
        # init_chat_model 형식("provider:model")과 달리 OpenAI SDK는 모델명만 받는다
        model=settings.rerank_model.split(":", 1)[-1],
        # 리랭커가 돌려줄 개수 = answerer가 받을 개수. 통제군(top_k=11)이 성립하는 지점이다
        top_n=cfg.top_k,
    )
    build = FusedRerankingRetriever if cfg.fuse_before_rerank else RerankingRetriever
    return build(hybrid, reranker, top_k=cfg.top_k, distance_cutoff=cfg.distance_cutoff)
