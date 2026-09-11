---
name: ship
description: 워킹 트리의 코드 변경을 검증하고 main PR→CI 체크→squash 머지까지 자동화한다 (하네스 중립 워크플로우의 Codex 어댑터). 사용자가 "ship", "배포해줘"(슬래시 없이 평문으로) 등 배포를 요청하면 사용한다. Codex에는 커스텀 슬래시 커맨드가 없으므로 "/ship"은 동작하지 않는다.
---

# ship (Codex 어댑터)

하네스 중립 배포 워크플로우의 **Codex 진입점**이다. 절차 원본은 리포의 `automation/ship.md`(+ `automation/pipeline.md`)에 있다. 그 파일들을 읽고 그대로 실행하되, 아래 Codex 전용 오버라이드를 적용한다.

> 이 레포의 파이프라인은 **`main` 머지에서 끝난다.** 서버 배포는 cure-agent-be가 맡는다 (`automation/ship.md` 「워크플로우 전제」).

## 실행 절차

1. `automation/ship.md`를 읽고 Preflight → Phase 1~4를 순서대로 수행한다. Phase 4에서 `automation/pipeline.md`를 읽어 PR·머지를 수행한다.
2. 사용자 인자(타입+설명, 예: `feat 인용 검증 추가`)는 `automation/ship.md`의 Phase 1 규칙대로 처리한다.

## Codex Co-Author 트레일러 (`automation/pipeline.md` Step 1 오버라이드)

`automation/pipeline.md` Step 1 커밋 메시지 끝에 붙이는 Co-Author 트레일러는 **`Co-Authored-By: Codex <noreply@openai.com>`**로 한다 (다른 하네스의 트레일러가 아니라 이 Codex 트레일러를 쓴다).

## Codex 폴링 모드 (`automation/pipeline.md` 「폴링 실행 규칙」 오버라이드)

- 배경 재호출 프리미티브가 없으므로 CI 체크 폴링은 `automation/bin/merge-gate.sh`를 **포그라운드 블로킹 호출**로 **한 번의 셸 호출**로 완료까지 실행한다 — 스크립트가 종료 마커를 `echo`하고 반환할 때까지 그 호출이 블로킹된다.
- `workspace-write` 샌드박스는 네트워크가 차단될 수 있고, 폴링 스크립트는 내부에서 `gh`를 반복 호출하므로 기존 승인 prefix를 재사용할 수 없다. 따라서 **폴링 스크립트 호출을 최초 호출부터 `sandbox_permissions: "require_escalated"`로 실행**하고, GitHub 상태 확인에 네트워크 접근이 필요하다는 짧은 `justification`을 붙인다. 샌드박스 안에서 먼저 실행해 `TIMEOUT`을 기다린 뒤 재조회하지 않는다.
- 스크립트 출력은 파일로 리다이렉트(`> "$LOG" 2>&1`)한다. 스크립트 내부 타임아웃을 상한으로 삼고, **셸 명령 타임아웃은 그보다 넉넉히** 설정한다 (머지 게이트 15분 → 셸 타임아웃 20분+). 없으면 스크립트가 셸 타임아웃에 먼저 끊겨 미완료로 오판될 수 있다.
- `automation/pipeline.md`의 `API_ERROR` 마커가 나오면 실제 stderr를 요약해 보고하고 중단한다. 이를 체크 대기나 GitHub 자체 장애로 추측하거나 `TIMEOUT`으로 바꾸지 않는다.
- 검증(Phase 2)의 `make` 명령은 폴링과 분리된 별도 셸 호출로 돌린다. 폴링 자체는 `gh`/`git` CLI만 쓴다.

## 안전 규칙 (훅 부재 보완 — 필수)

Codex 세션에서 Claude Code의 PreToolUse 훅이 동작한다고 가정하지 않는다. `automation/pipeline.md`의 「민감 파일 커밋 금지」 규칙을 **LLM이 직접** 준수한다 (Step 1의 `sensitive-gate.sh` 기계 검사와 병행):

- `git add/commit/push` 전에 대상에 다음이 없는지 확인한다: `.env`(및 `.env.*`), `.claude/settings.local.json`, `.mcp.json`, `certs/` 하위 키·인증서, `*.pem`, `*.p12`, `*.p8`, `*.key`, `id_rsa*`.
- 발견 시 커밋하지 말고 사용자에게 보고한다 (스테이징돼 있으면 `git reset HEAD <file>`로 해제).
