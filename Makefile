.PHONY: install test test-py test-ts lint dev sim protocol clean

PY := .venv/bin/python
NODE_TEST := node --experimental-strip-types --no-warnings --test

install:
	python3 -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -q -e ".[dev]"
	cd apps/client && npm install --no-audit --no-fund

test: test-py test-ts

test-py:
	$(PY) -m pytest python/tests -q

# No build step: Node 22 strips the types directly.
test-ts:
	cd apps/client && $(NODE_TEST) 'src/**/*.test.ts'

# Regenerate the TypeScript event types from the Pydantic schema.
# `make test-py` fails if this is stale.
protocol:
	$(PY) tools/generate_ts_protocol.py

lint:
	.venv/bin/ruff check python
	.venv/bin/ruff format --check python

dev:
	PROCTOR_MASTER_SECRET=dev-secret \
	PROCTOR_CONSOLE_TOKEN=dev-token \
	PROCTOR_DB_PATH=proctor.db \
	.venv/bin/uvicorn proctor_gateway.app:app --reload --port 8000

sim:
	$(PY) -m proctor_sim --scenario $(or $(SCENARIO),honest) --tamper $(or $(TAMPER),none)

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ *.egg-info
