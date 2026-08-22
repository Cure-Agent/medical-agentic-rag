"""재현성 문자열은 **실행된 구성값**을 기록해야 한다 — 순수 함수라 LLM·DB 없이 검증한다.

이 파일이 생긴 이유: policy_version_for가 컷을 Settings에서 읽던 동안 `rerank`(실제 3.5)와
`rerank_cut9`(9.0)가 둘 다 `rerank9.0`으로 기록돼, 결과 파일만으로 두 구성을 구분할 수 없었다.
컷은 라우팅이 AgentConfig에서 읽으므로 기록도 같은 출처를 봐야 한다.
"""

import pytest

from app.agent.graph import PRESETS
from app.config import Settings


def policy_for(preset: str) -> str:
    cfg = PRESETS[preset]
    return Settings().policy_version_for(
        top_k=cfg.top_k,
        rerank=cfg.enable_rerank,
        distance_cutoff=cfg.distance_cutoff,
        rerank_cutoff=cfg.rerank_score_cutoff if cfg.enable_rerank else None,
        fuse_before_rerank=cfg.fuse_before_rerank,
    )


def test_rerank_cutoff_is_recorded_from_config():
    assert "-rerank3.5@" in policy_for("rerank")
    assert "-rerank9.0@" in policy_for("rerank_cut9")


def test_cut_variants_are_distinguishable():
    """컷만 다른 두 구성은 문자열이 달라야 한다 — 이 단언이 원래 버그를 잡는다."""
    assert policy_for("rerank") != policy_for("rerank_cut9")


def test_no_rerank_part_without_rerank():
    assert "-rerank" not in policy_for("baseline")


def test_fused_variant_is_distinguishable():
    """변형 A와 B는 검색 경로가 다르다(질의별 재정렬 vs 병합 후 1회) — 문자열도 달라야 한다."""
    assert "-rerankfused3.5@" in policy_for("rerank_decomp_fused")
    assert policy_for("rerank_decomp_fused") != policy_for("rerank")


def test_distance_cutoff_is_recorded():
    assert "-cut0.48" in policy_for("baseline")


def test_top_k_control_arm_is_distinguishable():
    """근거 개수 통제군은 top_k만 다르다 — 문자열이 같으면 통제 변인이 기록에서 사라진다."""
    assert policy_for("rerank") != policy_for("rerank_topk11")
    assert "-top11-" in policy_for("rerank_topk11")


def test_missing_rerank_cutoff_raises():
    """폴백 대신 예외 — 조용히 기본값을 기록하는 것이 애초의 결함이었다."""
    with pytest.raises(ValueError, match="rerank_cutoff"):
        Settings().policy_version_for(top_k=5, rerank=True, distance_cutoff=0.48)


def test_search_policy_maps_one_to_one_to_policy_version():
    """검색 정책 ↔ policy_version은 1:1이어야 한다.

    분해·평가기는 검색 정책이 아니라 에이전트 구조라 문자열에 들어가지 않는다 —
    baseline/decomposition/full이 같은 문자열인 것은 정상이고, 그 축은 결과 파일의
    `preset` 필드가 기록한다. 여기서 막는 것은 **검색 정책이 다른데 문자열이 같은** 경우다.
    """
    by_policy: dict[str, tuple] = {}
    for name, cfg in PRESETS.items():
        key = (
            cfg.top_k,
            cfg.enable_rerank,
            cfg.distance_cutoff,
            cfg.rerank_score_cutoff if cfg.enable_rerank else None,
            cfg.fuse_before_rerank,
        )
        policy = policy_for(name)
        if policy in by_policy:
            assert by_policy[policy] == key, f"{name}: 검색 정책이 다른데 문자열이 같다 — {policy}"
        by_policy[policy] = key
