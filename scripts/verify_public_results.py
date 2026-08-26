#!/usr/bin/env python3
"""익명화된 공개 점수만으로 README의 실험 효과를 다시 계산한다.

원문 질문, 답변, 검색 chunk, 내부 ID는 읽지 않는다. 각 문항의 5회 성공 여부로
평균 정답률, 처리군-통제군 차이, 2단 bootstrap CI, 양측 부호검정을 계산한다.
"""

import argparse
import csv
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from math import comb
from pathlib import Path
from statistics import mean

BOOTSTRAP_SAMPLES = 10_000
SEED = 7
REPEATS = 5
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "docs/experiments/evidence/paired_scores.csv"

COMPARISONS = {
    "query_decomposition": ("rerank_topk11", "rerank_decomp_fused_topk11"),
    "evidence_topk": ("top-5", "top-11"),
    "facet_prompt": ("standard", "enumerate_facets"),
    "retrieval_loop": ("rerank", "rerank_full"),
}
EXPERIMENT_ORDER = {name: index for index, name in enumerate(COMPARISONS)}
JUDGE_ORDER = {"strict_1": 0, "strict_2": 1}
SCORE_FIELDS = tuple(
    [f"control_{index}" for index in range(1, REPEATS + 1)]
    + [f"treatment_{index}" for index in range(1, REPEATS + 1)]
)
FIELDS = ("experiment", "judge_pass", "case_id", *SCORE_FIELDS)


@dataclass(frozen=True)
class PairedScores:
    case_id: str
    control: tuple[int, ...]
    treatment: tuple[int, ...]


@dataclass(frozen=True)
class Summary:
    control_rate: float
    treatment_rate: float
    delta: float
    ci_low: float
    ci_high: float
    positive: int
    negative: int
    ties: int
    p_value: float


def load_scores(path: Path) -> dict[tuple[str, str], list[PairedScores]]:
    """CSV 스키마와 익명화·값 범위를 검증하고 비교 단위로 묶는다."""
    grouped: dict[tuple[str, str], list[PairedScores]] = defaultdict(list)
    seen: set[tuple[str, str, str]] = set()

    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValueError(f"unexpected CSV fields: {reader.fieldnames}")

        for line_number, row in enumerate(reader, start=2):
            experiment = row["experiment"]
            judge_pass = row["judge_pass"]
            case_id = row["case_id"]
            if experiment not in COMPARISONS:
                raise ValueError(f"line {line_number}: unknown experiment {experiment!r}")
            if judge_pass not in JUDGE_ORDER:
                raise ValueError(f"line {line_number}: unknown judge pass {judge_pass!r}")
            if re.fullmatch(r"case_[0-9]{2}", case_id) is None:
                raise ValueError(f"line {line_number}: non-anonymous case ID {case_id!r}")

            key = (experiment, judge_pass, case_id)
            if key in seen:
                raise ValueError(f"line {line_number}: duplicate row {key}")
            seen.add(key)

            try:
                values = tuple(int(row[field]) for field in SCORE_FIELDS)
            except ValueError as error:
                raise ValueError(f"line {line_number}: scores must be integers") from error
            if any(value not in (0, 1) for value in values):
                raise ValueError(f"line {line_number}: scores must be 0 or 1")

            grouped[(experiment, judge_pass)].append(
                PairedScores(
                    case_id=case_id,
                    control=values[:REPEATS],
                    treatment=values[REPEATS:],
                )
            )

    for key, rows in grouped.items():
        rows.sort(key=lambda row: row.case_id)
        expected = [f"case_{index:02d}" for index in range(1, len(rows) + 1)]
        actual = [row.case_id for row in rows]
        if actual != expected:
            raise ValueError(f"{key}: case IDs must be contiguous: {actual}")
    return dict(grouped)


def paired_bootstrap(
    rows: list[PairedScores], *, samples: int = BOOTSTRAP_SAMPLES, seed: int = SEED
) -> tuple[float, float]:
    """문항과 문항 내 실행을 함께 재표집하는 95% percentile CI."""
    rng = random.Random(seed)
    differences: list[float] = []
    for _ in range(samples):
        total = 0.0
        for _ in range(len(rows)):
            row = rows[rng.randrange(len(rows))]
            control = mean(row.control[rng.randrange(REPEATS)] for _ in range(REPEATS))
            treatment = mean(row.treatment[rng.randrange(REPEATS)] for _ in range(REPEATS))
            total += treatment - control
        differences.append(total / len(rows))
    differences.sort()
    return (
        differences[int(0.025 * samples)],
        differences[min(int(0.975 * samples), samples - 1)],
    )


def sign_test(differences: list[float]) -> tuple[int, int, int, float]:
    """문항별 평균 차이의 양측 정확 이항검정. 동점은 제외한다."""
    positive = sum(value > 0 for value in differences)
    negative = sum(value < 0 for value in differences)
    ties = len(differences) - positive - negative
    n = positive + negative
    if n == 0:
        return positive, negative, ties, 1.0
    tail = sum(comb(n, index) for index in range(min(positive, negative) + 1)) / 2**n
    return positive, negative, ties, min(1.0, 2 * tail)


def summarize(rows: list[PairedScores], *, samples: int, seed: int) -> Summary:
    control_rate = mean(mean(row.control) for row in rows)
    treatment_rate = mean(mean(row.treatment) for row in rows)
    differences = [mean(row.treatment) - mean(row.control) for row in rows]
    ci_low, ci_high = paired_bootstrap(rows, samples=samples, seed=seed)
    positive, negative, ties, p_value = sign_test(differences)
    return Summary(
        control_rate=control_rate,
        treatment_rate=treatment_rate,
        delta=treatment_rate - control_rate,
        ci_low=ci_low,
        ci_high=ci_high,
        positive=positive,
        negative=negative,
        ties=ties,
        p_value=p_value,
    )


def print_report(
    grouped: dict[tuple[str, str], list[PairedScores]], *, samples: int, seed: int
) -> None:
    print("Comparisons (control → treatment):")
    for experiment, (control, treatment) in COMPARISONS.items():
        print(f"- {experiment}: {control} → {treatment}")
    print()
    print("| experiment | judge | n×k | control | treatment | delta | 95% CI | sign (+/−/=), p |")
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    ordered = sorted(
        grouped,
        key=lambda key: (EXPERIMENT_ORDER[key[0]], JUDGE_ORDER[key[1]]),
    )
    for experiment, judge_pass in ordered:
        rows = grouped[(experiment, judge_pass)]
        result = summarize(rows, samples=samples, seed=seed)
        print(
            f"| {experiment} | {judge_pass} | {len(rows)}×{REPEATS} "
            f"| {result.control_rate:.3f} | {result.treatment_rate:.3f} "
            f"| {result.delta:+.3f} "
            f"| [{result.ci_low:+.3f}, {result.ci_high:+.3f}] "
            f"| {result.positive}/{result.negative}/{result.ties}, {result.p_value:.3f} |"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--samples", type=int, default=BOOTSTRAP_SAMPLES)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    if args.samples <= 0:
        parser.error("--samples must be positive")
    print_report(load_scores(args.csv), samples=args.samples, seed=args.seed)


if __name__ == "__main__":
    main()
