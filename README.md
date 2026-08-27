# Medical Agentic RAG

[English](README.md) | [한국어](README.ko.md)

> **When does Agentic RAG actually help?**
>
> An ablation study of query decomposition, context expansion, facet-enumeration
> prompting, iterative retrieval, and reranking over a production-derived medical
> guideline RAG system.

This repository ports the hybrid retrieval path from
[CureAgent](https://github.com/Cure-Agent/cure-agent-be), then isolates proposed
agentic interventions so that each one can be evaluated before it reaches the
production system.

## Research Question

CureAgent's production hybrid retriever achieved **1.000 candidate coverage** with
dense retrieval, character n-gram retrieval, and Reciprocal Rank Fusion (RRF), yet
incorrect answers remained. If the supporting evidence is already present in the
candidate set, are the remaining bottlenecks:

- the **structure of the question**, especially multi-faceted questions that may
  benefit from decomposition; and
- the **retrieval stopping decision**, or whether the system can tell when it has
  enough evidence?

> **Current conclusion (2026-08-24):** Under the current evaluation setting, the
> tested agentic interventions did not provide sufficiently consistent evidence to
> justify production adoption. Query decomposition, additional context,
> facet-enumeration prompting, and iterative retrieval all failed to meet the
> rollout bar. The largest exploratory effect came from reranking
> (complex-question accuracy 0.17 → 0.50, **k=1**), so production retains the
> simpler single-query retrieval path. This is not evidence that Agentic RAG never
> helps; it is a decision based on the present sample, uncertainty, mechanism, and
> cost. See the [experiment log](docs/experiments/INDEX.md).

## Production-Derived Baseline

The comparison baseline mirrors CureAgent's retrieval policy:

1. Embed the original question and run dense retrieval with `pgvector` cosine
   distance.
2. Run lexical retrieval with `pg_trgm` `word_similarity`.
3. Fuse the untruncated union with RRF.
4. Rerank the full candidate set with an LLM, then select the top five chunks.
5. Apply separate retrieval and generation gates.

| Component | Configuration | Why it is fixed this way |
|---|---|---|
| Dense arm | `text-embedding-3-small`, 1,536 dimensions; `pgvector` cosine distance | The corpus and queries must use the same embedding space. |
| Lexical arm | `pg_trgm` `word_similarity`, not BM25 | Whitespace loss in the corpus makes word tokenization unreliable; character n-grams remain robust to attached particles and spacing. |
| RRF | K=60 over the untruncated union | Candidate coverage measured 0.978 after top-30 truncation and 1.000 over the union. |
| Lexical tie-breaking | `ORDER BY similarity DESC, id ASC` | Seventy-two ties were observed at the top-30 boundary; a secondary key makes retrieval deterministic. |
| Distance gate | Cosine distance 0.48 | No losses were observed across 118 questions with two paraphrase styles. |
| LLM reranker | Untruncated candidates → listwise reranking → top five; 300-character excerpts | Production evaluation improved Recall@5 from 0.780 to 0.983. |
| Rerank score gate | Top-1 relevance cutoff | The historical `rerank` preset uses 3.5. The production-parity `prod_rerank` preset uses 9 after a [229-question, two-run cutoff sweep](https://github.com/Cure-Agent/cure-agent-be/blob/dev/docs/rag-eval/2026-08-25-cut-sweep-verdict.md). Its role is generation-cost control, not answerability. |

Reranking was initially excluded because the experiment was intended to isolate
query understanding. That was a design error: production already uses a reranker,
so gains measured against a no-reranker baseline cannot justify a production
change. Reranking had already resolved many of the multi-faceted errors that the
agentic interventions were intended to address.

## Agentic Pipeline

```text
Question
   │
   ▼
Decompose into 1–4 subqueries
   │
   ▼
Hybrid Retrieve → RRF → optional Rerank
   │
   ▼
Evaluate Evidence
   │
   ├── Sufficient ───────────────→ Answer
   │
   └── Insufficient
          │
          ├── Budget remains ────→ Generate follow-up query
          │                              │
          │                              └──→ Retrieve again
          │
          └── Budget exhausted ──→ Abstain
```

This is the `full` path. Rerank-enabled variants replace retrieval with
**untruncated candidates → rerank → top-k → score gate**. The answer node also acts
as a generation gate: if the retrieved evidence cannot support an answer, it emits
a structured `insufficient_evidence` signal and routes to abstention.

- **Code, not the LLM, owns the retry budget.** Routing functions in
  `app/agent/graph.py` are pure and unit-tested without model calls.
- Evidence is stored in a `chunk_id`-keyed dictionary, which removes duplicates
  across retrieval rounds.
- **Abstention is a normal terminal state.** In a medical setting, explaining what
  evidence is missing is preferable to generating an unsupported answer.
- **The two gates answer different questions.** Retrieval gates estimate
  *relevance* and avoid unnecessary generation cost; the generation gate estimates
  *answerability*. The evaluation observed evidence rated 10/10 for relevance that
  still could not answer the question.
- LangGraph 1.x provides graph orchestration; LangChain 1.x provides structured
  outputs and model abstractions.

## Experiments

Each completed experiment has an immutable evidence document in
[docs/experiments/](docs/experiments/INDEX.md). Observed numbers are not rewritten
after the fact. A later result adds a `superseded` banner, while a factual or
statistical correction adds an `erratum` banner. This preserves failed comparisons
as evidence of how an evaluation can go wrong.

The current decision is based on **12 complex questions × five pipeline runs**.
Values separated by `/` are two `grade-strict-v1` judge passes over the same
pipeline outputs; the retrieval-loop experiment was judged once.

| Intervention | Observed effect | Decision |
|---|---|---|
| Query decomposition, variant B | Accuracy **+0.100 / +0.117**, 95% CIs [−0.150, +0.350] / [−0.117, +0.367]; sign counts 4:3 in both judge passes. Retrieval-layer facet coverage **+0.022**, 95% CI [−0.075, +0.144]. | Do not roll out globally. Effects ranged from −0.60 to +0.80 by question, and the retrieval-layer mechanism remains uncertain. |
| More context, top 5 → top 11 | Accuracy **−0.083 / −0.083**, 95% CI [−0.267, +0.067]. | No reproducible gain; it traded retrieval-layer and generation-layer failures rather than removing them. |
| Facet-enumeration answer prompt | Accuracy **−0.067 / −0.083**, 95% CIs [−0.183, +0.033] / [−0.200, +0.017]; sign counts 0:4 / 0:5. | No observed benefit, with a potentially adverse direction. |
| Iterative retrieval, `rerank_full` | Accuracy **+0.050**; sign count 2:2, 95% CI [−0.117, +0.250]. Only 12 of 60 rows entered the loop, and most of the gain came from one question. | Do not add the loop under the current evidence and cost profile. |

The earlier uncorrected top-5 → top-11 estimate was 0.000. Both corrected judge
passes produced −0.083; the rollout decision did not change.

Statistical significance is not used as the sole rollout criterion. With five
discordant pairs, even a 5:0 split has a minimum two-sided sign-test p-value of
0.0625. The `n=12` design is not inherently incapable of significance, but these
observed sign counts, confidence intervals containing zero, question-level
heterogeneity, mechanism, and cost do not support a global rollout.

## Key Findings

1. **Query decomposition did not show consistent gains.** A few question forms
   improved sharply, while others regressed; the average concealed that
   heterogeneity.
2. **Increasing retrieved context did not improve accuracy.** More chunks changed
   where failures occurred but did not consistently remove them.
3. **Iterative retrieval produced limited gains.** Only three questions ever
   entered the loop, and one question accounted for most of the observed benefit.
4. **Reranking produced the largest observed improvement.** The 0.17 → 0.50 result
   is still exploratory because it came from the superseded k=1 ablation, but it
   exposed the importance of using a production-parity baseline.
5. **Relevance and answerability require separate gates.** A retrieval score is
   useful for controlling generation cost; it cannot determine whether the
   evidence is sufficient to answer a multi-part question.

These findings do **not** imply that the current retriever is sufficient. Corrected
complex-question accuracy was 0.600 for one control comparison, and `cpx-010`
failed in all five runs under every tested configuration.

The retrieval loop also reduced unstable questions—those that succeeded in only
some runs—from four to two. This is separate from average accuracy and comes from
one k=5 experiment, so it remains an observation to reproduce rather than a claim.

## Evaluation Methodology

The full dataset contains 36 questions: 12 simple, 12 complex, and 12 deliberately
insufficient questions. Correct behavior is category-specific: answerable questions
must cover every required key point without abstaining, while insufficient
questions should abstain.

The main repeated ablations use:

- five independent pipeline runs per configuration;
- strict key-point grading with explicit recommendation-grade reconciliation;
- question-level accuracy and key-point coverage;
- retrieval-layer facet coverage to distinguish retrieval failures from generation
  failures;
- paired per-question success rates, a two-stage bootstrap over questions and
  runs, and a two-sided exact sign test; and
- result metadata including the code commit (`code_version`), judge ruleset,
  dataset, preset, model, repetition count, and API-error count.

The evaluator was itself measured twice. Across 639 key-point decisions, four
changed between judge passes (0.6%), with no abstention decisions changing. That
allowed the project to distinguish a measurement correction from evaluator noise.

## What We Learned About Evaluating RAG Systems

The most valuable result is not a winning configuration. It is a record of how the
measurements failed and how those failures changed the decision.

1. **A single run is not an experiment.** The first control score was 0.75; its
   five-run estimate was 0.600. Four of 12 questions changed outcome across runs.
   Non-determinism began in the reranker, not only in generation: for the same
   questions and candidates, the top-five set changed in 21 of 36 cases.
2. **A comparison without a commit hash may be invalid.** Eight rows in the first
   ablation came from different code epochs. File timestamps and trace fingerprints
   were needed to reconstruct the mistake. `run_eval` now records `code_version`
   on every result row.
3. **Answer accuracy alone cannot distinguish retrieval failures from generation
   failures.** Supporting-chunk coverage must be measured independently at the
   key-point level before deciding where to intervene.
4. **Control configurations can fail silently.** A context-size control once
   collapsed into the production control because the reranker request count was
   fixed. A prompt flag also failed to reach its node, causing two configurations
   to run the same prompt. Both paths are now protected by tests.
5. **The evaluator must also be repeated.** Without a second judge pass, the shift
   from +0.217 to +0.100 / +0.117 could have been mislabeled as noise instead of a
   correction to gold labels and grading logic.
6. **Writing a rule in a prompt does not enforce it.** A recommendation-grade rule
   was present from the start and was still violated 10/10 times for one key point.
   The evaluator now extracts grades first and reconciles them deterministically in
   code (`evals/judge.py::reconcile_grade`).
7. **LLM labelers can match format instead of content.** Repeated recommendation
   templates caused the labeler to select evidence for the wrong intervention when
   the excerpt omitted the leading subject. Five of 33 key points were mislabeled
   this way.

## Reproducibility

The repository includes the authored evaluation questions and expected key points,
but not generated answers, retrieved chunks, source-document IDs, or the vector
database. The aggregate effects remain independently reproducible without exposing
those materials:

- [Anonymized paired binary scores](docs/experiments/evidence/paired_scores.csv)
  use pseudonymous IDs such as `case_01` and contain only five-run success values.
- [The verification script](scripts/verify_public_results.py) recomputes mean
  differences, two-stage bootstrap confidence intervals, and two-sided sign tests
  using only the Python standard library.

```bash
python scripts/verify_public_results.py
```

Experiment outputs under `results/` are intentionally gitignored and may be
overwritten. The observed figures are fixed in the experiment documents and in the
anonymized score artifact.

### Limitations

- The repeated intervention results use 12 complex questions; one question moves
  accuracy by 8.3 percentage points, and all reported intervention CIs include
  zero.
- Eleven key-point evidence labels were reviewed with an LLM-assisted workflow but
  have not yet received final human review.
- Simple questions were not regraded after the strict recommendation-grade rule was
  introduced.
- `cpx-010` remains unexplained after failing in every configuration and run.
- The iterative path bypasses the rerank score gate by construction. This did not
  affect the complex set, whose scores were 8–10, but its cost on insufficient
  questions has not been measured.

## Running the Experiments

Python 3.12+ and a PostgreSQL database with `pgvector` and `pg_trgm` are required.
The corpus is a read-only production export loaded into the `hybrid_probe` scratch
database.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # Set OPENAI_API_KEY and DATABASE_URL

pytest
uvicorn app.main:app --reload
# POST /ask {"question": "...", "preset": "prod_rerank"}
```

`prod_rerank` is the default and matches the current production retrieval policy:
one original query, top five chunks, and a rerank cutoff of 9. The historical
`rerank` preset keeps cutoff 3.5 so the 2026-08-23 and 2026-08-24 results remain
reproducible.

Run one ablation:

```bash
python -m evals.run_eval \
  --presets rerank,rerank_facets \
  --categories complex \
  --out results/run1
python -m evals.judge --results results/run1
python -m evals.metrics --results results/run1
```

Run a five-repeat paired comparison:

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

Sweep the rerank cutoff from one low-cutoff run:

```bash
# The run cutoff must be below the range to reconstruct.
for i in 1 2 3 4 5; do
  python -m evals.run_eval --presets rerank_cut05 --out results/cut$i
  python -m evals.judge --results results/cut$i
done
python -m evals.cut_sweep \
  --runs results/cut{1,2,3,4,5} \
  --preset rerank_cut05 \
  --run-cut 0.5
```

`--run-cut` must match the cutoff that was actually used. Lower cutoffs cannot be
reconstructed because gated questions never reached the answer node; treating
those missing decisions as abstentions would make lower cutoffs look artificially
safe. Run the sweep over all categories because insufficient and answerable
questions have opposing tradeoffs.

Measure retrieval-layer facet coverage:

```bash
python -m evals.facet_coverage label --runs results/rep{1,2,3,4,5}
python -m evals.facet_gold_review --runs results/rep{1,2,3,4,5}
python -m evals.facet_coverage score \
  --runs results/rep{1,2,3,4,5} \
  --a <A> \
  --b <B>
```

`facet_gold_review` uses the metric's monotonicity to reduce human review. Adding a
supporting chunk can only change “not retrieved” to “retrieved,” so key points that
were retrieved in every observation do not affect the comparison. This reduced the
review set from 33 key points to 11.

Repeat the judge on identical outputs:

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

Rerank experiments use concurrency 1. A single request contains roughly 57
candidates and 11k input tokens; concurrency 2 caused 10 failures in a 36-question
run. A 12-question complex comparison with two configurations and five repeats
takes approximately 11 minutes in the measured environment.

## Tech Stack

Python 3.12+ · FastAPI · LangGraph 1.x · LangChain 1.x · PostgreSQL · pgvector ·
pg_trgm · Pydantic 2 · OpenAI models · pytest

## Relationship to CureAgent

- [cure-agent-be](https://github.com/Cure-Agent/cure-agent-be) is the production
  service and the source of truth for CureAgent's architecture and retrieval
  policy.
- This repository is the experimentation and LLM-evaluation harness. It ports the
  production retrieval path, tests proposed changes, and sends only supported
  interventions back to the product. Under the current evidence, the production
  path remains unchanged.
- [cure-agent-fe](https://github.com/Cure-Agent/cure-agent-fe) is the product-facing
  interface for streaming answers, citations, patient workflows, and conversation
  history.

## Repository Layout

```text
app/
  agent/graph.py                         # Graph wiring, routing, and PRESETS
  agent/nodes/                           # Typed LLM boundary nodes
  agent/nodes/answerer.py                # Answer generation and answerability gate
  agent/state.py                         # AgentState and structured output schemas
  retrieval/hybrid.py                    # Dense + lexical retrieval SQL
  retrieval/rrf.py                       # RRF fusion, behavior locked by tests
  retrieval/reranker.py                  # Ported listwise reranker
  retrieval/reranking_retriever.py       # Variant A: rerank each subquery
  retrieval/fused_reranking_retriever.py # Variant B: merge, then rerank once
  retrieval/factory.py                   # Retrieval-path composition
  llm/prompts.py                         # Node prompts and facet variants
  api/routes.py                          # POST /ask and /ask/stream (SSE)
evals/
  dataset.jsonl                          # 36-question evaluation set
  run_eval.py                            # Ablation runner with commit metadata
  judge.py                               # LLM-as-judge plus deterministic rules
  metrics.py                             # Single-run preset/category metrics
  repeat_metrics.py                      # Repeated paired metrics and bootstrap CI
  facet_coverage.py                      # Retrieval-layer key-point coverage
  facet_gold_review.py                   # Minimal evidence-label review set
  cut_sweep.py                           # Post-hoc rerank cutoff sweep
  facet_gold.json                        # Supporting-chunk labels
docs/experiments/                        # Immutable experiment records and verdicts
  evidence/paired_scores.csv             # Public anonymized binary scores
scripts/verify_public_results.py         # Recompute public effects, CIs, and sign tests
tests/                                   # Offline graph, routing, RRF, rerank, judge,
                                         # and control-integrity tests
```
