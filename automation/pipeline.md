# pipeline — PR·머지 파이프라인 (하네스 중립 명세)

feature 브랜치의 변경사항을 커밋 → `main` PR → CI 체크 → squash 머지까지 자동화한다. `ship`의 Phase 4에서 사용한다.

> **이 레포의 파이프라인은 `main` 머지에서 끝난다.** 서버 배포는 cure-agent-be가 맡는다 — 이 레포에는 CD 워크플로우도 배포 대기 단계도 없다. cure-agent-fe판의 「Vercel 프로덕션 배포 확인」 단계를 옮기지 않은 이유다.

> **이 파일은 하네스 중립 「진실의 원천」이다.** 진입 어댑터(`ship`)를 통해 실행되며, 어댑터가 「폴링 실행 규칙」의 모드와 **커밋 Co-Author 트레일러**(Step 1)를 지정한다:
>
> **하네스별 Co-Author 트레일러** (Step 1 커밋 메시지 끝에 붙는 한 줄):
> - Claude Code → 세션 시스템 규칙이 지정하는 `Co-Authored-By:` 트레일러
> - Codex → `Co-Authored-By: Codex <noreply@openai.com>`

**실행 주체**: 전 과정(Step 1~2)을 하나의 실행 흐름이 직접 수행한다.
- **Claude Code**: 메인 세션이 직접 수행하고 서브에이전트에 위임하지 않는다 — 파이프라인은 "액션 → 체크 폴링 → 다음 액션"의 반복인데, 서브에이전트 안에서 백그라운드 폴링을 돌리면 완료 후 재개가 안 돼 대기 지점에서 멈춘다. 메인 세션의 `run_in_background` Bash는 완료 시 세션을 확실히 재호출한다.
- **Codex**: 병렬 위임 프리미티브가 없으므로 단일 호출 흐름에서 순차 수행한다.

## 폴링 실행 규칙

CI 체크 대기는 `automation/bin/merge-gate.sh`로 실행한다 — 루프를 즉석에서 작성하지 않는다. 스크립트의 *실행 모드*는 진입 어댑터가 지정한 **하네스별 폴링 모드**를 따른다:

| 하네스 | 폴링 모드 |
|--------|-----------|
| **Claude Code** | 스크립트를 **`run_in_background: true` Bash**로 실행. 포그라운드 `sleep` 차단·Monitor 도구 금지(단일 완료 대기에 부적합). 스크립트 완료 시 세션이 자동 재호출된다. |
| **Codex** | 스크립트를 포그라운드 **블로킹** 호출로 완료까지 대기. 출력은 파일로 리다이렉트(`> "$LOG" 2>&1`)하고, **하네스 셸 명령 타임아웃을 스크립트 내부 타임아웃보다 넉넉히** 설정한다. |

- 스크립트는 성공·실패·타임아웃 **모든 종료 상태에서 exit**하고, 종료 직전 결과 마커 1줄(`... result=...`)을 출력한다. 출력의 마커·종료코드로 분기한다. 게이트 정의·간격·타임아웃·마커 규약은 **스크립트 상단 주석이 원천**이다.
- GitHub 조회 실패는 pending으로 삼키지 않는다 — 3회 연속 실패 시 `result=API_ERROR` 마커로 종료된다(스크립트에 구현됨). `API_ERROR`는 체크 실패나 `TIMEOUT`과 구분해 실제 stderr를 요약하고 중단한다. 하네스의 네트워크 권한·인증 상태를 함께 확인한다.
- **알림이 애매하거나 오래 소식이 없으면 추측·무한 대기 말고 즉시 `gh pr view/checks`로 실제 상태를 확인**한 뒤 분기한다.

## Step 1: 커밋 & PR & 머지 (main)

1. `git status --short` — 커밋 안 된 변경이 있으면 `git add -A` → **민감 파일 게이트 통과 후** 커밋 (메시지 끝에 **현재 하네스의 Co-Author 트레일러**를 붙인다). 이미 커밋됐으면 스킵. 게이트는 반드시 스테이징 **후**에 돌린다 — add 이전 검사로는 untracked였던 민감 파일이 잡히지 않는다:
   - 게이트: `automation/bin/sensitive-gate.sh` 실행. `SENSITIVE_GATE result=PASS` → 커밋 진행. `result=BLOCKED`(종료코드 1) → 출력된 파일을 `git reset HEAD <파일>`로 해제하고 보고 후 중단. `result=ERROR` → git 조회 실패(fail-closed) — 통과로 취급하지 않고 보고 후 중단.
2. `git pull origin main --rebase` — 충돌 시 `git rebase --abort` 후 보고하고 중단. (ship Preflight에서 이미 rebase했으므로 보통 no-op이다 — Phase 2 검증이 도는 동안 main이 움직인 경우의 안전망.)
3. `git push --force-with-lease -u origin "<브랜치명>"`
4. PR 확보 — **기존 PR 확인부터** (재시작 시 같은 브랜치에 열린 PR이 이미 있다):
   - `gh pr list --head "<브랜치명>" --base main --state open --json number --jq '.[0].number // empty'` — 번호가 나오면 **생성을 스킵**하고 그 번호를 `<PR 번호>`로 재사용한다 (3의 push만으로 같은 PR이 업데이트되고 체크가 재실행된다).
   - 없으면 `.github/PULL_REQUEST_TEMPLATE.md` placeholder를 채워 `gh pr create --base main --head "<브랜치명>" --title "[<TYPE>/#이슈] <설명>"` (이슈 없으면 `[<TYPE>] <설명>`):
     - 본문의 각 섹션(작업 내용·변경 사항)을 변경 내용으로 채운다. **검증/체크리스트**는 실제 수행한 항목만 체크한다(Phase 2 로컬 검증 통과 항목 포함). 「스펙·수용 기준」은 `/implement` 경로가 아니면 제거한다.
     - 연결 이슈가 있으면 「관련 이슈」에 `Closes #N`을, 없으면 그 섹션을 비운다.
     - **라벨·담당자를 붙이지 않는다** (자동화 없음).
     - 출력 URL 끝 숫자가 `<PR 번호>`.
5. 머지 게이트 폴링: **`automation/bin/merge-gate.sh <PR 번호> test`** (30초 간격, 최대 15분) → `MERGE_GATE_DONE` 마커. **게이트 = `mergeable` MERGEABLE + 앵커 체크(`test`) 존재 + 비차단 예외를 제외한 모든 체크가 완료·성공 (실패 0, 대기 0)** — 정확한 판정 로직은 `automation/bin/merge-gate.jq`가 원천이다(`smoke-test.sh`가 fixture로 검증한다). 체크를 이름 허용목록으로 고르지 않으므로 잡이 추가·개명돼도 게이트가 자동으로 따라간다. 앵커는 「CI가 체크를 등록하기 전의 공집합 통과」를 막기 위해 존재만 확인하는 이름이다.
   - 이 클라이언트 게이트는 관측·진행 판단용이고, **강제는 `main` 브랜치 보호의 몫이다. 2026-09-11 실측으로 이 레포 `main`에는 브랜치 보호가 없다** (`gh api repos/Cure-Agent/medical-agentic-rag/branches/main/protection` → `404 Branch not protected`). 설정한다면 required status checks는 CI 잡 넷(`lint`·`typecheck`·`test`·`gitleaks`)이다. CI 잡 이름을 바꾸면 브랜치 보호와 위 앵커도 함께 갱신한다.

   > 목록을 손으로 적어 두면 실제 설정과 갈라진다. **판정의 원천은 서버 설정이지 이 문장이 아니다.** 의심스러우면 위 `gh api`로 확인하고, 확인한 값과 다르면 문장 쪽을 고친다.

   - `result=PASS` → `gh pr merge <PR 번호> --squash`. **`--auto`를 쓰지 않는 이유**: 머지를 서버에 위임하면 게이트 실패 시 아무 일도 일어나지 않은 채 대기만 남아 "실패 → 즉시 로그 요약·보고"라는 이 파이프라인의 실행 흐름이 끊긴다.
     - **`--squash`를 `--merge`로 바꾸지 않는다**: GitHub Actions는 `push` 이벤트 런의 제목으로 **head 커밋 메시지 첫 줄**을 쓴다(`pull_request` 이벤트만 PR 제목을 쓴다). `ci.yml`은 `push: [main]`과 `pull_request` 양쪽에 걸려 있으므로, squash로 머지해야 main push CI 런 제목이 앞선 PR CI 런 제목과 일치한다. merge commit으로 머지하면 런 제목이 `Merge pull request #N from Cure-Agent/<브랜치>`가 되어 **어느 변경의 CI인지 런 목록에서 식별할 수 없고**, main 히스토리에도 브랜치 커밋이 그대로 섞여 「한 줄 = 한 변경」이 깨진다.
   - `result=FAIL*` → 마커의 `failed=` 목록에 있는 체크의 로그를 요약해(`gh run view --log-failed` 등) 보고하고 중단. `CONFLICTING`·`TIMEOUT`·`API_ERROR`도 각각 요약해 중단.
   - `result=ERROR` → jq 부재 또는 판정 출력 이상(fail-closed) — 체크 실패가 아니라 실행 환경 문제다. 통과로 취급하지 않고 환경을 확인해 보고 후 중단.
6. `MERGE_SHA=$(gh pr view <PR 번호> --json mergeCommit --jq '.mergeCommit.oid')`
7. `git checkout main && git pull origin main`
   - `main`이 기본 브랜치이므로 PR 본문의 `Closes #N`은 머지 시 이슈를 **자동으로 닫는다**(연결 이슈가 있는 경우).

## Step 2: 정리 & 보고

각 명령이 실패해도 **중단하지 않고 계속 진행**한다(실패 내역만 기록):
1. `git branch -D "<브랜치명>"` — squash 머지는 새 SHA를 만들어 `-d`가 거부한다. Step 1-5에서 머지가 확인된 브랜치만 이 경로로 지운다.
2. `git push origin --delete "<브랜치명>"` (이미 삭제됐으면 무시)
3. 최종 보고: 머지 커밋(`MERGE_SHA`) + 정리 실패 내역(있으면). 머지 성공 + 정리 실패는 파이프라인 실패로 취급하지 않는다. **서버 반영은 이 보고의 범위가 아니다** — cure-agent-be가 맡는다.

## 실패 시 재시작 규칙

feature 브랜치와 PR은 재사용한다 — 수정 커밋을 **같은 브랜치에 push**하면 CI 체크가 재실행되고 같은 PR이 업데이트된다.

| 실패 시점 | 원인 | 재시작 |
|-----------|------|--------|
| Step 1-5 | CI 체크 실패 | 코드 수정 → **Step 1** (같은 브랜치에 새 커밋 push, 게이트 폴링 재개) |
| Step 1-5 | PR 충돌(`CONFLICTING`) | `git pull origin main --rebase`로 해결 → **Step 1** |

## 민감 파일 커밋 금지

`git add/commit/push` 대상에 아래 민감 파일이 포함되지 않도록 커밋 전 확인한다 (기본 이름 및 `<이름>.*` 확장 변형 포함). 기계적 검사는 Step 1의 **`automation/bin/sensitive-gate.sh`**(스테이징 후 `git diff --cached` 검사)가 수행한다 — **스크립트의 정규식이 패턴의 단일 원천**이고, 아래 목록과 훅의 배열은 사람용 요약/이중 방어다(불일치하면 스크립트가 우선). 패턴 변경은 스크립트에서 하고 훅을 같이 갱신한다:

- `.env` (및 `.env.local`, `.env.prod` 등 `.env.*` — `.env.example` 제외). `OPENAI_API_KEY`·`DATABASE_URL`이 여기 산다
- `.claude/settings.local.json`
- `.mcp.json` (로컬 IDE MCP 연결 — 개인 환경 전용, .gitignore 대상)
- `certs/` 하위 키·인증서, `*.pem`, `*.p12`, `*.p8`, `*.key`
- `id_rsa*`

발견 시 커밋하지 말고 사용자에게 보고한다 — 스테이징돼 있으면 `git reset HEAD <file>`로 해제한다.

> **하네스별 적용**: 모든 하네스가 Step 1에서 `sensitive-gate.sh`를 실행한다. Claude Code는 PreToolUse 훅(`.claude/hooks/validate-git-sensitive.sh`)이 `git add/commit/push`를 추가로 자동 차단한다(이중 방어). **Codex는 스크립트 실행 + 규칙 직접 준수**로 커버한다. 어느 하네스든 `.gitignore` + GitHub push protection + CI 단 gitleaks(기본 규칙셋)와 병행되는 것을 전제로 한다.

## 규칙

- 모든 `gh`/`git` 실패 시 에러 내용을 사용자에게 보고한다.
- PR 생성 시 `.github/PULL_REQUEST_TEMPLATE.md` 형식을 준수하고, **라벨·담당자는 부여하지 않는다**.
- **머지는 항상 `--squash`다** — `--merge`·`--rebase`로 바꾸지 않는다. 이유는 Step 1-5 참조.
- **CI 체크 통과 없이 머지하지 않는다.** 브랜치 보호가 없는 동안은 이 규칙과 머지 게이트가 유일한 강제다 — 게이트가 실패하면 `gh pr merge`를 부르지 말고 게이트 실패로 보고한다.
- **실패 시 재시작 규칙을 반드시 따른다.**
