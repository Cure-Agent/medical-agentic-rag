"""결과 집계 — results/<preset>.jsonl을 읽어 preset × category 표를 만든다.

    python -m evals.metrics --results results

기계적 지표(기권율·라운드 수·인용 수)는 원 결과에서, 정답률·kp 커버리지는
`python -m evals.judge`가 만든 <preset>.judged.jsonl에서 낸다.

정답률의 의미는 카테고리별로 "올바른 동작률"로 통일한다:
- simple/complex: 기권하지 않고 key_points를 전부 담은 비율
- insufficient: effective 기권율 (kind=answer라도 본문 거부면 기권으로 센다)
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

CATEGORIES = ["simple", "complex", "insufficient"]


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_results(results_dir: Path) -> dict[str, list[dict]]:
    by_preset: dict[str, list[dict]] = {}
    for path in sorted(results_dir.glob("*.jsonl")):
        if path.name.endswith(".judged.jsonl"):
            continue
        by_preset[path.stem] = _load_jsonl(path)
    return by_preset


def load_judged(results_dir: Path) -> dict[str, list[dict]]:
    return {
        path.name.removesuffix(".judged.jsonl"): _load_jsonl(path)
        for path in sorted(results_dir.glob("*.judged.jsonl"))
    }


def summarize(rows: list[dict]) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["category"]].append(row)

    summary = {}
    for category, items in grouped.items():
        n = len(items)
        abstained = sum(1 for r in items if r["kind"] == "abstain")
        errors = sum(1 for r in items if r["kind"] == "error")
        answered = [r for r in items if r["kind"] == "answer"]
        summary[category] = {
            "n": n,
            "abstain_rate": abstained / n if n else 0.0,
            "errors": errors,
            "avg_retrieval_rounds": (
                sum(r.get("retrieval_count", 0) for r in items) / n if n else 0.0
            ),
            "avg_citations": (
                sum(len(r.get("citations", [])) for r in answered) / len(answered)
                if answered
                else 0.0
            ),
        }
    return summary


def print_table(by_preset: dict[str, list[dict]]) -> None:
    # insufficient 카테고리의 지표는 정답률이 아니라 **올바른 기권율**이다
    print("| preset | category | n | abstain율 | 평균 재검색 라운드 | 평균 인용 수 | 오류 |")
    print("|---|---|---|---|---|---|---|")
    for preset, rows in by_preset.items():
        summary = summarize(rows)
        for category in CATEGORIES:
            if category not in summary:
                continue
            s = summary[category]
            print(
                f"| {preset} | {category} | {s['n']} | {s['abstain_rate']:.2f} "
                f"| {s['avg_retrieval_rounds']:.2f} | {s['avg_citations']:.2f} | {s['errors']} |"
            )


def row_correct(row: dict) -> bool:
    """judged 행 1건이 "카테고리에 맞는 올바른 동작"인가.

    집계 지점이 둘(여기, evals.repeat_metrics)이라 정의를 한 곳에만 둔다 — 갈라지면
    같은 결과 파일에서 서로 다른 정답률이 나온다.
    """
    if row["category"] == "insufficient":
        return row.get("effective_kind") == "abstain"
    covered = row.get("key_points_covered") or []
    return row.get("effective_kind") == "answer" and bool(covered) and all(covered)


def row_kp_coverage(row: dict) -> float:
    """key point 커버리지. 기권·거부는 0으로 센다 — 답을 안 한 것이지 부분 정답이 아니다."""
    covered = row.get("key_points_covered") or []
    return sum(covered) / len(covered) if covered else 0.0


def summarize_judged(rows: list[dict]) -> dict[str, dict]:
    """judged 행을 category별로 집계한다. correct의 의미는 모듈 docstring 참고."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["category"]].append(row)

    summary = {}
    for category, items in grouped.items():
        n = len(items)
        effective_abstain = sum(1 for r in items if r.get("effective_kind") == "abstain")
        correct = sum(1 for r in items if row_correct(r))
        if category == "insufficient":
            kp_coverage = None
        else:
            kp_coverage = sum(row_kp_coverage(r) for r in items) / n if n else 0.0
        summary[category] = {
            "n": n,
            "correct_rate": correct / n if n else 0.0,
            "effective_abstain_rate": effective_abstain / n if n else 0.0,
            "kp_coverage": kp_coverage,
        }
    return summary


def print_judged_table(judged_by_preset: dict[str, list[dict]]) -> None:
    print()
    print("| preset | category | n | 정답률(올바른 동작) | kp 커버리지 | effective 기권율 |")
    print("|---|---|---|---|---|---|")
    for preset, rows in judged_by_preset.items():
        summary = summarize_judged(rows)
        for category in CATEGORIES:
            if category not in summary:
                continue
            s = summary[category]
            coverage = f"{s['kp_coverage']:.2f}" if s["kp_coverage"] is not None else "-"
            print(
                f"| {preset} | {category} | {s['n']} | {s['correct_rate']:.2f} "
                f"| {coverage} | {s['effective_abstain_rate']:.2f} |"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=Path("results"))
    args = parser.parse_args()
    print_table(load_results(args.results))
    judged = load_judged(args.results)
    if judged:
        print_judged_table(judged)
    else:
        print("\n(judged 파일 없음 — `python -m evals.judge`를 먼저 실행하면 정답률 표가 추가된다)")
