# Cure Agent 에이전트 서비스 설계 문서 (분리본)

> **설계의 원본은 BE 레포에 있습니다**: [cure-agent-be/docs/architecture.md](https://github.com/Cure-Agent/cure-agent-be/blob/main/docs/architecture.md)
> 이 문서는 에이전트 서비스(이 레포)의 구조와 규칙만 다룹니다. 공통 계약(응답 봉투, 에러코드, SSE 이벤트 스키마, 인증)과 운영 검색 정책의 근거는 원본을 참조하며, 여기에 복사하지 않습니다(드리프트 방지).

---

## 1. 레포의 위치

- Cure Agent는 **단일 스펙 저장소 + 세 구현 레포**다: cure-agent-be(서버·스펙 원본), cure-agent-fe(화면), 이 레포(에이전트 서비스).
- 스펙은 BE `docs/specs/`에 있다. 이 레포가 구현할 기준은 수용 기준의 **`(AGENT)` 라벨**로 가른다 (`.claude/commands/implement.md` Phase 0).
- **서버 배포는 cure-agent-be가 맡는다.** 이 레포의 파이프라인은 `main` 머지에서 끝난다 (`automation/pipeline.md`).
- 이 레포는 Agentic RAG ablation 연구 저장소로 시작했다. 연구 종료 시점의 코드는 태그 **`ablation-study`**(6377f3b)로 고정했고, 판정과 증거는 `docs/experiments/`에 남는다 (§5).

---

## 2. 구조와 경계 원칙

```
medical-agentic-rag/
├── app/
│   ├── main.py                        # FastAPI 앱 + lifespan (psycopg 풀, 프리셋별 그래프 캐시)
│   ├── config.py                      # Settings — 실행 설정, 재현성 문자열(policy_version_for)
│   ├── api/routes.py                  # GET /healthz · POST /ask · POST /ask/stream (SSE)
│   ├── agent/
│   │   ├── graph.py                   # AgentConfig · PRESETS · 순수 라우팅 함수 · build_graph
│   │   ├── state.py                   # AgentState + 노드 입출력 스키마
│   │   └── nodes/                     # decompose · retrieve · evaluate · generate_queries · answer · abstain
│   ├── retrieval/
│   │   ├── base.py                    # Evidence · QueryResult · Retriever Protocol
│   │   ├── hybrid.py                  # dense(pgvector) + lexical(pg_trgm) SQL
│   │   ├── rrf.py                     # RRF 융합 (K=60, 무절단)
│   │   ├── reranker.py                # Reranker Protocol + OpenAI 리스트와이즈 리랭커
│   │   ├── reranking_retriever.py     # 하위 질의마다 리랭크 (변형 A)
│   │   ├── fused_reranking_retriever.py  # 병합 후 원 질문으로 1회 리랭크 (변형 B)
│   │   └── factory.py                 # 구성별 검색 경로 조립 — API와 evals가 같은 배선을 쓰는 단일 지점
│   └── llm/                           # client.py(모델 생성) · prompts.py
├── evals/                             # ablation 실행·채점·집계 (연구 도구 — 서비스 경로가 아니다)
├── tests/                             # 오프라인 단위 테스트 (e2e는 tests/e2e/ — 첫 e2e 스펙에서)
├── scripts/                           # fetch_spec.py(BE 스펙 조달) · verify_public_results.py
├── docs/experiments/                  # 불변 실험 기록 (§5)
├── automation/ · .claude/ · .codex/   # SDD 하네스 (§6)
└── Makefile                           # 검증 명령의 단일 원천 (§3)
```

**경계 원칙:**

- **노드는 상태 타입으로만 말한다.** 노드끼리 직접 부르지 않고, 배선은 오케스트레이터(`graph.py`)만 한다. 노드 구현의 모양은 `NodeFn` Protocol이다.
- **루프 예산·게이트 판정은 코드가 쥔다.** `decide_after_evaluate`·`decide_gate`·`decide_rerank_gate`는 순수 함수라 LLM 없이 테스트된다. LLM은 판정의 입력(평가·답변가능성 신호)만 낸다.
- **기권은 정상 종료 상태다.** 근거가 부족하면 `ABSTAIN_TEMPLATE`로 결정론적으로 기권한다. 검색 게이트는 관련도를, answerer는 답변가능성을 판정한다 — 둘은 다른 질문이다.
- **검색 정책 값의 단일 출처는 `AgentConfig`/`PRESETS`다.** `Settings`에 컷 사본을 두지 않는다 — 사본이 있던 동안 실행된 컷과 기록된 컷이 갈렸다(`app/config.py` 주석). 운영 정책 패리티는 `prod_rerank`이며, 과거 프리셋의 의미는 바꾸지 않고 새 이름을 둔다.
- **재현성 문자열에는 실행된 구성값을 전부 넘긴다.** `policy_version_for`는 기본값으로 폴백하지 않는다.
- **외부 유료 경계는 Protocol 뒤에 둔다** — 검색은 `Retriever`, 리랭크는 `Reranker`, LLM은 LangChain `BaseChatModel`. 포트는 프로세스 밖 경계에만 둔다는 원본 §3의 기준과 같다.
- **SQL은 리터럴 + 파라미터로만 조립한다** — 쿼리 본문은 `LiteralString`, 값은 `%(name)s`로 넘기고, 행 모양은 커서의 `row_factory`에서 정한다(`hybrid.py`).

---

## 3. 검증 명령

검증 명령의 단일 원천은 `Makefile`이다. CI(`.github/workflows/ci.yml`)와 하네스(`automation/*.md`)가 같은 타깃을 부른다.

| 명령 | 내용 | CI 잡 |
|---|---|---|
| `make lint` | `ruff check` | `lint` |
| `make typecheck` | pyright (standard 모드, Python 3.12, `app`·`evals`·`tests`·`scripts`) — 동결 게이트의 「빌드 GREEN」 | `typecheck` |
| `make test` | pytest (`tests/e2e/` 제외) | `test` |
| `make test-e2e` | pytest `tests/e2e/` — e2e 테스트 파일이 없으면 알리고 통과 | 첫 e2e 스펙에서 추가 |
| — | gitleaks 히스토리 시크릿 스캔 | `gitleaks` |

- **build·api-generate 타깃은 첫 스펙에서 추가한다.** 이 레포가 소비·노출할 계약의 형태가 그때 정해진다.
- pyright는 릴리스마다 추론이 바뀌므로 `pyproject.toml`에서 정확한 버전으로 고정한다.
- CI는 `requires-python`의 하한(3.12)으로 돈다.

---

## 4. 테스트

원본 §13의 원칙을 이 레포에 적용한다.

- **단위 테스트는 LLM·임베딩·DB 없이 돈다.** 외부 경계는 가짜로 꽂는다 — `tests/conftest.py`의 `FakeRetriever`·`make_evidence`, 리랭커·LLM 대역. CI 러너에는 `.env`가 없으므로, 실키에 기대는 테스트는 CI에서 드러난다.
- **통제 조건을 테스트로 잠근다.** 구성 간 차이가 의도한 축 하나뿐인지를 단언한다(`test_facet_prompt.py`·`test_cut_sweep.py`) — 통제군이 조용히 무너진 전례가 이 레포에 있다.
- **e2e**: 원본 §13은 외부 경계(LLM·embedding)를 fake로, DB는 실물로 둔다. 이 레포의 e2e는 `tests/e2e/`에 두며, 기동 방식은 첫 e2e 스펙이 확정한다.

---

## 5. 실험 기록

- `docs/experiments/`는 **시점 증거**다. 끝난 실험의 관측 숫자를 고치지 않는다 — 결론이 뒤집히면 새 문서를 쓰고 이전 문서에 `superseded` 배너를, 사실·통계 오류는 `erratum` 배너를 단다(`docs/experiments/INDEX.md`).
- 현재 판정: **검색 경로는 운영 현행 유지** — 원 질문 1개 → 하이브리드 → RRF 무절단 → 리랭커 → top-5 → 점수 게이트. 판정의 근거와 한계는 `INDEX.md`가 원천이다.
- 공개 점수는 `scripts/verify_public_results.py`로 다시 계산할 수 있다.

---

## 6. 작업 규칙 (SDD)

원본 §15 「5단계 이후 작업 규칙 (SDD)」와 `docs/sdd.ko.md`를 이 레포에 적용한 요약이다.

| spec 작성 후 `/implement` | `/problem` |
|---|---|
| 새 엔드포인트·모듈 | 버그 픽스·장애 대응 |
| 계약(요청·응답) 변경 | CI·하네스 조정, 리팩토링, 의존성 업그레이드 |
| 검색·생성 정책 값 변경 | 로그·관측 튜닝 |

- 판단 기준은 **"6개월 뒤 누군가 이 결정의 근거를 찾을 것인가"** 다.
- **이 레포는 스펙을 쓰지 않는다.** 스펙이 필요한 변경은 `/problem`이 「승격 핸드오프」를 만들어 BE `/spec`으로 넘긴다 (`automation/problem.md`).
- **두 경로 모두 테스트를 동결한다.** 절차 원본은 `automation/freeze.md` 하나다. 수용 기준 테스트는 구현 에이전트(Claude)와 분리된 작성자(Codex)가 파생하고, 동결은 게이트(기계 검증) · 예방(PreToolUse 훅) · 감사(동결 커밋 기준 diff)의 3중 장치로 강제한다.
- **라우팅은 사람이 고르지 않는다.** 운영 변경은 `/problem`으로 시작하고, 진단 후 영향 파일을 경로 게이트에 대조해 기계적으로 판정한다.
- **하네스 중립 설계**: 절차 원문은 `automation/`에 두고, `.claude/commands/`(Claude Code)와 `.codex/skills/`(Codex)는 폴링 모드·Co-Author 트레일러 같은 하네스 특성만 주입하는 어댑터다.
- 브랜치는 `main` 단일이다 — `<prefix>/<슬러그>` → `main` PR → squash 머지 (`automation/ship.md`).
