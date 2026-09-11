---
description: 운영 중 문제 분석부터 머지까지 — 진단 → 라우팅 판정 → 테스트 동결 → 구현 → PR·머지
argument-hint: <문제 상황 또는 증상 (예: 근거가 있는데 기권한다 + 질문·trace)>
---

# /problem — 문제 해결 하네스 (Claude Code 어댑터)

이 명령은 하네스 중립 문제 해결 워크플로우의 **Claude Code 진입점**이다.
절차 원본은 `automation/problem.md`에 있다 (하위 절차: `automation/freeze.md`, `automation/ship.md`).

**운영 중 변경은 무조건 이 명령으로 시작한다.** `/implement`는 스펙이 이미 있는 계획된 스텝의
진입점이지 사람이 고르는 갈림길이 아니다 — 하네스 선택은 진단 결과에 의존하므로,
`automation/problem.md` Phase 1.5가 **진단 후에 기계적으로** 판정한다.

## 실행

`automation/problem.md`를 읽고 Preflight → Phase 1~6을 그대로 실행한다.
`$ARGUMENTS`(증상·에러·재현 경로)는 Phase 1의 입력이다. 인자가 비어 있으면 사용자에게 문제 상황을 요청한다.

## Claude Code 하네스 설정 (원본 명세에 주입)

- **병렬 실행**: Phase 4에서 그룹이 2개 이상이면 각 그룹을 **Agent tool로 동시에 호출**한다
  (한 응답에 여러 tool use를 넣어야 실제로 동시 실행된다). 각 에이전트는 `git worktree add`로 만든
  독립 워크트리에서 작업하며, 완료 보고에 커밋 SHA 목록을 반드시 포함한다.
  **에이전트에게 `make test-e2e`를 시키지 않는다** — 통합 후 메인 세션이 일괄 수행한다(원본 「병렬 실행」).
- **테스트 동결 훅**: `automation/freeze.md`의 동결 등록은 PreToolUse 훅
  (`.claude/hooks/freeze-test-files.sh`)이 Edit/Write를 차단하는 방식으로 작동한다 — 훅은 도구 호출만
  막으므로 Bash 우회 편집(`sed -i` 등)은 Phase 5의 사후 감사가 잡는다(훅=예방, diff=감사).
- **Codex 호출**: `automation/freeze.md`의 4중 hang 방어를 그대로 적용한다. 긴 실행은
  `run_in_background: true`가 기본이며, 강제 kill(perl alarm 등)은 금지한다 — 지연 파일 착지가
  동결 무결성을 오염시킨다.
- **폴링 모드**: Phase 6의 CI 체크 대기는 `automation/bin/merge-gate.sh`를
  **`run_in_background: true` Bash**로 실행한다. 포그라운드 `sleep` 차단·Monitor 도구 금지.
  메인 세션이 직접 수행하고 서브에이전트에 위임하지 않는다.
- **Co-Author 트레일러**: 커밋 메시지 끝에 현재 세션의 시스템 규칙이 지정하는 `Co-Authored-By:`
  트레일러를 붙인다.
- **안전 규칙**: 「민감 파일 커밋 금지」는 커밋 전 `automation/bin/sensitive-gate.sh` 실행으로 기계
  검사하고, PreToolUse 훅(`.claude/hooks/validate-git-sensitive.sh`)이 `git add/commit/push`를 추가로
  자동 차단한다 — 이중 방어로 작동한다.
