# 검증 명령의 단일 원천 — CI(.github/workflows/ci.yml)와 하네스(automation/*.md)가 이 타깃을 부른다.
# 로컬은 .venv, CI는 setup-python이 올린 python3를 쓴다. 인자 전달: make test ARGS="tests/test_rrf.py -q"
# build·api-generate 타깃은 첫 스펙에서 추가한다 (docs/architecture.md §3).

PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
E2E_DIR := tests/e2e

.PHONY: lint typecheck test test-e2e

lint:
	$(PY) -m ruff check .

# 동결 게이트의 「빌드 GREEN」을 맡는다 (automation/freeze.md 절차 5 ②).
# --pythonpath: PATH의 python이 아니라 의존성이 설치된 인터프리터로 import를 해석하게 한다.
typecheck:
	$(PY) -m pyright --pythonpath "$$($(PY) -c 'import sys; print(sys.executable)')"

test:
	$(PY) -m pytest --ignore=$(E2E_DIR) $(ARGS)

# e2e 테스트 파일이 아직 없으면 알리고 통과한다. 파일이 있는데 수집되지 않으면(pytest 종료코드 5)
# 그대로 실패한다 — 러너 패턴 불일치가 조용한 통과로 새지 않게 한다.
test-e2e:
	@if [ -z "$$(find $(E2E_DIR) -name 'test_*.py' 2>/dev/null)" ]; then \
		echo "e2e 테스트 없음 ($(E2E_DIR)/test_*.py) — 첫 e2e 스펙에서 추가한다"; \
	else \
		$(PY) -m pytest $(E2E_DIR) $(ARGS); \
	fi
