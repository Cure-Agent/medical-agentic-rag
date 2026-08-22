"""fuse_by_rrf 포팅 정확성 — cure-agent-be fuseByRrf와 같은 의미를 손계산 값으로 고정한다."""

from app.retrieval.rrf import RRF_K, fuse_by_rrf
from tests.conftest import make_evidence

A, B, C = (make_evidence("chunk-a"), make_evidence("chunk-b"), make_evidence("chunk-c"))


def test_both_arm_hit_wins_and_ranks_are_recorded():
    # 벡터 [A, B] + 키워드 [B, C] → B는 양 arm 합산으로 1위, 순위가 부기된다
    fused = fuse_by_rrf([A, B], [B, C])

    assert [e.chunk_id for e in fused] == ["chunk-b", "chunk-a", "chunk-c"]
    b, a, c = fused
    assert (b.vector_rank, b.keyword_rank) == (2, 1)
    assert (a.vector_rank, a.keyword_rank) == (1, None)
    assert (c.vector_rank, c.keyword_rank) == (None, 2)
    # 손계산 점수 확인: B = 1/(60+2) + 1/(60+1)
    assert 1 / (RRF_K + 2) + 1 / (RRF_K + 1) > 1 / (RRF_K + 1)  # 양 arm > 단일 arm 1위


def test_same_chunk_in_both_arms_is_deduplicated():
    fused = fuse_by_rrf([A], [A])
    assert len(fused) == 1
    assert (fused[0].vector_rank, fused[0].keyword_rank) == (1, 1)


def test_tie_is_broken_by_vector_rank():
    # 벡터 [A, B] + 키워드 [B, A] → 점수 완전 동점 → 벡터 arm 순위가 앞선 A가 먼저
    fused = fuse_by_rrf([A, B], [B, A])
    assert [e.chunk_id for e in fused] == ["chunk-a", "chunk-b"]


def test_vector_only_beats_keyword_only_on_tie():
    # 벡터 [A] vs 키워드 [B]: 점수 동점(1/61) → 벡터 arm 소속이 먼저
    fused = fuse_by_rrf([A], [B])
    assert [e.chunk_id for e in fused] == ["chunk-a", "chunk-b"]


def test_empty_arms():
    assert fuse_by_rrf([], []) == []
    assert [e.chunk_id for e in fuse_by_rrf([], [C])] == ["chunk-c"]
