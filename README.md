# medical-agentic-rag

의료 가이드라인 코퍼스 위에서 **Agentic RAG가 실제로 언제 도움이 되는가**를 측정하는 실험 프로젝트.
운영 중인 의료 RAG 서비스([cure-agent-be](https://github.com/Cure-Agent/cure-agent-be))의 하이브리드 검색을 Base Retriever로 이식하고,
그 위에 query decomposition + iterative retrieval 루프를 얹어 ablation으로 비교한다.

## 문제의식

운영 시스템에서 하이브리드 검색(벡터 + 문자 n-gram, RRF 융합)으로 **후보 커버리지 1.000**을 실측한 뒤에도
남는 오답이 있었다. 후보군에 정답이 있는데 답이 틀린다면, 남은 병목은 검색이 아니라
**질문 구조**(다면 질문의 분해)와 **검색 종료 판단**(근거가 충분한가)이다 — 이 가설을 검증한다.

> **결과 요약 (2026-08-24)**: 분해·근거 증량·축 열거 프롬프트·반복 검색 루프는
> **전면 도입을 정당화할 만큼 일관된 이득을 보이지 않았다.** 가장 큰 관측 효과는 리랭커에서
> 나왔고(complex 0.17 → 0.50, 아직 k=1), 검색 경로는 원 질문 1개를 쓰는 운영 현행을 유지한다.
> 결론은 「가설이 틀렸다」가 아니라 **「현재 증거로 에이전틱 개입을 추가할 근거가 부족하다」**다.
> 대신 「관련도」와 「답변가능성」이 다른 질문이라는 것, 그리고 1회 실행 비교가 얼마나 쉽게
> 뒤집히는지를 확인했다. 자세한 내용은 [실험 기록](docs/experiments/INDEX.md).

## 아키텍처

```
                ┌────────────┐
        질문 →  │ ① Decompose │  (하위 질의 1~4개)
                └─────┬──────┘
                ┌─────▼──────┐
          ┌───→ │ ② Retrieve  │  (hybrid: pgvector cosine + pg_trgm word_similarity → RRF)
          │     └─────┬──────┘
          │     ┌─────▼──────┐
          │     │ ③ Evaluate  │  (근거 충분성 판정, 구조화 출력)
          │     └─────┬──────┘
          │   충분 ↙       ↘ 부족
          │  ┌──────┐    ┌───────────────┐
          │  │Answer │    │ 예산 남음?      │
          │  └──────┘    └──┬─────────┬──┘
          │        예 ↙            ↘ 아니오(최대 N=2)
          │  ┌────────────────┐   ┌────────┐
          └─ │ Query Generator │   │ Abstain │
             └────────────────┘   └────────┘
```

위 그림은 `full` 구성이다. 리랭크 구성에서는 ②가 **후보 무절단 → 리랭크 → top-k → 점수 게이트**로
바뀌고(운영 §29와 같은 순서), ④ Answer가 **생성 게이트**를 겸한다 — 근거로 답할 수 없으면
구조화 출력의 플래그로 알리고 Abstain 경로로 빠진다.

- **재검색 예산은 LLM이 아니라 코드가 쥔다** — `app/agent/graph.py`의 라우팅 함수는 순수 함수이고 LLM 없이 단위 테스트된다.
- 근거 풀은 chunk_id 키 dict — 라운드 간 중복이 저절로 제거된다.
- **abstain은 실패가 아니라 정상 종료 경로다.** 의료 도메인에서는 근거 없는 답변보다 "무엇이 부족한지"를 말하는 기권이 안전하다.
- **기권 게이트는 두 층이다** — 검색 게이트(거리·리랭크 점수)는 *관련도*를 보고 비용을 아끼며,
  생성 게이트는 *답변가능성*을 본다. 관련도가 10/10인데 답할 수 없는 사례가 실측됐다.
- LangGraph(1.x)가 그래프 배선, LangChain(1.x)이 구조화 출력·모델 추상화를 담당한다.

## 실험 결과

실험별 상세 기록은 **[docs/experiments/](docs/experiments/INDEX.md)** 에 있다. 한 실험 = 한 문서이고
끝나면 숫자를 고치지 않는다 — 뒤집히면 새 문서를 쓰고 이전 문서에는 `superseded` 또는
사실관계 오류를 밝히는 `erratum` 배너만 붙인다.
`results/` 원본은 gitignore로 제외하고, 관측 숫자는 문서 본문과
[익명화된 공개 점수](docs/experiments/evidence/paired_scores.csv)에 고정한다.

**현재 판정: 검색 경로는 운영 현행을 유지한다.** 제안됐던 네 개입은 모두 전면 도입 기준을
충족하지 못했다
(complex 12문항, k=5, 숫자는 [2026-08-24 교정본](docs/experiments/2026-08-24-measurement-fix.md)과
[루프 실험](docs/experiments/2026-08-24-retrieval-loop.md)):

| 개입 | 효과 | 판정 |
|---|---|---|
| query decomposition (변형 B) | +0.100 / +0.117, 부호검정 모두 4:3 | 이질성이 크고 검색층 효과도 +0.022, CI [−0.075, +0.144]로 불확실 |
| 근거 증량 (top-5 → top-11) | **−0.083 / −0.083** | CI [−0.267, +0.067]. 이득 재현 안 됨 — 생성층/검색층 실패를 맞바꿀 뿐 |
| answerer 축 열거 프롬프트 | **−0.067 / −0.083**, 부호검정 0:4 / 0:5 | 이득이 없고 방향이 반대일 수 있음 |
| 반복 검색 루프 (`rerank_full`) | +0.050, 부호검정 2:2 | CI [−0.117, +0.250]. **60행 중 12행만 루프에 진입**했고 이득 대부분이 문항 1개 |

### 공개 검증 산출물

위 정답률 효과는 각 실험의 파이프라인 출력을 `grade-strict-v1` 채점기로 다시 잰 공개 0/1 점수에서
재계산할 수 있다. `/`로 나눈 값은 채점 1·2회 결과이고, 루프는 1회만 채점했다. 과거 문서의
top-5 → top-11 `0.000`은 교정 전 채점값이며, 교정 채점 2회에서는 모두 `−0.083`이었다.
도입하지 않는다는 판정은 바뀌지 않는다.

- [익명화된 문항별 점수 CSV](docs/experiments/evidence/paired_scores.csv) — `case_01` 형식의
  가명 ID와 5회 성공 여부만 포함
- [검산 스크립트](scripts/verify_public_results.py) — 평균 차이, 문항→실행 2단 bootstrap CI,
  양측 부호검정을 표준 라이브러리만으로 재계산

```bash
python scripts/verify_public_results.py
```

질문·답변·검색 chunk·원문 ID·벡터 DB는 공개 산출물에 포함하지 않는다.

루프 실험에서 하나 남았다 — 흔들림 문항(k회 중 일부만 성공)이 4개에서 2개로 줄었다.
정답률과 별개의 관측이고 k=5 한 판이라 아직 주장하지 않는다.

**유의성만으로 도입 여부를 정하지 않는다.** 정확히는 불일치쌍이 5개인 비교에서는 다섯 개가
전부 한쪽이어도 양측 부호검정의 최솟값이 p=0.0625다. n=12 설계 자체가 원리적으로 유의할 수
없는 것은 아니다. 여기서는 관측된 부호(4:3, 0:5, 2:2), 0을 포함하는 CI, 문항별 이질성,
검색층 기전과 비용을 함께 보고 전면 도입 근거가 부족하다고 판단했다.

이것은 「분해를 추가할 근거가 없다」이지 「현재 검색이 충분하다」가 아니다. complex 정답률은
0.600이고 cpx-010은 모든 구성에서 5회 내내 0/5다.

강하게 남은 관측(아직 k=1 근거 포함): 리랭커가 단일 최대 지렛대, 두 층 기권 게이트,
점수 게이트의 역할은 안전이 아니라 비용 절감.

### 방법론에서 배운 것

이 프로젝트의 가장 값진 산출물은 표의 숫자가 아니라 **그 숫자가 어떻게 틀렸는지**다.

1. **k=1 비교는 노이즈를 본다.** 1차 실험의 「통제군 0.75 대 분해 0.83」에서 통제군의 참값은
   0.600이었다(운 좋은 실행). 12문항 중 4문항이 실행마다 뒤집힌다. 추론 모델이라 temperature를
   고정할 수 없고, 비결정성은 생성층이 아니라 **리랭커부터** 시작된다 — 같은 질문·같은 후보에
   top-5가 36문항 중 21건 달라졌다.
2. **결과에 커밋 해시가 없으면 비교가 무효가 될 수 있다.** 1차 실험 표의 8행이 같은 코드에서
   나오지 않았고, 파일 mtime과 trace 지문으로 역추적해야 했다. 지금은 `run_eval`이 결과 행마다
   `code_version`을 박는다.
3. **정답률은 검색 실패와 생성 실패를 구분하지 못한다.** key point 단위로 「지지 청크가 풀에
   들어왔는가」를 따로 재야 개입을 어디에 걸지 결정할 수 있다(`evals/facet_coverage.py`).
4. **통제군은 조용히 무너진다.** 근거 개수 통제군이 리랭커 요청 개수 고정 때문에 운영 재현군과
   같아진 적이 있고, 프롬프트 플래그가 노드까지 닿지 않으면 두 구성이 같은 프롬프트로 돌면서
   「차이 없음」이 나온다. 둘 다 테스트로 잠가 두었다.
5. **측정기도 반복해야 한다.** 파이프라인은 5회 돌리면서 채점은 파일당 한 번씩 돌렸다.
   채점을 2회 돌려 보니 노이즈는 0.6%였고, 교정 후 효과는 +0.100 / +0.117이었다. 그래서
   +0.217에서 줄어든 것을 「노이즈가 걷혔다」가 아니라 **「틀린 게 고쳐졌다」**로 부를 수
   있게 됐다. 반복 없이는 그 둘을 못 가른다.
6. **프롬프트에 규칙을 적는 것과 규칙이 지켜지는 것은 다르다.** 「권고등급이 명시되면 값까지
   일치해야 한다」는 채점 규칙은 처음부터 프롬프트에 있었고 한 문항에서 10/10 위반됐다.
   값을 **먼저 적게 하고** 비교는 코드가 해야 지켜진다(`evals/judge.py`의 `reconcile_grade`).
7. **LLM 라벨러는 내용이 아니라 형식을 보고 고른다.** 같은 지침에서 권고안마다 「(3) 권고안
   도출에 대한 설명 … 권고등급 …」이 반복되면 다른 권고안이 gold로 들어온다. 33개 kp 중
   5개가 그랬다.

## 기존 시스템에서 가져온 것 (이식 근거)

| 항목 | 값 | 근거 |
|---|---|---|
| RRF 융합 | K=60, 합집합 무절단 | top-30 절단 시 후보 커버리지 0.978, 합집합 1.000 (실측) |
| 키워드 arm | pg_trgm `word_similarity` (BM25 아님) | 코퍼스의 어절 경계 공백 소실 — 어절 토큰화 불성립, 문자 n-gram은 조사·붙임에 강건 |
| 키워드 동점 처리 | `ORDER BY similarity DESC, id ASC` | top-30 경계에 동점 72건 실측 — 2차 정렬 없이는 실행마다 결과가 다름 |
| 거리 게이트 | cosine distance 0.48 | paraphrase 2문체 118문항 손실 0 실측 |
| 임베딩 | text-embedding-3-small (1536d) | 코퍼스와 동일 모델 — 좌표계가 다르면 코사인 거리 무의미 |
| LLM 리랭커 | 후보 무절단 → 리스트와이즈 재정렬 → top-5, 발췌 300자 | Recall@5 0.780 → 0.983 (운영 §29 실측) |
| 리랭크 점수 게이트 | top1 관련도 컷 | 이 실험의 `rerank`는 3.5. 운영은 [229문항 × 2회 컷 스윕](https://github.com/Cure-Agent/cure-agent-be/blob/dev/docs/rag-eval/2026-08-25-cut-sweep-verdict.md) 후 **9 유지** — 안전이 아니라 생성 호출 비용 절감용 |

리랭커는 처음엔 "이 실험의 축은 query understanding이므로 검색 정책은 고정한다"며 제외했다.
**그 판단이 틀렸다** — 운영에 리랭커가 켜져 있으므로 리랭커 없는 baseline과 비교한 이득은
이식 판단에 쓸 수 없고, 실제로 리랭커가 다면 질문 오답의 상당 부분을 이미 풀고 있었다.

## 실행

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # OPENAI_API_KEY, DATABASE_URL 채우기

pytest                          # LLM·DB 없이 도는 배선·포팅 테스트
uvicorn app.main:app --reload   # POST /ask {"question": "...", "preset": "prod_rerank"}
```

`prod_rerank`가 현재 운영 **검색 정책** 패리티(원 질문 1개, top-5, 점수 컷 9)이며 API 기본값이다.
`rerank`는 2026-08-23~24 실험 결과의 의미를 보존하기 위해 컷 3.5로 남겨 둔 실험 프리셋이다.

ablation 한 번:

```bash
python -m evals.run_eval --presets rerank,rerank_facets --categories complex --out results/run1
python -m evals.judge --results results/run1
python -m evals.metrics --results results/run1
```

**k회 반복**(1회 비교는 노이즈다 — 위 방법론 1번):

```bash
for i in 1 2 3 4 5; do
  python -m evals.run_eval --presets <A>,<B> --categories complex --out results/rep$i
  python -m evals.judge --results results/rep$i
done
python -m evals.repeat_metrics --runs results/rep{1,2,3,4,5} --a <A> --b <B>
```

**리랭크 점수 컷 스윕**(컷을 몇으로 둘 것인가 — 재실행 없이 사후 계산한다):

```bash
# 관심 구간의 하한 아래에서 한 번만 돌린다. 컷은 라우팅에만 쓰이므로
# 이 실행 하나로 [0.5, ∞) 전 구간을 재구성할 수 있다.
for i in 1 2 3 4 5; do
  python -m evals.run_eval --presets rerank_cut05 --out results/cut$i
  python -m evals.judge --results results/cut$i
done
python -m evals.cut_sweep --runs results/cut{1,2,3,4,5} --preset rerank_cut05 --run-cut 0.5
```

`--run-cut`은 그 실행이 **실제로** 돌아간 컷이다. 그보다 낮은 컷은 재구성할 수 없어
도구가 거부한다 — 컷에 걸린 문항은 answerer를 거치지 않아 생성 게이트 판정이 비어 있고,
그 칸을 기권으로 채우면 낮은 컷이 실제보다 안전해 보인다. 카테고리는 **전체**로 돌린다:
컷의 손익은 insufficient(누출)와 answerable(과잉 기권) 양쪽에서 반대 방향으로 나므로
complex만 돌리면 절반만 보게 된다.

**검색층 facet coverage**(정답률이 검색/생성 어느 층의 실패인지 가른다):

```bash
python -m evals.facet_coverage label --runs results/rep{1,2,3,4,5}   # gold 라벨 (채점할 실행 전부)
python -m evals.facet_gold_review --runs results/rep{1,2,3,4,5}      # 검수 대상 추리기 (아래)
python -m evals.facet_coverage score --runs results/rep{1,2,3,4,5} --a <A> --b <B>
```

`facet_gold_review`는 LLM 라벨을 사람이 확인할 때 볼 것만 남긴다. 판정이
`retrieved = bool(set(gold) & pool)`이라 gold 추가는 「없음」 → 「있음」 한 방향으로만
움직이므로, **관측이 전부 「있음」인 key point는 재라벨해도 결과가 안 바뀐다.** 2026-08-24에
33개가 11개로 줄었고, 그 11개가 두 실험의 「없음」 셀 100%를 만들었다.

**채점도 반복한다**(측정기가 흔들리면 교정과 노이즈를 못 가른다):

```bash
mkdir -p results/judge2/rep1
for f in results/rep1/*.jsonl; do
  case "$f" in *.judged.jsonl) ;; *) cp "$f" results/judge2/rep1/;; esac   # 원 결과만 복사
done
python -m evals.judge --results results/judge2/rep1
```

채점 판이 바뀌면 같은 답변의 정답률이 달라진다 — 결과 행의 `judge_rules`로 판을 구분한다.

리랭크 구성은 동시성 1로 내려간다 — 호출 1회가 후보 57개(~11k 토큰)라 2로 두면 429가 터진다
(36문항 중 10건 실패 실측). complex 12문항 × 2구성 × 5회 ≈ 11분.

코퍼스는 운영 DB의 읽기 전용 덤프(`hybrid_probe` 스크래치 DB)를 쓴다 — 청크 + 임베딩 + pg_trgm/vector 확장.

## 구조

```
app/
  agent/graph.py                      # 배선·라우팅 + PRESETS — LLM 호출 없음, 순수 제어 흐름
  agent/nodes/                        # 노드 = 타입 있는 입출력의 함수 (LLM 접점은 여기뿐)
  agent/nodes/answerer.py             #   답변 + 생성 게이트(insufficient_evidence)
  agent/state.py                      # AgentState, AnswererOutput 등 스키마
  retrieval/hybrid.py                 # 하이브리드 검색 SQL 이식 — limit=None이 절단 지점
  retrieval/rrf.py                    # RRF 융합 이식 (tests/test_rrf.py로 동작 동일성 고정)
  retrieval/reranker.py               # 리스트와이즈 리랭커 이식 (§29) + 관대한 순위 파싱
  retrieval/reranking_retriever.py    # 변형 A — 하위 질의별 리랭크
  retrieval/fused_reranking_retriever.py  # 변형 B — 병합 후 원 질문으로 1회
  retrieval/factory.py                # 구성별 검색 경로 조립 (절단 지점이 여기서 갈린다)
  llm/prompts.py                      # 노드별 프롬프트 (+ 축 열거 변형)
  api/routes.py                       # POST /ask, /ask/stream(SSE)
evals/
  dataset.jsonl                       # 평가셋 36문항
  run_eval.py                         # ablation CLI — 결과 행에 code_version(커밋 해시) 기록
  judge.py                            # LLM-as-judge 채점
  metrics.py                          # preset × category 표 (1회 실행)
  repeat_metrics.py                   # k회 반복 집계 — 문항별 성공률 + 2단 부트스트랩 CI
  facet_coverage.py                   # 검색층 facet coverage + 실패 층위 교차표
  facet_gold_review.py                # gold 검수 대상 추리기 — 단조성으로 33개를 11개로 좁힌다
  cut_sweep.py                        # 리랭크 점수 컷 사후 스윕 — 실행 1회로 컷 전 구간
  facet_gold.json                     # key point별 지지 청크 라벨 (2026-08-24 검수 반영, 사람 확인 대기)
docs/experiments/                     # 실험 기록 (숫자 불변, 정정은 배너) — INDEX.md가 현재 판정 관리
  evidence/paired_scores.csv          # 원문 없이 공개한 익명 0/1 점수
scripts/verify_public_results.py      # 공개 점수에서 효과·CI·부호검정 재계산
tests/                                # 배선·라우팅·RRF·리랭크·채점·통제조건 — 전부 오프라인
```
