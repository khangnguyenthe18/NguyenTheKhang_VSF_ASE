# STREAMING_PLAN.md — Luồng BSS qua MQTT + Kafka (GATE1_PLAN §3 "Luồng MQTT + Kafka", §4)

> Plan thực thi cho argon. Đọc trước: CLAUDE.md §3, §5.1, §13; GATE1_PLAN.md §2, §4; DETECTOR_PLAN.md §2.
> Phạm vi đã chốt với chủ dự án: mô phỏng luồng mentor xác nhận
> `Pin/Tủ (CAN/RS485) → MQTT (AWS IoT Core) → Kafka → Microservices`, chạy trên laptop 16 GB.

## 1. Mục tiêu và "Done khi"

Detector đọc event từ Kafka thay cho file JSONL, **không sửa logic phát hiện / phân loại / gộp**. Event của tủ (bước 6–7) và telemetry đi qua MQTT rồi qua cầu nối vào Kafka; event của app và thanh toán (bước 1–5) ghi thẳng vào Kafka.

Done khi:
- `make stream-up` bật Redpanda + Mosquitto + Mailpit; `make stream-down` tắt cả ba.
- `make e2e PROFILE=smoke` chạy hết luồng publisher → MQTT/Kafka → detector → Mailpit + mock Teams và tự dừng khi hết dữ liệu.
- **Tập `session_id` detector phát hiện qua Kafka bằng đúng tập khi chạy JSONL** trên cùng `smoke` (so tập, không so thứ tự).
- Chạy JSONL (`make detect`) cho output **byte-identical** với trước khi sửa (so sha256 mọi file trong `data/<profile>/`).
- `make check` xanh; test tích hợp tự skip khi broker chưa chạy.

## 2. Nguyên tắc bắt buộc

1. **Detector không biết nguồn dữ liệu:** chỉ phụ thuộc `EventSource` (`ase/common/transport.py`). Không import `aiokafka` / `aiomqtt` trong `ase/detector/` ngoài chỗ chọn nguồn ở `main.py`.
2. **Không đổi kết quả JSONL.** Mọi tham số mới có giá trị mặc định giữ hành vi cũ. Evaluator và field ablation (ngày 10) vẫn chạy trên JSONL.
3. **Thời gian nghiệp vụ vẫn là `SimClock`** tiến theo `occurred_at`. Đồng hồ thật chỉ được dùng để: giãn nhịp phát (`--speed`), timeout idle, backoff kết nối lại. Không gọi `datetime.now()` trong detector.
4. **Payload là dữ liệu untrusted:** validate bằng Pydantic (`SwapEvent`, `CabinetTelemetry`). Sai → log + đếm + bỏ, không crash.
5. **Idempotency:** giữ nguyên `event_id`; dedup vẫn ở `SessionTracker`. Event trùng qua 2 đường chỉ được tính 1 lần.
6. **Cấu hình qua `Settings` (`ASE_*`) và `knowledge/detector.yaml`,** không hardcode host, port, topic.
7. **Dependency:** chỉ `uv add aiokafka aiomqtt`. Cần thêm gì khác → dừng lại hỏi.
8. **Message commit không ghi ngày/tháng.** Cuối việc: `make check` xanh, commit, dừng lại báo cáo.

## 3. Hiện trạng code liên quan (đã kiểm tra)

| Chỗ | Hiện trạng | Hệ quả cho việc này |
|---|---|---|
| `ase/detector/main.py` `run_detector` | Tự tạo `JsonlSource(profile_dir / "ase.swap.events.jsonl", clock, speed)` | Thêm tham số `source: EventSource \| None`; `None` = JSONL như cũ |
| `SessionTracker.on_event` | Tự gọi `clock.advance(occurred_at)` rồi pop timer có hạn `<= now` | Cần cơ chế "cho phép đến trễ" (mục 6) |
| `Dispatcher.tick(clock.now())` | Đóng chu kỳ tin gom khi clock vượt mốc | Dùng cùng clock, không cần sửa |
| `write_outputs` | Chỉ ghi file **một lần lúc kết thúc** | Chế độ Kafka cần ghi `incidents.jsonl` định kỳ (mục 7) |
| `ContextStore` | Đọc Parquet một lần, lọc theo thời gian `<= now` | Giữ nguyên; telemetry trên Kafka chỉ để demo (GATE1_PLAN §4.3) |
| `docker-compose.yml` | Chỉ có `mailpit` (`axllent/mailpit:v1.23`, bind `127.0.0.1`) | Thêm 2 service cùng kiểu |
| `JsonlSink` | `orjson` + `OPT_UTC_Z` | Dùng đúng định dạng này cho payload Kafka/MQTT |

## 4. Hạ tầng (`docker-compose.yml`)

- **Redpanda**, 1 node, chế độ dev: ghim tag cụ thể (tra tag hiện hành trên registry, **không** dùng `latest`).
  - Giới hạn tài nguyên: `--smp 1 --memory 1G --overprovisioned` (máy 16 GB dùng chung).
  - Cổng Kafka chỉ bind `127.0.0.1`. Lưu ý: phải khai báo listener nội bộ (cho container) và listener ngoài (cho `localhost`) cùng địa chỉ advertise tương ứng. Sai chỗ này client trên host kết nối được nhưng không produce/consume được. Kiểm tra với tài liệu đúng phiên bản đã ghim.
  - Tạo topic khi khởi động (lệnh `rpk` trong Makefile hoặc service init): `ase.swap.events` (3 partition, key = `session_id`), `ase.cabinet.telemetry` (1 partition, key = `cabinet_id`).
- **Mosquitto** `eclipse-mosquitto:2.0.x` (ghim bản cụ thể), cổng 1883 bind `127.0.0.1`. File cấu hình tối thiểu trong repo: `listener 1883` + `allow_anonymous true`, kèm comment: môi trường thật dùng TLS + chứng chỉ thiết bị (như AWS IoT Core).
- Mailpit giữ nguyên.
- Ghi `docker stats` khi cả ba chạy cùng lúc vào báo cáo cuối việc.

## 5. Code (`ase/streaming/`)

| File | Nội dung |
|---|---|
| `__init__.py` | — |
| `config.py` hoặc thêm vào `Settings` | `kafka_bootstrap` (mặc định `localhost:<cổng ngoài>`), `kafka_events_topic`, `kafka_telemetry_topic`, `kafka_group_id`, `mqtt_host`, `mqtt_port`, `mqtt_qos` (1). Prefix `ASE_`. Thêm vào `.env.example`. |
| `mqtt_topics.py` | Quy ước topic: `bss/{country}/{station_id}/{cabinet_id}/events` và `.../telemetry`. Hàm dựng topic + hàm tách topic (có test). `country` lấy từ bảng `stations` của profile. |
| `codec.py` | `encode_event` / `decode_event`, `encode_telemetry` / `decode_telemetry`: `orjson` + `OPT_UTC_Z`, decode bằng `model_validate_json`. Lỗi → trả `None` + lý do để caller đếm. |
| `kafka_io.py` | `KafkaSource(EventSource)`: consumer `aiokafka`, decode, **không** tự `clock.advance` (để tracker làm, như hiện tại), dừng khi idle quá `max_idle_seconds` (đồng hồ thật). `KafkaSink(EventSink)`: producer, key = `session_id`, `acks="all"`. Cả hai nhận client qua constructor để test bằng client giả. |
| `publisher.py` | `python -m ase.streaming.publisher --profile smoke [--speed N]`. Đọc `ase.swap.events.jsonl` **đúng thứ tự dòng**. Bước 1–5 → Kafka trực tiếp. Bước 6–7 → MQTT. Telemetry (`cabinet_telemetry.parquet`, sort theo `heartbeat_at`) trộn vào theo thời gian rồi → MQTT. `--speed` giãn nhịp theo `occurred_at` như `JsonlSource`; mặc định: xem mục 6. In số đã gửi mỗi đường. |
| `bridge.py` | `python -m ase.streaming.bridge`: subscribe `bss/+/+/+/events` và `bss/+/+/+/telemetry`, decode + validate, produce sang topic Kafka tương ứng (giữ `event_id`). Payload sai: log + đếm + bỏ. Mất kết nối: backoff + jitter rồi kết nối lại. Ctrl+C: đóng sạch, in thống kê. |
| `ase/detector/main.py` | `--source jsonl\|kafka`, `--max-idle-seconds` (mặc định 10). `run_detector(..., source=None)`. Hết luồng (file hết hoặc idle) thì `tracker.flush()` + `disp.flush_all()` + ghi output như cũ. |

## 6. Vấn đề thời gian khi có 2 đường (quan trọng nhất)

**Hiện tượng:** `SimClock` tiến theo `occurred_at` lớn nhất đã thấy. Đường Kafka (bước 1–5) thường nhanh hơn đường MQTT → bridge → Kafka (bước 6–7). Khi phát nhanh, event bước 1–5 của phiên khác đẩy clock vượt hạn SLA của phiên đang chờ bước 6, trước khi event bước 6 của nó kịp qua bridge → **timeout giả**. Ví dụ: phát 2.000 event/giây với `occurred_at` cách nhau trung bình vài giây, bridge trễ 50 ms là đủ để clock chạy trước vài phút.

**Yêu cầu giải pháp:**
1. Thêm tham số `allowed_lateness_seconds` cho `SessionTracker` (mặc định `0` → hành vi y hệt hiện tại). Timer SLA chỉ được bắn khi `deadline <= clock.now() - allowed_lateness`. `flush()` không đổi.
2. Giá trị lấy từ `detector.yaml`, khối mới `stream: {allowed_lateness_seconds: 10}`; chỉ áp khi `--source kafka`. JSONL luôn dùng `0`.
3. Đo và ghi vào `coverage.json` (thêm field, không đổi field cũ): `max_arrival_lag_seconds` = max(`clock.now()` lúc event đến − `occurred_at` của nó).
4. Publisher mặc định cho `make e2e`: chọn `--speed` sao cho `max_arrival_lag_seconds` < `allowed_lateness_seconds` trên `smoke`; ghi giá trị đã chọn vào Makefile kèm comment lý do.
5. Ghi rõ trong báo cáo: độ trễ cho phép làm alert timeout đến muộn thêm tối đa `allowed_lateness_seconds` (ảnh hưởng SF5 của lỗi im lặng).

**Không làm:** sort lại event trong detector, đổi `SimClock` thành đồng hồ thật, hay sửa simulator.

## 7. Output khi chạy Kafka

- Dashboard (bảng Cảnh báo) đọc `data/<profile>/incidents.jsonl`. Ở chế độ Kafka, ghi lại file này **định kỳ** (mỗi lần đóng chu kỳ tin gom và mỗi lần có alert P1), ghi ra file tạm rồi `rename` cho nguyên tử.
- Chế độ JSONL giữ nguyên: chỉ ghi lúc kết thúc (giữ byte-identical).
- Chạy Kafka ghi vào thư mục riêng `data/<profile>/stream/` để không đè output JSONL dùng cho evaluator. Nếu dashboard cần đọc từ đây, thêm đường dẫn ứng viên trong `ase/dashboard/metrics.py` (cạnh 2 đường hiện có), không đổi logic khác.

## 8. Makefile

| Target | Việc |
|---|---|
| `stream-up` / `stream-down` | Bật/tắt Redpanda + Mosquitto + Mailpit, tạo topic, chờ broker sẵn sàng (health check, có timeout) |
| `stream-demo PROFILE=demo SPEED=60` | `stream-up` → chạy bridge + detector (`--source kafka --notify`) nền → publisher; in URL Mailpit, mock Teams, dashboard; Ctrl+C dừng hết |
| `e2e PROFILE=smoke` | Như trên, phát nhanh theo mục 6.4, tự dừng khi detector idle; in tóm tắt và so tập `session_id` với chạy JSONL |

Giữ `make demo` hiện tại làm dự phòng khi không bật được Docker.

## 9. Test

**Unit (không cần broker), `tests/unit/test_streaming.py`:**
- `codec`: round-trip `SwapEvent` / `CabinetTelemetry`; payload hỏng (JSON sai, thiếu `occurred_at`, datetime naive) → `None`, không ném.
- `mqtt_topics`: dựng/tách topic; `station_id` lạ.
- `KafkaSource` với consumer giả: yield đúng event, bỏ payload hỏng, dừng khi idle.
- `KafkaSink` với producer giả: key = `session_id`.
- Publisher với producer/MQTT giả: bước 1–5 sang Kafka, 6–7 và telemetry sang MQTT, giữ thứ tự dòng trong mỗi đường.
- Bridge với client giả: payload sai không làm dừng vòng lặp; payload đúng sang đúng topic.
- `SessionTracker` với `allowed_lateness_seconds=10`: event bước kế đến sau hạn SLA nhưng trong 10 giây → không timeout; quá 10 giây → timeout, `detected_at` đúng.
- Hồi quy: `run_detector` với `source=None` và với `JsonlSource` truyền vào cho output sha256 giống hệt (dùng `smoke` sinh vào `tmp_path`, như `tests/unit/test_correlator.py`).

**Tích hợp (`tests/e2e/`, tự skip khi không kết nối được `127.0.0.1` cổng Kafka/MQTT, giống test Mailpit):**
- Publish `smoke` → bridge → detector `--source kafka` → tập `session_id` phát hiện bằng chạy JSONL.
- Event trùng gửi qua cả 2 đường chỉ được tính 1 lần (`duplicate_events_dropped` tăng đúng).
- Publish 1 payload MQTT sai → bridge vẫn chạy, đếm lỗi = 1.

## 10. Dừng lại hỏi chủ dự án khi

- Không làm được đường MQTT đúng hạn → hỏi trước khi chuyển tạm sang Kafka-only (GATE1_PLAN §3 "Nếu trễ tiến độ").
- Cần `allowed_lateness_seconds` > 30 để tập phát hiện bằng JSONL → báo số đo, không tự tăng.
- Redpanda + Mosquitto + Mailpit làm máy thiếu RAM.
- Cần đổi field của `SwapEvent` / `CabinetTelemetry` / `Incident`, hoặc sửa simulator.
- Cần dependency ngoài `aiokafka`, `aiomqtt`.

## 11. Ngoài phạm vi

- Context (telemetry, payment) đọc từ stream thay cho Parquet: Sprint 1.
- KPI dashboard tự cập nhật theo stream: Sprint 1 (chỉ bảng Cảnh báo cập nhật, mục 7).
- Redis dedup, Kafka Connect, TLS/chứng chỉ thiết bị, tích hợp đối tác (`ase.anomaly.behavior.v0`).
- Meta-alert consumer lag (CLAUDE.md §13): chỉ đo `max_arrival_lag_seconds`, chưa phát alert.
