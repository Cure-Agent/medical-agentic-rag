"""복합 문항 근거 고정 — composite_spec.jsonl의 지침 키워드로 로컬 코퍼스에서 권고 청크를 뽑아
composite.jsonl에 박는다. 한 번 만들면 파일이 평가셋이다(DB 없이 돈다).

    .venv/bin/python -m evals.synthesis.build_composite

근거 선택 — 지침 제목 ILIKE 키워드, 권고등급이 있는 청크(권고문 자체)만, 본문 900자 이하. 질문에
든 치료어(침·전침·한약·추나·약침·봉약침·뜸)가 본문에 있는 청크를 앞에 두고 최대 4개. `mismatch`
문항은 무관한 지침의 청크를 붙여 기권(insufficientEvidence)을 기대한다.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import psycopg
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TREATMENT_WORDS = ("봉약침", "약침", "전침", "침", "한약", "추나", "뜸")
MAX_EVIDENCE = 4
MAX_CHARS = 900

SQL = """
select ec.id, g.title, gs.path, ec.recommendation_number, ec.content
from evidence_chunks ec
join guideline_versions gv on gv.id = ec.guideline_version_id
join guidelines g on g.id = gv.guideline_id
left join guideline_sections gs on gs.id = ec.section_id
where gv.status = 'ACTIVE'
  and ec.recommendation_grade is not null
  and length(ec.content) <= %s
  and g.title ilike %s
order by ec.recommendation_number, ec."order"
"""


def pick(rows: list[tuple], question: str) -> list[dict]:
    words = [w for w in TREATMENT_WORDS if w in question]

    def score(row: tuple) -> int:
        content = row[4]
        return -sum(1 for w in words if w in content)

    ordered = sorted(rows, key=score)
    seen: set[str] = set()
    out: list[dict] = []
    for cid, title, path, rec, content in ordered:
        key = content.strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "id": str(cid),
                "guidelineTitle": title.strip(),
                "sectionPath": list(path or []) + ([rec] if rec else []),
                "excerpt": re.sub(r"\s+\n", "\n", content.strip()),
            }
        )
        if len(out) == MAX_EVIDENCE:
            break
    return out


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    dsn = os.environ.get("DATABASE_URL", "postgresql://cure:cure@localhost:5432/hybrid_probe")
    specs = [
        json.loads(line)
        for line in (HERE / "composite_spec.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    out_path = HERE / "composite.jsonl"
    with psycopg.connect(dsn) as conn, out_path.open("w", encoding="utf-8") as f:
        for spec in specs:
            rows = conn.execute(SQL, (MAX_CHARS, f"%{spec['guideline']}%")).fetchall()
            evidence = pick(rows, spec["question"])
            if not evidence:
                raise SystemExit(f"{spec['id']}: '{spec['guideline']}' 권고 청크 없음")
            row = {
                "id": spec["id"],
                "patient": spec["patient"],
                "question": spec["question"],
                "guideline": spec["guideline"],
                "expected_abstain": bool(spec.get("mismatch")),
                "interaction": spec.get("interaction"),
                "abstain_ok": bool(spec.get("abstain_ok")),
                "note": spec.get("note"),
                "evidence": evidence,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"{spec['id']:7s} {spec['guideline']:8s} 근거 {len(evidence)}")
    print(f"저장 {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
