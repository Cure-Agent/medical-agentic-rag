#!/usr/bin/env bash
# 머지 게이트 폴링 — PR이 머지 가능해질 때까지 대기한다 (30초 간격, 최대 15분).
#
# 게이트 = mergeable MERGEABLE
#        + 앵커 체크 존재 — 체크 등록 전 공집합 통과(vacuous pass) 방지
#        + 비차단 예외를 제외한 모든 체크가 완료·성공 (실패 0, 대기 0, 체크 1개 이상)
# 체크를 이름 허용목록으로 고르지 않는다 — 잡이 추가·개명돼도 게이트가 자동으로 따라간다.
# 판정 로직·비차단 예외 목록의 원천은 merge-gate.jq다 (smoke-test.sh가 fixture로 검증한다).
# 이 클라이언트 게이트는 관측·진행 판단용이고, 강제는 main 브랜치 보호(required status checks)의
# 몫이다 — 설정 현황은 automation/pipeline.md Step 1-5.
#
# 사용법: merge-gate.sh <PR번호> <앵커 체크명>...     예) merge-gate.sh 12 test
# 종료 마커: MERGE_GATE_DONE result=PASS|FAIL...|TIMEOUT...|API_ERROR|ERROR
#   ERROR = jq 부재 또는 판정 출력 이상 (fail-closed — TIMEOUT까지 기다리지 않고 즉시 종료)
set -u

if [ $# -lt 2 ]; then
  echo "usage: $0 <PR번호> <앵커 체크명>..." >&2
  exit 2
fi
PR=$1
shift
BIN=$(cd "$(dirname "$0")" && pwd) || exit 1
command -v jq >/dev/null 2>&1 \
  || { echo "jq를 찾을 수 없다 — 게이트 판정에 필요하다"; echo "MERGE_GATE_DONE result=ERROR"; exit 1; }
INTERVAL=30
LIMIT=900
API_ERRORS=0
SECONDS=0

while :; do
  # GitHub 조회 실패를 pending으로 삼키지 않는다 — 3회 연속 실패 시 API_ERROR로 종료.
  if S=$(gh pr view "$PR" --json mergeable,statusCheckRollup); then
    API_ERRORS=0
  else
    API_ERRORS=$((API_ERRORS + 1))
    echo "GitHub API query failed ($API_ERRORS/3)"
    if [ "$API_ERRORS" -ge 3 ]; then echo "MERGE_GATE_DONE result=API_ERROR"; exit 1; fi
    sleep "$INTERVAL"; continue
  fi
  RESULT=$(echo "$S" | jq -r -f "$BIN/merge-gate.jq" --args "$@") || RESULT=""
  case "$RESULT" in
    PASS)     echo "MERGE_GATE_DONE result=PASS"; exit 0 ;;
    FAIL*)    echo "MERGE_GATE_DONE result=$RESULT"; exit 1 ;;
    PENDING*) : ;;
    # 판정이 비었거나 형식이 다르면 조용히 TIMEOUT까지 돌지 않고 즉시 끝낸다 (fail-closed).
    *) echo "판정 출력이 비었거나 형식이 다르다: '$RESULT'"
       echo "MERGE_GATE_DONE result=ERROR"; exit 1 ;;
  esac
  if [ "$SECONDS" -ge "$LIMIT" ]; then echo "MERGE_GATE_DONE result=TIMEOUT $RESULT"; exit 1; fi
  sleep "$INTERVAL"
done
