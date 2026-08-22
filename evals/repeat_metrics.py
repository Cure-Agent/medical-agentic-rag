"""반복 실행 집계 — 같은 구성을 k회 돌린 결과를 문항별 성공률로 접어 페어 비교한다.

    python -m evals.repeat_metrics --runs results/rep1 results/rep2 results/rep3 \
        --a rerank_topk11 --b rerank_decomp_fused_topk11

**왜 필요한가.** 1회 실행에서 문항당 관측은 베르누이 draw 하나다. 운영 패리티 모델
gpt-5.4-mini가 추론 모델이라 temperature를 고정할 수 없고, 실행마다 complex 2~4문항이
뒤집힌다(실측). 통제군 0.75 대 분해 0.83의 실체는 불일치쌍 3개(2:1)뿐이고 McNemar
정확검정 p=1.0이다 — 노이즈가 신호보다 크다. k회 반복해 문항별 성공률을 쓰면 문항 내
노이즈가 √k배 줄고, 남은 불확실성을 CI로 정직하게 보고할 수 있다.

**보고하는 것.**
- 문항별 성공률과 **흔들림 진단**(k회 중 일부만 성공한 문항 수) — 노이즈의 크기 자체가 결과다
- 페어 차이의 2단 부트스트랩 CI — 문항 표본과 실행 노이즈를 **둘 다** 재표집한다.
  문항만 재표집하면 문항별 성공률을 참값으로 취급하게 되어 CI가 좁게 나온다.
- 부호검정(정확 이항) — 1회 실행의 McNemar에 대응하는 반복실행 판
- kp 단위 커버리지 — 전부-아니면-전무보다 민감해서 같은 데이터에서 더 많이 뽑는다

`kind=error` 행은 관측에서 **뺀다**. API 실패를 오답으로 세면 구성이 아니라 그날의 TPM을
재게 된다. 뺀 개수는 실행 요약에 찍는다 — 조용히 줄이지 않는다.
"""

import argparse
import json
import random
from math import comb
from pathlib import Path
from statistics import mean

from evals.metrics import row_correct, row_kp_coverage

BOOTSTRAP_SAMPLES = 10_000
SEED = 7  # 재현성 — 부트스트랩 CI가 실행마다 달라지면 표에 옮겨 적을 수 없다

Observations = dict[str, list[float]]  # 문항 id → k회분 점수


def load_run(run_dir: Path, preset: str) -> dict[str, dict]:
    path = run_dir / f"{preset}.judged.jsonl"
    assert path.exists(), f"없는 파일: {path} — `python -m evals.judge --results {run_dir}` 먼저"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {row["id"]: row for row in rows}


def is_answerable(row: dict) -> bool:
    """kp 커버리지는 answerable 문항에만 정의된다 — insufficient는 key point가 없다."""
    return row["category"] != "insufficient"


def collect(run_dirs: list[Path], preset: str, score, *, keep=None) -> tuple[Observations, int]:
    """문항 id → 실행별 점수. 오류 행은 관측에서 빼고 그 개수를 함께 돌려준다."""
    runs = [load_run(d, preset) for d in run_dirs]
    ids = sorted({item_id for run in runs for item_id in run})
    obs: Observations = {}
    dropped = 0
    for item_id in ids:
        values = []
        for run in runs:
            row = run.get(item_id)
            if row is None:
                continue
            if keep is not None and not keep(row):
                continue
            if row.get("kind") == "error":
                dropped += 1
                continue
            values.append(float(score(row)))
        if values:
            obs[item_id] = values
    return obs, dropped


def paired_bootstrap(
    a_obs: Observations, b_obs: Observations, *, samples: int = BOOTSTRAP_SAMPLES, seed: int = SEED
) -> tuple[float, float]:
    """문항 → 실행 2단 재표집으로 (B - A) 평균 차이의 95% CI를 낸다.

    같은 문항에 두 구성이 모두 관측을 가진 경우만 쓴다 — 페어 비교라 짝이 맞아야 한다.
    """
    ids = sorted(i for i in a_obs if i in b_obs)
    n = len(ids)
    if n == 0:
        return (0.0, 0.0)
    rng = random.Random(seed)
    diffs = []
    for _ in range(samples):
        total = 0.0
        for _ in range(n):
            item_id = ids[rng.randrange(n)]
            a, b = a_obs[item_id], b_obs[item_id]
            ra = sum(a[rng.randrange(len(a))] for _ in range(len(a))) / len(a)
            rb = sum(b[rng.randrange(len(b))] for _ in range(len(b))) / len(b)
            total += rb - ra
        diffs.append(total / n)
    diffs.sort()
    return diffs[int(0.025 * samples)], diffs[min(int(0.975 * samples), samples - 1)]


def sign_test(diffs: list[float]) -> tuple[int, int, float]:
    """문항별 차이의 부호검정(양측 정확 이항). 동점 문항은 검정에서 빠진다."""
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    n = pos + neg
    if n == 0:
        return pos, neg, 1.0
    k = min(pos, neg)
    tail = sum(comb(n, i) for i in range(k + 1)) / 2**n
    return pos, neg, min(1.0, 2 * tail)


def stability(obs: Observations) -> tuple[int, int, int]:
    """(항상 성공, 항상 실패, 흔들림) 문항 수. 흔들림이 곧 문항 내 노이즈의 크기다."""
    always = sum(1 for v in obs.values() if all(x >= 1.0 for x in v))
    never = sum(1 for v in obs.values() if all(x <= 0.0 for x in v))
    return always, never, len(obs) - always - never


def print_item_table(a_obs: Observations, b_obs: Observations, a: str, b: str, k: int) -> None:
    print(f"\n## 문항별 성공률 (k={k})\n")
    print(f"| 문항 | {a} | {b} | 차이 |")
    print("|---|---|---|---|")
    for item_id in sorted(set(a_obs) | set(b_obs)):
        av, bv = a_obs.get(item_id, []), b_obs.get(item_id, [])
        ra = mean(av) if av else float("nan")
        rb = mean(bv) if bv else float("nan")
        mark = "  ←" if av and bv and abs(rb - ra) >= 0.5 else ""
        print(
            f"| {item_id} | {ra:.2f} ({sum(av):.0f}/{len(av)}) "
            f"| {rb:.2f} ({sum(bv):.0f}/{len(bv)}) | {rb - ra:+.2f}{mark} |"
        )
    print("\n`←` = k회의 과반이 뒤집히는 문항. 이 표에서 결론을 끌 만한 문항은 여기뿐이다.")


def print_comparison(label: str, a_obs: Observations, b_obs: Observations, a: str, b: str) -> None:
    ids = sorted(i for i in a_obs if i in b_obs)
    diffs = [mean(b_obs[i]) - mean(a_obs[i]) for i in ids]
    mean_a = mean(mean(a_obs[i]) for i in ids)
    mean_b = mean(mean(b_obs[i]) for i in ids)
    lo, hi = paired_bootstrap(a_obs, b_obs)
    pos, neg, p = sign_test(diffs)
    crosses_zero = lo <= 0.0 <= hi
    print(f"\n### {label}\n")
    print(f"- {a}: **{mean_a:.3f}**   |   {b}: **{mean_b:.3f}**   |   차이 {mean_b - mean_a:+.3f}")
    print(f"- 95% CI (2단 부트스트랩, n={len(ids)}문항): [{lo:+.3f}, {hi:+.3f}]")
    ties = len(ids) - pos - neg
    print(f"- 부호검정: B 우세 {pos}문항 / A 우세 {neg}문항 / 동점 {ties}문항 → p = {p:.3f}")
    verdict = (
        "CI가 0을 포함한다 — **차이를 주장할 수 없다.**"
        if crosses_zero
        else "CI가 0을 넘지 않는다 — 이 표본에서 차이는 실재한다."
    )
    print(f"- 판정: {verdict}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=Path, nargs="+", required=True, help="실행별 결과 디렉터리")
    parser.add_argument("--a", required=True, help="기준 구성 (예: rerank_topk11)")
    parser.add_argument("--b", required=True, help="비교 구성 (예: rerank_decomp_fused_topk11)")
    args = parser.parse_args()

    a_correct, a_dropped = collect(args.runs, args.a, lambda r: 1.0 if row_correct(r) else 0.0)
    b_correct, b_dropped = collect(args.runs, args.b, lambda r: 1.0 if row_correct(r) else 0.0)
    a_kp, _ = collect(args.runs, args.a, row_kp_coverage, keep=is_answerable)
    b_kp, _ = collect(args.runs, args.b, row_kp_coverage, keep=is_answerable)

    k = len(args.runs)
    print("## 실행 요약\n")
    print(f"- 실행 {k}회: {', '.join(str(d) for d in args.runs)}")
    print(f"- 문항: {args.a} {len(a_correct)}개 / {args.b} {len(b_correct)}개")
    print(f"- 관측에서 뺀 오류 행: {args.a} {a_dropped}건 / {args.b} {b_dropped}건")

    print("\n## 흔들림 진단\n")
    print("| 구성 | 항상 성공 | 항상 실패 | 흔들림 |")
    print("|---|---|---|---|")
    for name, obs in ((args.a, a_correct), (args.b, b_correct)):
        always, never, wobbly = stability(obs)
        print(f"| {name} | {always} | {never} | **{wobbly}** |")
    print("\n흔들림 문항이 많으면 1회 실행 비교는 그 문항들의 동전던지기를 본 것이다.")

    print_item_table(a_correct, b_correct, args.a, args.b, k)

    print("\n## 페어 비교")
    print_comparison("정답률 (카테고리에 맞는 올바른 동작)", a_correct, b_correct, args.a, args.b)
    print_comparison("key point 커버리지 (더 민감한 지표)", a_kp, b_kp, args.a, args.b)


if __name__ == "__main__":
    main()
