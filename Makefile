PROFILE ?= smoke
UV ?= uv

.PHONY: check lint type test sim

check: lint type test

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

type:
	$(UV) run mypy

test:
	$(UV) run pytest -q

sim:
	$(UV) run python -m ase.simulator --profile $(PROFILE)

