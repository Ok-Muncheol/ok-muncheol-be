# 실행·검증 명령을 한곳에 모은다. 인자 전달: make test ARGS="tests/automation -q"
# .venv가 있으면 그 인터프리터(Windows는 .venv/Scripts/python.exe)를, 없으면 PATH의 python3를 쓴다.

PY ?= $(firstword $(wildcard .venv/bin/python .venv/Scripts/python.exe) python3)

.PHONY: check contracts lint test
check:
	$(PY) scripts/check_scaffold.py

contracts:
	$(PY) scripts/export_contract_examples.py

lint:
	$(PY) -m ruff check .

test:
	$(PY) -m pytest $(ARGS)
