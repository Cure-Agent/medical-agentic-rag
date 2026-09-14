"""라우팅 평가 — 분류기 + 실행 경로 표의 **실행 경로** 정확도를 실제 모델로 잰다.

    .venv/bin/python -m evals.run_routing            # 전 문항 × 3회, results/routing/<시각>.jsonl
    .venv/bin/python -m evals.run_routing --runs 1 --only boundary

BE docs/specs/51의 131/132는 그때의 합성 44문항 ×3 + 운영 28문항 ×3인데 문항 원본과 프롬프트가
남아 있지 않다. 이 스크립트는 그 수치를 재현하지 않고 **이 레포에 고정된 평가셋**으로 기준선을 새로
잡는다. 정답은 분류기 판정 원문이 아니라 `plan_route`를 거친 실행 경로다 — 코드가 보증하는 것이
그것이다(스펙 「분류 경계」).

평가셋 (`evals/routing/*.jsonl`, 한 줄 한 문항):
  - boundary.jsonl      합성 경계 문항 — 특정 환자 기록·복합·조건 붙은 환자군·라벨 표기 변형·
                        라벨 2개·목록/집계·등록/수정/삭제·잡담/운영·지시어·지침 일반
  - demo_prompts.jsonl  FE 예시 질의문 — 운영 질문의 근사(스펙 41: 운영 USER 메시지 63건 중
                        placeholder 원문 9건, PATIENT_GUIDANCE 질문 37/37이 데모 추천 질의문)
  - evalset.jsonl       BE test/fixtures/rag-eval/evalset.json 229문항 — 전부 라벨 없는 지침 질문

행 형식: {"id", "category", "question", "expected_routes": [실행 경로 후보],
          "expected_label": 라벨|null}
  - expected_routes가 여럿이면 그중 하나면 정답이다(지시어 문항: OTHER만 아니면 된다).
  - expected_label은 대소문자 무시로 비교한다 — 분류기는 질문에 적힌 표기를 그대로 내야 한다.

출력:
  results/routing/<시각>.jsonl          호출 1건 1행 — 판정 원문·실행 경로·정오·지연
  results/routing/<시각>.summary.json   전체·범주별 정확도, 라벨 정확도, 문항 단위 일관성,
                                        지연, 토큰

추적은 서비스와 같은 `configure_tracing("true")`로 켠다(`LANGSMITH_API_KEY` 없으면 끔). traceId는
`routing-<시각>`이라 운영 턴과 섞이지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models import BaseChatModel

from app.service.llm import AGENT_MODEL, AgentModels, LlmCallError
from app.service.routing import CLASSIFIER_VERSION, classify, plan_route
from app.service.tracing import configure_tracing, flush_tracing, traced_turn

ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = ROOT / "evals" / "routing"
OUT_DIR = ROOT / "results" / "routing"
CONCURRENCY = 4


def load_dataset(only: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(DATASET_DIR.glob("*.jsonl")):
        if only and path.stem != only:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                row["source"] = path.stem
                rows.append(row)
    return rows


def code_version() -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
        )
        if head.returncode != 0:
            return "unknown"
        return head.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")
    except OSError:
        return "unknown"


def label_ok(expected: str | None, actual: str | None) -> bool:
    if expected is None:
        return actual is None
    return actual is not None and actual.strip().casefold() == expected.strip().casefold()


async def one_call(
    models: AgentModels,
    row: dict[str, Any],
    run: int,
    trace_id: str,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    async with sem:
        started = time.monotonic()
        try:
            decision = await classify(models.classifier(), row["question"], trace_id=trace_id)
        except LlmCallError as e:
            return {
                "id": row["id"],
                "run": run,
                "source": row["source"],
                "category": row["category"],
                "question": row["question"],
                "error": f"{e} / {e.__cause__!r}",
                "route_ok": False,
                "label_ok": False,
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        latency = round((time.monotonic() - started) * 1000)
    plan = plan_route(decision)
    return {
        "id": row["id"],
        "run": run,
        "source": row["source"],
        "category": row["category"],
        "question": row["question"],
        "decision": decision.model_dump(),
        "plan": {
            "route": plan.route,
            "case_label": plan.case_label,
            "abstain_reason": plan.abstain_reason,
        },
        "expected_routes": row["expected_routes"],
        "expected_label": row.get("expected_label"),
        "route_ok": plan.route in row["expected_routes"],
        "label_ok": label_ok(row.get("expected_label"), plan.case_label),
        "latency_ms": latency,
    }


def percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))
    return ordered[idx]


def summarize(rows: list[dict[str, Any]], runs: int) -> dict[str, Any]:
    def acc(items: list[dict[str, Any]], key: str) -> dict[str, Any]:
        ok = sum(1 for r in items if r[key])
        return {"ok": ok, "total": len(items), "rate": round(ok / len(items), 4) if items else None}

    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_source[r["source"]].append(r)
        by_category[r["category"]].append(r)
        by_id[r["id"]].append(r)

    # 문항 단위 — 전 회차 정답 / 일부만 정답(흔들림) / 전 회차 오답
    stable_ok = flaky = stable_bad = 0
    failures: list[dict[str, Any]] = []
    for qid, items in by_id.items():
        oks = [r["route_ok"] for r in items]
        if all(oks):
            stable_ok += 1
        elif any(oks):
            flaky += 1
        else:
            stable_bad += 1
        if not all(oks):
            failures.append(
                {
                    "id": qid,
                    "category": items[0]["category"],
                    "question": items[0]["question"],
                    "expected_routes": items[0].get("expected_routes"),
                    "got": [
                        (r.get("plan") or {}).get("route", "ERROR")
                        + "/"
                        + str((r.get("decision") or {}).get("route", "-"))
                        for r in items
                    ],
                    "labels": [(r.get("decision") or {}).get("patient_labels") for r in items],
                }
            )
    latencies = [r["latency_ms"] for r in rows if "error" not in r]
    return {
        "route_accuracy": acc(rows, "route_ok"),
        "label_accuracy": acc(rows, "label_ok"),
        "errors": sum(1 for r in rows if "error" in r),
        "by_source": {k: acc(v, "route_ok") for k, v in sorted(by_source.items())},
        "by_category": {k: acc(v, "route_ok") for k, v in sorted(by_category.items())},
        "per_question": {
            "runs": runs,
            "stable_ok": stable_ok,
            "flaky": flaky,
            "stable_bad": stable_bad,
        },
        "latency_ms": {
            "p50": percentile(latencies, 0.5),
            "p90": percentile(latencies, 0.9),
            "max": max(latencies) if latencies else 0,
        },
        "failures": sorted(failures, key=lambda f: f["id"]),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--only", help="evals/routing/<이름>.jsonl 하나만")
    parser.add_argument("--no-trace", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env", override=False)
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("OPENAI_API_KEY가 없다", file=sys.stderr)
        return 2
    traced = not args.no_trace and bool(os.environ.get("LANGSMITH_API_KEY"))
    tracing = configure_tracing("true" if traced else "")

    dataset = load_dataset(args.only)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    trace_id = f"routing-{stamp}"
    usage = UsageMetadataCallbackHandler()
    models = AgentModels(api_key)
    # with_config는 Runnable을 돌려주지만 bind()·ainvoke()를 그대로 갖는다 — classify는 그 둘만 쓴다
    classifier = cast(BaseChatModel, models.classifier().with_config(callbacks=[usage]))
    models = AgentModels(api_key, classifier=classifier)
    sem = asyncio.Semaphore(CONCURRENCY)
    print(
        f"{len(dataset)}문항 × {args.runs}회 = {len(dataset) * args.runs}호출 · {AGENT_MODEL} · "
        f"{CLASSIFIER_VERSION} · 추적 {'켬' if traced else '끔'} · traceId {trace_id}"
    )

    started = time.monotonic()
    with traced_turn(tracing):
        rows = await asyncio.gather(
            *(
                one_call(models, row, run, trace_id, sem)
                for run in range(1, args.runs + 1)
                for row in dataset
            )
        )
    await asyncio.to_thread(flush_tracing, tracing)
    elapsed = round(time.monotonic() - started, 1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{stamp}.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for r in sorted(rows, key=lambda r: (r["run"], r["id"])):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    summary = summarize(list(rows), args.runs)
    tokens = {
        model: {"input": u.get("input_tokens"), "output": u.get("output_tokens")}
        for model, u in usage.usage_metadata.items()
    }
    summary_doc = {
        "trace_id": trace_id,
        "model": AGENT_MODEL,
        "classifier_version": CLASSIFIER_VERSION,
        "code_version": code_version(),
        "dataset": {"questions": len(dataset), "runs": args.runs, "only": args.only},
        "elapsed_s": elapsed,
        "tokens": tokens,
        **summary,
    }
    (OUT_DIR / f"{stamp}.summary.json").write_text(
        json.dumps(summary_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ra, la = summary["route_accuracy"], summary["label_accuracy"]
    print(f"\n실행 경로 {ra['ok']}/{ra['total']} ({ra['rate']}) · 라벨 {la['ok']}/{la['total']}")
    pq = summary["per_question"]
    print(
        f"문항 단위 — 전 회차 정답 {pq['stable_ok']} · 흔들림 {pq['flaky']} · "
        f"전 회차 오답 {pq['stable_bad']}"
    )
    for k, v in summary["by_category"].items():
        print(f"  {k:28s} {v['ok']:4d}/{v['total']:<4d} {v['rate']}")
    lat = summary["latency_ms"]
    print(f"지연 p50 {lat['p50']}ms · p90 {lat['p90']}ms · 최대 {lat['max']}ms · 총 {elapsed}s")
    print(f"토큰 {tokens} · 오류 {summary['errors']}")
    if summary["failures"]:
        print("\n오답 문항:")
        for f_ in summary["failures"]:
            print(f"  {f_['id']} [{f_['category']}] 기대 {f_['expected_routes']} → {f_['got']}")
            print(f"    {f_['question']}")
    print(f"\n저장 {out.relative_to(ROOT)} · {out.with_suffix('.summary.json').name}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
