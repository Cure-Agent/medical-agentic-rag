from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    """에이전트 서비스 실행 설정 — **프로세스 환경변수만 읽는다.**

    실험 앱의 `Settings`와 달리 `.env`를 읽지 않는다. 운영은 compose가 환경을 넘기고,
    파일 한 줄이 BE 주소나 추적 스위치를 조용히 바꾸지 못하게 한다.
    """

    model_config = SettingsConfigDict(extra="ignore")

    # BE 호출 주소 — FE rewrites와 같은 이름. 운영은 compose가 내부 주소(http://app:3000)를 넘긴다
    be_origin: str = "http://localhost:3000"
    # 추적 스위치 원문 — 해석은 tracing.configure_tracing이 한다
    agent_tracing_enabled: str = ""
