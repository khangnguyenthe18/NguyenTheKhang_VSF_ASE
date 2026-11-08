export PATH := $(CURDIR)/.venv/bin:$(HOME)/.local/bin:$(PATH)
PROFILE ?= smoke
UV ?= $(shell command -v uv 2>/dev/null)
UV_RUN ?= $(if $(UV),$(UV) run --no-sync,)
SPEED ?= 60
# E2E_SPEED=18000 (~5 giờ mô phỏng/giây, ~7s cho 24h của smoke). Ở 18000×, 25ms thời gian thật
# tương đương 450s mô phỏng: E2E_PATH_YIELD_MS nhường khi chuyển từ bước 6-7 (MQTT -> bridge ->
# Kafka) sang bước 1-5 (Kafka trực tiếp) để giữ lệch đường đi trong ngưỡng allowed_lateness_seconds.
E2E_SPEED ?= 18000
E2E_PATH_YIELD_MS ?= 25

.PHONY: check lint type test sim detect detect-ablation eval ablation teams dashboard notify-demo mail-up mail-down demo stream-up stream-down stream-demo e2e

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

FIELDS ?= ALL
detect:
	$(UV_RUN) python -m ase.detector --profile $(PROFILE) --fields $(FIELDS) $(if $(NOTIFY),--notify,)

detect-ablation:
	@for f in T0 T0+T1 T0+T1+T2 ALL; do \
	  $(UV_RUN) python -m ase.detector --profile $(PROFILE) --fields $$f || exit 1; \
	done

# Evaluator rule-only (EVALUATOR_PLAN): chạy detector (JSONL) rồi đo SF. Với PROFILE=eval chỉ
# đánh giá split test (10 ngày cuối); các profile khác đánh giá toàn bộ.
eval: detect
	$(UV_RUN) python -m ase.evaluator --profile $(PROFILE) $(if $(filter eval,$(PROFILE)),--split test,)

# Field ablation T0 → T3 trên dev: bảng "tập trường → SF2/SF4" (data/dev/ablation/report.md).
ablation:
	$(MAKE) detect-ablation PROFILE=dev
	$(UV_RUN) python -m ase.evaluator.ablation --profile dev

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
	  $(UV_RUN) python -m ase.streaming.publisher --profile $(PROFILE) --speed $(E2E_SPEED) --path-yield-ms $(E2E_PATH_YIELD_MS); \
	  wait $$DET_PID; \
	'
	@$(UV_RUN) python -c 'import json, pathlib; from ase.knowledge.loader import default_knowledge; allowed = default_knowledge().detector.stream.allowed_lateness_seconds; p = pathlib.Path("data/$(PROFILE)"); j_ids = {json.loads(l)["session_id"] for l in (p / "detections.jsonl").read_text().splitlines() if l}; k_ids = {json.loads(l)["session_id"] for l in (p / "stream" / "detections.jsonl").read_text().splitlines() if l}; cov = json.loads((p / "stream" / "coverage.json").read_text())["coverage"]; lat = cov["max_lateness_vs_clock_seconds"]; wm_lag = cov["max_watermark_lag_seconds"]; print(f"[e2e] JSONL sessions={len(j_ids)}, Kafka sessions={len(k_ids)}, max_lateness_vs_clock_seconds={lat}s, max_watermark_lag_seconds={wm_lag}s (allowed={allowed}s)"); assert j_ids == k_ids, f"Mismatch! only_jsonl={j_ids - k_ids}, only_kafka={k_ids - j_ids}"; assert lat < allowed, f"max_lateness_vs_clock_seconds={lat} >= {allowed}"'

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
