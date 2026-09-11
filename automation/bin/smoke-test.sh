#!/usr/bin/env bash
# automation/bin 게이트 스크립트 스모크 테스트 — automation/bin을 고친 변경이면 로컬에서 돌린다.
# (CI 잡은 lint·typecheck·test·gitleaks 넷이고 이 스크립트는 거기 없다 — automation/ship.md Phase 2)
# 핵심 검증 대상 둘: (1) fail-closed 성질 — git 조회 실패가 통과(PASS)로 새지 않고
# ERROR(비 0 종료)로 끝나는지, (2) merge-gate.jq 판정 로직 — fixture JSON으로 게이트 규칙을 검증.
# gh 호출이 필요 없는 경로만 검사한다. 의존성은 git·jq뿐이다.
# 종료 마커: SMOKE result=PASS|FAIL
set -u

BIN=$(cd "$(dirname "$0")" && pwd) || exit 1
command -v jq >/dev/null 2>&1 \
  || { echo "jq가 필요합니다 (merge-gate 판정 테스트)"; echo "SMOKE result=FAIL"; exit 1; }
FAILED=0

# check <설명> <기대 종료코드> <기대 마커> <명령...>
check() {
  local desc=$1 want_rc=$2 want_marker=$3
  shift 3
  local out rc
  out=$("$@" 2>&1)
  rc=$?
  if [ "$rc" -ne "$want_rc" ] || ! grep -q "$want_marker" <<<"$out"; then
    echo "FAIL: $desc (rc=$rc, 기대 rc=$want_rc, 기대 마커 '$want_marker')"
    # 여러 줄 들여쓰기는 sed가 더 명확하다
    # shellcheck disable=SC2001
    sed 's/^/  | /' <<<"$out"
    FAILED=1
  else
    echo "ok: $desc"
  fi
}

# 스크래치 리포 — 사용자 리포의 인덱스·워킹트리를 건드리지 않는다.
TMP=$(mktemp -d) || exit 1
trap 'rm -rf "$TMP"' EXIT
git -C "$TMP" init -q
echo hi > "$TMP/note.txt"
git -C "$TMP" add note.txt
git -C "$TMP" -c user.email=smoke@test -c user.name=smoke commit -qm init
cd "$TMP" || exit 1

# --- sensitive-gate 판정 테스트
check "sensitive-gate: 민감 파일 없음 → PASS" \
  0 "SENSITIVE_GATE result=PASS" "$BIN/sensitive-gate.sh"
echo secret > .env
git add -f .env
check "sensitive-gate: .env 스테이징 → BLOCKED" \
  1 "SENSITIVE_GATE result=BLOCKED" "$BIN/sensitive-gate.sh"
git reset -q HEAD .env
echo 'KEY=' > .env.example
git add -f .env.example
check "sensitive-gate: .env.example 스테이징 → PASS (템플릿 예외)" \
  0 "SENSITIVE_GATE result=PASS" "$BIN/sensitive-gate.sh"
git reset -q HEAD .env.example
echo key > AuthKey_test.p8
git add -f AuthKey_test.p8
check "sensitive-gate: *.p8 스테이징 → BLOCKED" \
  1 "SENSITIVE_GATE result=BLOCKED" "$BIN/sensitive-gate.sh"
git reset -q HEAD AuthKey_test.p8
echo '{}' > .mcp.json
git add -f .mcp.json
check "sensitive-gate: .mcp.json 스테이징 → BLOCKED" \
  1 "SENSITIVE_GATE result=BLOCKED" "$BIN/sensitive-gate.sh"
git reset -q HEAD .mcp.json
check "sensitive-gate: git 실패 → ERROR (fail-closed)" \
  1 "SENSITIVE_GATE result=ERROR" env GIT_DIR=/nonexistent "$BIN/sensitive-gate.sh"

# --- merge-gate.jq 판정 fixture 테스트 (gh 없이 게이트 규칙 자체를 검증)
GATE_JQ="$BIN/merge-gate.jq"
TEST_OK='{"__typename":"CheckRun","name":"test","status":"COMPLETED","conclusion":"SUCCESS"}'
LINT_OK='{"__typename":"CheckRun","name":"lint","status":"COMPLETED","conclusion":"SUCCESS"}'

printf '{"mergeable":"MERGEABLE","statusCheckRollup":[%s,%s]}' \
  "$TEST_OK" "$LINT_OK" > fixture.json
check "merge-gate.jq: 전부 성공 → PASS" \
  0 "^PASS$" jq -r -f "$GATE_JQ" fixture.json --args test

printf '{"mergeable":"MERGEABLE","statusCheckRollup":[{"__typename":"CheckRun","name":"test","status":"COMPLETED","conclusion":"FAILURE"},%s]}' \
  "$LINT_OK" > fixture.json
check "merge-gate.jq: 앵커 체크 실패 → FAIL" \
  0 "^FAIL failed=test" jq -r -f "$GATE_JQ" fixture.json --args test

printf '{"mergeable":"MERGEABLE","statusCheckRollup":[%s,{"__typename":"CheckRun","name":"lint","status":"COMPLETED","conclusion":"FAILURE"}]}' \
  "$TEST_OK" > fixture.json
check "merge-gate.jq: 비앵커 체크 실패도 차단 (전수 검사) → FAIL" \
  0 "^FAIL failed=lint" jq -r -f "$GATE_JQ" fixture.json --args test

printf '{"mergeable":"MERGEABLE","statusCheckRollup":[%s]}' \
  "$LINT_OK" > fixture.json
check "merge-gate.jq: 앵커 부재 → PENDING (vacuous pass 방지)" \
  0 "^PENDING anchors_absent=test" jq -r -f "$GATE_JQ" fixture.json --args test

printf '{"mergeable":"MERGEABLE","statusCheckRollup":[%s,{"__typename":"CheckRun","name":"lint","status":"IN_PROGRESS","conclusion":null}]}' \
  "$TEST_OK" > fixture.json
check "merge-gate.jq: 진행 중 체크 있음 → PENDING" \
  0 "^PENDING .*pending=lint" jq -r -f "$GATE_JQ" fixture.json --args test

printf '{"mergeable":"CONFLICTING","statusCheckRollup":[%s,%s]}' \
  "$TEST_OK" "$LINT_OK" > fixture.json
check "merge-gate.jq: 충돌 → FAIL mergeable=CONFLICTING" \
  0 "mergeable=CONFLICTING" jq -r -f "$GATE_JQ" fixture.json --args test

printf '{"mergeable":"MERGEABLE","statusCheckRollup":[]}' > fixture.json
check "merge-gate.jq: 체크 0개 → PENDING (vacuous pass 방지)" \
  0 "^PENDING" jq -r -f "$GATE_JQ" fixture.json --args test

check "merge-gate.sh: 인자 부족 → usage 에러" \
  2 "usage" "$BIN/merge-gate.sh" 1

# --- validate-git-sensitive 훅 판정 테스트 (모의 PreToolUse 입력 — 훅은 git add를 실행하지 않는다)
# 게이트(sensitive-gate.sh)와 짝인 Claude Code 이중 방어선이다. -f 강제 추가·플래그 인자·디렉터리
# pathspec 케이스는 cure-agent-fe판 훅이 놓치던 입력이다 (훅 상단 「git add」 주석의 ①~③).
HOOK="$BIN/../../.claude/hooks/validate-git-sensitive.sh"
hook() {
  local out
  out=$(jq -n --arg c "$1" '{tool_input:{command:$c}}' | "$HOOK") \
    || { echo "HOOK result=ERROR"; return 1; }
  if [ -n "$out" ]; then echo "HOOK result=DENY"; else echo "HOOK result=ALLOW"; fi
}

HR="$TMP/hookrepo"
mkdir -p "$HR"
git -C "$HR" init -q
printf '.env\n' > "$HR/.gitignore"
echo ok > "$HR/ok.txt"
git -C "$HR" add .gitignore ok.txt
git -C "$HR" -c user.email=smoke@test -c user.name=smoke commit -qm init
echo secret > "$HR/.env"
cd "$HR" || exit 1

check "hook: 일반 파일 add → ALLOW" 0 "HOOK result=ALLOW" hook 'git add ok.txt'
check "hook: git add . (무시된 .env는 올라가지 않음) → ALLOW" 0 "HOOK result=ALLOW" hook 'git add .'
check "hook: git add -f .env (무시 파일 강제 추가) → DENY" 0 "HOOK result=DENY" hook 'git add -f .env'
check "hook: git add -f . (무시된 .env까지 올라감) → DENY" 0 "HOOK result=DENY" hook 'git add -f .'
mkdir -p certs && echo key > certs/server.key
check "hook: git -C . add -f certs/server.key (글로벌 옵션 값 오인 없음) → DENY" \
  0 "HOOK result=DENY" hook 'git -C . add -f certs/server.key'
check "hook: git add certs (디렉터리 pathspec) → DENY" 0 "HOOK result=DENY" hook 'git add certs'
check "hook: git add -A (untracked 키) → DENY" 0 "HOOK result=DENY" hook 'git add -A'
rm -rf certs
git add -f .env
check "hook: .env가 스테이징된 채 commit → DENY" 0 "HOOK result=DENY" hook 'git commit -m x'
git reset -q HEAD .env
check "hook: add·commit·push가 아닌 git 명령 → ALLOW" 0 "HOOK result=ALLOW" hook 'git status'
cd "$TMP" || exit 1

if [ "$FAILED" -ne 0 ]; then
  echo "SMOKE result=FAIL"
  exit 1
fi
echo "SMOKE result=PASS"
