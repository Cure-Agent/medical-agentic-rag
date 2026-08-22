from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """실행 설정. 검색 정책 기본값은 cure-agent-be 운영 실측값을 그대로 가져왔다.

    - distance_cutoff 0.48: paraphrase 실측 118문항 손실 0 (docs/specs/28 개정)
    - arm_k 30: Recall@30 실측 기반 후보군 크기 (docs/specs/29)
    - RRF 융합은 절단하지 않는다 — 후보 커버리지 1.000 실측 (docs/specs/31)

    **게이트 컷의 단일 출처는 `AgentConfig`다.** 여기 있는 distance_cutoff는 리랭크 없는
    경로의 기본값이고, 실제 라우팅이 보는 값은 구성(프리셋)이 정한다 — 재현성 문자열도
    Settings가 아니라 실행된 구성값을 받아 쓴다(`policy_version_for`).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://cure:cure@localhost:5432/hybrid_probe"
    openai_api_key: str = ""

    # 코퍼스 임베딩과 같은 모델이어야 한다 — 다르면 좌표계가 달라 코사인 거리가 무의미하다
    embedding_model: str = "text-embedding-3-small"
    # 운영(cure-agent-be llm.config.ts) 패리티 — 실험 결과를 이식 판단에 쓰려면 모델이 같아야 한다
    agent_model: str = "openai:gpt-5.4-mini"
    # LLM-as-judge용 — 피평가 모델과 분리. 비추론 모델이라 temperature 0 재현성이 유지된다
    judge_model: str = "openai:gpt-4.1-mini"

    arm_k: int = 30
    top_k: int = 5
    distance_cutoff: float = 0.48
    max_retrieval: int = 2

    # 리랭커 모델만 여기 있다 — **점수 컷은 Settings에 두지 않는다.** 컷은 구성마다 다르고
    # (`AgentConfig.rerank_score_cutoff`, `*_cut9` 프리셋) 라우팅이 그 값을 쓰므로, Settings에
    # 사본을 두면 실행된 컷과 기록된 컷이 갈린다 — 실제로 갈렸다(rerank 구성이 3.5로 돌고
    # policy_version에는 9.0이 박혀 rerank와 rerank_cut9의 문자열이 같아졌다).
    rerank_model: str = "openai:gpt-5.4-mini"

    # LLM 프롬프트에 싣는 근거 상한 — 라운드가 쌓여도 컨텍스트가 무한히 자라지 않게
    max_evidence_for_llm: int = 20

    def policy_version_for(
        self,
        *,
        top_k: int,
        rerank: bool,
        distance_cutoff: float,
        rerank_cutoff: float | None = None,
        fuse_before_rerank: bool = False,
    ) -> str:
        """GenerationRun 재현성 문자열 (cure-agent-be 관례를 따른다).

        결과 파일만 봐도 어떤 검색 정책으로 돌았는지 복원할 수 있어야 한다. 그래서
        **구성마다 달라지는 값은 전부 인자로 받는다** — Settings 기본값으로 폴백하지
        않는다. 폴백이 있던 동안 컷이 거짓 기록됐고(위 rerank_model 주석), 폴백은
        기본값과 실행값이 우연히 같을 때 조용히 맞는 척한다.

        `rerank=True`면 `rerank_cutoff`가 필수다 — 빠뜨리면 예외로 즉시 드러난다.

        `fuse_before_rerank`(변형 B)는 검색 경로 자체를 바꾸므로 — 하위 질의별 재정렬이
        아니라 후보를 병합해 원 질문으로 1회 — 문자열에 남긴다. 없으면 A와 B가 같은
        재현성 문자열을 갖는다.
        """
        if rerank and rerank_cutoff is None:
            raise ValueError("rerank=True면 rerank_cutoff를 명시해야 한다 (실행된 컷을 기록한다)")
        fused = "fused" if fuse_before_rerank else ""
        rerank_part = f"-rerank{fused}{rerank_cutoff}@{self.rerank_model}" if rerank else ""
        return (
            f"agentic-hybrid-rrf60-arm{self.arm_k}-top{top_k}"
            f"-cut{distance_cutoff}{rerank_part}/{self.embedding_model}+{self.agent_model}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
