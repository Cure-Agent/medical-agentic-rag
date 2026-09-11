#!/usr/bin/env bash
# 민감 파일 커밋 게이트 — 스테이징된 파일에 비밀·키·로컬 설정이 없는지 검사한다.
# 반드시 git add(스테이징) 후에 실행한다 — add 이전 검사로는 untracked였던 민감 파일이 잡히지 않는다.
#
# 이 정규식이 민감 파일 패턴의 단일 원천이다. automation/pipeline.md 「민감 파일 커밋 금지」의
# 목록과 .claude/hooks/validate-git-sensitive.sh(Claude Code 이중 방어)의 배열은 이 정규식을
# 따라간다 — 패턴 변경은 여기서 하고 둘을 같이 갱신한다.
# 게이트는 fail-closed다 — git 조회가 실패하면 통과가 아니라 ERROR로 종료한다.
# 종료 마커: SENSITIVE_GATE result=PASS|BLOCKED|ERROR
#   BLOCKED(종료코드 1)면 출력된 파일을 git reset HEAD <파일>로 해제하고 사용자에게 보고한다.
set -u

STAGED=$(git diff --cached --name-only) \
  || { echo "SENSITIVE_GATE result=ERROR"; exit 1; }
# *.example은 플레이스홀더 템플릿이라 커밋 허용 (.env.example — .gitignore의 !.env.example과 정합)
MATCHES=$(printf '%s\n' "$STAGED" | grep -E \
  '(^|/)\.env(\..+)?$|(^|/)\.claude/settings\.local\.json$|(^|/)\.mcp\.json$|(^|/)certs/|\.(pem|p12|p8|key)$|(^|/)id_rsa[^/]*$' \
  | grep -v -E '\.example$' \
  || true)

if [ -n "$MATCHES" ]; then
  echo "차단된 민감 파일:"
  # 여러 줄 들여쓰기는 sed가 더 명확하다
  # shellcheck disable=SC2001
  echo "$MATCHES" | sed 's/^/  /'
  echo "SENSITIVE_GATE result=BLOCKED"
  exit 1
fi
echo "SENSITIVE_GATE result=PASS"
