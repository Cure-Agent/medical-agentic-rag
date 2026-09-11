# 에이전트 서비스 이미지 (BE docs/specs/49) — 운영 compose가 ghcr.io/cure-agent/medical-agentic-rag로 띄운다.
#
# - 의존성은 uv.lock으로만 설치한다. --locked는 잠금이 pyproject.toml과 어긋나면 빌드를 실패시킨다
#   (BE --frozen-lockfile과 같은 이유 — 테스트한 버전과 배포되는 버전이 갈리지 않게).
# - 이미지에는 서비스 앱(app/service)만 싣는다. 실험 코드(/ask·evals·psycopg 검색)는 싣지 않는다.
# - 추적 변수(AGENT_TRACING_ENABLED·LANGSMITH_*·LANGCHAIN_*)를 ENV로 박지 않는다 — 켜는 통로는 배포
#   환경이 연다(tests/e2e/test_agent_image.py).

FROM python:3.13-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.13-slim
# python:3.13-slim의 기본 사용자는 root다 — 전용 무권한 사용자로 실행한다
RUN groupadd --system --gid 10001 agent \
    && useradd --system --uid 10001 --gid agent --no-create-home --shell /usr/sbin/nologin agent
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY app/__init__.py app/__init__.py
COPY app/service app/service
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.service.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
