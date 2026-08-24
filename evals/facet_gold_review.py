"""gold 라벨 검수 대상 뽑기 — 층 귀속을 못 믿게 만드는 key point만 골라낸다.

    python -m evals.facet_gold_review --runs results/rep{1,2,3,4,5} results/facet{1,2,3,4,5}

**왜 필요한가.** `facet_coverage`의 gold는 LLM 라벨러가 후보 합집합에서 고른 것이고,
「지지하는 청크 전부」가 아니라 **대표 2~3개**만 고르는 누락 편향이 실측됐다(2026-08-23
두 실험 모두). 그 결과 같은 사실을 담은 다른 청크가 풀에 있어도 「근거 없음」으로 세어
**검색층 실패가 과대 추정**된다. 분해 기각의 핵심 근거가 「검색층 실패가 165건 중 2~3건뿐」
이므로, 그 2~3건이 진짜인지가 판정 전체를 떠받친다.

**검수 범위를 좁히는 근거.** 판정은 `retrieved = bool(set(gold) & pool)`이다. gold에 청크를
**추가**하는 것은 교집합을 키우기만 하므로 「없음」 → 「있음」으로만 움직인다. 즉

    관측 전부가 이미 「있음」인 key point는 재라벨해도 결과가 바뀔 수 없다.

그래서 검수 대상은 **「없음」 관측이 한 번이라도 있는 key point**뿐이다. 33개를 통째로 보는
대신 여기로 줄인다. 두 종류가 섞여 있고 성격이 다르다:

| 셀 | 뜻 | 검수에서 기대하는 것 |
|---|---|---|
| 없음/담음 | gold는 풀에 없는데 답은 맞혔다 | 답변이 인용한 청크가 빠진 gold일 수 있다 |
| 없음/놓침 | 검색층 실패로 세는 셀 | 여기가 진짜여야 분해 기각 근거가 선다 |

`없음/담음` 관측에서 답변이 실제로 **인용한** 청크는 정의상 풀 안에 있고 채점자가 그 kp를
「담았다」고 본 근거이므로, 누락 gold의 1순위 후보다. 그걸 청크 본문과 함께 뽑아 준다.
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from app.config import get_settings
from evals.facet_coverage import META_KEY, fetch_chunks, pool_of

EXCERPT = 220  # 사람이 훑는 용도 — 판정은 원문을 열어서 한다


def judged_files(run_dir: Path) -> list[Path]:
    return sorted(run_dir.glob("*.judged.jsonl"))


def observe(run_dirs: list[Path], gold: dict) -> tuple[dict, dict]:
    """(문항, kp 인덱스)별 셀 카운트와, 「없음」일 때의 풀·인용을 모은다."""
    cells: dict[tuple[str, int], Counter] = defaultdict(Counter)
    misses: dict[tuple[str, int], dict[str, set]] = defaultdict(
        lambda: {"pool": set(), "cited": set()}
    )
    for run_dir in run_dirs:
        for path in judged_files(run_dir):
            preset = path.name.removesuffix(".judged.jsonl")
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                entry = gold.get(row["id"])
                if not entry or row.get("kind") == "error":
                    continue
                pool = pool_of(row)
                answered = row.get("key_points_covered") or []
                for index, gold_ids in enumerate(entry["gold"]):
                    covered = bool(answered[index]) if index < len(answered) else False
                    if not gold_ids:
                        cells[(row["id"], index)]["미라벨"] += 1
                        continue
                    retrieved = bool(set(gold_ids) & pool)
                    cell = (
                        ("정상" if covered else "생성층 실패")
                        if retrieved
                        else ("근거 없이 정답" if covered else "검색층 실패")
                    )
                    cells[(row["id"], index)][cell] += 1
                    if not retrieved:
                        key = misses[(row["id"], index)]
                        key["pool"] |= pool - set(gold_ids)
                        if covered:
                            key["cited"] |= set(row.get("citations") or []) - set(gold_ids)
                        cells[(row["id"], index)][f"@{preset}"] += 1
    return cells, misses


def priority(counts: Counter) -> tuple[int, str]:
    """낮은 번호가 먼저. 판정을 흔드는 순서다."""
    if counts["근거 없이 정답"] and counts["검색층 실패"]:
        return 0, "A — 두 셀 모두 발생. 라벨이 얇아 층 귀속이 실행마다 갈린다"
    if counts["근거 없이 정답"]:
        return 1, "B — 근거 없이 정답. 인용 청크가 곧 빠진 gold일 가능성이 높다"
    if counts["검색층 실패"]:
        return 2, "C — 검색층 실패만. **분해 기각 근거를 직접 떠받치는 칸**"
    return 3, "D — 미라벨"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, nargs="+", required=True, help="채점에 쓸 실행 전부")
    parser.add_argument("--gold", type=Path, default=Path("evals/facet_gold.json"))
    parser.add_argument("--full", action="store_true", help="면제된 key point도 함께 찍는다")
    args = parser.parse_args()

    gold = json.loads(args.gold.read_text())
    meta = gold.pop(META_KEY, {})
    cells, misses = observe(args.runs, gold)

    total_kp = sum(len(v["gold"]) for v in gold.values())
    targets = sorted(
        (key for key, c in cells.items() if c["근거 없이 정답"] or c["검색층 실패"] or c["미라벨"]),
        key=lambda k: (priority(cells[k])[0], k),
    )

    print("# gold 라벨 검수 대상\n")
    print(f"- gold: `{args.gold}` (라벨러 {meta.get('model', '?')}, 발췌 {meta.get('excerpt')}자)")
    files = sum(len(judged_files(d)) for d in args.runs)
    print(f"- 관측: 실행 {len(args.runs)}개 × 프리셋 — {files}개 파일")
    print(f"- key point {total_kp}개 중 **검수 대상 {len(targets)}개**")
    print(
        f"- 면제 {total_kp - len(targets)}개: 모든 관측이 「있음」이라 "
        "gold를 늘려도 판정이 안 바뀐다(교집합은 커지기만 한다)\n"
    )

    print("## 우선순위\n")
    print("| 순위 | 문항 | kp | key point | gold | 없음/담음 | 없음/놓침 | 있음 | 사유 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for rank, key in enumerate(targets, 1):
        item_id, index = key
        counts = cells[key]
        entry = gold[item_id]
        text = entry["key_points"][index]
        hit = counts["정상"] + counts["생성층 실패"]
        print(
            f"| {rank} | {item_id} | kp{index + 1} | {text} | {len(entry['gold'][index])}개 "
            f"| **{counts['근거 없이 정답']}** | **{counts['검색층 실패']}** | {hit} "
            f"| {priority(counts)[1].split(' — ')[0]} |"
        )

    # 누락 후보 청크 본문 — 사람이 판정하려면 텍스트가 있어야 한다
    wanted = sorted({cid for key in targets for cid in misses[key]["cited"]})
    chunks = fetch_chunks(wanted, get_settings().database_url) if wanted else {}

    print("\n## 문항별 검수 카드\n")
    for rank, key in enumerate(targets, 1):
        item_id, index = key
        entry, counts = gold[item_id], cells[key]
        presets = {k[1:]: v for k, v in counts.items() if k.startswith("@")}
        print(f"### {rank}. {item_id} kp{index + 1} — {priority(counts)[1]}\n")
        print(f"- **key point**: {entry['key_points'][index]}")
        print(f"- **질문**: {entry['question']}")
        ids = ", ".join(f"`{c}`" for c in entry["gold"][index])
        hit = counts["정상"] + counts["생성층 실패"]
        where = ", ".join(f"{p}×{n}" for p, n in sorted(presets.items()))
        print(f"- **현재 gold** ({len(entry['gold'][index])}개): {ids}")
        print(
            f"- **셀**: 없음/담음 {counts['근거 없이 정답']}"
            f" · 없음/놓침 {counts['검색층 실패']} · 있음 {hit}"
        )
        print(f"- **「없음」이 난 구성**: {where}")
        cited = sorted(misses[key]["cited"])
        pool_only = len(misses[key]["pool"]) - len(cited)
        if cited:
            print(f"\n  **누락 후보 — 답변이 인용했는데 gold가 아닌 청크 {len(cited)}개**\n")
            for cid in cited:
                chunk = chunks.get(cid)
                body = (
                    chunk["content"][:EXCERPT].replace("\n", " ") + "…"
                    if chunk
                    else "(DB 조회 실패)"
                )
                title = chunk["guideline_title"] if chunk else "?"
                print(f"  - `{cid}` ({title})\n    > {body}")
        else:
            print("\n  인용 후보 없음 — 「없음/놓침」만 발생. 풀 전체를 열어 확인해야 한다.")
        print(f"\n  (그 외 풀에만 있던 비-gold 청크 {pool_only}개)\n")

    if args.full:
        print("\n## 면제된 key point\n")
        for key in sorted(set(cells) - set(targets)):
            print(f"- {key[0]} kp{key[1] + 1} — 관측 {sum(cells[key].values())}회 전부 「있음」")


if __name__ == "__main__":
    main()
