---
description: BE docs/specs/ 스펙의 AGENT 범위 구현 — 브랜치 → 테스트 동결 → 구현 → 검증 → PR·머지(ship 위임)
argument-hint: <spec 번호 (예: 41)>
---

# /implement — 스펙 기반 구현 하네스 (agent)

**스펙이 계약이고, 동결된 테스트가 심판이다.** 이 절차 밖의 임기응변을 금지한다.

> **스펙은 이 레포에 없다.** `docs/specs/`는 **BE 레포(`Cure-Agent/cure-agent-be`)**에 있다.
> 이 프로젝트는 **단일 스펙 저장소 + 세 구현 레포**(cure-agent-be · cure-agent-fe · 이 레포) 구조다 —
> 계약이 하나뿐이라 스펙도 하나다. 원칙의 원본은 BE `docs/sdd.ko.md`와 `docs/architecture.md` §15다.

## Phase 0 — Preflight

1. **스펙 조달**: `python3 scripts/fetch_spec.py <번호>` — 로컬 형제 경로
   (`../cure-agent-be/docs/specs/`)가 있으면 그것을, 없으면 **BE `dev`**의 raw를(없으면 `main`을)
   가져와 `.cure-implement/spec-<번호>.md`에 둔다. 못 찾으면 중단한다.
   > **`dev`가 기본인 이유**: BE의 기본 브랜치가 `dev`이고 스펙은 `dev-only`로 착지한다 —
   > **스펙은 항상 dev에 먼저 있고 main에는 다음 `full` 배포가 실어 나른다.** 형제 경로를 쓸
   > 때 그 레포가 `dev`가 아니면 스크립트가 경고한다(`main` 체크아웃 상태면 오래된 스펙을 읽는다).
2. **AGENT 범위 판별**: 스펙의 수용 기준 중 **`(AGENT)` 라벨이 달린 항목만** 이 레포가 구현한다.
   `(BE)`·`(FE)` 기준은 건드리지 않는다. **라벨이 없거나 이 레포 몫이 모호하면 사람에게 분할을
   확인받는다** — 추측으로 가르지 않는다.
   > 2026-09-11 기준 BE `.claude/commands/spec.md`의 라벨 규칙은 `(BE)`·`(FE)` 둘뿐이다. `(AGENT)`
   > 라벨이 없는 스펙이 오면 이 단계에서 멈추는 것이 정상 경로다.
3. 스펙이 §링크한 이 레포 `docs/architecture.md` 섹션을 읽는다. **§2(구조와 경계 원칙)·§3(검증 명령)·
   §4(테스트)는 항상 포함**한다.
   > **형태는 문서가 아니라 코드에서 본다.** API 요청·응답은 `app/api/routes.py`의 Pydantic 모델,
   > 검색 정책은 `app/agent/graph.py`의 `PRESETS`, 노드 입출력은 `app/agent/state.py`가 진실이다.
4. `git status` clean 확인, `git checkout main && git pull origin main`.
5. **브랜치 생성**: `"<prefix>/<슬러그>"` → checkout. **이슈를 만들지 않는다**
   (`automation/ship.md` 「워크플로우 전제」 — 이 레포는 PR-퍼스트다).

> **커밋 제목에 `#`를 쓰지 않는다.** 이슈가 없어 `#N`에 넣을 번호가 없고, spec 번호를 `#`와 함께
> 쓰면 **GitHub이 같은 번호의 PR·이슈로 자동 링크한다** — BE와 FE 양쪽에 잘못 걸린 전례가 있다.
> 형식은 **`[TYPE] spec <번호> <요약>`**이며, squash 머지가 붙이는 `(#PR번호)`가 추적 식별자다.

## Phase 1 — 계획

- AGENT 범위 수용 기준 각 항목 ↔ 구현 파일(모듈·노드·프리셋) 매핑 계획을 세운다.
- **스펙에 모호함·결함이 있으면 구현하지 않는다.** 스펙은 BE 레포에 있으므로 **여기서 고칠 수 없다** —
  결함을 사용자에게 보고하고 BE 쪽 수정을 확정받은 뒤 진행한다. 테스트 동결 후 발견해도 동일
  (`automation/freeze.md` TEST-DISPUTE의 「명세 결함」).

## Phase 2 — 테스트 동결 (작성: Codex / 리뷰·동결: Claude)

**절차 원본은 `automation/freeze.md`다.** 그 문서를 읽고 아래 파라미터로 실행한다:

| 파라미터 | 값 |
|---|---|
| 명세 | Phase 0에서 조달한 스펙의 **(AGENT) 기준** + §링크한 `docs/architecture.md` 섹션 |
| 작업 ID | **스펙 번호** (커밋 제목 `[TEST] spec <번호> …`에 쓰인다 — `#` 금지) |
| 동결 단위 | 스펙 1개 = 1 단위 |
| 참조 패턴 파일 | `tests/conftest.py` + 대상 모듈의 기존 `tests/test_*.py` 1~2개 전문 |

Codex 프롬프트에 **이 레포가 책임지는 기준만** 싣고, BE·FE 범위는 「건드리지 마라」로 명시한다 —
싣지 않으면 Codex가 다른 레포의 동작을 여기서 단언하려 시도한다.

**구현이 이미 존재하면 여기서 중단하고 보고한다.** 이 하네스를 지나서는 나올 수 없는 상태다
(Phase 2 다음이 Phase 3이다) — 우회했다는 신호이므로 조용히 이어가지 않는다. 구현을 둔 채로
파생하면 Codex가 코드를 읽어 오라클이 구현을 추인할 뿐이고, 그러면 동결이 아무것도 검증하지 못한다.

동결 후 구현이 테스트를 통과시키지 못하면 `automation/freeze.md`의 TEST-DISPUTE 3분류를 따르며,
**분류에 따라 이 단계에서 멈출지가 갈린다**:

- **스펙 결함**(수용 기준이 모호·모순) → **Phase 1 규칙으로 회귀**한다. BE에서 스펙을 먼저 고치고
  Codex 재파생 → 재동결, 사유는 커밋 메시지에 남긴다. 사용자 확인이 곧 스펙 확정 행위이므로 여기는
  사람이 잡는다.
- **테스트 결함**(스펙은 옳고 테스트가 이를 잘못 대표) → **Codex 판정**에 넘긴다. 인정되면 Codex가
  고친 개정본을 재검증 4계층(범위·스텁 재RED·본 브랜치 GREEN·결정성)에 걸고 재동결한 뒤 **Phase 1
  회귀 없이 구현을 이어간다.** 판정 결과는 Phase 4 보고에 `⚠️ TEST-DISPUTE` 항목으로 싣는다.
- **구현 결함**(기본 추정) → 구현을 고친다.

## Phase 3 — 구현

동결 테스트가 전부 통과할 때까지 구현한다. 필수 규칙(`docs/architecture.md` §2·§4):

- **노드는 `app/agent/state.py`의 타입으로만 말한다** — 노드끼리 직접 부르지 않고, 배선은
  오케스트레이터(`app/agent/graph.py`)만 한다
- **루프 예산·게이트 판정은 LLM이 아니라 `graph.py`의 순수 라우팅 함수가 쥔다**
- **검색 정책 값(컷·top_k·리랭크 여부)의 단일 출처는 `AgentConfig`/`PRESETS`다** — `Settings`에
  사본을 두지 않는다. 운영 정책이 바뀌면 기존 프리셋의 의미를 바꾸지 않고 새 이름을 둔다
  (`rerank` 3.5 보존 · `prod_rerank` 9 분리 선례)
- **`policy_version_for`에는 실행된 구성값을 전부 인자로 넘긴다** — Settings 기본값 폴백 금지
- **외부 유료 경계(LLM·임베딩·리랭커)는 Protocol 뒤에 둔다**(`Retriever`·`Reranker`·`BaseChatModel`) —
  테스트가 실키 없이 돌아야 한다
- **SQL은 `LiteralString` 리터럴 + `%(name)s` 파라미터로만 조립한다** (`app/retrieval/hybrid.py`)
- **`docs/experiments/`의 기존 관측 숫자를 고치지 않는다** — 정정은 새 문서와 배너로 (§5)

## Phase 4 — 검증

1. `make lint && make typecheck && make test && make test-e2e` 전부 green.
2. **동결 무결성 감사**: `automation/freeze.md`의 「사후 감사」를 실행한다 — 동결 커밋에서 목록을
   복원해 `git diff`가 비어 있는지 확인하고, 비어있지 않으면 중단·보고한다. 감사는 항상 **마지막
   코드 변경 뒤**에 실행하고, 감사 이후 코드가 다시 바뀌면 재실행한다.
3. 수용 기준 항목별 → 커버하는 테스트 매핑을 만든다 (최종 보고·PR 본문에 포함).
4. 스펙의 Out of scope를 침범하지 않았는지 점검한다.
5. **사용자 확인 대기** — 수용 기준 매핑·계약 변경 요약·**디스퓨트 판정 내역**(`⚠️ TEST-DISPUTE`:
   테스트 이름 · 판정 · 근거 · 재검증 결과)을 보고하고 머지 승인을 받는다. OK 시에도 **동결은 아직
   해제하지 않는다**(머지 실패로 재수정하는 시나리오에서 테스트가 무방비가 되지 않도록).

   > **spec 승인이 이것을 대체하지 않는다.** spec은 「무엇을 만들지」의 합의이고 여기는 「이렇게
   > 만들어진 결과물을 머지할지」다. Phase 5가 `automation/ship.md`에 위임하면 ship Preflight가
   > feature 브랜치를 감지해 **Phase 1·3을 스킵**하므로 ship이 가진 사용자 확인이 건너뛰어지고,
   > 이후 `automation/pipeline.md`가 PR → 게이트 → **squash 머지**까지 멈추지 않는다. 이 확인이
   > 없으면 구현 완료부터 `main` 머지까지 사람이 개입할 지점이 **하나도 없다.**

## Phase 5 — PR·머지·후속

1. 구현 커밋: `[FEAT] spec <번호> <요약>` — 동결 커밋과 분리 유지. 트레일러는 현재 하네스가
   지정하는 AI 협업 트레일러 한 줄.
2. **PR·머지는 `automation/ship.md`에 위임한다.** 그 문서를 읽고 실행하면 현재 feature 브랜치를
   Preflight가 감지해 **Phase 1·3을 스킵하고 Phase 2(검증)로 직행**한다.
   전달할 컨텍스트: 브랜치명, 타입, 스펙 번호, 그리고 **Phase 4-1 검증 완료 사실**(전 구간 green).
   - PR 본문 「스펙·수용 기준」에 **스펙 번호·BE 링크 + (AGENT) 수용 기준 ↔ 테스트 매핑 표**를 넣는다.
3. **머지 성공 후** `automation/freeze.md`의 「동결 해제」를 수행한다 — 머지가 실패해 코드를
   재수정하는 동안은 동결을 유지해 테스트를 계속 보호한다.
4. **브랜치 정리**는 `automation/pipeline.md` Step 2가 수행한다. 누락되면 스텝마다 브랜치가 누적되므로
   최종 보고 전에 `git branch -a`로 확인한다.
   > **squash 머지 함정**: squash는 커밋 SHA를 새로 만들므로 원본 브랜치는 `main`의 조상이
   > **아니다**. `git branch -d`가 거부하는데, 확인 없이 `-D`를 쓰면 머지되지 않은 작업을 조용히
   > 날린다. **PR 머지 여부를 먼저 확인**한 뒤 확인된 것만 지운다:
   > `gh pr list --head <브랜치> --state merged --json number`
5. **서버 반영은 이 레포의 범위가 아니다** — cure-agent-be가 맡는다. 최종 보고에 그 사실을 적는다.
6. 최종 보고: 수용 기준 매핑, 계약 변경 여부, 머지 커밋, 동결 해제 완료 여부.
