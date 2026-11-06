export PATH := $(CURDIR)/.venv/bin:$(HOME)/.local/bin:$(PATH)
PROFILE ?= smoke
UV ?= $(shell command -v uv 2>/dev/null)
UV_RUN ?= $(if $(UV),$(UV) run --no-sync,)

.PHONY: check lint type test sim teams dashboard notify-demo mail-up mail-down

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

teams:
	$(UV_RUN) uvicorn ase.notifier.mock_teams.app:app --port 8000

dashboard:
	$(UV_RUN) uvicorn ase.notifier.mock_teams.app:app --port 8000

mail-up:
	docker compose up -d mailpit

mail-down:
	docker compose down

notify-demo:
	$(UV_RUN) python -m ase.notifier.demo $(if $(DRY),--dry-run,)
