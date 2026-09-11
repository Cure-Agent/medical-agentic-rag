#!/usr/bin/env bash
#
# Claude Code PreToolUse hook: git 명령에서 민감 파일 감지 시 차단
#
# 설계 노트:
# - 위협 모델은 '악의적 우회'가 아니라 'LLM의 부주의한 커밋'이다.
#   deny는 민감 파일이 실제로 존재할 때만 발동하므로, 명령 감지의
#   false positive 비용은 '검사 한 번 더 도는 것'뿐이다.
# - 이 hook은 automation/bin/sensitive-gate.sh(파이프라인 게이트)와 병행하는
#   이중 방어선이다. 패턴 원천은 sensitive-gate.sh의 정규식 — 변경 시 함께 갱신한다.
#   .gitignore + GitHub push protection + CI 단 gitleaks와 병행하는 것을 전제로 한다.
# - 판정 회귀는 automation/bin/smoke-test.sh가 모의 입력으로 검증한다.
#
set -e

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# git 호출 지점 매칭: 명령 시작 / && ; | 체인 / 서브셸 / command 프리픽스,
# git -C <dir>, --git-dir, --work-tree, -c key=val 글로벌 옵션 허용
GIT_PREFIX='(^|[;&|(`]|\$\()[[:space:]]*(command[[:space:]]+)?git([[:space:]]+(-C[[:space:]]+[^[:space:]]+|--git-dir(=[^[:space:]]+)?|--work-tree(=[^[:space:]]+)?|-c[[:space:]]+[^[:space:]]+))*[[:space:]]+'

has_git_sub() { echo "$COMMAND" | grep -qE "${GIT_PREFIX}$1\b"; }

if ! has_git_sub '(add|commit|push)'; then
  exit 0
fi

SENSITIVE_PATTERNS=(
  ".claude/settings.local.json"
  ".mcp.json"
  ".env"
  "certs/*"
  "*.pem"
  "*.p12"
  "*.p8"
  "*.key"
  "id_rsa*"
)

BLOCKED_FILES=()

matches_sensitive() {
  local file="$1" pattern base
  # basename 명령 대신 확장으로 자른다 — `-`로 시작하는 이름을 옵션으로 읽고 죽지 않게
  base=${file%/}
  base=${base##*/}
  # *.example은 플레이스홀더 템플릿이라 커밋 허용 — sensitive-gate.sh의 예외와 정합
  case "$base" in
    *.example) return ;;
  esac
  for pattern in "${SENSITIVE_PATTERNS[@]}"; do
    # 패턴의 glob 매칭(*.pem 등)이 의도이므로 case 패턴을 따옴표로 감싸지 않는다
    # shellcheck disable=SC2254
    case "$file" in
      $pattern|*/$pattern) BLOCKED_FILES+=("$file"); return ;;
    esac
    # .env.prod 같은 확장 변형 (basename 기준)
    # shellcheck disable=SC2254
    case "$base" in
      $pattern|${pattern}.*) BLOCKED_FILES+=("$file"); return ;;
    esac
  done
}

check_file_list() {
  # 주의: 반드시 `check_file_list < <(...)` 형태로 호출할 것.
  # `... | check_file_list`는 서브셸에서 실행되어 BLOCKED_FILES 변경이 유실된다.
  while IFS= read -r file; do
    [ -n "$file" ] && matches_sensitive "$file"
  done
}

# ── git add: 추가 후보 파일 검사 ──
# cure-agent-fe판에서 이식하며 고친 결함 세 가지 (2026-09-11, 케이스별 스크래치 리포 비교로 재현):
#   ① `.`로 시작하는 인자(.env, `-C .`의 값)를 광역 add로 오인해 무시 파일을 뺀 후보만 봤다 —
#      `git add -f .env`·`git add -f .`가 통과했다. BSD sed가 `\b`를 몰라 `git add` 앞부분이
#      인자에 그대로 남은 것도 오인을 키웠다
#   ② `-f` 같은 플래그를 파일로 넘겨 macOS basename이 옵션 오류로 죽었다 — 훅 오류는 차단이 아니다
#   ③ 디렉터리 pathspec(`git add keys`)을 펼치지 않아 그 아래 키 파일을 보지 못했다
if has_git_sub 'add'; then
  ADD_SEG=$(echo "$COMMAND" | grep -oE "${GIT_PREFIX}add([[:space:]][^;&|]*)?" | head -1)
  # git과 글로벌 옵션(-C <dir> 등)을 걷어내고 add 뒤 인자만 남긴다
  ADD_ARGS=$(printf '%s\n' "$ADD_SEG" | sed -E 's/^.*[[:space:]]add([[:space:]]+|$)//')
  FORCE=false
  ALL=false
  PATHSPECS=()
  set -f  # 인자의 *를 셸이 확장하지 않는다 — pathspec은 git이 해석한다
  # 인자들을 공백 분리하는 것이 의도다
  # shellcheck disable=SC2086
  for tok in $ADD_ARGS; do
    case "$tok" in -f|--force|-[!-]*f*) FORCE=true ;; esac
    case "$tok" in -A|--all|-u|--update|-[!-]*A*|-[!-]*u*) ALL=true ;; esac
    case "$tok" in -*) ;; *) PATHSPECS+=("$tok") ;; esac
  done
  set +f
  # 명시한 경로 이름 자체 — 아직 없는 파일이어도 이름으로 막는다
  if [ ${#PATHSPECS[@]} -gt 0 ]; then
    check_file_list < <(printf '%s\n' "${PATHSPECS[@]}")
  fi
  # pathspec이 실제로 끌어올 후보 — 인자가 없거나 -A/-u면 레포 전체
  if [ ${#PATHSPECS[@]} -eq 0 ] || [ "$ALL" = true ]; then
    SCOPE=(:/)
  else
    SCOPE=("${PATHSPECS[@]}")
  fi
  check_file_list < <(git ls-files --others --modified --exclude-standard -- "${SCOPE[@]}" 2>/dev/null)
  # -f는 .gitignore에 걸린 파일도 올린다 — 무시된 후보까지 본다 (통째로 무시된 디렉터리는 한 줄)
  if [ "$FORCE" = true ]; then
    check_file_list < <(git ls-files --others --ignored --exclude-standard --directory -- "${SCOPE[@]}" 2>/dev/null)
  fi
fi

# ── git commit: 스테이징 파일 + (-a 계열이면) tracked modified 검사 ──
if has_git_sub 'commit'; then
  check_file_list < <(git diff --cached --name-only 2>/dev/null)
  COMMIT_SEG=$(echo "$COMMAND" | grep -oE "${GIT_PREFIX}commit[^;&|]*" | head -1)
  # -a / -am / --all: 커밋 시점에 tracked modified를 스테이징 → --cached로 안 잡힘
  if echo "$COMMIT_SEG" | grep -qE '[[:space:]](-[a-zA-Z]*a[a-zA-Z]*|--all)\b'; then
    check_file_list < <(git diff --name-only 2>/dev/null)
  fi
fi

# ── git push: 아직 원격에 없는 커밋들에 포함된 파일 검사 ──
# (스테이징 검사는 push 대상이 아니므로 부정확 — 커밋된 파일을 봐야 한다)
if has_git_sub 'push'; then
  check_file_list < <(git log --branches --not --remotes --name-only --pretty=format: -n 300 2>/dev/null | sort -u)
fi

if [ ${#BLOCKED_FILES[@]} -gt 0 ]; then
  FILES_LIST=$(printf '%s\n' "${BLOCKED_FILES[@]}" | sort -u | paste -sd ', ' -)
  jq -n --arg files "$FILES_LIST" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: ("민감 파일 감지로 차단: " + $files + ". 스테이징 파일은 git reset HEAD <file>로 해제하고, 커밋에 이미 포함됐다면 해당 커밋을 정리한 뒤 재시도하세요.")
    }
  }'
  exit 0
fi

exit 0
