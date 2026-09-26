# Medical Agentic RAG

**English** · [한국어](README.ko.md)

> **CureAgent's agent service.** It classifies one clinician question, executes the
> matching path through the backend's internal APIs, and streams the result to the
> browser over SSE.

Two things live in this repository:

1. **The agent service** (`app/service/`) — production code. Every verified commit on
   `main` publishes a container image that
   [cure-agent-be](https://github.com/Cure-Agent/cure-agent-be) pulls and deploys.
2. **The Agentic RAG ablation study** — the research this repository started as. Its
   verdict fixed the production retrieval policy; the code at the end of the study is
   tagged `ablation-study` and the evidence stays in
   [docs/experiments/](docs/experiments/INDEX.md). See
   [Origins](#origins-the-agentic-rag-ablation-study).

[docs/architecture.md](docs/architecture.md) is the design document for this
repository. The cross-repo contract — response envelope, error-code registry, SSE
event schema, authentication — lives in
[cure-agent-be/docs/architecture.md](https://github.com/Cure-Agent/cure-agent-be/blob/main/docs/architecture.md)
and is deliberately not copied here.

## The Agent Service

### API surface

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/agent/healthz` | Process liveness only. It never calls the backend, so a backend outage cannot escalate into an agent restart or a deploy rollback. |
| `GET /api/v1/agent/me` | Relays the received `Cookie` and CSRF header to the backend's `GET /api/v1/auth/me` and returns that verdict unchanged. The success payload carries `clinicianId` and `clinicId` only. |
| `POST /api/v1/agent/conversations/{conversationId}/messages/stream` | One agent turn as SSE. |
| `GET /metrics` | Prometheus exposition, deliberately **outside** the `/api/v1/agent` router prefix, so the nginx rule that forwards that prefix never exposes it. |

To the browser there is one API surface, `/api/v1`. Production nginx forwards only
`/api/v1/agent/` to this service and everything else to the backend. The agent calls
the backend directly, bypassing nginx (`http://app:3000`); the internal APIs it uses
(`/api/v1/internal/agent/…`) are blocked with 404 at nginx and omitted from the
backend's OpenAPI document.

### Before the stream opens

Three checks run before any LLM call, and each failure is a plain response envelope:

1. **Body validation** — `content` 1–4000 chars, `clientRequestId` 1–100 chars, and an
   optional `responseLang` of `ko` or `en`. Otherwise 422 `VALIDATION_FAILED`. A
   conversation id that is not backend-id-shaped is 404 `NOT_FOUND` without asking the
   backend.
2. **Access-token pre-check** — the `access_token` cookie is read as a JWT *without*
   signature verification to look at `exp` and `iat`. If the remaining lifetime is
   shorter than the run deadline (150 s), the request is 401 `AUTH_TOKEN_EXPIRED` and
   the backend is never called. Tokens whose *total* lifetime is at or below the
   deadline are exempt — rejecting those would put the frontend in a refresh loop.
3. **Acceptance** — `POST …/internal/agent/conversations/{id}/turns`. This single call
   settles authentication, clinic scope, CSRF, and duplicate detection, which is why it
   comes **before** the classifier: an unauthenticated request cannot burn LLM cost. A
   non-201 response is relayed verbatim; no response at all is 502
   `AGENT_BACKEND_UNAVAILABLE`.

The `assistantMessageId` returned by acceptance is the anchor for disconnect recovery.

### Classification and the route table

**Code owns the label dimension; the LLM owns the meaning dimension.** The classifier
emits `{route, patient_labels}` under a strict JSON schema, and a pure function
(`routing.plan_route`) turns that into the executed path. Moving the label rules into
the prompt measurably degraded classification, so they stay in the table.

| Classifier route | Labels | Executed path | Outcome |
|---|---|---|---|
| `GUIDELINE` | 0 | guideline | backend guideline tool |
| `GUIDELINE` | 1 | composite | |
| `PATIENT` | 1 | patient | `NOT_FOUND`/`AMBIGUOUS` from the patient tool → ABSTAINED `patient_unresolved` |
| `PATIENT` | 0 | patient | abstains with `patient_unresolved`, no tool call |
| `COMPOSITE` | 1 | composite | patient resolution failure as above |
| `COMPOSITE` | 0 | guideline | |
| any | ≥ 2 | other | abstains with `out_of_scope`, no tool call |
| `OTHER` | — | other | abstains with `out_of_scope`, no tool call |

Labels are trimmed, emptied entries dropped, and counted case-insensitively. A
specific patient is addressed only by the case label written in the question.

### The four paths

```text
common     message.accepted → classify → agent.progress{stage: "routed", route}
guideline  relay the guideline tool's SSE events verbatim — never calls finish
patient    patient tool → agent.progress{patient_loaded} → answer.delta*
             → [finish COMPLETED] → answer.completed
composite  patient tool → evidence tool: retrieval.started → retrieval.progress*
             → (answer.started if the gate passes) → retrieval.evidence×N
             → retrieval.completed → answer.delta* (only after the verdict)
             → [finish: the backend structures and stores the clinical guidance]
             → answer.completed{message, guidance} | answer.abstained
other      [finish ABSTAINED] → answer.abstained
```

- **Composite answers are judged by whoever writes them.** The evidence tool stops at
  the relevance gate; the agent then generates the answer and decides answerability in
  one structured call. No delta is streamed until the verdict field closes
  (`synthesis.VerdictFirstParser`).
- **The clinical guidance is relayed, not authored.** The backend's finish call
  structures, validates, and stores it in the same transaction as the answer; the agent
  lifts `guidance` out of the finish response into `answer.completed{message, guidance}`.
  Only a finish that produced guidance carries that key, so the event omits it otherwise.
- Citations are the `[n]` markers that survive in the final answer, mapped to the id of
  the n-th `retrieval.evidence` frame.
- A terminal event is sent only **after** the finish response arrives. Abstention is a
  normal terminal state.

### Backend internal APIs

Every call carries the received `Cookie` verbatim plus `X-CSRF-Protection` **only when
it was received**. No other request header is forwarded, and the agent never mints a
credential of its own.

| Call | Path (`POST …/internal/agent/…`) | Body |
|---|---|---|
| guideline tool | `turns/{assistantMessageId}/guideline-answer` | `{classifierVersion}` |
| patient tool | `turns/{assistantMessageId}/patient` | `{caseLabel}` |
| evidence tool | `turns/{assistantMessageId}/guideline-evidence` | `{query}` |
| finish | `turns/{assistantMessageId}/finish` | `{status, route?, classifierVersion, …}` |

**One owner closes each turn.** The guideline tool persists its own answer through the
chat pipeline, so the agent never calls finish on that path — not even on failure or
disconnect. Every other path is closed by the agent's finish call: failures as `FAILED`,
client disconnects as `CANCELLED`. The evidence tool does not change turn state.

Timeouts: 5 s for JSON calls, 5 s connect / 30 s read for internal SSE and for finish
(the backend structures guidance inside finish), a 150 s run deadline measured from
request arrival, a 15 s SSE heartbeat, and 45 s for the first LLM response.

### Tracing

LangSmith tracing is off unless `AGENT_TRACING_ENABLED=true`, because the SDK's
environment variables can be switched on by a `true` in either namespace and cannot be
switched back off by `false` — one line in an `.env` file would otherwise enable tracing
silently. The switch is pinned once at startup.

Once a turn is routed to the patient or composite path, the **whole branch** runs under
a hiding client rather than selected calls, since hiding only the patient tool's output
would let the same record reappear inside the synthesis prompt. The project name is
re-applied per turn: `langsmith.configure(project_name=…)` only reaches the calling
task's contextvar, and a value set in the lifespan never reached the request task — in
production that sent classifier runs to the `default` project.

## Running the Service

Python 3.12+ and [uv](https://docs.astral.sh/uv/). Dependencies are pinned by
`uv.lock`, which CI and the image both install from.

```bash
uv sync
BE_ORIGIN=http://localhost:3000 OPENAI_API_KEY=sk-... \
  .venv/bin/uvicorn app.service.main:create_app --factory --port 8000
```

`ServiceSettings` reads **process environment variables only** — `BE_ORIGIN`,
`AGENT_TRACING_ENABLED`, `OPENAI_API_KEY` — and never an `.env` file, so no file can
quietly change the backend address or the tracing switch. The app starts without an API
key; `healthz` and `/metrics` are unaffected and a stream ends in `LLM_UNAVAILABLE`
after acceptance.

The default model for both the classifier and synthesis is `gpt-5.4-mini`, matching the
backend.

## Verification Commands

`Makefile` is the single source of these commands; CI (`.github/workflows/ci.yml`) and
the SDD harness (`automation/*.md`) call the same targets.

| Command | What it runs | CI job |
|---|---|---|
| `make lint` | `ruff check` | `lint` |
| `make typecheck` | pyright, standard mode, Python 3.12, over `app`·`evals`·`tests`·`scripts` | `typecheck` |
| `make test` | pytest, excluding `tests/e2e/` | `test` |
| `make test-e2e` | pytest `tests/e2e/` — container tests, Docker required | `test-e2e` |
| `make build` | builds the service image locally | — |
| — | gitleaks history scan | `gitleaks` |

Unit tests run without an LLM, an embedding model, or a database. External boundaries
are faked: the backend over an `httpx.MockTransport` that records requests, the
classifier and synthesis models as LangChain fake chat models, and the run deadline and
heartbeat interval injected — all through
`create_app(backend_transport=…, classifier_model=…, synthesis_model=…)`. CI runners have
no `.env`, so a test that leaks onto a real boundary fails there.

Two kinds of test need more than that. Anything that configures a process global —
LangSmith caches env lookups and `configure` mutates the process — is decided in a child
process, and trace hiding is asserted on the bytes that actually leave the process
against a capture server, each "absent" assertion paired with a positive control in the
same capture. Mid-stream behavior (no delta before the verdict, `CANCELLED` on
disconnect) drives ASGI directly, because `TestClient` buffers the response until the
app returns.

## Deployment

`main` push → all verification jobs pass → the `image` job builds
`ghcr.io/cure-agent/medical-agentic-rag` for `linux/amd64` and pushes `latest` plus the
run number. The job `needs` every other job, because a broken `latest` fails the
backend's deploy.

**The backend owns server deployment.** This repository's pipeline ends at the `main`
merge; cure-agent-be pulls the published image. The image installs from `uv.lock` with
`--no-dev`, carries `app/service` only — no experiment code, no psycopg retrieval path —
runs as a non-root user, and hard-codes no tracing variables.

## Evaluating the Agent

These runs call real models and write to the gitignored `results/`.

```bash
.venv/bin/python scripts/smoke_llm.py           # 4 real calls through the service code path
.venv/bin/python -m evals.run_routing           # executed-path accuracy, 303 questions × 3
.venv/bin/python -m evals.run_synthesis         # synthesis generation + LLM judge
```

- `scripts/smoke_llm.py` exercises what fake chat models cannot: that `gpt-5.4-mini`
  accepts the strict `response_format` binding, that stream-end usage arrives, and that
  the hand-written incremental parser survives real chunk boundaries.
- `evals/routing/` holds the fixed routing set — 65 boundary questions, the 9 frontend
  demo prompts, and the backend's 229-question eval set. It is graded on the executed
  path after `plan_route`, not on the classifier's raw verdict, because that is what the
  code guarantees.
- `evals/synthesis/` holds 12 synthetic patient records, 36 patient-path questions, and
  40 composite items whose evidence is real corpus text (8 of them must abstain).
  Generation uses the service functions themselves.

Scores are not quoted here: `results/` is gitignored and overwritten, and the synthesis
judge has a measured false-positive rate high enough that its output needs review before
use. A run worth keeping is fixed as a dated record instead — the routing run on the
boundary set, scored on both the raw verdict and the executed path, is in
[docs/experiments/2026-09-14-routing-boundary.md](docs/experiments/2026-09-14-routing-boundary.md).

## Origins: the Agentic RAG Ablation Study

This repository began as an ablation study asking **when Agentic RAG actually helps.**
It ported CureAgent's hybrid retrieval path, then isolated each proposed agentic
intervention so it could be evaluated before reaching production.

> **Verdict (2026-08-24):** none of the tested interventions met the rollout bar, so the
> production retrieval path stays as it is — one original query → hybrid retrieval → RRF
> over the untruncated union → LLM reranker → top 5 → score gate. This is not evidence
> that Agentic RAG never helps; it is a decision based on the observed sample,
> uncertainty, mechanism, and cost.

The decision rests on **12 complex questions × five pipeline runs**. Values separated by
`/` are two judge passes over the same outputs.

| Intervention | Observed effect | Decision |
|---|---|---|
| Query decomposition (variant B) | Accuracy **+0.100 / +0.117**, 95% CIs [−0.150, +0.350] / [−0.117, +0.367]; sign counts 4:3 | No rollout — per-question effects ranged −0.60 to +0.80 and the retrieval-layer mechanism stayed unclear |
| More context, top 5 → top 11 | Accuracy **−0.083 / −0.083**, 95% CI [−0.267, +0.067] | No reproducible gain; it traded failures rather than removing them |
| Facet-enumeration answer prompt | Accuracy **−0.067 / −0.083**; sign counts 0:4 / 0:5 | No benefit, possibly adverse |
| Iterative retrieval (`rerank_full`) | Accuracy **+0.050**; sign count 2:2, 95% CI [−0.117, +0.250]; only 12 of 60 rows entered the loop | Not worth the cost under this evidence |

The largest observed improvement came from **reranking** (complex accuracy 0.17 → 0.50),
but it is exploratory: it came from the superseded k=1 ablation. Its real value was
exposing a design error — reranking had been excluded to isolate query understanding,
yet production already reranks, so gains measured against a no-reranker baseline cannot
justify a production change.

**Relevance and answerability need separate gates.** A retrieval score controls
generation cost; it cannot decide whether evidence answers a multi-part question. The
evaluation saw evidence rated 10/10 for relevance that still could not answer the
question. That separation is why the composite path in the service above judges
answerability where the answer is written.

### What the measurements taught

Each completed experiment has an immutable document in
[docs/experiments/](docs/experiments/INDEX.md): observed numbers are never rewritten, a
later result adds a `superseded` banner, and a factual or statistical correction adds an
`erratum` banner. Failed comparisons are kept as evidence of how an evaluation goes wrong.

1. **A single run is not an experiment.** The first control scored 0.75; its five-run
   estimate was 0.600. Non-determinism started in the reranker, not only in generation —
   for identical inputs the top-five set changed in 21 of 36 cases.
2. **A comparison without a commit hash may be invalid.** Eight rows of the first
   ablation came from different code epochs. `run_eval` now records `code_version` on
   every row.
3. **Answer accuracy cannot separate retrieval failure from generation failure.**
   Supporting-chunk coverage has to be measured independently at the key-point level.
4. **Control configurations fail silently.** A context-size control once collapsed into
   the production control; a prompt flag once failed to reach its node. Both paths are
   now locked by tests.
5. **The evaluator must be repeated too.** Across 639 key-point decisions, four changed
   between judge passes (0.6%) and no abstention decision changed — that is what allowed
   a measurement correction to be told apart from evaluator noise.
6. **Writing a rule in a prompt does not enforce it.** A recommendation-grade rule was
   violated 10/10 times for one key point; grades are now extracted first and reconciled
   deterministically in code (`evals/judge.py::reconcile_grade`).
7. **LLM labelers match format instead of content.** Repeated recommendation templates
   made the labeler pick evidence for the wrong intervention when the excerpt omitted
   the leading subject — 5 of 33 key points were mislabeled that way.

Statistical significance was not the sole rollout criterion. With five discordant pairs
even a 5:0 split has a minimum two-sided sign-test p-value of 0.0625; the observed sign
counts, CIs containing zero, question-level heterogeneity, mechanism, and cost together
did not support a rollout. `cpx-010` failed in all five runs under every configuration
and remains unexplained. Full limitations are in
[docs/experiments/INDEX.md](docs/experiments/INDEX.md).

### Reproducing the study

The repository publishes the authored questions and expected key points, but not
generated answers, retrieved chunks, source-document ids, or the vector database. The
aggregate effects are still reproducible without them:

```bash
python scripts/verify_public_results.py   # stdlib only
```

It recomputes mean differences, two-stage bootstrap CIs, and two-sided sign tests from
[anonymized paired scores](docs/experiments/evidence/paired_scores.csv).

Running the ablations needs the study's own environment: a PostgreSQL database with
`pgvector` and `pg_trgm` holding the read-only corpus export in the `hybrid_probe`
scratch database, plus `.env` (see `.env.example`). The experiment app is separate from
the service app:

```bash
.venv/bin/uvicorn app.main:app --reload
# POST /ask {"question": "...", "preset": "prod_rerank"}

python -m evals.run_eval --presets rerank,rerank_facets --categories complex --out results/run1
python -m evals.judge   --results results/run1
python -m evals.metrics --results results/run1
```

`prod_rerank` is the default and matches production: one original query, top five
chunks, rerank cutoff 9. The historical `rerank` preset keeps cutoff 3.5 so the
2026-08-23 and 2026-08-24 results stay reproducible.

A five-repeat paired comparison, a post-hoc cutoff sweep, and retrieval-layer facet
coverage:

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

- `--run-cut` must match the cutoff the run actually used, and the run's cutoff must sit
  below the range being reconstructed. Lower cutoffs cannot be reconstructed, because
  gated questions never reached the answer node and counting those as abstentions would
  make low cutoffs look artificially safe. Sweep all categories: insufficient and
  answerable questions pull in opposite directions.
- `facet_gold_review` exploits the metric's monotonicity — adding a supporting chunk can
  only flip "not retrieved" to "retrieved" — which cut the human review set from 33 key
  points to 11.
- Rerank experiments use concurrency 1. One request is roughly 57 candidates and 11k
  input tokens; concurrency 2 produced 10 failures in a 36-question run.

## Relationship to CureAgent

CureAgent is **one spec repository and three implementation repositories**.

- [cure-agent-be](https://github.com/Cure-Agent/cure-agent-be) — the production server,
  the home of `docs/specs/`, and the source of truth for architecture and retrieval
  policy. It also deploys this service's image.
- [cure-agent-fe](https://github.com/Cure-Agent/cure-agent-fe) — the product-facing
  interface for streaming answers, citations, patient workflows, and conversation
  history.
- **This repository** — the agent service, plus the experimentation and LLM-evaluation
  harness it grew out of.

Specs live in the backend repository; this repository implements the acceptance criteria
labeled `(AGENT)` and writes no specs of its own. `scripts/fetch_spec.py` fetches a spec
by number, and `automation/` holds the procedures — test freezing, routing between
`/implement` and `/problem`, and shipping — with `.claude/commands/` and `.codex/skills/`
as harness-specific adapters.

## Repository Layout

```text
app/service/                             # THE SERVICE — the only code in the image
  main.py                                # create_app · lifespan (tracing switch, BE client, turn settings)
  routes.py                              # healthz · me · POST …/messages/stream
  turn.py                                # one turn: classify → execute path → finish · heartbeat · disconnect
  routing.py                             # classifier (strict JSON) · route table (pure) · search-input label stripping
  synthesis.py                           # patient/composite synthesis · verdict-first parser · markers → citations
  backend.py                             # BE client — forwards received credentials only, no response → 502
  access_token.py                        # access-JWT lifetime pre-check (exp·iat, signature not verified)
  sse.py · envelope.py · config.py       # SSE frames · §10.1 envelope + ULID traceId · env-only settings
  llm.py · tracing.py                    # chat models behind BaseChatModel · LangSmith switch + hiding client
app/                                     # THE STUDY — experiment app, not in the image
  main.py · api/routes.py                # experiment app: /healthz · POST /ask · /ask/stream
  agent/graph.py · agent/state.py        # AgentConfig · PRESETS · pure routing functions · node schemas
  agent/nodes/                           # decompose · retrieve · evaluate · generate_queries · answer · abstain
  retrieval/hybrid.py · rrf.py           # dense (pgvector) + lexical (pg_trgm) SQL · RRF fusion, K=60, untruncated
  retrieval/reranker.py                  # ported listwise reranker
  retrieval/reranking_retriever.py       # variant A: rerank each subquery
  retrieval/fused_reranking_retriever.py # variant B: merge, then rerank once
  retrieval/factory.py                   # retrieval-path composition — one wiring point for API and evals
  config.py · llm/prompts.py             # experiment settings, policy_version_for · node prompts and facet variants
evals/
  run_routing.py · routing/              # executed-path accuracy · boundary · demo prompts · 229-question set
  run_synthesis.py · synthesis/          # synthesis judge · patients · patient questions · composite items
  run_eval.py · judge.py · metrics.py    # ablation runner with commit metadata · LLM judge + deterministic rules
  repeat_metrics.py · cut_sweep.py       # repeated paired metrics and bootstrap CI · post-hoc cutoff sweep
  facet_coverage.py · facet_gold*.json   # retrieval-layer key-point coverage · supporting-chunk labels
  dataset.jsonl                          # 36-question evaluation set
tests/                                   # offline unit tests (service, graph, routing, RRF, rerank, judge, tracing)
tests/e2e/                               # container tests — builds and runs the image, fails if Docker is absent
scripts/                                 # smoke_llm.py · fetch_spec.py · verify_public_results.py
docs/architecture.md                     # this repository's design document
docs/experiments/                        # immutable experiment records and verdicts
automation/ · .claude/ · .codex/         # SDD harness: procedures + per-harness adapters
Dockerfile · Makefile · uv.lock          # service image · verification commands · dependency lock
```

## Tech Stack

Python 3.12+ · FastAPI · Starlette SSE · httpx · LangChain 1.x · LangSmith ·
prometheus-client · Pydantic 2 · OpenAI models · uv · Docker · pytest · pyright · ruff

The study path additionally uses LangGraph 1.x, PostgreSQL, pgvector, and pg_trgm. The
service path uses none of them — it holds no database pool and imports no experiment
code.
