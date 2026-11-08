# EVALUATOR_PLAN.md — Evaluator rule-only + field ablation (GATE1_PLAN §5, §6)

> Plan thực thi cho Sonnet. Đọc trước: CLAUDE.md §4 (SF1–SF9), §6.1, §8, §10.1; GATE1_PLAN.md §5, §6, §8; DETECTOR_PLAN.md §5.
> Mục đích: đo baseline rule-only (Gate 1) và kiểm chứng giả định rủi ro nhất: *với các trường mà hệ thống thật có, rule + correlator đạt SF4 ≥ 85%*.

## 0. Quy tắc làm việc (đọc kỹ)

- **Sửa file bằng chỉnh sửa có mục tiêu, không viết lại cả file từ trí nhớ.** Trước đây một agent ghi đè `ase/dashboard/metrics.py` và target `demo` của `Makefile` bằng bản cũ, làm mất tính năng (đã khôi phục ở commit `9799203`). Với `Makefile` và `pyproject.toml`: chỉ **thêm**, không sửa các target có sẵn.
- **Không sửa** `ase/detector/`, `ase/simulator/`, `ase/streaming/`, `ase/dashboard/`, `ase/notifier/`, `ase/knowledge/*.yaml`. Evaluator chỉ **đọc** output. Số chưa đạt thì ghi vào báo cáo, không chỉnh rule cho đẹp số.
- Evaluator là nơi **duy nhất** ngoài test được đọc `ground_truth.csv`.
- Chỉ thêm dependency `scikit-learn` (`uv add scikit-learn`). Cần thêm gì khác → dừng lại hỏi.
- Message commit không ghi ngày/tháng. Cuối việc: `make check` xanh, commit, dừng lại báo cáo.

## 1. Done khi

- `make eval PROFILE=eval` sinh `data/eval/eval/report.json` + `report.md` với số thật trên **split `test`**.
- `make eval PROFILE=dev` chạy được (toàn bộ phiên, dùng để chỉnh rule).
- `make ablation` sinh `data/dev/ablation/report.md`: bảng 4 preset + kết luận + danh sách trường cần xin.
- `make check` xanh; có `tests/unit/test_evaluator.py` (mục 7).

## 2. Đầu vào (đã có, chỉ đọc)

Detector ghi vào `data/<profile>/` (preset `ALL`) hoặc `data/<profile>/ablation/<preset>/`. Model nằm ở `ase/detector/correlator.py`, `dispatch.py`, `output.py`, `ase/common/models.py`.

| File | Model | Dùng cho |
|---|---|---|
| `detections.jsonl` | `Detection`: `session_id`, `category`, `severity`, `error_code`, `rule_id`, `kind`, `occurred_at`, `detected_at`, `incident_id`, `country`, `timezone`, ... | SF2, SF3, SF4, SF5 |
| `incidents.jsonl` | `IncidentRecord` (1 dòng / revision; có `merged_into`, `is_user_spike`, `dispatch`, `alert_sent_at`, `team`, `batch_ids`) | SF7, SF8 |
| `alerts.jsonl` | `P1Alert`: `incident_id`, `revision`, `team`, `channel`, `sent_at` | SF5 P1, SF7, SF8 |
| `batches.jsonl` | `TeamBatch` (`rows: list[BatchRow]`, `incidents`, `window_start`, `window_end`) | SF5 P2/P3/USER, SF7 |
| `coverage.json` | `RunReport` (`coverage.total_events_processed`, `total_sessions_seen`, `disabled_rules`, ...) | SF1, ablation |
| `ase.swap.events.jsonl` | `SwapEvent` (file nguồn) | SF1 (đếm `event_id` khác nhau) |
| `ground_truth.csv` | `GroundTruthRow`: `session_id`, `is_failure`, `category`, `scenario`, `injected_at`, `severity`, `error_code`, `scenario_instance_id`, `country`, `split` | mọi SF |

Ground truth không bao giờ có `UNKNOWN`. Phiên bẫy `pending_then_ok` có `is_failure=false`, `scenario="pending_then_ok"`.

## 3. Profile và split

| Profile | Trạng thái | Tập đánh giá |
|---|---|---|
| `dev` | Đã sinh (14.159 phiên, split `baseline` / `dev`) | Toàn bộ phiên |
| `eval` | **Chưa sinh lần nào** (`profiles.yaml` ghi "chỉ có cấu hình") | Chỉ `split == "test"` (10 ngày cuối) |

- Sinh `eval`: `make sim PROFILE=eval`. Simulator tự kiểm số case tối thiểu (`ase/simulator/checks.py`: mỗi nhóm ≥ 50 case trong tập test, mỗi scenario ≥ 2 lần). **Không đạt → dừng lại hỏi**, không sửa `profiles.yaml` hay simulator.
- Detector chạy trên **toàn bộ** 30 ngày (cần lịch sử cho baseline); evaluator chỉ lọc phiên `split == "test"` khi tính số.
- Ghi rõ `profile`, `split`, số phiên, `git_sha` trong báo cáo.

## 4. Cách tính từng SF (rule-only)

Đơn vị là **phiên**, trừ SF7/SF8 tính theo alert. Lọc tập đánh giá trước (mục 3).

| SF | Cách tính | Ngưỡng |
|---|---|---|
| SF1 | (a) `coverage.total_events_processed` / số `event_id` khác nhau trong `ase.swap.events.jsonl`; (b) `coverage.total_sessions_seen` / số dòng ground truth (trên toàn profile, không lọc split) | 100% |
| SF2 | Recall = phiên lỗi thật có trong `detections.jsonl` / tổng phiên lỗi thật. Báo theo nhóm và riêng **lỗi im lặng** (`error_code` ground truth là `STEP_TIMEOUT` hoặc `PAID_NOT_DELIVERED`) | ≥ 95% |
| SF3 | Precision = phiên phát hiện là lỗi thật / tổng phiên phát hiện. Liệt kê riêng phiên `pending_then_ok` bị báo nhầm | ≥ 90% |
| SF4 | Trên phiên vừa phát hiện vừa là lỗi thật: accuracy + macro-F1 của `category` dự đoán so với ground truth. `UNKNOWN` tính là sai. Dùng `sklearn.metrics.classification_report` và `confusion_matrix` với nhãn = 6 nhóm thật + `UNKNOWN` (chỉ ở cột dự đoán) | ≥ 85% |
| SF5 | **P1:** với mỗi incident P1 thật (mục 4.1), thời điểm alert P1 đầu tiên chứa bất kỳ phiên nào của nó (`alerts.jsonl.sent_at`, ghép qua `incidents.jsonl.session_ids`) − `injected_at` sớm nhất của incident. Báo p50/p95/max và theo `scenario`. **P2/P3/USER:** `window_end` của tin gom chứa phiên − `injected_at` của phiên; báo p50/p95/max, ngưỡng ≤ 15 phút | P1 p95 < 60s |
| SF7 | Trên mọi alert P1 + mọi tin gom: (a) % **đúng team** — team mong đợi = `Routing.route_for(category_thật, country).team` (P1: nhóm thật đa số của các phiên trong incident; tin gom: từng dòng); (b) % **hợp lệ schema** — parse lại bằng `Incident` / `TeamBatch`; (c) % **đủ mã GD + giờ địa phương** — render bằng `render_email` / `render_batch_email` (không gửi) và kiểm có `session_id` và chuỗi `format_local(occurred_at, timezone)` | 100% |
| SF8 | Số dòng `alerts.jsonl` / số incident P1 thật. Báo thêm số tin gom theo team mỗi ngày | ≤ 1,2 |
| SF6, SF9 | Cần LLM → ghi "chưa đo — Sprint 1" | — |

### 4.1 Incident thật (cho SF5, SF8)
- Mỗi `scenario_instance_id` khác rỗng là 1 incident thật; severity = severity cao nhất của các phiên trong đó.
- Mỗi phiên lỗi nền (`scenario_instance_id` rỗng) là 1 incident thật riêng.
- Incident P1 thật = incident có severity `P1`.

### 4.2 Giới hạn đã biết — ghi đúng như đo được, không "sửa"
- **SF5 của `PAID_NOT_DELIVERED`** dự kiến 110–150s (> 60s): lỗi im lặng chỉ lộ ra khi hết SLA 120s của bước 6. Đã đo: không có ngưỡng "chờ bắt đầu bước 6" nào vừa an toàn vừa < 60s (event bước 6 đầu tiên đến tới 65s sau thanh toán). Báo cáo phải nêu riêng dòng này và câu hỏi cho mentor: hệ thống thật có phát event lúc mở khoang không.
- Cửa sổ gộp SYSTEM là 15 phút (`detector.yaml` `window_minutes_by_category`), khác 5 phút ở CLAUDE.md §13: nêu trong báo cáo.

### 4.3 Số tham khảo để tự kiểm (dev, preset `ALL`, đo trước đây)
924/924 phiên lỗi được phát hiện, 0 báo nhầm, phân loại đúng khoảng 99,0% (9 phiên `step_timeout` có nhãn HARDWARE bị `UNKNOWN`), 13 alert P1. Lệch nhiều so với các số này → kiểm lại cách tính trước khi báo cáo.

## 5. Code (`ase/evaluator/`)

| File | Nội dung |
|---|---|
| `__init__.py`, `__main__.py` | CLI `python -m ase.evaluator --profile dev [--split test] [--run-dir PATH]`. `--run-dir` mặc định `data/<profile>/`. |
| `loader.py` | Đọc các file ở mục 2 thành model Pydantic (không dùng dict trôi nổi qua ranh giới module). Đọc ground truth bằng polars. Lọc split. |
| `metrics.py` | Mỗi SF 1 hàm thuần, nhận model đã load, trả model kết quả (`SF1Result`, ...). Không I/O. |
| `report.py` | `EvalReport` (Pydantic): `config: "rule-only"`, `profile`, `split`, `git_sha`, `preset`, kết quả SF, `n_sessions`. Ghi `report.json` + `report.md`: bảng SF (giá trị, ngưỡng, đạt/chưa đạt), theo nhóm, confusion matrix, 20 phiên báo sai / bỏ sót tiêu biểu (`session_id`, nhóm thật, nhóm dự đoán, `rule_id`, `scenario`). |
| `ablation.py` | Chạy evaluator cho 4 preset (`T0`, `T0+T1`, `T0+T1+T2`, `ALL`) đọc từ `data/dev/ablation/<preset>/` và `data/dev/`; ghi `data/dev/ablation/report.md` (mục 6). |

- Output: `data/<profile>/eval/report.json` và `report.md` (với `eval` là `data/eval/eval/`).
- `git_sha` lấy bằng `git rev-parse --short HEAD` (subprocess); không có git → `"unknown"`.
- Kết quả xác định: chạy 2 lần cùng dữ liệu ra `report.json` giống hệt (trừ `git_sha` nếu commit khác).

### Makefile (chỉ thêm target, thêm vào `.PHONY`)
- `eval`: `detect PROFILE=$(PROFILE)` rồi `python -m ase.evaluator --profile $(PROFILE)`, thêm `--split test` khi `PROFILE=eval`.
- `ablation`: `detect-ablation PROFILE=dev` (target đã có) rồi `python -m ase.evaluator.ablation --profile dev`.

## 6. Field ablation (`data/dev/ablation/report.md`)

Bảng chính:

| Preset | SF2 tổng | SF2 lỗi im lặng | SF4 accuracy | SF4 macro-F1 | SF4 theo 6 nhóm | Rule bị tắt (`coverage.disabled_rules`) |
|---|---|---|---|---|---|---|

Kết luận phải có:
1. **Tầng tối thiểu** đạt đồng thời SF4 ≥ 85% và SF2 ≥ 95%. Không preset nào đạt → nói rõ.
2. **Nhóm lỗi nào phụ thuộc trường nào:** so SF4 theo nhóm giữa 2 preset liền nhau (ví dụ HARDWARE tăng mạnh từ `T0+T1+T2` lên `ALL` → cần trường T3).
3. **Danh sách trường cần xin trong dữ liệu masked,** xếp theo mức tăng SF4 khi thêm tầng chứa nó. Ghi kèm nguồn dự kiến theo luồng mentor đã xác nhận, đánh dấu "đề xuất, cần mentor xác nhận":
   - Thiết bị qua MQTT: `cabinet_id`, `slot_id`, `door_state`, `battery_detected`, `heartbeat_at`, `temperature`, `hw_error_code`, `soc`, `battery_id`.
   - Microservice qua Kafka: `error_code`, `payment.captured`, `payment_provider`, `app_version`, `user_id`, `latency_ms`.
   - ERP/MES: `lot_id`, `soh`.
4. Nếu `ALL` vẫn chưa đạt: nêu nguyên nhân (từ danh sách phiên sai) và đưa vào backlog; **không** sửa simulator.

## 7. Test (`tests/unit/test_evaluator.py`)

Dữ liệu tự dựng nhỏ (vài phiên), đáp án tính tay:
- SF2: 4 phiên lỗi thật, bắt 3 → 75%; lỗi im lặng tính riêng đúng.
- SF3: phiên `pending_then_ok` bị báo → precision giảm đúng và xuất hiện trong danh sách bẫy.
- SF4: `UNKNOWN` tính sai; macro-F1 khớp `sklearn` tính tay.
- SF5 P1: alert `sent_at` − `injected_at` sớm nhất của incident; incident có revision 2 chỉ tính alert đầu.
- SF5 tin gom: phiên trong tin gom có `window_end` − `injected_at` = 14 phút → đạt; 16 phút → không đạt.
- SF7: alert gửi sai team → % đúng team giảm; batch row USER phải là `CSKH`.
- SF8: 3 alert cho 2 incident P1 thật → 1,5.
- Incident thật: 2 phiên cùng `scenario_instance_id` = 1 incident; phiên nền = mỗi phiên 1 incident.
- Lọc split: phiên `split != "test"` không được tính khi `--split test`.
- Hồi quy: chạy evaluator trên `smoke` sinh vào `tmp_path` (như `tests/unit/test_correlator.py`), SF2 = 100%, `report.json` ổn định giữa 2 lần chạy.

## 8. Dừng lại hỏi chủ dự án khi

- `make sim PROFILE=eval` không đạt kiểm tra số case tối thiểu.
- Baseline trên `eval`: SF2 < 80% hoặc SF4 < 70% → báo ngay, không tự hạ ngưỡng hay sửa dữ liệu (GATE1_PLAN §8).
- Định nghĩa một SF ở mục 4 không áp được vào dữ liệu (ví dụ thiếu `injected_at`).
- Cần sửa file ngoài `ase/evaluator/`, `tests/unit/test_evaluator.py`, `Makefile` (chỉ thêm), `pyproject.toml` / `uv.lock` (chỉ `scikit-learn`).
- Sinh `eval` hoặc chạy detector trên `eval` quá 10 phút.

## 9. Ngoài phạm vi
- Cấu hình rule + LLM, SF6, SF9, Batch API: Sprint 1.
- Evaluator cho chạy qua Kafka (`data/<profile>/stream/`): chỉ JSONL để tái lập được.
- Dashboard tab Metrics đọc `report.json`: Sprint 1.
