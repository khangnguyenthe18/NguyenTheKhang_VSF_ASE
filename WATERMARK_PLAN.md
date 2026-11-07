# WATERMARK_PLAN.md — Đồng hồ theo watermark partition cho `--source kafka`

> Plan thực thi cho argon. Đọc trước: STREAMING_PLAN.md (toàn bộ), CLAUDE.md §3 mục 9–10, §5.1, §13.
> Bối cảnh: bỏ cách trộn partition theo `occurred_at` trong `KafkaSource` thì `make e2e PROFILE=smoke` hỏng.

## 1. Vấn đề (đã đo)

`SimClock` tiến theo `occurred_at` lớn nhất đã thấy. Topic `ase.swap.events` có 3 partition, và mỗi lần `getmany` aiokafka trả trọn batch của partition này rồi mới tới partition khác. Đồng hồ nhảy tới cuối partition đầu, nên event của partition sau thành "đến trễ" và bắn timeout giả.

| Cấu hình (`smoke`, 3 partition) | Phiên lệch so với JSONL | `max_arrival_lag_seconds` |
|---|---|---|
| Trộn partition theo `occurred_at` (đã bỏ) | 0 | 7,9 s |
| Không trộn, `E2E_SPEED=18000`, 3 lần | 0 / +3 / +2 timeout giả | 177–193 s |
| Không trộn, `E2E_SPEED=1800` | 0 | 12,6 s |

Độ trễ tăng theo tốc độ phát, nên chỉnh tốc độ không giải quyết tận gốc. Độ trễ thật giữa hai đường MQTT/Kafka chỉ khoảng 8 s (bằng độ trễ vốn có trong file JSONL).

## 2. Giải pháp: watermark = min theo partition

Đây là cách các hệ stream (Flink, Kafka Streams) xử lý thời gian sự kiện trên nhiều partition.

- Mỗi partition `p` có `high_p` = `occurred_at` lớn nhất đã đọc từ `p`.
- **Watermark** = `min(high_p)` trên các partition **còn dữ liệu chưa đọc**. Partition đã đọc tới cuối (`position >= highwater`) không giữ watermark lại.
  - Mọi partition đều đã đọc tới cuối → watermark = `max(high_p)` (để đồng hồ không đứng yên khi luồng ngừng).
  - Partition còn backlog nhưng chưa đọc được event nào → watermark **không tăng** (chưa biết thời gian của nó).
- Watermark không bao giờ lùi (`SimClock.advance` đã đảm bảo).
- **Nguồn dữ liệu sở hữu đồng hồ:** `KafkaSource` gọi `clock.advance(watermark)` trước mỗi lần `yield`, giống `JsonlSource` hiện đang `advance(occurred_at)` trước khi `yield`.
- `allowed_lateness_seconds` (10 s) giờ chỉ còn phải che độ trễ giữa hai đường MQTT/Kafka, không phải độ lệch giữa các partition.

## 3. Thay đổi code

### 3.1 `SessionTracker` (`ase/detector/state_machine.py`)
- Thêm tham số `advance_clock: bool = True`. `True` giữ nguyên hành vi hiện tại. `False`: không gọi `clock.advance` trong `on_event`, vì nguồn đã làm.
- Trong `on_event`, giới hạn của lần pop timer đầu đổi từ `occ_utc` thành `min(occ_utc, now)`. Lý do: ở chế độ watermark, `occurred_at` của event có thể lớn hơn đồng hồ; không được bắn timer trước watermark. Ở chế độ JSONL, `now >= occ_utc` nên kết quả không đổi.
- `detected_at` của `EXPLICIT_FAILURE` đổi thành `max(now, occ_utc)`, để không bao giờ phát hiện trước khi lỗi xảy ra. JSONL không đổi (vì `now >= occ_utc`).
- Thêm số đo `max_lateness_vs_clock_seconds` = max(`clock.now()` − `occurred_at`) lúc event đến, tính trên event có `occurred_at < clock.now()`. Đây là số phải nhỏ hơn `allowed_lateness_seconds`. Giữ nguyên `max_arrival_lag_seconds` hiện có.

### 3.2 `KafkaSource` (`ase/streaming/kafka_io.py`)
- Nhận thêm `clock: SimClock | None`. Có clock → bật chế độ watermark.
- Theo dõi `high_p` theo `TopicPartition` của từng message.
- Tính trạng thái "đọc tới cuối" bằng `consumer.highwater(tp)` (đồng bộ, có sau mỗi lần fetch) và `await consumer.position(tp)`. Tách phần này sau một interface nhỏ (`PartitionProgress` hoặc hàm truyền vào) để unit test bằng client giả.
- Trước mỗi `yield`: cập nhật `high_p`, tính watermark, `clock.advance(watermark)` nếu có giá trị.
- Thứ tự `yield` giữ đúng thứ tự aiokafka trả về (đã bỏ trộn ở bước trước). **Không** sắp xếp lại theo `occurred_at`.
- Thêm số đo `max_watermark_lag_seconds` = max(`max(high_p)` − watermark), để biết partition nào kéo watermark lại. In ở tóm tắt cuối và ghi vào `coverage.json`.
- Hết luồng (idle quá `max_idle_seconds`): `clock.advance(max(high_p))` rồi kết thúc, để `tracker.flush()` chạy như cũ.

### 3.3 Nối dây (`ase/detector/main.py`)
- `--source kafka`: tạo `KafkaSource(clock=sim_clock, ...)` và `SessionTracker(..., advance_clock=False, allowed_lateness_seconds=...)`.
- `--source jsonl`: không đổi gì.
- `Classifier`, `ContextStore` (payment chỉ dùng khi `callback_at <= now`, telemetry khi `heartbeat_at <= now`) và `Dispatcher.tick` dùng cùng clock. Ở chế độ watermark, các tra cứu này bảo thủ hơn (không bao giờ nhìn trước tương lai), không cần sửa.

### 3.4 Publisher
- Khoảng nghỉ 25 ms khi chuyển từ đường MQTT sang Kafka hiện đang viết cứng: đổi thành tham số `--path-yield-ms` (mặc định 25), ghi vào Makefile kèm comment.
- Đo `make e2e` cả với `--path-yield-ms 0` và ghi kết quả vào báo cáo cuối việc (mục 6), để biết watermark còn cần khoảng nghỉ này không.

### 3.5 `coverage.json` / `CoverageReport`
- Chỉ thêm field: `max_lateness_vs_clock_seconds`, `max_watermark_lag_seconds` (mặc định 0). Không đổi field cũ.
- Assert trong target `e2e` của Makefile đổi sang `max_lateness_vs_clock_seconds < allowed_lateness_seconds`, đọc ngưỡng từ `detector.yaml` (không viết cứng `10.0`).

## 4. Nguyên tắc
- **JSONL không đổi:** `detections.jsonl`, `incidents.jsonl`, `alerts.jsonl`, `batches.jsonl`, `ase.anomaly.transaction.v0.jsonl` của `make detect PROFILE=dev` và `smoke` phải giống hệt sha256 trước khi sửa. `coverage.json` chỉ được khác ở các field mới thêm.
- Không gọi `datetime.now()` trong detector. Đồng hồ thật chỉ dùng cho timeout idle, backoff, nhịp phát.
- Không sửa simulator, `scenarios.yaml`, `state_machine.yaml`. Topic giữ 3 partition.
- Không thêm dependency.
- Message commit không ghi ngày/tháng.

## 5. Test

**Unit (`tests/unit/test_streaming.py`, consumer giả có partition):**
- Watermark = min theo partition: 2 partition, P0 tới 10:05, P1 tới 10:01 → clock = 10:01.
- Partition đã đọc tới cuối không giữ watermark; mọi partition tới cuối → watermark = max.
- Partition còn backlog chưa đọc event nào → watermark không tăng.
- Watermark không lùi khi partition chậm gửi event cũ hơn.
- Hai phiên ở 2 partition, batch P0 (thời gian muộn) trả trước batch P1 (thời gian sớm hơn): **không** có timeout giả. Đây là đúng lỗi đã đo ở mục 1.
- `SessionTracker(advance_clock=False)`: event có `occurred_at > clock.now()` không làm bắn timer trước watermark; `detected_at = max(now, occurred_at)`.
- Hồi quy: `run_detector` với JSONL cho sha256 5 file output giống hệt (test hiện có trong `test_streaming.py` / `test_correlator.py`).

**Tích hợp (`tests/e2e/`, skip khi không có broker):**
- Siết test hiện có: so **tập** `session_id` với JSONL (giống target `make e2e`). Hiện `pytest tests/e2e` pass trong khi `make e2e` fail, tức test đang lỏng hơn tiêu chí "Done khi".

## 6. Done khi
- `make e2e PROFILE=smoke` pass **3 lần liên tiếp** với `E2E_SPEED=18000`, 3 partition, không trộn partition.
- Chạy thêm 1 lần trên `dev` (14 nghìn phiên) qua Kafka: tập phiên phát hiện bằng JSONL. Ghi thời gian chạy.
- Báo cáo cuối việc có bảng: cấu hình (`--path-yield-ms` 25 / 0) × lần chạy → phiên lệch, `max_lateness_vs_clock_seconds`, `max_watermark_lag_seconds`.
- 5 file output JSONL giống hệt sha256 (mục 4).
- `make check` xanh (cả khi bật và tắt broker).

## 7. Dừng lại hỏi chủ dự án khi
- Cần `allowed_lateness_seconds` > 30 để đạt "Done khi".
- `highwater` / `position` của aiokafka 0.15.0 không cho biết đúng partition đã đọc tới cuối (ví dụ `highwater` là `None` lâu sau khi fetch).
- Cần sửa file ngoài: `state_machine.py`, `kafka_io.py`, `main.py`, `fields.py` (`CoverageReport`), `classifier.py` (`build_coverage_report`), `publisher.py`, `Makefile`, test.
- Chạy `dev` qua Kafka quá 10 phút.

## 8. Ngoài phạm vi
- Watermark cho topic telemetry (detector vẫn đọc telemetry từ Parquet).
- Meta-alert khi watermark đứng yên quá lâu (CLAUDE.md §13 "Consumer lag"): chỉ đo `max_watermark_lag_seconds`, để Sprint 1.
- Nhiều consumer / consumer group rebalance: 1 detector đọc mọi partition.
