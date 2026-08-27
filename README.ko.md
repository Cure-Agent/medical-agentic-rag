# Medical Agentic RAG

[English](README.md) | [한국어](README.ko.md)

> **Agentic RAG는 실제로 언제 도움이 되는가?**
>
> 운영 의료 가이드라인 RAG 시스템에서 가져온 베이스라인 위에서 query decomposition,
> context expansion, facet-enumeration prompting, iterative retrieval, reranking을
> 평가한 ablation 연구.

이 저장소는 [CureAgent](https://github.com/Cure-Agent/cure-agent-be)의 하이브리드
검색 경로를 이식한 뒤, 제안된 에이전틱 개입을 각각 분리해 운영 시스템에 반영하기 전에
효과를 평가한다.

## 연구 질문

CureAgent의 운영 하이브리드 검색기는 dense retrieval, 문자 n-gram 검색, Reciprocal Rank
Fusion(RRF)으로 **후보 커버리지 1.000**을 달성했지만 오답은 남아 있었다. 정답을 뒷받침하는
근거가 이미 후보군에 있다면, 남은 병목은 다음 두 가지인가?

- 특히 다면 질문에서 분해가 도움이 될 수 있는 **질문의 구조**
- 시스템이 근거가 충분한 시점을 판단할 수 있는가에 해당하는 **검색 종료 판단**

> **현재 결론(2026-08-24):** 현재 평가 설정에서 테스트한 에이전틱 개입은 운영 도입을
> 정당화할 만큼 일관된 근거를 보이지 않았다. Query decomposition, context 추가,
> facet-enumeration prompting, iterative retrieval 모두 전면 도입 기준을 충족하지 못했다.
> 가장 큰 탐색적 효과는 reranking에서 나왔고(complex 질문 정답률 0.17 → 0.50,
> **k=1**), 운영은 더 단순한 단일 질의 검색 경로를 유지한다. 이는 Agentic RAG가 절대
> 도움이 되지 않는다는 근거가 아니라, 현재 표본·불확실성·기전·비용을 바탕으로 내린
> 결정이다. 자세한 내용은 [실험 기록](docs/experiments/INDEX.md)을 참고한다.

## 운영 기반 베이스라인

비교 베이스라인은 CureAgent의 검색 정책을 재현한다.

1. 원 질문을 임베딩하고 `pgvector` cosine distance로 dense retrieval을 수행한다.
2. `pg_trgm` `word_similarity`로 lexical retrieval을 수행한다.
3. 절단하지 않은 합집합을 RRF로 융합한다.
4. 전체 후보군을 LLM으로 reranking한 뒤 상위 5개 chunk를 선택한다.
5. 검색 게이트와 생성 게이트를 각각 적용한다.

| 구성 요소 | 설정 | 이 설정을 고정한 이유 |
|---|---|---|
| Dense arm | `text-embedding-3-small`, 1,536차원; `pgvector` cosine distance | 코퍼스와 질의가 같은 임베딩 공간을 사용해야 한다. |
| Lexical arm | BM25가 아닌 `pg_trgm` `word_similarity` | 코퍼스의 공백 소실로 어절 토큰화가 불안정하다. 문자 n-gram은 조사·붙임과 띄어쓰기에 강건하다. |
| RRF | 절단하지 않은 합집합에 K=60 | top-30 절단 시 후보 커버리지는 0.978, 합집합에서는 1.000으로 측정됐다. |
| Lexical 동점 처리 | `ORDER BY similarity DESC, id ASC` | top-30 경계에서 동점 72건이 관측됐다. 2차 키를 두어 검색을 결정적으로 만든다. |
| 거리 게이트 | Cosine distance 0.48 | 두 가지 paraphrase 문체로 만든 118문항에서 손실 0건을 관측했다. |
| LLM reranker | 후보 무절단 → listwise reranking → top 5; 300자 발췌 | 운영 평가에서 Recall@5가 0.780에서 0.983으로 향상됐다. |
| Rerank 점수 게이트 | Top-1 관련도 cutoff | 과거 `rerank` preset은 3.5를 사용한다. 운영 parity인 `prod_rerank`는 [229문항 × 2회 cutoff sweep](https://github.com/Cure-Agent/cure-agent-be/blob/dev/docs/rag-eval/2026-08-25-cut-sweep-verdict.md) 이후 9를 사용한다. 역할은 답변가능성 판정이 아니라 생성 비용 통제다. |

처음에는 query understanding을 분리해 측정하려고 reranking을 제외했다. 이는 설계 오류였다.
운영에는 이미 reranker가 있으므로, reranker가 없는 베이스라인에서 측정한 이득으로는 운영
변경을 정당화할 수 없다. Reranking은 에이전틱 개입이 해결하려던 다면 질문 오류 중 상당수를
이미 해결하고 있었다.

## 에이전틱 파이프라인

```text
질문
  │
  ▼
1–4개 하위 질의로 분해
  │
  ▼
하이브리드 검색 → RRF → 선택적 Rerank
  │
  ▼
근거 평가
  │
  ├── 충분 ───────────────────→ 답변
  │
  └── 부족
         │
         ├── 예산 남음 ───────→ 후속 질의 생성
         │                            │
         │                            └──→ 재검색
         │
         └── 예산 소진 ───────→ 기권
```

위 그림은 `full` 경로다. Rerank가 활성화된 변형은 검색 단계를 **후보 무절단 → rerank →
top-k → 점수 게이트**로 바꾼다. Answer 노드는 생성 게이트도 겸한다. 검색 근거로 답할 수
없으면 구조화된 `insufficient_evidence` 신호를 내보내고 기권 경로로 보낸다.

- **재검색 예산은 LLM이 아니라 코드가 관리한다.** `app/agent/graph.py`의 routing 함수는
  순수 함수이며 모델 호출 없이 단위 테스트한다.
- 근거는 `chunk_id`를 key로 하는 dictionary에 저장해 검색 라운드 사이의 중복을 제거한다.
- **기권은 정상적인 종료 상태다.** 의료 환경에서는 근거 없는 답을 생성하는 것보다 어떤
  근거가 부족한지 설명하는 편이 낫다.
- **두 게이트는 서로 다른 질문에 답한다.** 검색 게이트는 *관련도*를 추정해 불필요한 생성
  비용을 피하고, 생성 게이트는 *답변가능성*을 추정한다. 평가에서는 관련도 10/10인 근거로도
  질문에 답할 수 없는 사례가 관측됐다.
- LangGraph 1.x는 graph orchestration을, LangChain 1.x는 structured output과 model
  abstraction을 담당한다.

## 실험

완료된 각 실험에는 [docs/experiments/](docs/experiments/INDEX.md)의 불변 증거 문서가 있다.
관측 숫자는 사후에 다시 쓰지 않는다. 후속 결과가 나오면 `superseded` 배너를, 사실 또는
통계 오류를 교정하면 `erratum` 배너를 추가한다. 평가가 어떻게 잘못될 수 있는지 보여주는
증거로서 실패한 비교도 보존한다.

현재 결정은 **complex 질문 12개 × pipeline 실행 5회**를 근거로 한다. `/`로 구분한 값은
같은 pipeline 출력에 대한 `grade-strict-v1` judge 2회 결과이고, retrieval loop 실험은
한 번만 채점했다.

| 개입 | 관측 효과 | 결정 |
|---|---|---|
| Query decomposition, 변형 B | 정답률 **+0.100 / +0.117**, 95% CI [−0.150, +0.350] / [−0.117, +0.367]; 두 judge 실행 모두 부호 4:3. 검색층 facet coverage **+0.022**, 95% CI [−0.075, +0.144]. | 전면 도입하지 않는다. 문항별 효과가 −0.60에서 +0.80까지 분포했고 검색층 기전도 불확실하다. |
| Context 증량, top 5 → top 11 | 정답률 **−0.083 / −0.083**, 95% CI [−0.267, +0.067]. | 재현 가능한 이득이 없다. 검색층 실패와 생성층 실패를 제거하지 않고 서로 맞바꿨다. |
| Facet-enumeration answer prompt | 정답률 **−0.067 / −0.083**, 95% CI [−0.183, +0.033] / [−0.200, +0.017]; 부호 0:4 / 0:5. | 관측된 이득이 없고, 방향이 오히려 불리할 수 있다. |
| Iterative retrieval, `rerank_full` | 정답률 **+0.050**; 부호 2:2, 95% CI [−0.117, +0.250]. 60행 중 12행만 loop에 진입했고 이득 대부분이 문항 1개에서 나왔다. | 현재 근거와 비용 조건에서는 loop를 추가하지 않는다. |

교정 전 top-5 → top-11 추정값은 0.000이었다. 교정된 두 judge 실행은 모두 −0.083을
산출했으며, 도입하지 않는다는 결정은 바뀌지 않았다.

통계적 유의성만으로 도입 여부를 결정하지 않는다. 불일치쌍이 5개라면 5:0이어도 양측
부호검정 p-value의 최솟값은 0.0625다. `n=12` 설계가 원리적으로 유의할 수 없는 것은
아니지만, 여기서 관측한 부호·0을 포함하는 신뢰구간·문항별 이질성·기전·비용은 전면 도입을
뒷받침하지 않는다.

## 핵심 결과

1. **Query decomposition은 일관된 이득을 보이지 않았다.** 일부 질문 형태는 크게 개선됐지만
   다른 질문은 악화됐고, 평균은 이 이질성을 가렸다.
2. **검색 context 증량은 정답률을 개선하지 않았다.** Chunk를 늘리면 실패 위치가 달라졌지만
   실패를 일관되게 제거하지 못했다.
3. **Iterative retrieval의 이득은 제한적이었다.** Loop에 진입한 문항은 3개뿐이었고,
   관측 이득 대부분을 문항 1개가 만들었다.
4. **Reranking에서 가장 큰 관측 개선이 나왔다.** 0.17 → 0.50은 폐기된 k=1 ablation에서
   나온 탐색적 결과지만, 운영 parity 베이스라인의 중요성을 드러냈다.
5. **관련도와 답변가능성에는 별도 게이트가 필요하다.** 검색 점수는 생성 비용 통제에는
   유용하지만, 근거가 다면 질문에 답하기에 충분한지는 판단할 수 없다.

이 결과는 현재 검색기가 충분하다는 의미가 **아니다**. 한 통제 비교에서 교정된 complex 질문
정답률은 0.600이었고, `cpx-010`은 테스트한 모든 구성에서 5회 모두 실패했다.

Retrieval loop는 일부 실행에서만 성공한 흔들림 문항을 4개에서 2개로 줄이기도 했다. 이는
평균 정답률과 별개의 관측이며 k=5 실험 한 번에서 나온 값이므로, 주장이라기보다 재현할
대상으로 남겨 둔다.

## 평가 방법론

전체 데이터셋은 simple 12개, complex 12개, 의도적으로 근거가 부족한 insufficient 12개로
총 36문항이다. 올바른 동작은 category마다 다르다. 답변 가능한 질문은 기권하지 않고 필요한
key point를 모두 충족해야 하며, insufficient 질문은 기권해야 한다.

주요 반복 ablation은 다음을 사용한다.

- 구성별 독립 pipeline 실행 5회
- 명시적인 권고등급 대조를 포함한 엄격한 key-point 채점
- 문항 단위 정답률과 key-point coverage
- 검색 실패와 생성 실패를 구분하는 검색층 facet coverage
- 문항별 paired 성공률, 문항과 실행에 대한 2단 bootstrap, 양측 exact sign test
- code commit(`code_version`), judge ruleset, dataset, preset, model, 반복 횟수,
  API 오류 수를 포함한 결과 metadata

평가기 자체도 두 번 측정했다. 639개 key-point 판정 중 judge 실행 사이에 바뀐 것은 4개
(0.6%)였고 기권 판정은 하나도 바뀌지 않았다. 덕분에 측정 교정과 evaluator noise를 구분할
수 있었다.

## RAG 시스템 평가에서 배운 것

가장 값진 결과는 승리한 구성이 아니다. 측정이 어떻게 실패했고 그 실패가 의사결정을 어떻게
바꿨는지 남긴 기록이다.

1. **실행 1회는 실험이 아니다.** 최초 통제 점수는 0.75였지만 5회 추정값은 0.600이었다.
   12문항 중 4문항이 실행마다 결과가 바뀌었다. 비결정성은 생성뿐 아니라 reranker부터
   시작됐다. 같은 질문과 후보에서도 top-five 집합이 36건 중 21건에서 달라졌다.
2. **Commit hash가 없는 비교는 무효일 수 있다.** 첫 ablation의 8개 행이 서로 다른 code
   epoch에서 나왔다. 오류를 재구성하려면 file timestamp와 trace fingerprint가 필요했다.
   이제 `run_eval`은 모든 결과 행에 `code_version`을 기록한다.
3. **정답률만으로는 검색 실패와 생성 실패를 구분할 수 없다.** 어디에 개입할지 결정하려면
   key-point 단위의 supporting-chunk coverage를 별도로 측정해야 한다.
4. **통제 구성은 조용히 깨질 수 있다.** Reranker 요청 개수가 고정돼 context-size 통제군이
   운영 통제군과 같아진 적이 있다. Prompt flag가 node까지 전달되지 않아 두 구성이 같은
   prompt로 실행된 적도 있다. 두 경로 모두 이제 test로 보호한다.
5. **Evaluator도 반복 측정해야 한다.** 두 번째 judge 실행이 없었다면 +0.217에서
   +0.100 / +0.117로 바뀐 값을 gold label과 채점 로직의 교정이 아니라 noise로 잘못
   해석할 수 있었다.
6. **Prompt에 규칙을 적는다고 규칙이 강제되지는 않는다.** 권고등급 규칙은 처음부터 있었지만
   한 key point에서 10/10번 위반됐다. 이제 evaluator가 등급을 먼저 추출하고 코드에서
   결정적으로 대조한다(`evals/judge.py::reconcile_grade`).
7. **LLM labeler는 내용 대신 형식을 맞출 수 있다.** 발췌문이 앞부분의 주어를 누락했을 때
   반복되는 권고안 template 때문에 다른 중재의 근거를 선택했다. 33개 key point 중 5개가
   이 방식으로 오라벨됐다.

## 재현성

저장소에는 직접 작성한 평가 질문과 기대 key point가 포함되지만, 생성 답변·검색 chunk·원문
document ID·vector database는 포함하지 않는다. 이런 자료를 공개하지 않고도 aggregate 효과를
독립적으로 재현할 수 있다.

- [익명화된 paired binary score](docs/experiments/evidence/paired_scores.csv)는 `case_01`과
  같은 가명 ID를 사용하고 5회 성공값만 포함한다.
- [검산 스크립트](scripts/verify_public_results.py)는 Python 표준 라이브러리만으로 평균 차이,
  2단 bootstrap 신뢰구간, 양측 부호검정을 다시 계산한다.

```bash
python scripts/verify_public_results.py
```

`results/` 아래의 실험 출력은 의도적으로 gitignore되며 덮어쓸 수 있다. 관측값은 실험 문서와
익명화된 점수 산출물에 고정한다.

### 한계

- 반복 개입 결과는 complex 질문 12개를 사용한다. 한 문항이 정답률을 8.3 percentage point
  움직이며, 보고한 모든 개입의 신뢰구간은 0을 포함한다.
- Key-point 근거 label 11개는 LLM 보조 workflow로 검토했지만 최종 사람 검수는 아직 받지 않았다.
- 엄격한 권고등급 규칙을 도입한 뒤 simple 질문은 재채점하지 않았다.
- `cpx-010`은 모든 구성과 실행에서 실패했으며 원인은 아직 설명되지 않았다.
- Iterative path는 설계상 rerank 점수 게이트를 우회한다. 점수가 8–10인 complex set에는
  영향을 주지 않았지만 insufficient 질문에서의 비용은 측정하지 않았다.

## 실험 실행

Python 3.12+와 `pgvector`, `pg_trgm`을 설치한 PostgreSQL database가 필요하다. 코퍼스는
`hybrid_probe` scratch database에 적재한 운영 데이터의 read-only export를 사용한다.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # OPENAI_API_KEY와 DATABASE_URL 설정

pytest
uvicorn app.main:app --reload
# POST /ask {"question": "...", "preset": "prod_rerank"}
```

`prod_rerank`는 기본값이며 현재 운영 검색 정책과 동일하다. 원 질문 1개, top 5 chunk,
rerank cutoff 9를 사용한다. 과거 `rerank` preset은 2026-08-23과 2026-08-24 결과를
재현할 수 있도록 cutoff 3.5를 유지한다.

Ablation 1회를 실행한다.

```bash
python -m evals.run_eval \
  --presets rerank,rerank_facets \
  --categories complex \
  --out results/run1
python -m evals.judge --results results/run1
python -m evals.metrics --results results/run1
```

Paired comparison을 5회 반복한다.

```bash
for i in 1 2 3 4 5; do
  python -m evals.run_eval \
    --presets <A>,<B> \
    --categories complex \
    --out results/rep$i
  python -m evals.judge --results results/rep$i
done
python -m evals.repeat_metrics \
  --runs results/rep{1,2,3,4,5} \
  --a <A> \
  --b <B>
```

낮은 cutoff로 실행한 결과에서 rerank cutoff를 sweep한다.

```bash
# 실행 cutoff는 재구성하려는 범위의 최솟값보다 낮아야 한다.
for i in 1 2 3 4 5; do
  python -m evals.run_eval --presets rerank_cut05 --out results/cut$i
  python -m evals.judge --results results/cut$i
done
python -m evals.cut_sweep \
  --runs results/cut{1,2,3,4,5} \
  --preset rerank_cut05 \
  --run-cut 0.5
```

`--run-cut`은 실제 실행에 사용한 cutoff와 일치해야 한다. Gate에 걸린 질문은 answer node에
도달하지 않았으므로 더 낮은 cutoff는 재구성할 수 없다. 누락된 판정을 기권으로 처리하면 낮은
cutoff가 실제보다 안전해 보인다. Insufficient 질문과 answerable 질문의 tradeoff가 반대이므로
sweep은 모든 category를 대상으로 실행한다.

검색층 facet coverage를 측정한다.

```bash
python -m evals.facet_coverage label --runs results/rep{1,2,3,4,5}
python -m evals.facet_gold_review --runs results/rep{1,2,3,4,5}
python -m evals.facet_coverage score \
  --runs results/rep{1,2,3,4,5} \
  --a <A> \
  --b <B>
```

`facet_gold_review`는 metric의 단조성을 이용해 사람 검수 범위를 줄인다. Supporting chunk를
추가하면 “not retrieved”가 “retrieved”로만 바뀔 수 있으므로, 모든 관측에서 retrieved인
key point는 비교에 영향을 주지 않는다. 이 원리로 검수 대상을 33개 key point에서 11개로
줄였다.

같은 출력에 대해 judge를 반복 실행한다.

```bash
mkdir -p results/judge2/rep1
for f in results/rep1/*.jsonl; do
  case "$f" in
    *.judged.jsonl) ;;
    *) cp "$f" results/judge2/rep1/ ;;
  esac
done
python -m evals.judge --results results/judge2/rep1
```

Rerank 실험은 concurrency 1을 사용한다. 요청 1회에 약 57개 후보와 11k input token이
포함되며, concurrency 2에서는 36문항 실행 중 10건이 실패했다. 측정 환경에서 complex
12문항, 2개 구성, 5회 반복 비교에는 약 11분이 걸린다.

## 기술 스택

Python 3.12+ · FastAPI · LangGraph 1.x · LangChain 1.x · PostgreSQL · pgvector ·
pg_trgm · Pydantic 2 · OpenAI models · pytest

## CureAgent와의 관계

- [cure-agent-be](https://github.com/Cure-Agent/cure-agent-be)는 운영 서비스이며 CureAgent의
  architecture와 검색 정책에 대한 source of truth다.
- 이 저장소는 실험 및 LLM evaluation harness다. 운영 검색 경로를 이식하고 제안된 변경을
  테스트하며, 근거가 뒷받침하는 개입만 제품에 반영한다. 현재 근거에서는 운영 경로를
  변경하지 않는다.
- [cure-agent-fe](https://github.com/Cure-Agent/cure-agent-fe)는 streaming 답변, citation,
  환자 workflow, 대화 history를 제공하는 product-facing interface다.

## 저장소 구조

```text
app/
  agent/graph.py                         # Graph 배선, routing, PRESETS
  agent/nodes/                           # Type이 있는 LLM 경계 node
  agent/nodes/answerer.py                # 답변 생성과 answerability gate
  agent/state.py                         # AgentState와 structured output schema
  retrieval/hybrid.py                    # Dense + lexical retrieval SQL
  retrieval/rrf.py                       # Test로 동작을 고정한 RRF fusion
  retrieval/reranker.py                  # 이식한 listwise reranker
  retrieval/reranking_retriever.py       # 변형 A: 각 하위 질의를 rerank
  retrieval/fused_reranking_retriever.py # 변형 B: 병합한 뒤 한 번 rerank
  retrieval/factory.py                   # Retrieval 경로 구성
  llm/prompts.py                         # Node prompt와 facet 변형
  api/routes.py                          # POST /ask와 /ask/stream (SSE)
evals/
  dataset.jsonl                          # 36문항 평가셋
  run_eval.py                            # Commit metadata를 기록하는 ablation runner
  judge.py                               # LLM-as-judge와 결정적 규칙
  metrics.py                             # 단일 실행의 preset/category metric
  repeat_metrics.py                      # 반복 paired metric과 bootstrap CI
  facet_coverage.py                      # 검색층 key-point coverage
  facet_gold_review.py                   # 최소 근거 label 검수 집합
  cut_sweep.py                           # 사후 rerank cutoff sweep
  facet_gold.json                        # Supporting-chunk label
docs/experiments/                        # 불변 실험 기록과 판정
  evidence/paired_scores.csv             # 공개 익명 binary score
scripts/verify_public_results.py         # 공개 effect, CI, sign test 재계산
tests/                                   # Offline graph, routing, RRF, rerank, judge,
                                         # control integrity test
```
