"""BE 스펙 조달 — `/implement <번호>` Phase 0의 입력을 만든다.

`docs/specs/`는 이 레포에 없다. 스펙은 **BE 레포**에 산다(단일 스펙 저장소 + 세 구현 레포).
이 스크립트가 그것을 `.cure-implement/spec-<번호>.md`로 가져온다. 이 레포가 맡을 기준은
수용 기준의 `(AGENT)` 라벨로 가른다 (`.claude/commands/implement.md` Phase 0).

조달 순서:
  1. 로컬 형제 경로 `../cure-agent-be/docs/specs/<번호>-*.md` — 있으면 그대로 쓴다.
     레포를 나란히 두고 작업하는 로컬에서는 **아직 머지되지 않은 스펙도** 읽을 수 있다.
  2. BE `dev`의 raw — 형제 경로에 없을 때(CI·다른 머신·워크트리). 파일명을 모르므로
     GitHub contents API로 `<번호>-`로 시작하는 파일을 먼저 찾는다.
  3. BE `main`의 raw — dev에서 못 찾았을 때 한 번 더.

**기본 ref가 `dev`인 이유**: BE의 기본 브랜치가 `dev`이고 스펙은 `dev-only`로 착지한다.
스펙은 항상 dev에 먼저 있고 main에는 다음 `full` 배포가 실어 나른다 — main을 기본으로 두면
갓 쓴 스펙도, dev에만 반영된 개정도 못 읽는다. main 재시도는 dev에서 정리된 옛 스펙 대비다.

조달 순서는 cure-agent-fe `scripts/fetch-spec.mjs`와 같다. 표준 라이브러리만 쓴다 —
스펙 조달이 의존성 설치에 묶이지 않게.

실행: python3 scripts/fetch_spec.py 41
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / ".cure-implement"
SIBLING_BE = ROOT.parent / "cure-agent-be"
SIBLING_SPECS = SIBLING_BE / "docs" / "specs"

REPO = os.environ.get("CURE_AGENT_BE_REPO", "Cure-Agent/cure-agent-be")
REF = os.environ.get("CURE_AGENT_BE_REF", "dev")
FALLBACK_REF = "main"
USER_AGENT = "medical-agentic-rag-fetch-spec"


def matches(name: str, prefixes: tuple[str, ...]) -> bool:
    return name.endswith(".md") and name.startswith(prefixes)


def warn_if_sibling_off_ref() -> None:
    """형제 레포가 기본 ref와 다른 브랜치면 알린다.

    형제 경로는 체크아웃된 브랜치의 파일을 읽으므로, main에 있으면 오래된 스펙을 읽는다.
    """
    try:
        branch = subprocess.run(
            ["git", "-C", str(SIBLING_BE), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return
    if branch != REF:
        print(
            f"경고: 형제 BE 레포 브랜치가 {branch}다(기대 {REF}) — 낡은 스펙일 수 있다",
            file=sys.stderr,
        )


def from_sibling(prefixes: tuple[str, ...]) -> tuple[str, str] | None:
    if not SIBLING_SPECS.is_dir():
        return None
    hits = sorted(p for p in SIBLING_SPECS.iterdir() if p.is_file() and matches(p.name, prefixes))
    if not hits:
        return None
    warn_if_sibling_off_ref()
    return str(hits[0]), hits[0].read_text(encoding="utf-8")


def get(url: str, *, accept: str | None = None) -> bytes:
    # 조회 실패를 "없음"으로 삼키지 않는다 — 없음은 목록 조회가 성공했을 때만 판정한다
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise SystemExit(f"스펙 조회 실패: HTTP {error.code} — {url}") from error
    except urllib.error.URLError as error:
        raise SystemExit(f"스펙 조회 실패: {error.reason} — {url}") from error


def from_remote(ref: str, prefixes: tuple[str, ...]) -> tuple[str, str] | None:
    list_url = f"https://api.github.com/repos/{REPO}/contents/docs/specs?ref={ref}"
    entries = json.loads(get(list_url, accept="application/vnd.github+json"))
    hit = next(
        (e for e in entries if e.get("type") == "file" and matches(e.get("name", ""), prefixes)),
        None,
    )
    if hit is None:
        return None
    raw_url = f"https://raw.githubusercontent.com/{REPO}/{ref}/docs/specs/{hit['name']}"
    return raw_url, get(raw_url).decode("utf-8")


def main() -> None:
    number = sys.argv[1] if len(sys.argv) > 1 else ""
    if not re.fullmatch(r"\d+", number):
        raise SystemExit("사용법: python3 scripts/fetch_spec.py <스펙 번호>   예) ... 41")
    # 스펙 파일명은 zero-padding이 섞여 있다(05·41) — 양쪽 다 매칭한다
    prefixes = (f"{number}-", f"{number.zfill(2)}-")

    found = from_sibling(prefixes) or from_remote(REF, prefixes)
    if found is None and REF == "dev":
        found = from_remote(FALLBACK_REF, prefixes)
    if found is None:
        fallback = f" 및 @{FALLBACK_REF}" if REF == "dev" else ""
        raise SystemExit(
            f"스펙 {number}을(를) 찾지 못했습니다.\n"
            f"  - 로컬 형제 경로: {SIBLING_SPECS} (없거나 해당 번호 없음)\n"
            f"  - 원격: {REPO}@{REF}{fallback} docs/specs/\n"
            "아직 BE에 머지되지 않은 스펙이면 레포를 형제 디렉토리로 두고 다시 실행하세요."
        )

    source, body = found
    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / f"spec-{number}.md"
    out_path.write_text(body, encoding="utf-8")
    print(f"스펙 {number} 조달 완료")
    print(f"  출처: {source}")
    print(f"  저장: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
