"""검색층 facet coverage — key point별로 "지지 청크가 answerer의 근거 풀에 들어왔는가"를 잰다.

    python -m evals.facet_coverage label --runs results results/rep2 --out evals/facet_gold.json
    python -m evals.facet_coverage score --runs results/rep1 results/rep2 \
        --gold evals/facet_gold.json --a rerank_topk11 --b rerank_decomp_fused_topk11

**왜 이 지표인가.** 정답률은 검색 실패와 생성 실패를 구분하지 못한다. 분해는 검색측
개입이므로, 답이 틀린 원인이 "근거가 풀에 없었다"가 아니면 분해로 고칠 수 없다. 1차
실측에서 B가 이긴 두 문항의 성격이 실제로 갈렸다 — cpx-008은 B 인용 4건 중 0건이
통제군 풀에 있었고(진짜 검색층 이득, 풀 교집합 2/11), cpx-001은 4건 중 3건이 이미
통제군 풀에 있었다(통제군은 근거를 갖고도 틀렸다 = 생성층 문제). 이 지표는 그 구분을
문항이 아니라 key point 단위로 낸다.

얻는 것: 측정 단위가 12문항 → 33 key point로 늘고, 생성 노이즈가 빠지고, 새 문항을
한 개도 만들지 않는다. 채점은 `evals.repeat_metrics`가 남긴 실행 결과의 trace를
재사용하므로 **추가 파이프라인 실행이 0회**다.

**한계(반드시 같이 읽을 것).** gold 라벨은 관측된 근거 풀의 합집합에 대해서만 붙인다.
어떤 구성도 끝내 검색하지 못한 청크는 후보 목록에 아예 없으므로, 이 지표는 코퍼스
9,727청크 기준의 절대 recall이 아니라 **구성 간 상대 비교**다. 지지 청크를 찾지 못한
key point는 `미라벨`로 따로 세어 보고한다 — 두 구성이 똑같이 실패하는 항목이라
비교에는 무용하지만, 조용히 빼면 커버리지가 실제보다 좋아 보인다.
"""

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from statistics import mean

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from app.config import get_settings
from app.llm.client import make_judge_llm
from evals.repeat_metrics import paired_bootstrap

CONCURRENCY = 2  # judge/run_eval과 같은 이유 — 워커 8은 429 폭탄 실측
META_KEY = "_meta"  # gold 파일의 provenance 항목 — 문항 id와 섞이지 않게 언더스코어로 시작한다
# 리랭커의 300자보다 넉넉하게 준다. 라벨은 12회 호출뿐이라 비용보다 판정 정확도가 우선이고,
# 권고등급·근거수준은 청크 뒷부분에 오는 경우가 있어 300자로 자르면 지지 판정을 놓친다.
# 코퍼스 평균 청크가 894자(실측)라 900이면 대부분 전문이다.
LABEL_EXCERPT = 900

LABEL_SYSTEM = """너는 한의 임상 지침 코퍼스에서 채점 기준(key point)의 근거 청크를 찾는 라벨러다.
질문, key point 목록, 후보 청크 목록(번호 매김)이 주어진다.
각 key point에 대해 그 사실을 **직접 지지하는** 청크 번호를 모두 고른다.

규칙:
- 주제만 같고 그 사실을 담지 않은 청크는 고르지 않는다. 어휘가 겹치는 것은 근거가 아니다.
- key point가 권고등급·근거수준을 명시하면, 청크에 그 값이 있어야 지지로 인정한다.
- 지지하는 청크가 없으면 빈 배열을 낸다. 억지로 채우지 않는다 — 빈 배열도 정보다.
- 출력은 제시된 key point 순서 그대로, 같은 개수의 배열이다.
"""

CHUNK_SQL = """
SELECT ec.id AS chunk_id, ec.content, g.title AS guideline_title
FROM evidence_chunks ec
JOIN guideline_versions gv ON ec.guideline_version_id = gv.id
JOIN guidelines g ON gv.guideline_id = g.id
WHERE ec.id = ANY(%s)
"""


class FacetLabels(BaseModel):
    supporting: list[list[int]] = Field(
        default_factory=list,
        description="key point 순서대로, 그 사실을 지지하는 후보 청크의 1-기반 번호 목록",
    )


def pool_of(row: dict) -> set[str]:
    """answerer가 실제로 받은 근거 풀. 재검색 라운드가 있으면 합집합이다."""
    pool: set[str] = set()
    for step in row.get("trace") or []:
        pool.update(step.get("new_chunks") or [])
    return pool


def load_dataset(path: Path) -> dict[str, dict]:
    rows = (json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return {row["id"]: row for row in rows}


def result_files(run_dir: Path) -> list[Path]:
    return sorted(p for p in run_dir.glob("*.jsonl") if not p.name.endswith(".judged.jsonl"))


def candidate_union(
    run_dirs: list[Path], dataset: dict[str, dict], categories: set[str] | None = None
) -> dict[str, set[str]]:
    """문항별 후보 청크 합집합 — 주어진 실행들에서 어떤 구성이든 풀에 올린 청크 전부.

    **채점할 실행을 전부 넘겨야 한다.** 라벨에 안 들어간 청크는 gold가 될 수 없으므로,
    일부 실행만 보고 라벨을 만들면 나머지 실행에서 그 청크를 검색한 구성이 부당하게
    낮게 나온다. `score`가 gold의 provenance와 대조해 이 실수를 잡는다.
    """
    union: dict[str, set[str]] = {}
    for run_dir in run_dirs:
        for path in result_files(run_dir):
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                item = dataset.get(row["id"])
                if not item or not item.get("key_points"):
                    continue
                if categories and item["category"] not in categories:
                    continue
                union.setdefault(row["id"], set()).update(pool_of(row))
                union[row["id"]].update(row.get("citations") or [])
    return union


def fetch_chunks(chunk_ids: list[str], database_url: str) -> dict[str, dict]:
    if not chunk_ids:
        return {}
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        rows = conn.execute(CHUNK_SQL, (chunk_ids,)).fetchall()
    return {row["chunk_id"]: row for row in rows}


async def label_one(llm, item: dict, candidates: list[dict], sem: asyncio.Semaphore) -> dict:
    key_points = item["key_points"]
    async with sem:
        listing = "\n".join(
            f"[{i + 1}] ({c['guideline_title']}) {c['content'][:LABEL_EXCERPT]}"
            for i, c in enumerate(candidates)
        )
        points = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(key_points))
        user = (
            f"## 질문\n{item['question']}\n\n"
            f"## Key points ({len(key_points)}개)\n{points}\n\n"
            f"## 후보 청크 ({len(candidates)}개)\n{listing}"
        )
        labels = await llm.ainvoke([("system", LABEL_SYSTEM), ("user", user)])

    # 길이 불일치·범위 밖 번호는 모델 실수다. 관대하게 보정하되 없는 근거를 만들지는 않는다.
    supporting = (labels.supporting + [[]] * len(key_points))[: len(key_points)]
    gold = [
        sorted(
            {candidates[n - 1]["chunk_id"] for n in numbers if 1 <= n <= len(candidates)}
        )
        for numbers in supporting
    ]
    return {
        "question": item["question"],
        "key_points": key_points,
        "gold": gold,
        "candidates": len(candidates),
    }


async def run_label(args: argparse.Namespace) -> None:
    settings = get_settings()
    dataset = load_dataset(args.dataset)
    categories = {c.strip() for c in args.categories.split(",") if c.strip()}
    union = candidate_union(args.runs, dataset, categories)
    assert union, f"후보가 비었다 — {args.runs}에 결과 파일이 있는가?"

    all_ids = sorted({cid for ids in union.values() for cid in ids})
    chunks = fetch_chunks(all_ids, settings.database_url)
    missing = len(all_ids) - len(chunks)
    print(f"[label] 문항 {len(union)}개 / 후보 청크 {len(all_ids)}개 (DB 조회 실패 {missing}개)")

    llm = make_judge_llm(settings).with_structured_output(FacetLabels)
    sem = asyncio.Semaphore(CONCURRENCY)
    item_ids = sorted(union)
    labeled = await asyncio.gather(
        *(
            label_one(
                llm,
                dataset[item_id],
                [chunks[cid] for cid in sorted(union[item_id]) if cid in chunks],
                sem,
            )
            for item_id in item_ids
        )
    )
    gold = dict(zip(item_ids, labeled, strict=True))

    unlabeled = sum(1 for v in gold.values() for g in v["gold"] if not g)
    total_kp = sum(len(v["gold"]) for v in gold.values())
    # provenance — 어떤 실행의 풀을 보고 라벨을 붙였는지. score가 이걸로 범위 실수를 잡는다.
    gold[META_KEY] = {
        "runs": sorted(str(d) for d in args.runs),
        "dataset": str(args.dataset),
        "excerpt": LABEL_EXCERPT,
        "model": settings.judge_model,
    }
    args.out.write_text(json.dumps(gold, ensure_ascii=False, indent=2))
    print(f"[label] key point {total_kp}개 중 지지 청크 못 찾음 {unlabeled}개 → {args.out}")
    print("[label] 33개 안팎이니 사람이 한 번 훑어보고 쓰는 것을 권한다.")


def score_preset(run_dirs: list[Path], preset: str, gold: dict) -> tuple[dict, Counter, int]:
    """문항별 검색 커버리지 관측과, 검색층/생성층 실패 교차표를 낸다."""
    obs: dict[str, list[float]] = {}
    crosstab: Counter = Counter()
    dropped = 0
    for run_dir in run_dirs:
        path = run_dir / f"{preset}.judged.jsonl"
        assert path.exists(), f"없는 파일: {path} — judge를 먼저 돌렸는가?"
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            entry = gold.get(row["id"])
            if not entry:
                continue
            if row.get("kind") == "error":
                dropped += 1
                continue
            pool = pool_of(row)
            answered = row.get("key_points_covered") or [False] * len(entry["gold"])
            hits = []
            for index, gold_ids in enumerate(entry["gold"]):
                if not gold_ids:
                    crosstab["미라벨"] += 1  # 어떤 구성도 지지 청크를 못 찾은 key point
                    continue
                retrieved = bool(set(gold_ids) & pool)
                covered = bool(answered[index]) if index < len(answered) else False
                hits.append(1.0 if retrieved else 0.0)
                if retrieved and covered:
                    crosstab["정상"] += 1
                elif retrieved and not covered:
                    crosstab["생성층 실패"] += 1
                elif not retrieved and not covered:
                    crosstab["검색층 실패"] += 1
                else:
                    crosstab["근거 없이 정답"] += 1
            if hits:
                obs.setdefault(row["id"], []).append(mean(hits))
    return obs, crosstab, dropped


def print_crosstab(name: str, crosstab: Counter) -> None:
    scored = sum(v for k, v in crosstab.items() if k != "미라벨")
    print(f"\n### {name} — key point {scored}개 (미라벨 {crosstab['미라벨']}개 제외)\n")
    print("| 근거가 풀에 | 답변이 담았나 | 개수 | 해석 |")
    print("|---|---|---|---|")
    rows = [
        ("있음", "담음", "정상", "—"),
        ("있음", "놓침", "생성층 실패", "**분해로 못 고친다** (근거는 이미 있었다)"),
        ("없음", "놓침", "검색층 실패", "**분해가 노릴 수 있는 지점**"),
        ("없음", "담음", "근거 없이 정답", "환각이거나 gold 라벨 누락 — 개별 확인 필요"),
    ]
    for pool_state, answer_state, key, note in rows:
        count = crosstab[key]
        share = f"{count / scored:.0%}" if scored else "-"
        print(f"| {pool_state} | {answer_state} | {count} ({share}) | {note} |")


def run_score(args: argparse.Namespace) -> None:
    gold = json.loads(args.gold.read_text())
    meta = gold.pop(META_KEY, {})
    # 라벨에 안 쓰인 실행을 채점하면, 그 실행에서만 뜬 청크는 gold가 될 수 없어 커버리지가
    # 낮게 나온다. 조용히 틀리는 대신 여기서 멈춘다 — label을 전 실행으로 다시 돌려야 한다.
    labeled_runs = set(meta.get("runs", []))
    scoring_runs = {str(d) for d in args.runs}
    assert not labeled_runs or scoring_runs <= labeled_runs, (
        f"gold는 {sorted(labeled_runs)}만 보고 만들어졌는데 "
        f"{sorted(scoring_runs - labeled_runs)}를 채점하려 한다 — "
        "`label`을 채점할 실행 전부로 다시 돌려라."
    )
    a_obs, a_cross, a_dropped = score_preset(args.runs, args.a, gold)
    b_obs, b_cross, b_dropped = score_preset(args.runs, args.b, gold)

    total_kp = sum(len(v["gold"]) for v in gold.values())
    unlabeled = sum(1 for v in gold.values() for g in v["gold"] if not g)
    print("## 실행 요약\n")
    print(f"- 실행 {len(args.runs)}회, 문항 {len(gold)}개, key point {total_kp}개")
    print(f"- 지지 청크를 못 찾아 비교에서 뺀 key point: **{unlabeled}개** (두 구성 모두 실패)")
    print(f"- 관측에서 뺀 오류 행: {args.a} {a_dropped}건 / {args.b} {b_dropped}건")

    ids = sorted(i for i in a_obs if i in b_obs)
    mean_a = mean(mean(a_obs[i]) for i in ids)
    mean_b = mean(mean(b_obs[i]) for i in ids)
    lo, hi = paired_bootstrap(a_obs, b_obs)
    print("\n## 검색층 facet coverage (문항 평균)\n")
    print(
        f"- {args.a}: **{mean_a:.3f}**   |   {args.b}: **{mean_b:.3f}**"
        f"   |   차이 {mean_b - mean_a:+.3f}"
    )
    print(f"- 95% CI (2단 부트스트랩, n={len(ids)}문항): [{lo:+.3f}, {hi:+.3f}]")
    print(
        "- 판정: "
        + (
            "CI가 0을 포함한다 — **검색층에서도 차이를 주장할 수 없다.**"
            if lo <= 0.0 <= hi
            else "CI가 0을 넘지 않는다 — 분해가 실제로 근거를 더 넣고 있다."
        )
    )

    print("\n## 실패 층위 교차표")
    print_crosstab(args.a, a_cross)
    print_crosstab(args.b, b_cross)
    print(
        f"\n`{args.a}`의 「검색층 실패」가 작으면 분해의 상한 자체가 작다 — "
        "리랭커가 이미 근거를 넣고 있고 남은 오답은 생성 문제라는 뜻이다."
    )

    print("\n## 문항별 검색 커버리지\n")
    print(f"| 문항 | {args.a} | {args.b} | 차이 |")
    print("|---|---|---|---|")
    for item_id in ids:
        ra, rb = mean(a_obs[item_id]), mean(b_obs[item_id])
        print(f"| {item_id} | {ra:.2f} | {rb:.2f} | {rb - ra:+.2f} |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    label = sub.add_parser("label", help="후보 합집합에 gold 지지 청크를 라벨링한다")
    label.add_argument("--runs", type=Path, nargs="+", required=True, help="채점할 실행 **전부**")
    label.add_argument("--dataset", type=Path, default=Path("evals/dataset.jsonl"))
    label.add_argument("--out", type=Path, default=Path("evals/facet_gold.json"))
    label.add_argument("--categories", default="complex", help="쉼표 구분. 비우면 answerable 전부")

    score = sub.add_parser("score", help="실행 결과의 근거 풀을 gold로 채점한다")
    score.add_argument("--runs", type=Path, nargs="+", required=True)
    score.add_argument("--gold", type=Path, default=Path("evals/facet_gold.json"))
    score.add_argument("--a", required=True, help="기준 구성")
    score.add_argument("--b", required=True, help="비교 구성")

    args = parser.parse_args()
    if args.command == "label":
        asyncio.run(run_label(args))
    else:
        run_score(args)


if __name__ == "__main__":
    main()
