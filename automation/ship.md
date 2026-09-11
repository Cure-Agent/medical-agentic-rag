# ship — 코드 변경 배포 자동화 (하네스 중립 명세)

워킹 트리의 코드 변경사항을 검증하고 `main` 머지까지 자동화한다.
코드 수정이 이미 완료된 상태에서 사용한다 — 문제 분석·코드 변경은 수행하지 않는다.

> **이 파일은 하네스 중립 「진실의 원천」이다.** 직접 실행 대상이 아니라, 사용하는 LLM 하네스의 진입 어댑터를 통해 실행된다:
> - Claude Code → `.claude/commands/ship.md`
> - Codex → `.codex/skills/ship/SKILL.md`
>
> 각 어댑터는 **폴링 모드**와 **안전 규칙 적용 방식**(훅 유무)을 지정한다. Phase 4의 CI 체크 폴링은 `automation/pipeline.md`의 「폴링 실행 규칙」에 정의된 **하네스별 모드**를 따른다.

## 워크플로우 전제 (agent)

- **PR-퍼스트**: 이슈를 새로 만들지 않는다. `<prefix>/<슬러그>` 브랜치 → `main` PR → squash 머지. 브랜치 모델은 cure-agent-fe와 같은 **`main` 단일**이다 — BE의 `dev` PR → 배포 PR 2단계가 없다. 사람이 이미 만든 이슈가 있으면 브랜치 슬러그에 번호를 넣고 PR 본문 `Closes #N`으로 연결한다(선택).
- **라벨·담당자 자동화는 없다.** ship은 라벨·담당자를 부여하지 않는다 — 컨벤션에 맞는 PR 제목·브랜치명만 만든다.
- **이 레포 파이프라인의 끝은 `main` 머지다. 서버 배포는 cure-agent-be가 맡는다.** 이 레포에는 CD 워크플로우가 없고 ship은 배포를 기다리지 않는다 — FE판의 Vercel 배포 대기 단계를 옮기지 않았다.
- **`docs/experiments/`는 시점 증거다.** 기존 실험 문서의 관측 숫자를 고치는 변경을 싣지 않는다 — 정정은 새 문서와 `superseded`·`erratum` 배너로 한다(`docs/experiments/INDEX.md`).

## Preflight

1. `gh auth status` — 인증 실패 시 **중단**.
2. `git fetch origin main` — 이후 모든 main 대비 비교(변경 유무 판정 등)는 **`origin/main` 기준**이다. 로컬 `main`은 뒤처져 있을 수 있으므로 비교 기준으로 쓰지 않는다.
3. 현재 브랜치 판별:
   - **feature 브랜치** (`<prefix>/...`, `main` 아님):
     - 브랜치 prefix에서 타입을 파싱한다.
     - `git log origin/main..HEAD`와 `git status --porcelain`으로 변경사항 확인 — 커밋도 변경도 없으면 **중단** ("배포할 변경사항이 없습니다"). 변경 유무 판정에 `git diff`(무인자)를 쓰지 않는다 — staged 변경이 보이지 않는다.
     - 최신 동기화: `git pull --rebase --autostash origin main` — 충돌 처리는 main 경로와 동일하다(자동 해결하지 않는다). 이 rebase로 Phase 1의 분석 diff에 main 쪽 무관한 변경이 섞이지 않고, Phase 2가 실제 push될 트리를 검증하며, `automation/pipeline.md` Step 1-2의 rebase는 사실상 no-op이 된다.
     - **Phase 3(브랜치 생성)을 스킵**한다 (타입은 브랜치 prefix로 확정).
   - **`main`**:
     - `git log origin/main..HEAD --oneline` — **push 안 된 로컬 커밋이 있으면 중단**하고 커밋 목록과 함께 보고한다. `main` 직접 커밋은 ship이 배포하지 않는다 — 워킹트리만 보는 아래 판정이 이 커밋들을 놓치므로, 사용자가 브랜치로 옮기는 등 직접 처리해야 한다.
     - `git status --porcelain` — 변경사항이 없으면 **중단** ("배포할 변경사항이 없습니다").
     - 최신 동기화: `git pull --rebase --autostash origin main`. **충돌 시 자동 해결하지 않는다** — rebase 충돌은 `git rebase --abort`로 원상복구 후 보고하고 중단, autostash 재적용 충돌은 변경이 stash에 보존된 상태이므로(`git stash list`로 확인) 그대로 두고 보고하고 중단한다.
     - feature 브랜치는 Phase 3에서 생성한다.

## Phase 1: 변경사항 분석

> 사용자가 인자로 타입과 설명을 직접 제공한 경우 (예: `ship feat 인용 검증 추가`), 해당 값을 사용하고 사용자 확인 없이 Phase 2로 직행한다.

1. `git diff origin/main`과 `git status --porcelain`(untracked 신규 파일 확인)으로 변경된 파일과 내용을 분석한다. 무인자 `git diff`를 쓰지 않는다 — staged·커밋된 변경이 보이지 않아, 변경이 모두 커밋된 feature 브랜치에서는 분석 대상이 비어 보인다.
2. 변경 성격에 맞는 타입을 결정한다:

   | 타입 | 브랜치 prefix | PR 제목 |
   |------|---------------|---------|
   | 기능 | `feat/` | `[FEAT/#이슈] 설명` |
   | 운영 문제 | `bug/` | `[BUG] 설명` |
   | 버그 수정 | `fix/` | `[FIX] 설명` |
   | 리팩토링 | `refactor/` | `[REFACTOR] 설명` |
   | 테스트 | `test/` | `[TEST/#이슈] 설명` |
   | 문서 | `docs/` | `[DOCS/#이슈] 설명` |
   | 기타 | `chore/` | `[CHORE] 설명` |
   | CI | `ci/` | `[CI] 설명` |

   연결 이슈가 없으면 `/#이슈`를 생략한다 (예: `[FEAT] 설명`).

   **`[BUG]`과 `[FIX]`를 가른다 — 세기 위해서다.**
   - `[BUG]` = **운영에서 관측된** 문제. 서비스에서 사용자나 로그가 드러낸 것.
   - `[FIX]` = 그 밖의 자체 발견 수정. 머지 전에 잡은 것, 계약 정합 등.

   하나의 `[FIX]`가 두 부류를 같이 담으면 운영 문제 표본을 셀 수 없다. `git log --grep='^\[BUG\]'`이 그 표본 목록이고, `automation/problem.md`의 라우팅 게이트가 실제로 옳게 잡고 있는지를 사후에 되짚는 근거다. prefix를 `fix/`와 갈라 둔 이유는 Preflight가 **브랜치 prefix에서 타입을 파싱**하기 때문이다 — 같은 prefix를 쓰면 자동화가 둘을 구분하지 못한다.
3. 변경 내용을 한 줄로 요약한다.
4. **사용자 확인 대기**:
   ```
   변경사항 요약:
   - 타입: feat
   - 설명: ...
   - 변경 파일: N개

   이대로 진행할까요? (타입이나 설명을 변경하려면 알려주세요)
   ```

## Phase 2: 검증

CI(`.github/workflows/ci.yml`)의 잡은 넷이다(`lint`·`typecheck`·`test`·`gitleaks`). 그중 셋을 **같은 명령으로** 로컬 선검증하고, CI에 없는 검사 둘을 조건부로 더한다:

1. **린트**: `make lint`
2. **타입 검사**: `make typecheck` — 동결 게이트의 「빌드 GREEN」과 같은 검사다
3. **단위 테스트**: `make test`
4. **e2e**: `make test-e2e` — **CI 잡이 아직 없으므로**, e2e 테스트가 생긴 뒤에는 여기가 머지 전 유일한 실행 지점이다. 테스트가 없으면 타깃이 알리고 통과한다. `/problem`으로 들어온 변경은 그 Phase 5-2가 이미 돌렸다
5. **게이트 스크립트 스모크**: `automation/bin/`을 고친 변경이면 `automation/bin/smoke-test.sh` — 이것도 CI 잡이 아니다

**`gitleaks`만 로컬 선검증에서 뺀다** — 히스토리 시크릿 스캔이라 어느 변경이든 로컬 선검증 대상이 아니고, 머지 게이트가 잡는다.

- 게이트 실패 시 → 실패 내용을 사용자에게 보고하고 **중단**한다 (자동 수정하지 않는다). 원인 확인은 파일 읽기 도구로 최소한만 하고, 심층 진단하지 않는다.
- 의존성 변경(`pyproject.toml`)이 있으면 `pip install -e ".[dev]"`로 먼저 맞춘다.

## Phase 3: 브랜치 (이슈 생성 없음)

> Preflight에서 이미 feature 브랜치로 진입한 경우 이 Phase를 스킵한다.

1. feature 브랜치 생성: `<prefix>/<슬러그>` → checkout (변경사항은 워킹 트리에 그대로 유지됨).
   - 슬러그는 변경 내용을 나타내는 짧은 **영문 kebab-case**로 만들고, 연결 이슈가 있으면 앞에 번호를 붙인다 (예: `feat/12-citation-check`).
2. 이슈는 생성하지 않는다. 연결할 기존 이슈가 있으면 번호를 기억해 Phase 4의 PR 본문 `Closes #N`에 넣는다.

## Phase 4: PR·머지

`automation/pipeline.md`를 읽고 Step 1부터 실행한다.
CI 체크 대기 폴링은 `automation/pipeline.md`의 「폴링 실행 규칙」에 정의된 **현재 하네스의 폴링 모드**를 따른다 (진입 어댑터가 지정).

전달할 컨텍스트:
- 브랜치명, 타입, 설명 (Phase 1 요약 또는 사용자 인자)
- (선택) 연결 이슈번호

## 규칙

- Phase 1~3에서는 코드를 수정하지 않는다 — 이미 완료된 변경사항을 검증하는 것이 목적이다.
- 검증 실패 시 (Phase 2) 자동 수정하지 않고 사용자에게 보고한다.
- **라벨·담당자를 부여하지 않는다.**
- 모든 `gh`/`git` 명령 실패 시 에러 내용을 사용자에게 보고한다.
- PR 생성 시 `.github/PULL_REQUEST_TEMPLATE.md` 형식을 준수한다.
- **민감 파일 커밋 금지**: 과정의 `git add/commit/push`는 `automation/pipeline.md`의 「민감 파일 커밋 금지」 규칙을 따른다.
