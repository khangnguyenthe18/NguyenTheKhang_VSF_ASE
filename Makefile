PROFILE ?= smoke
UV ?= uv
UV_RUN ?= $(UV) run --no-sync

.PHONY: check lint type test sim

check: lint type test

lint:
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .

type:
	$(UV_RUN) mypy

test:
	$(UV_RUN) pytest -q

sim:
	$(UV_RUN) python -m ase.simulator --profile $(PROFILE)

