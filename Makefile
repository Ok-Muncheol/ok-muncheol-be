PY ?= python3

.PHONY: check contracts
check:
	$(PY) scripts/check_scaffold.py

contracts:
	$(PY) scripts/export_contract_examples.py
