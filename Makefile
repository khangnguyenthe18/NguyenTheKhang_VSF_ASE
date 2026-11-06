export PATH := $(CURDIR)/.venv/bin:$(HOME)/.local/bin:$(PATH)
PROFILE ?= smoke
UV ?= $(shell command -v uv 2>/dev/null)
UV_RUN ?= $(if $(UV),$(UV) run --no-sync,)

.PHONY: check lint type test sim teams dashboard notify-demo mail-up mail-down demo

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

demo:
	@curl -sf -o /dev/null http://127.0.0.1:8000/ || { \
	  echo "[LỖI] Dashboard / mock Teams chưa chạy trên cổng 8000."; \
	  echo "      Mở terminal khác chạy 'make dashboard', rồi chạy lại 'make demo'."; exit 1; }
	$(UV_RUN) python -m ase.simulator --profile demo
	docker compose up -d mailpit
	@echo "Xoá outbox và card cũ (data/notifier.sqlite, data/outbox_preview/)..."
	rm -f data/notifier.sqlite
	rm -rf data/outbox_preview/
	$(UV_RUN) python -m ase.notifier.demo --from-profile demo
	@echo "================================================================"
	@echo "Demo ready! (dashboard đang chạy ở terminal kia) Mở:"
	@echo "  - NOC Dashboard: http://localhost:8000/dashboard?profile=demo"
	@echo "  - Mock Teams:    http://localhost:8000"
	@echo "  - Mailpit:       http://localhost:8025"
	@echo "================================================================"
