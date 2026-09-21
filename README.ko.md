# Medical Agentic RAG

[English](README.md) | [한국어](README.ko.md)

> **CureAgent의 에이전트 서비스.** 의료인이 보낸 질문 하나를 분류해 맞는 경로를 BE 내부 API로
> 실행하고, 결과를 SSE로 브라우저에 흘린다.

이 저장소에는 두 가지가 있다.

1. **에이전트 서비스** (`app/service/`) — 운영 코드다. 검증을 통과한 `main` 커밋마다 컨테이너
   이미지를 올리고, [cure-agent-be](https://github.com/Cure-Agent/cure-agent-be)가 그 이미지를
   당겨 배포한다.
2. **Agentic RAG ablation 연구** — 이 저장소가 시작한 자리다. 그 판정이 운영 검색 정책을 고정했고,
   연구 종료 시점의 코드는 `ablation-study` 태그로, 증거는
   [docs/experiments/](docs/experiments/INDEX.md)에 남아 있다.
   [기원](#기원-agentic-rag-ablation-연구) 절을 참고한다.

[docs/architecture.md](docs/architecture.md)가 이 저장소의 설계 문서다. 레포를 넘나드는 공통
계약(응답 봉투, 에러코드 레지스트리, SSE 이벤트 스키마, 인증)은
[cure-agent-be/docs/architecture.md](https://github.com/Cure-Agent/cure-agent-be/blob/main/docs/architecture.md)에
있고, 드리프트를 막기 위해 여기에 복사하지 않는다.

## 에이전트 서비스

### API 표면

| 엔드포인트 | 역할 |
|---|---|
| `GET /api/v1/agent/healthz` | 프로세스 생존만 본다. BE를 부르지 않아 BE 장애가 에이전트 재시작·배포 롤백으로 번지지 않는다. |
| `GET /api/v1/agent/me` | 받은 `Cookie`·CSRF 헤더를 그대로 BE `GET /api/v1/auth/me`에 넘겨 그 판정을 바꾸지 않고 돌려준다. 성공 응답에는 `clinicianId`·`clinicId`만 싣는다. |
| `POST /api/v1/agent/conversations/{conversationId}/messages/stream` | 에이전트 턴 하나를 SSE로 흘린다. |
| `GET /metrics` | Prometheus 수집 표면. 라우터 접두사(`/api/v1/agent`) **밖**에 일부러 두었다 — nginx가 그 접두사만 에이전트로 보내므로 차단 규칙을 더하지 않아도 외부에서 닿지 않는다. |

브라우저에게는 `/api/v1` 하나가 API 표면이다. 운영 nginx가 `/api/v1/agent/` 아래만 이 서비스로
보내고 나머지는 BE로 보낸다. 에이전트는 BE를 nginx 밖(`http://app:3000`)에서 부르며, 에이전트만
쓰는 내부 API(`/api/v1/internal/agent/…`)는 운영 nginx가 404로 막고 BE OpenAPI에서도 빠진다.

### 스트림을 열기 전

LLM을 부르기 전에 셋을 거른다. 각 실패는 §10.1 봉투다.

1. **본문 검증** — `content` 1~4000자, `clientRequestId` 1~100자, 선택인 `responseLang`은
   `ko`|`en`이다. 어기면 422 `VALIDATION_FAILED`. 대화 id가 BE id 모양이 아니면 BE에 묻지 않고
   404 `NOT_FOUND`다.
2. **access 토큰 선검사** — `access_token` 쿠키를 서명 검증 없이 JWT로 읽어 `exp`·`iat`만 본다.
   남은 수명이 실행 상한(150초)보다 짧으면 401 `AUTH_TOKEN_EXPIRED`이고 BE를 부르지 않는다.
   **전체** 수명이 상한 이하인 토큰은 검사하지 않는다 — 새로 받아도 못 넘으니 FE가 refresh 루프에
   빠진다.
3. **수락** — `POST …/internal/agent/conversations/{id}/turns`. 이 호출 하나가 인증·클리닉
   스코프·CSRF·중복 판정을 겸하므로 분류기보다 **앞**에 둔다 — 비로그인 요청이 LLM 비용을 태우지
   못한다. 201이 아닌 응답은 그대로 중계하고, 응답을 못 받으면 502 `AGENT_BACKEND_UNAVAILABLE`이다.

수락이 준 `assistantMessageId`가 끊김 복구의 기준점이다.

### 분류와 실행 경로 표

**라벨 차원은 코드가, 의미 차원은 LLM이 쥔다.** 분류기는 strict JSON 스키마로 `{route,
patient_labels}`만 내고, 실제 경로는 순수 함수(`routing.plan_route`)가 정한다. 라벨 규칙을
프롬프트로 옮긴 구성은 분류가 오히려 나빠졌으므로 규칙은 표에 남는다.

| 분류기 route | 라벨 | 실행 경로 | 결말 |
|---|---|---|---|
| `GUIDELINE` | 0 | 지침 | BE 지침 도구 |
| `GUIDELINE` | 1 | 복합 | |
| `PATIENT` | 1 | 환자 | 환자 도구가 `NOT_FOUND`·`AMBIGUOUS`면 ABSTAINED `patient_unresolved` |
| `PATIENT` | 0 | 환자 | 도구 없이 `patient_unresolved`로 기권 |
| `COMPOSITE` | 1 | 복합 | 환자 해석 실패는 위와 같다 |
| `COMPOSITE` | 0 | 지침 | |
| 무엇이든 | 2 이상 | 기타 | 도구 없이 `out_of_scope`로 기권 |
| `OTHER` | — | 기타 | 도구 없이 `out_of_scope`로 기권 |

라벨은 앞뒤 공백을 지우고 빈 값을 버린 뒤 대소문자를 무시하고 서로 다른 것만 센다. 특정 환자는
질문에 적힌 케이스 라벨로만 가리킨다.

### 네 갈래

```text
공통  message.accepted → 분류 → agent.progress{stage: "routed", route}
지침  지침 도구 SSE의 이벤트를 받은 순서·내용 그대로 흘린다 — 완결을 부르지 않는다
환자  환자 도구 → agent.progress{patient_loaded} → answer.delta*
        → [완결 COMPLETED] → answer.completed
복합  환자 도구 → 근거 도구: retrieval.started → retrieval.progress*
        → (게이트를 통과하면 answer.started) → retrieval.evidence×N
        → retrieval.completed → answer.delta*(판정 뒤)
        → [완결: BE가 임상 참고안을 구조화·저장한다]
        → answer.completed{message, guidance} | answer.abstained
기타  [완결 ABSTAINED] → answer.abstained
```

- **복합의 판정은 답을 쓰는 쪽이 한다.** 근거 도구는 관련도 게이트에서 멈추고, 에이전트가 원문
  근거와 환자 기록으로 생성과 답변가능성 판정을 한 번에 한다. 판정 필드가 닫히기 전에는 델타를
  흘리지 않는다(`synthesis.VerdictFirstParser`).
- **참고안은 만들지 않고 흘린다.** BE 완결이 임상 참고안을 구조화·검증해 답변과 같은 트랜잭션에
  세우고, 에이전트는 완결 응답의 `guidance`를 `message`에서 떼어
  `answer.completed{message, guidance}`로 보낸다. 참고안을 만든 완결에만 그 키가 있으므로 없으면
  이벤트에도 키를 만들지 않는다.
- 인용은 최종 답변에 남은 마커 `[n]`을 n번째 `retrieval.evidence` 프레임의 id에 맺은 것이다.
- 종결 이벤트는 완결 응답을 받은 **뒤에** 보낸다. 기권은 정상 종료 상태다.

### BE 내부 API

모든 호출에 받은 `Cookie` 원문과, **받았을 때만** `X-CSRF-Protection`을 싣는다. 그 밖의 요청
헤더는 넘기지 않으며, 에이전트는 자격을 스스로 만들지 않는다.

| 호출 | 경로 (`POST …/internal/agent/…`) | 본문 |
|---|---|---|
| 지침 도구 | `turns/{assistantMessageId}/guideline-answer` | `{classifierVersion}` |
| 환자 도구 | `turns/{assistantMessageId}/patient` | `{caseLabel}` |
| 근거 도구 | `turns/{assistantMessageId}/guideline-evidence` | `{query}` |
| 완결 | `turns/{assistantMessageId}/finish` | `{status, route?, classifierVersion, …}` |

**턴을 닫는 주체는 경로마다 하나다.** 지침 도구는 채팅 파이프라인으로 답변 저장까지 하므로 그
경로에서는 에이전트가 완결을 부르지 않는다 — 실패·끊김에도 마찬가지다. 나머지 경로는 에이전트의
완결이 닫는다: 실패는 `FAILED`, 클라이언트 끊김은 `CANCELLED`다. 근거 도구는 턴 상태를 바꾸지
않는다.

시간 상한: JSON 호출 5초, 내부 SSE와 완결은 connect 5초·read 30초(BE가 완결 안에서 참고안을
구조화한다), 실행 상한은 요청 도착부터 150초, SSE 하트비트 15초, LLM 첫 응답 45초다.

### 추적

LangSmith 추적은 `AGENT_TRACING_ENABLED=true`일 때만 켜진다. SDK 환경변수는 한쪽 네임스페이스의
`true`로 켜지고 `false`로 끌 수 없어서, 그대로 두면 `.env` 한 줄이 조용히 추적을 켠다. 스위치는
기동 때 한 번 고정한다.

턴이 환자·복합으로 정해진 뒤는 실행을 골라서가 아니라 **분기 전체**를 숨김 클라이언트로 감싼다 —
환자 도구 출력만 숨기면 같은 기록이 합성 프롬프트로 다시 실린다. 프로젝트명은 턴마다 다시 건다.
`langsmith.configure(project_name=…)`는 호출한 태스크의 contextvar에만 남아, lifespan에서 건 값이
요청 태스크로 이어지지 않았다 — 운영에서 분류기 실행이 `default` 프로젝트로 갔다.

## 서비스 실행

Python 3.12+와 [uv](https://docs.astral.sh/uv/)가 필요하다. 의존성은 `uv.lock`이 정하고, CI와
이미지가 같은 잠금에서 설치한다.

```bash
uv sync
BE_ORIGIN=http://localhost:3000 OPENAI_API_KEY=sk-... \
  .venv/bin/uvicorn app.service.main:create_app --factory --port 8000
```

`ServiceSettings`는 **프로세스 환경변수만 읽는다** — `BE_ORIGIN`·`AGENT_TRACING_ENABLED`·
`OPENAI_API_KEY`. `.env`는 읽지 않는다: 파일 한 줄이 BE 주소나 추적 스위치를 조용히 바꾸지 못하게
한다. 키가 없어도 앱은 뜬다 — `healthz`·`/metrics`는 LLM과 무관하고, 스트림은 수락 뒤
`LLM_UNAVAILABLE`로 끝난다.

분류기와 합성의 기본 모델은 BE와 같은 `gpt-5.4-mini`다.

## 검증 명령

검증 명령의 단일 원천은 `Makefile`이다. CI(`.github/workflows/ci.yml`)와 SDD
하네스(`automation/*.md`)가 같은 타깃을 부른다.

| 명령 | 내용 | CI 잡 |
|---|---|---|
| `make lint` | `ruff check` | `lint` |
| `make typecheck` | pyright (standard 모드, Python 3.12, `app`·`evals`·`tests`·`scripts`) | `typecheck` |
| `make test` | pytest (`tests/e2e/` 제외) | `test` |
| `make test-e2e` | pytest `tests/e2e/` — 컨테이너 테스트, Docker 필요 | `test-e2e` |
| `make build` | 서비스 이미지 로컬 빌드 | — |
| — | gitleaks 히스토리 시크릿 스캔 | `gitleaks` |

단위 테스트는 LLM·임베딩·DB 없이 돈다. 외부 경계는 가짜로 꽂는다 — BE 호출은 요청을 기록하는
`httpx.MockTransport`로, 분류기·합성 LLM은 LangChain 가짜 채팅 모델로, 실행 상한과 하트비트 주기도
주입한다(`create_app(backend_transport=…, classifier_model=…, synthesis_model=…)`). CI 러너에는
`.env`가 없으므로 실제 경계로 새는 테스트는 거기서 드러난다.

여기서 모자란 두 부류가 있다. 프로세스 전역을 바꾸는 설정은 자식 프로세스에서 판정한다 —
LangSmith는 환경변수 조회를 캐시하고 `configure`가 프로세스 전역을 바꾼다. 추적 숨김은 프로세스를
떠난 바이트로 판정하고(캡처 서버), 「없다」 단언은 같은 캡처의 양성 대조와 짝짓는다. 스트림 도중의
행동(판정 전 델타 없음, 끊김 `CANCELLED`)은 ASGI를 직접 구동한다 — `TestClient`는 앱 호출이 끝날
때까지 응답을 버퍼링한다.

## 배포

`main` push → 검증 잡 전부 통과 → `image` 잡이
`ghcr.io/cure-agent/medical-agentic-rag`를 `linux/amd64`로 빌드해 `latest`와 실행 번호로 올린다.
깨진 `latest`는 BE 배포를 실패로 끝내므로 이 잡은 다른 잡 전부에 `needs`로 걸린다.

**서버 배포는 BE가 맡는다.** 이 레포의 파이프라인은 `main` 머지에서 끝나고 cure-agent-be가 올라간
이미지를 당겨 간다. 이미지는 `uv.lock`에서 `--no-dev`로 설치하고, `app/service`만 싣고(실험
코드·psycopg 검색 경로는 싣지 않는다), 비-root로 돌며, 추적 변수를 ENV로 박지 않는다.

## 에이전트 평가

아래 실행은 실제 모델을 부르고 gitignore된 `results/`에 남는다.

```bash
.venv/bin/python scripts/smoke_llm.py           # 운영 코드 경로 그대로 실호출 4건
.venv/bin/python -m evals.run_routing           # 실행 경로 정확도, 303문항 × 3회
.venv/bin/python -m evals.run_synthesis         # 합성 생성 + LLM 심판
```

- `scripts/smoke_llm.py`는 가짜 채팅 모델로 볼 수 없는 것만 본다 — `gpt-5.4-mini`가 strict
  `response_format` 바인딩을 받는가, 스트림 끝 usage가 실제로 오는가, 손으로 짠 증분 파서가 실제
  청크 경계에서 버티는가.
- `evals/routing/`이 고정된 라우팅 평가셋이다 — 경계 문항 65개, FE 데모 질의문 9개, BE의 229문항
  평가셋. 채점 기준은 분류기 판정 원문이 아니라 `plan_route`를 거친 **실행 경로**다. 코드가
  보증하는 것이 그것이다.
- `evals/synthesis/`에는 합성 환자 기록 12건, 환자 경로 질문 36개, 코퍼스의 실제 권고를 근거로 박은
  복합 문항 40개(그중 8건은 기권해야 한다)가 있다. 생성은 서비스 함수를 그대로 쓴다.

수치는 이 문서에 적지 않는다 — `results/`는 gitignore이고 덮어쓰며, 합성 심판의 거짓 양성이
그대로 믿기에는 많다(쓰기 전에 사람이 본다).

## 기원: Agentic RAG ablation 연구

이 저장소는 **Agentic RAG가 실제로 언제 도움이 되는가**를 묻는 ablation 연구로 시작했다. CureAgent의
하이브리드 검색 경로를 이식한 뒤, 제안된 에이전틱 개입을 각각 분리해 운영에 닿기 전에 평가했다.

> **판정(2026-08-24):** 테스트한 개입은 어느 것도 전면 도입 기준을 충족하지 못했고, 운영 검색 경로는
> 현행 유지다 — 원 질문 1개 → 하이브리드 → RRF 무절단 → LLM 리랭커 → top-5 → 점수 게이트. 이는
> Agentic RAG가 절대 도움이 되지 않는다는 근거가 아니라, 관측된 표본·불확실성·기전·비용을 바탕으로
> 내린 결정이다.

판정은 **complex 12문항 × 5회 실행**에 근거한다. `/`로 나뉜 값은 같은 출력에 대한 두 번의 채점이다.

| 개입 | 관측 효과 | 판정 |
|---|---|---|
| 질의 분해(변형 B) | 정답률 **+0.100 / +0.117**, 95% CI [−0.150, +0.350] / [−0.117, +0.367]; 부호 4:3 | 도입 안 함 — 문항별 효과가 −0.60~+0.80이고 검색층 기전이 불명확했다 |
| 컨텍스트 증량, top 5 → top 11 | 정답률 **−0.083 / −0.083**, 95% CI [−0.267, +0.067] | 재현되는 이득 없음. 실패를 없애지 않고 옮겼다 |
| 축 열거 답변 프롬프트 | 정답률 **−0.067 / −0.083**; 부호 0:4 / 0:5 | 이득 없음, 방향이 불리할 수 있다 |
| 반복 검색(`rerank_full`) | 정답률 **+0.050**; 부호 2:2, 95% CI [−0.117, +0.250]; 60행 중 12행만 루프에 들어갔다 | 이 근거와 비용으로는 도입 안 함 |

가장 큰 관측 개선은 **리랭킹**에서 나왔지만(complex 정답률 0.17 → 0.50) 폐기된 k=1 ablation에서 온
탐색적 수치다. 그 값은 오히려 설계 오류를 드러낸 데 있다 — query understanding을 분리하려고
리랭킹을 제외했는데, 운영에는 이미 리랭커가 있으므로 리랭커 없는 베이스라인에서 잰 이득으로는 운영
변경을 정당화할 수 없다.

**관련도와 답변가능성은 다른 게이트를 쓴다.** 검색 점수는 생성 비용을 통제할 뿐, 근거가 다면 질문에
답할 수 있는지는 정하지 못한다. 관련도 10/10을 받고도 질문에 답하지 못하는 근거를 실제로 관측했다.
위 복합 경로가 답을 쓰는 자리에서 답변가능성을 판정하는 이유가 이것이다.

### 측정이 가르쳐 준 것

끝난 실험마다 [docs/experiments/](docs/experiments/INDEX.md)에 불변 문서가 있다. 관측 숫자는 고치지
않고, 뒤집히면 이전 문서에 `superseded` 배너를, 사실·통계 오류는 `erratum` 배너를 단다. 실패한
비교도 「평가가 어떻게 틀어지는가」의 증거로 남긴다.

1. **1회 실행은 실험이 아니다.** 첫 통제군 점수는 0.75였고 5회 추정은 0.600이었다. 비결정성은 생성이
   아니라 리랭커에서 시작했다 — 같은 입력에 top-5 집합이 36건 중 21건 달라졌다.
2. **커밋 해시 없는 비교는 무효일 수 있다.** 첫 ablation의 8행이 서로 다른 코드 epoch에서 나왔다.
   `run_eval`은 이제 모든 행에 `code_version`을 기록한다.
3. **정답률만으로는 검색 실패와 생성 실패를 가르지 못한다.** 근거 청크 커버리지를 key point 단위로
   따로 재야 한다.
4. **통제군은 조용히 무너진다.** 컨텍스트 크기 통제군이 운영 통제군으로 붕괴한 적이 있고, 프롬프트
   플래그가 노드에 닿지 못한 적도 있다. 두 경로 모두 지금은 테스트가 잠근다.
5. **채점자도 반복해야 한다.** 639개 key point 판정 중 두 채점 사이에 4건(0.6%)이 바뀌고 기권 판정은
   하나도 바뀌지 않았다 — 그래서 측정 교정을 채점자 노이즈와 구분할 수 있었다.
6. **프롬프트에 규칙을 쓴다고 지켜지지 않는다.** 권고 등급 규칙이 한 key point에서 10/10 위반됐다.
   지금은 등급을 먼저 추출해 코드가 결정론적으로 조정한다(`evals/judge.py::reconcile_grade`).
7. **LLM 라벨러는 내용 대신 형식을 맞춘다.** 반복되는 권고 템플릿 때문에, 발췌에서 주어가 빠지면
   라벨러가 다른 개입의 근거를 골랐다 — 33개 key point 중 5개가 그렇게 오라벨됐다.

통계적 유의성을 단독 기준으로 쓰지 않았다. 불일치 쌍이 5개면 5:0이어도 양측 부호검정 p의 최솟값이
0.0625다. 관측된 부호 수, 0을 포함하는 CI, 문항별 이질성, 기전, 비용을 함께 보고 전면 도입을 하지
않았다. `cpx-010`은 모든 구성·모든 실행에서 실패했고 아직 설명되지 않았다. 한계 전문은
[docs/experiments/INDEX.md](docs/experiments/INDEX.md)에 있다.

### 연구 재현

이 저장소는 작성한 질문과 기대 key point는 공개하지만 생성된 답변·검색된 청크·원문 문서 id·벡터
DB는 공개하지 않는다. 그것 없이도 집계 효과는 재현된다.

```bash
python scripts/verify_public_results.py   # 표준 라이브러리만 쓴다
```

[익명화된 짝 점수](docs/experiments/evidence/paired_scores.csv)에서 평균 차이, 2단 부트스트랩 CI,
양측 부호검정을 다시 계산한다.

ablation을 직접 돌리려면 연구 환경이 필요하다 — `pgvector`·`pg_trgm`을 올린 PostgreSQL의
`hybrid_probe` 스크래치 DB에 코퍼스 read-only 익스포트를 적재하고, `.env`를 채운다(`.env.example`
참고). 실험 앱은 서비스 앱과 별개다.

```bash
.venv/bin/uvicorn app.main:app --reload
# POST /ask {"question": "...", "preset": "prod_rerank"}

python -m evals.run_eval --presets rerank,rerank_facets --categories complex --out results/run1
python -m evals.judge   --results results/run1
python -m evals.metrics --results results/run1
```

`prod_rerank`가 기본값이고 운영과 같다 — 원 질문 1개, top 5 청크, rerank cutoff 9. 과거 `rerank`
preset은 2026-08-23·2026-08-24 결과를 재현할 수 있도록 cutoff 3.5를 유지한다.

5회 반복 짝 비교, 사후 cutoff sweep, 검색층 facet 커버리지:

```bash
for i in 1 2 3 4 5; do
  python -m evals.run_eval --presets <A>,<B> --categories complex --out results/rep$i
  python -m evals.judge --results results/rep$i
done
python -m evals.repeat_metrics --runs results/rep{1,2,3,4,5} --a <A> --b <B>

python -m evals.cut_sweep --runs results/cut{1,2,3,4,5} --preset rerank_cut05 --run-cut 0.5

python -m evals.facet_coverage label --runs results/rep{1,2,3,4,5}
python -m evals.facet_gold_review  --runs results/rep{1,2,3,4,5}
python -m evals.facet_coverage score --runs results/rep{1,2,3,4,5} --a <A> --b <B>
```

- `--run-cut`은 실행에 실제로 쓴 cutoff와 같아야 하고, 그 cutoff는 재구성하려는 범위보다 낮아야 한다.
  더 낮은 cutoff는 재구성할 수 없다 — 게이트에 걸린 질문은 답변 노드에 닿지 못했고, 그 판정을
  기권으로 세면 낮은 cutoff가 인위적으로 안전해 보인다. sweep은 모든 범주에서 돌린다: 근거 부족
  질문과 답변 가능 질문의 트레이드오프가 반대 방향이다.
- `facet_gold_review`는 지표의 단조성을 이용한다 — 근거 청크가 늘면 「미검색」이 「검색」으로만 바뀔
  수 있다. 덕분에 사람이 볼 대상이 33개 key point에서 11개로 줄었다.
- 리랭크 실험은 동시성 1로 돈다. 한 요청이 후보 약 57개·입력 약 11k 토큰이고, 동시성 2에서는
  36문항 실행에 실패 10건이 났다.

## CureAgent와의 관계

CureAgent는 **단일 스펙 저장소 + 세 구현 레포**다.

- [cure-agent-be](https://github.com/Cure-Agent/cure-agent-be) — 운영 서버이고 `docs/specs/`가 사는
  곳이며, architecture와 검색 정책의 source of truth다. 이 서비스의 이미지도 BE가 배포한다.
- [cure-agent-fe](https://github.com/Cure-Agent/cure-agent-fe) — 스트리밍 답변, 인용, 환자 워크플로,
  대화 이력을 제공하는 제품 화면이다.
- **이 저장소** — 에이전트 서비스와, 그것이 자라 나온 실험·LLM 평가 하네스다.

스펙은 BE 레포에 있다. 이 레포는 수용 기준 중 `(AGENT)` 라벨이 달린 것을 구현하고 스펙을 직접 쓰지
않는다. `scripts/fetch_spec.py`가 번호로 스펙을 조달하고, 절차(테스트 동결, `/implement`와
`/problem` 라우팅, 머지)는 `automation/`에 있으며 `.claude/commands/`·`.codex/skills/`는 하네스
특성만 주입하는 어댑터다.

## 저장소 구조

```text
app/service/                             # 서비스 — 운영 이미지에 싣는 유일한 코드
  main.py                                # create_app · lifespan(추적 스위치 · BE 클라이언트 · 턴 설정)
  routes.py                              # healthz · me · POST …/messages/stream
  turn.py                                # 턴 하나: 분류 → 경로 실행 → 완결 · 하트비트 · 끊김 정리
  routing.py                             # 분류기(strict JSON) · 실행 경로 표(순수) · 검색 입력의 라벨 제거
  synthesis.py                           # 환자·복합 합성 · 판정 선행 증분 파서 · 마커 → 인용
  backend.py                             # BE 클라이언트 — 받은 자격만 싣는다, 무응답 → 502
  access_token.py                        # access JWT 잔여 수명 선검사(exp·iat, 서명은 검증하지 않는다)
  sse.py · envelope.py · config.py       # SSE 프레임 · §10.1 봉투 + ULID traceId · 환경변수만 읽는 설정
  llm.py · tracing.py                    # BaseChatModel 뒤의 채팅 모델 · LangSmith 스위치 + 숨김 클라이언트
app/                                     # 연구 — 실험 앱, 이미지에 싣지 않는다
  main.py · api/routes.py                # 실험 앱: /healthz · POST /ask · /ask/stream
  agent/graph.py · agent/state.py        # AgentConfig · PRESETS · 순수 라우팅 함수 · 노드 입출력 스키마
  agent/nodes/                           # decompose · retrieve · evaluate · generate_queries · answer · abstain
  retrieval/hybrid.py · rrf.py           # dense(pgvector) + lexical(pg_trgm) SQL · RRF 융합, K=60, 무절단
  retrieval/reranker.py                  # 이식한 listwise 리랭커
  retrieval/reranking_retriever.py       # 변형 A: 하위 질의마다 리랭크
  retrieval/fused_reranking_retriever.py # 변형 B: 병합 후 1회 리랭크
  retrieval/factory.py                   # 검색 경로 조립 — API와 evals가 같은 배선을 쓰는 단일 지점
  config.py · llm/prompts.py             # 실험 설정, policy_version_for · 노드 프롬프트와 축 변형
evals/
  run_routing.py · routing/              # 실행 경로 정확도 · 경계 문항 · 데모 질의문 · 229문항 평가셋
  run_synthesis.py · synthesis/          # 합성 심판 · 환자 기록 · 환자 질문 · 복합 문항
  run_eval.py · judge.py · metrics.py    # 커밋 메타데이터를 남기는 ablation 러너 · LLM 심판 + 결정적 규칙
  repeat_metrics.py · cut_sweep.py       # 반복 짝 지표와 부트스트랩 CI · 사후 cutoff sweep
  facet_coverage.py · facet_gold*.json   # 검색층 key point 커버리지 · 근거 청크 라벨
  dataset.jsonl                          # 36문항 평가셋
tests/                                   # 오프라인 단위 테스트(서비스·그래프·라우팅·RRF·리랭크·심판·추적)
tests/e2e/                               # 컨테이너 테스트 — 이미지를 빌드해 띄운다, Docker에 못 닿으면 실패
scripts/                                 # smoke_llm.py · fetch_spec.py · verify_public_results.py
docs/architecture.md                     # 이 저장소의 설계 문서
docs/experiments/                        # 불변 실험 기록과 판정
automation/ · .claude/ · .codex/         # SDD 하네스: 절차 원문 + 하네스별 어댑터
Dockerfile · Makefile · uv.lock          # 서비스 이미지 · 검증 명령 · 의존성 잠금
```

## 기술 스택

Python 3.12+ · FastAPI · Starlette SSE · httpx · LangChain 1.x · LangSmith ·
prometheus-client · Pydantic 2 · OpenAI models · uv · Docker · pytest · pyright · ruff

연구 경로는 여기에 LangGraph 1.x · PostgreSQL · pgvector · pg_trgm을 더 쓴다. 서비스 경로는 그중
아무것도 쓰지 않는다 — DB 풀을 열지 않고 실험 코드를 import하지 않는다.
