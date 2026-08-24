"""리랭크 점수 컷 사후 스윕 — 실행 하나로 컷 전 구간의 손익을 재구성한다.

    python -m evals.cut_sweep --runs results --preset rerank --run-cut 3.5

**왜 사후 계산이 성립하는가.** `rerank_score_cutoff`는 라우팅에만 쓰인다 —
`app/agent/graph.py`의 `route_gate` 한 곳이다. 검색 경로에는 들어가지 않는다:
`factory.make_retriever`가 리트리버에 넘기는 값은 `top_k`와 `distance_cutoff`뿐이다.
컷을 바꿔도 후보군·리랭크 점수·answerer가 보는 근거 풀이 전부 같고, 달라지는 것은
「그 점수로 answer 노드에 갈 것인가」 하나다. 그래서 컷 c로 돌린 결과에서
**c 이상의 임의의 컷**을 정확히 재구성할 수 있다.

**c 미만은 재구성할 수 없다.** 컷에 걸린 문항은 answerer를 거치지 않아 생성 게이트의
판정이 존재하지 않는다. 그래서 `--run-cut`을 필수로 받고, 그보다 낮은 컷 요청은 거부한다 —
비어 있는 칸을 「기권」으로 채우면 낮은 컷이 실제보다 안전해 보인다.

**왜 별도 실행 비교보다 나은가.** 컷 3.5와 컷 9를 따로 돌려 비교하면 리랭커
비결정성이 섞인다 — v1에서 cpx-007이 한 실행 8.0, 다른 실행 10.0이었고 컷 9는 그
흔들림 지대 한복판이라 컷의 효과와 실행 노이즈를 분리할 수 없었다. 사후 스윕은 같은
점수·같은 답변에 컷만 갈아끼우므로 문항 단위로 완전히 짝지어진 비교다.

**이 도구가 답하지 못하는 것.** 여기서 나오는 컷은 `evals/dataset.jsonl` 36문항 위의
값이지 운영 컷이 아니다. 운영 컷은 운영 히스토그램(질문 분포가 다르다) 위에 생성
게이트를 얹어 다시 재야 한다. 이 표가 말할 수 있는 것은 **기전**이다 — 「생성 게이트가
있으면 검색 게이트를 낮춰도 누출이 늘지 않는가」.
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

from evals.metrics import row_correct
from evals.repeat_metrics import paired_bootstrap

# 리랭커 자가보고 점수의 상한. 그리드 기본값을 만들 때만 쓴다.
MAX_SCORE = 10.0

# 문항 1건의 호출 비용(USD). **점수 게이트의 존재 이유가 비용 절감이므로 그 절감을
# 직접 보여준다** — 건수만 세면 「26/36 호출」이 얼마를 아낀 것인지 알 수 없다.
#
# 핵심은 두 값의 비율이다: 점수 게이트는 리랭크 **뒤**에 있으므로 아낄 수 있는 것은
# answerer 호출 하나뿐이고, 리랭크 비용은 게이트에 걸린 문항도 이미 지출한 뒤다.
# 절감의 상한이 문항당 비용의 1/3로 구조적으로 묶인다.
#
# 2026-08-24 프로브 실측(cpx-001, 후보 59개, gpt-5.4-mini, reasoning 0):
#   리랭크   입력 11,733t × $0.75/1M + 출력 ~30t × $4.50/1M
#   answerer 입력  3,077t × $0.75/1M + 출력 447t × $4.50/1M
# 단가나 모델이 바뀌면 프로브를 다시 돌려 이 두 값을 갱신할 것.
RERANK_COST_USD = 0.00894
ANSWERER_COST_USD = 0.00432


def load_judged(run_dir: Path, preset: str) -> list[dict]:
    path = run_dir / f"{preset}.judged.jsonl"
    assert path.exists(), f"없는 파일: {path} — `python -m evals.judge --results {run_dir}` 먼저"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def top1_score(row: dict) -> float | None:
    """행의 검색 게이트 입력값. None이면 점수 게이트가 성립하지 않은 행이다.

    `route_gate`가 보는 것과 같은 값이어야 한다 — 분해 구성은 하위 질의마다 점수가
    하나씩 나오고 게이트는 **최댓값**을 본다(`decide_rerank_gate`). 점수가 하나도
    없으면(전 질의 리랭크 실패) 실제 라우팅은 거리 게이트로 폴백하므로, 그 행은
    컷과 무관하다 — 여기서 None을 돌려주고 호출측이 원 결과를 그대로 쓴다.
    """
    scores = [v for v in (row.get("rerank_scores") or {}).values() if v is not None]
    return max(scores) if scores else None


def rethreshold(row: dict, cut: float) -> dict:
    """judged 행을 컷 `cut`에서의 결과로 다시 쓴다.

    검색 게이트에 걸리면 answerer가 아예 호출되지 않으므로 kp 커버리지는 빈 리스트다
    (`metrics.row_kp_coverage`가 0으로 센다). 기권 본문은 `ABSTAIN_TEMPLATE`라
    결정론적이고 judge도 LLM 없이 채점하므로(`judge.judge_one`), 재채점이 필요 없다.
    """
    score = top1_score(row)
    if score is None or score >= cut:
        return row
    return {**row, "kind": "abstain", "effective_kind": "abstain", "key_points_covered": []}


def gate_layer(row: dict, cut: float) -> str:
    """이 행을 **어느 층이** 처리했는가. 컷 손익의 정체가 여기서 드러난다.

    - retrieval: 검색 게이트(점수 컷)가 잡음 — answerer 호출이 나가지 않는다
    - generation: 검색 게이트를 통과했지만 answerer가 근거로 답할 수 없다고 판정
    - answered: 답변이 나감
    - fallback: 리랭크 점수가 없어 거리 게이트가 판정한 행 (컷과 무관)
    """
    score = top1_score(row)
    if score is None:
        return "fallback"
    if score < cut:
        return "retrieval"
    return "generation" if row.get("effective_kind") == "abstain" else "answered"


def default_grid(rows: list[dict], run_cut: float) -> list[float]:
    """관측된 점수 자체를 컷 후보로 쓴다 — 게이트가 `>= cut`이라 「s점은 통과」로 읽힌다.

    리랭커 자가보고는 정수라 그 사이 값은 같은 분할을 만든다. 컷을 스윕한다면서
    아무 데도 경계가 놓이지 않는 값을 늘어놓아 봐야 표만 길어진다.
    """
    observed = {s for s in (top1_score(r) for r in rows) if s is not None and s > run_cut}
    return [run_cut, *sorted(observed), MAX_SCORE + 1.0]


def validate_grid(grid: list[float], run_cut: float) -> None:
    """실행 컷보다 낮은 컷은 거부한다 — 이 도구가 정직하려면 여기서 멈춰야 한다.

    낮은 컷에서는 「검색 게이트를 통과한 뒤 생성 게이트가 무엇을 했는가」가 데이터에
    없다. 비어 있는 칸을 기권으로 채우면 낮은 컷이 실제보다 안전해 보이는데, 그것이
    바로 이 스윕으로 반증하려는 명제다.
    """
    too_low = sorted(c for c in grid if c < run_cut)
    if too_low:
        raise ValueError(
            f"실행 컷 {run_cut:g}보다 낮은 컷은 재구성할 수 없다: {too_low}. "
            f"그 점수대 문항은 answerer를 거치지 않아 생성 게이트 판정이 없다 — "
            f"컷 {min(too_low):g} 이하로 재실행해야 한다."
        )


def summarize(rows: list[dict], cut: float) -> dict:
    """컷 하나에서의 집계. answerable(simple+complex)과 insufficient를 나눠 본다.

    두 축은 실패의 방향이 반대다 — insufficient는 답하면 지고(누출), answerable은
    기권하면 진다(과잉 기권). 컷 하나로 둘을 동시에 좋게 할 수 없으므로 합산 정답률
    한 줄로 보고하면 맞바꿈이 숨는다.
    """
    layers: Counter[str] = Counter()
    leaked: list[str] = []
    over_abstained: list[str] = []
    correct = {"answerable": 0, "insufficient": 0}
    total = {"answerable": 0, "insufficient": 0}

    for row in rows:
        if row.get("kind") == "error":  # API 실패를 컷의 손익으로 세지 않는다
            continue
        axis = "insufficient" if row["category"] == "insufficient" else "answerable"
        total[axis] += 1
        layer = gate_layer(row, cut)
        layers[layer] += 1
        new_row = rethreshold(row, cut)
        if row_correct(new_row):
            correct[axis] += 1
        if axis == "insufficient" and new_row["effective_kind"] == "answer":
            leaked.append(row["id"])
        # 과잉 기권은 **컷이 만든 것만** 센다 — 원래도 기권이던 행은 컷의 책임이 아니다
        if axis == "answerable" and layer == "retrieval" and row.get("effective_kind") == "answer":
            over_abstained.append(row["id"])

    return {
        "cut": cut,
        "layers": layers,
        "leaked": leaked,
        "over_abstained": over_abstained,
        "correct": correct,
        "total": total,
    }


def print_score_histogram(rows: list[dict]) -> None:
    """카테고리별 top1 점수 분포 — 「컷을 어디에 둘 수 있는가」의 직접 근거다.

    answerable의 최저 점수와 insufficient의 최고 점수가 겹치지 않으면 그 사이 구간은
    전부 같은 결과를 낸다. 겹치면 컷 하나로는 분리되지 않는다는 뜻이고, 그때 남은
    질문은 「겹친 구간을 생성 게이트가 받아내는가」로 넘어간다.
    """
    buckets: dict[str, Counter[float | None]] = defaultdict(Counter)
    for row in rows:
        if row.get("kind") == "error":
            continue
        axis = "insufficient" if row["category"] == "insufficient" else "answerable"
        buckets[axis][top1_score(row)] += 1

    scores = sorted(
        {s for c in buckets.values() for s in c if s is not None},
    )
    print("\n## top1 리랭크 점수 분포 (관측)\n")
    print("| 축 | " + " | ".join(f"{s:g}" for s in scores) + " | 점수없음 |")
    print("|---" * (len(scores) + 2) + "|")
    for axis in ("answerable", "insufficient"):
        counts = buckets.get(axis, Counter())
        cells = " | ".join(str(counts.get(s, 0)) for s in scores)
        print(f"| {axis} | {cells} | {counts.get(None, 0)} |")

    ans = [s for s, n in buckets["answerable"].items() if s is not None and n]
    ins = [s for s, n in buckets["insufficient"].items() if s is not None and n]
    if ans and ins:
        print(
            f"\nanswerable 최저 **{min(ans):g}** / insufficient 최고 **{max(ins):g}** — "
            + (
                f"겹침 구간 [{min(ans):g}, {max(ins):g}]. 컷 하나로 두 축이 분리되지 않는다."
                if min(ans) <= max(ins)
                else "겹치지 않는다. 이 사이의 컷은 전부 같은 결과다."
            )
        )


def print_sweep(rows: list[dict], grid: list[float], k: int) -> None:
    print("\n## 컷 스윕\n")
    print(
        "| 컷 | 검색게이트 | 생성게이트 | 답변 | 폴백 "
        "| ins 누출 | ans 과잉기권 | ins 정답률 | ans 정답률 | answerer 호출 | 절감 |"
    )
    print("|---" * 11 + "|")
    # 게이트가 없을 때(= 전 문항이 answerer까지 감)의 비용이 절감률의 분모다
    ungated = len(rows) * (RERANK_COST_USD + ANSWERER_COST_USD)
    for cut in grid:
        s = summarize(rows, cut)
        layers = s["layers"]
        calls = layers["generation"] + layers["answered"] + layers["fallback"]
        spent = len(rows) * RERANK_COST_USD + calls * ANSWERER_COST_USD
        label = f"{cut:g}" if cut <= MAX_SCORE else f">{MAX_SCORE:g} (전면차단)"
        # n=0인 축은 "-"다. 0.000으로 찍으면 「전부 틀렸다」로 읽히는데, 실은 그 축을
        # 재지 않은 실행이다(complex만 돌린 반복 실행이 그렇다)
        rates = {
            axis: (f"{s['correct'][axis] / s['total'][axis]:.3f}" if s["total"][axis] else "-")
            for axis in ("insufficient", "answerable")
        }
        print(
            f"| {label} | {layers['retrieval']} | {layers['generation']} | {layers['answered']} "
            f"| {layers['fallback']} | **{len(s['leaked'])}** | **{len(s['over_abstained'])}** "
            f"| {rates['insufficient']} | {rates['answerable']} "
            f"| {calls}/{len(rows)} | {(ungated - spent) / ungated:.1%} |"
        )
    totals = summarize(rows, grid[0])["total"]
    absent = [axis for axis in ("insufficient", "answerable") if not totals[axis]]
    if absent:
        print(f"\n⚠️ 이 실행에 **{', '.join(absent)} 문항이 없다** — 그 축은 판단할 수 없다.")
    print(
        f"\n관측 단위는 문항×실행이다(k={k}). "
        "**ins 누출**은 기권해야 할 문항이 답한 건수(안전성 실패), "
        "**ans 과잉기권**은 답할 수 있었는데 검색 게이트가 막은 건수(유용성 실패)다."
    )
    per_item = RERANK_COST_USD + ANSWERER_COST_USD
    print(
        f"\n`절감`은 게이트가 없을 때(문항당 ${per_item:.5f}) 대비 아낀 비율이다. "
        f"**상한이 {ANSWERER_COST_USD / per_item:.0%}로 "
        "구조적으로 묶여 있다** — 점수 게이트는 리랭크 뒤에 있어서 아낄 수 있는 것이 "
        "answerer 호출 하나뿐이고, 게이트에 걸린 문항도 리랭크 비용은 이미 냈다. "
        "지연은 별개 축이다: 무관 질문에 answerer 왕복 한 번을 아끼는 것은 응답 시간 이득이라 "
        "이 표에 안 잡힌다."
    )


def print_flips(rows: list[dict], grid: list[float]) -> None:
    """컷 경계에서 결과가 바뀌는 문항. 표의 숫자가 어느 문항에서 왔는지 드러낸다.

    손익은 **재임계 전후의 정답 여부**로 가른다. 「검색 게이트가 새로 잡았다」 자체는
    손해가 아니다 — insufficient 문항은 어느 층이 잡아도 정답이고, 그때 컷이 바꾼 것은
    결과가 아니라 answerer 호출 한 번이다(비용 절감). 애초에 틀리던 문항도 마찬가지다.
    """
    print("\n## 컷 경계에서 뒤집히는 문항\n")
    changed = False
    for lo, hi in zip(grid, grid[1:], strict=False):
        if hi > MAX_SCORE:  # 전면차단 경계는 sanity check용이라 문항을 나열할 의미가 없다
            continue
        newly = [
            r
            for r in rows
            if r.get("kind") != "error"
            and gate_layer(r, lo) != "retrieval"
            and gate_layer(r, hi) == "retrieval"
        ]
        if not newly:
            continue
        changed = True
        lost = [r for r in newly if row_correct(r) and not row_correct(rethreshold(r, hi))]
        print(
            f"**컷 {lo:g} → {hi:g}** — 검색 게이트가 새로 잡는 {len(newly)}건 "
            f"(정답이 깨지는 것 {len(lost)}건, 호출만 아끼는 것 {len(newly) - len(lost)}건):\n"
        )
        # k회 반복이면 같은 문항이 여러 번 걸린다 — 문항으로 접어야 「몇 회 중 몇 회」가 보인다
        by_item: dict[str, list[dict]] = defaultdict(list)
        for r in newly:
            by_item[r["id"]].append(r)
        for item_id, hits in sorted(by_item.items()):
            n_lost = sum(1 for r in hits if row_correct(r) and not row_correct(rethreshold(r, hi)))
            category = hits[0]["category"]
            scores = "/".join(f"{top1_score(r):g}" for r in sorted(hits, key=top1_score))
            verdict = (
                f"**손해 {n_lost}회** — 정답이 기권으로 바뀐다"
                if n_lost
                else "무해 — 판정 그대로, answerer 호출만 아낀다"
            )
            print(f"- `{item_id}` ({category}, {len(hits)}회 걸림, 점수 {scores}) → {verdict}")
        print()
    if not changed:
        print("없음 — 이 그리드 안에서 컷을 옮겨도 결과가 같다.")


def print_paired(runs: list[Path], preset: str, base: float, grid: list[float]) -> None:
    """기준 컷 대비 정답률 차이의 CI. **컷 비교는 완전히 짝지어진다.**

    두 컷을 따로 돌려 비교하면 리랭커 비결정성이 섞이지만(v1의 cpx-007이 실행마다
    8↔10), 여기서는 같은 실행·같은 점수·같은 답변에 컷만 갈아끼운다. 남는 불확실성은
    문항 표집과 실행 노이즈뿐이라 `repeat_metrics`의 2단 부트스트랩을 그대로 쓴다.
    """
    per_run = [load_judged(d, preset) for d in runs]

    def observations(cut: float) -> dict[str, list[float]]:
        obs: dict[str, list[float]] = defaultdict(list)
        for rows in per_run:
            for row in rows:
                if row.get("kind") == "error":
                    continue
                obs[row["id"]].append(1.0 if row_correct(rethreshold(row, cut)) else 0.0)
        return dict(obs)

    base_obs = observations(base)
    print(f"\n## 기준 컷 {base:g} 대비 정답률 차이 (문항 페어, k={len(runs)})\n")
    print("| 컷 | 정답률 | 차이 | 95% CI | 판정 |")
    print("|---" * 5 + "|")
    for cut in grid:
        if cut == base or cut > MAX_SCORE:
            continue
        obs = observations(cut)
        ids = sorted(i for i in base_obs if i in obs)
        base_rate = mean(mean(base_obs[i]) for i in ids)
        rate = mean(mean(obs[i]) for i in ids)
        lo, hi = paired_bootstrap(base_obs, obs)
        verdict = "차이 주장 불가" if lo <= 0.0 <= hi else ("**개선**" if lo > 0 else "**손해**")
        print(
            f"| {cut:g} | {rate:.3f} | {rate - base_rate:+.3f} "
            f"| [{lo:+.3f}, {hi:+.3f}] | {verdict} |"
        )
    print(f"\n기준 컷 {base:g}의 정답률은 {mean(mean(v) for v in base_obs.values()):.3f}다.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=Path, nargs="+", required=True, help="실행별 결과 디렉터리")
    parser.add_argument("--preset", required=True, help="스윕할 구성 (예: rerank)")
    parser.add_argument(
        "--run-cut",
        type=float,
        required=True,
        help="그 실행이 **실제로** 돌아간 컷. 이 값 미만은 재구성할 수 없어 거부한다. "
        "옛 결과 파일의 policy_version에는 컷이 거짓 기록돼 있으니(9.0 고정) preset으로 판단할 것.",
    )
    parser.add_argument(
        "--grid", default="", help="쉼표 구분 컷 목록. 비우면 관측 점수에서 만든다."
    )
    args = parser.parse_args()

    rows = [row for run_dir in args.runs for row in load_judged(run_dir, args.preset)]
    grid = (
        [float(x) for x in args.grid.split(",") if x.strip()]
        if args.grid
        else default_grid(rows, args.run_cut)
    )
    validate_grid(grid, args.run_cut)

    codes = {r.get("code_version") for r in rows}
    print("## 실행 요약\n")
    print(f"- 결과: {', '.join(f'{d}/{args.preset}.judged.jsonl' for d in args.runs)}")
    print(f"- 관측 {len(rows)}행 (문항×실행), k={len(args.runs)}")
    print(f"- 실행 컷 **{args.run_cut:g}** → 재구성 가능 구간 **[{args.run_cut:g}, ∞)**")
    print(f"- code_version: {', '.join(str(c) for c in sorted(codes, key=str))}")
    if None in codes:
        print("  - ⚠️ 커밋 해시 없는 실행이 섞여 있다 — 코드 epoch를 보장할 수 없다")

    print_score_histogram(rows)
    print_sweep(rows, grid, len(args.runs))
    print_paired(args.runs, args.preset, grid[0], grid)
    print_flips(rows, grid)


if __name__ == "__main__":
    main()
