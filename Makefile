export PATH := $(CURDIR)/.venv/bin:$(HOME)/.local/bin:$(PATH)
PROFILE ?= smoke
UV ?= $(shell command -v uv 2>/dev/null)
UV_RUN ?= $(if $(UV),$(UV) run --no-sync,)
SPEED ?= 60
# E2E_SPEED=18000 (~5 giờ mô phỏng/giây, ~7s cho 24h của smoke) kết hợp nhường 25ms ở bước
# chuyển MQTT -> Kafka giúp bản tin bước 6-7 luôn qua bridge vào Kafka trước khi phiên kế tiếp
# đẩy SimClock, giữ max_arrival_lag_seconds < allowed_lateness_seconds (10s trong detector.yaml).
E2E_SPEED ?= 18000

.PHONY: check lint type test sim detect detect-ablation teams dashboard notify-demo mail-up mail-down demo stream-up stream-down stream-demo e2e

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

stream-up:
	docker compose up -d mailpit mosquitto redpanda
	@echo "Chờ Redpanda và Mosquitto sẵn sàng..."
	@for i in $$(seq 1 30); do \
	  if docker exec ase-redpanda rpk cluster info -X brokers=127.0.0.1:19092 >/dev/null 2>&1; then \
	    break; \
	  fi; \
	  if [ $$i -eq 30 ]; then echo "[LỖI] Redpanda không sẵn sàng sau 30s"; exit 1; fi; \
	  sleep 1; \
	done
	@docker exec ase-redpanda rpk topic create ase.swap.events -p 3 -r 1 -X brokers=127.0.0.1:19092 >/dev/null 2>&1 || true
	@docker exec ase-redpanda rpk topic create ase.cabinet.telemetry -p 1 -r 1 -X brokers=127.0.0.1:19092 >/dev/null 2>&1 || true
	@echo "Redpanda + Mosquitto + Mailpit đã sẵn sàng."

stream-down:
	docker compose down

stream-demo: stream-up
	@test -f data/$(PROFILE)/ase.swap.events.jsonl || $(UV_RUN) python -m ase.simulator --profile $(PROFILE)
	@docker exec ase-redpanda rpk topic delete ase.swap.events ase.cabinet.telemetry -X brokers=127.0.0.1:19092 >/dev/null 2>&1 || true
	@docker exec ase-redpanda rpk topic create ase.swap.events -p 3 -r 1 -X brokers=127.0.0.1:19092 >/dev/null 2>&1 || true
	@docker exec ase-redpanda rpk topic create ase.cabinet.telemetry -p 1 -r 1 -X brokers=127.0.0.1:19092 >/dev/null 2>&1 || true
	@echo "================================================================"
	@echo "Stream Demo (profile=$(PROFILE), speed=$(SPEED))"
	@echo "  - NOC Dashboard: http://localhost:8000/dashboard?profile=$(PROFILE)"
	@echo "  - Mock Teams:    http://localhost:8000"
	@echo "  - Mailpit:       http://localhost:8025"
	@echo "================================================================"
	@bash -c '\
	  trap "kill \$$BRIDGE_PID \$$DET_PID 2>/dev/null || true" EXIT INT TERM; \
	  $(UV_RUN) python -m ase.streaming.bridge & BRIDGE_PID=$$!; \
	  $(UV_RUN) python -m ase.detector --profile $(PROFILE) --source kafka --notify --max-idle-seconds 12 & DET_PID=$$!; \
	  sleep 3.5; \
	  $(UV_RUN) python -m ase.streaming.publisher --profile $(PROFILE) --speed $(SPEED); \
	  wait $$DET_PID; \
	'

e2e: stream-up
	@test -f data/$(PROFILE)/ase.swap.events.jsonl || $(UV_RUN) python -m ase.simulator --profile $(PROFILE)
	$(UV_RUN) python -m ase.detector --profile $(PROFILE) --source jsonl
	@docker exec ase-redpanda rpk topic delete ase.swap.events ase.cabinet.telemetry -X brokers=127.0.0.1:19092 >/dev/null 2>&1 || true
	@docker exec ase-redpanda rpk topic create ase.swap.events -p 3 -r 1 -X brokers=127.0.0.1:19092 >/dev/null 2>&1 || true
	@docker exec ase-redpanda rpk topic create ase.cabinet.telemetry -p 1 -r 1 -X brokers=127.0.0.1:19092 >/dev/null 2>&1 || true
	@bash -c '\
	  set -e; \
	  TEAMS_PID=""; \
	  if ! $(UV_RUN) python -c "import socket; s = socket.create_connection((\"127.0.0.1\", 8000), 0.3); s.close()" >/dev/null 2>&1; then \
	    $(UV_RUN) uvicorn ase.notifier.mock_teams.app:app --port 8000 --log-level warning & TEAMS_PID=$$!; \
	  fi; \
	  trap "kill \$$BRIDGE_PID \$$DET_PID \$$TEAMS_PID 2>/dev/null || true" EXIT INT TERM; \
	  $(UV_RUN) python -m ase.streaming.bridge & BRIDGE_PID=$$!; \
	  $(UV_RUN) python -m ase.detector --profile $(PROFILE) --source kafka --notify --max-idle-seconds 6 & DET_PID=$$!; \
	  sleep 3.5; \
	  $(UV_RUN) python -m ase.streaming.publisher --profile $(PROFILE) --speed $(E2E_SPEED); \
	  wait $$DET_PID; \
	'
	@$(UV_RUN) python -c 'import json, pathlib; p = pathlib.Path("data/$(PROFILE)"); j_ids = {json.loads(l)["session_id"] for l in (p / "detections.jsonl").read_text().splitlines() if l}; k_ids = {json.loads(l)["session_id"] for l in (p / "stream" / "detections.jsonl").read_text().splitlines() if l}; cov = json.loads((p / "stream" / "coverage.json").read_text())["coverage"]; lag = cov["max_arrival_lag_seconds"]; print(f"[e2e] JSONL sessions={len(j_ids)}, Kafka sessions={len(k_ids)}, max_arrival_lag_seconds={lag}s"); assert j_ids == k_ids, f"Mismatch! only_jsonl={j_ids - k_ids}, only_kafka={k_ids - j_ids}"; assert lag < 10.0, f"max_arrival_lag_seconds={lag} >= 10.0"'

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
