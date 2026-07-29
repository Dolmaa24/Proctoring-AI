.PHONY: install test lint dev sim clean

PY := .venv/bin/python

install:
	python3 -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -q -e ".[dev]"

test:
	$(PY) -m pytest python/tests -q

lint:
	.venv/bin/ruff check python
	.venv/bin/ruff format --check python

dev:
	PROCTOR_MASTER_SECRET=dev-secret \
	.venv/bin/uvicorn proctor_gateway.app:app --reload --port 8000

sim:
	$(PY) -m proctor_sim --scenario $(or $(SCENARIO),honest) --tamper $(or $(TAMPER),none)

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ *.egg-info
