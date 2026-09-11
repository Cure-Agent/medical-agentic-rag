#!/usr/bin/env bash
#
# Claude Code PreToolUse hook: 동결 테스트 파일의 Edit/Write 차단 (작성: Codex 또는 Claude 단독 폴백)
#
# frozen-tests.txt가 없으면 무조건 통과.
# 존재하면 대상 file_path가 목록에 있는지 확인하여 deny.
#
# frozen-tests.txt는 리포 루트 기준 상대 경로를 기록한다 (automation/freeze.md 「동결 등록」).
# 대상 파일을 그 파일이 속한 git 워크트리 루트 기준 상대 경로로 정규화하여 비교한다.
#
set -e

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

[ -n "$FILE_PATH" ] || exit 0

FROZEN_LIST="${CLAUDE_PROJECT_DIR:-.}/.cure-implement/frozen-tests.txt"
[ -f "$FROZEN_LIST" ] || exit 0

# 대상 파일 → 절대 경로
if [[ "$FILE_PATH" != /* ]]; then
  FILE_PATH="$(pwd)/$FILE_PATH"
fi

# 대상 파일이 속한 git 워크트리 루트 기준 상대 경로로 정규화
FILE_DIR=$(dirname "$FILE_PATH")
TOPLEVEL=$(git -C "$FILE_DIR" rev-parse --show-toplevel 2>/dev/null || echo "")
if [ -n "$TOPLEVEL" ]; then
  REL_PATH="${FILE_PATH#"$TOPLEVEL"/}"
else
  REL_PATH="$FILE_PATH"
fi

while IFS= read -r frozen; do
  [ -z "$frozen" ] && continue
  # 상대 경로(정규화) 또는 절대 경로 양쪽 매칭 지원
  if [ "$REL_PATH" = "$frozen" ] || [ "$FILE_PATH" = "$frozen" ]; then
    jq -n --arg f "$REL_PATH" '{
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: ("🧊 TEST-FREEZE: " + $f + " — 동결된 수용 기준 테스트입니다. 구현 코드를 수정하여 통과시키세요. 테스트가 틀렸다고 판단되면 직접 고치지 말고 automation/freeze.md 「이의 제기(TEST-DISPUTE)」를 따르세요.")
      }
    }'
    exit 0
  fi
done < "$FROZEN_LIST"

exit 0
