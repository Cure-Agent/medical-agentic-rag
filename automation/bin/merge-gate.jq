# merge-gate 판정 — merge-gate.sh가 `gh pr view --json mergeable,statusCheckRollup` 출력을 stdin으로,
# 앵커 체크명들을 --args 위치 인자로 넘긴다. 출력은 "PASS" | "FAIL ..." | "PENDING ..." 한 줄.
# 로직을 바꾸면 smoke-test.sh의 fixture 기대값도 갱신한다.
#
# FE판은 Vercel 프리뷰 체크의 존재를 vacuous pass 방어로 썼다. 이 레포에는 Vercel이 없으므로
# BE pr-gate.jq처럼 호출측이 지정한 앵커 체크의 존재로 같은 방어를 한다.

# 비차단 예외 체크 — 게이트에서 제외할 이름. 현재 없음: 생기면 여기에만 추가한다.
def exempt: [];

def cname($x): (($x.name // $x.context) // "");

# CheckRun(.conclusion)과 commit status(StatusContext, .state)를 모두
# PASS/PENDING/FAIL로 정규화한다 (NEUTRAL·SKIPPED는 통과).
def state($x):
  if $x.__typename == "StatusContext" then
    (if $x.state=="SUCCESS" then "PASS"
     elif ($x.state=="PENDING" or $x.state=="EXPECTED") then "PENDING"
     else "FAIL" end)
  else
    (if $x.status!="COMPLETED" then "PENDING"
     elif ($x.conclusion=="SUCCESS" or $x.conclusion=="NEUTRAL" or $x.conclusion=="SKIPPED") then "PASS"
     else "FAIL" end)
  end;

($ARGS.positional) as $anchors
| (.mergeable) as $m
# 게이트 대상 = 비차단 예외를 뺀 모든 체크. 이름을 고르는 게 아니라 실패 0을 요구한다 —
# 잡이 추가·개명돼도 게이트가 자동으로 따라간다.
| [.statusCheckRollup[]? | select((cname(.)) as $n | (exempt | index($n)) == null)] as $checks
| [$checks[] | select(state(.)=="FAIL") | cname(.)] as $failed
| [$checks[] | select(state(.)=="PENDING") | cname(.)] as $pending
# 앵커 체크는 존재 자체가 필수 — 체크 등록 전 공집합 통과(vacuous pass)를 막는다.
| [$anchors[] | select(. as $a | ([$checks[] | select(cname(.)==$a)] | length) == 0)] as $absent
| if (($failed | length) > 0 or $m=="CONFLICTING") then
    "FAIL failed=\($failed | join(",")) mergeable=\($m)"
  elif ($m=="MERGEABLE" and ($absent | length)==0 and ($pending | length)==0 and ($checks | length) > 0) then
    "PASS"
  else
    "PENDING anchors_absent=\($absent | join(",")) pending=\($pending | join(",")) mergeable=\($m)"
  end
