# DETECTOR_PLAN.md — Detector rule-only (CLAUDE.md §15 bước 3)

> Plan thực thi. Đọc CLAUDE.md (§2, §3, §6, §10.1, §13), MOCK_PLAN.md §3a và §8 trước khi code.
> Hạn: **07–08/11**, trước Gate 1 (11/11). Bỏ ra ngoài phạm vi: LLM, anomaly EWMA, meta-alert consumer lag; để Sprint 1.

## 1. Mục tiêu

Đọc luồng `ase.swap.events.jsonl` theo đúng thứ tự dòng của file. Phát hiện phiên lỗi, kể cả lỗi im lặng; phân loại nhóm lỗi và mức độ bằng rule deterministic; gộp các phiên cùng lỗi thành **incident**. Kết quả ghi ra file để evaluator (bước 5) đo SF1–SF5, SF7, SF8, và tuỳ chọn gửi cảnh báo qua notifier đã có.

Done khi:
- `make detect PROFILE=dev` chạy hết `dev` dưới 30 giây.
- Mỗi scenario instance trong `dev` sinh ít nhất 1 incident.
- `deploy_503` (cả 3 nước) ra **1** incident, không phải 3.
- Field ablation chạy được với mọi tầng T0–T3 (mục 6).
- `make check` xanh.

## 2. Nguyên tắc bắt buộc

1. **Không đọc ground truth.** Code trong `ase/detector/` không bao giờ mở `ground_truth.csv`. Chỉ test được phép đọc. Có test chặn, giống test của dashboard.
2. **Không nhìn trước tương lai.** Thông tin từ bảng phụ (payments, telemetry, sessions) chỉ dùng khi thời điểm của nó ≤ `clock.now()`:
   - payment theo `callback_at`;
   - telemetry theo `heartbeat_at`.
   - Không dùng `swap_sessions.final_status` / `ended_at`: đó là kết quả cuối mà hệ thống thật chưa biết lúc đang chạy.
   - Bảng tĩnh (`stations`, `cabinets`, `slots`, `batteries`, `users`) dùng tự do.
3. **Thời gian:** dùng `SimClock` từ [ase/common/transport.py](ase/common/transport.py). Không gọi `datetime.now()`. Timer timeout được kiểm tra **mỗi khi clock tiến**; hết file thì flush các timer còn treo theo thời điểm cuối (CLAUDE.md §5.1).
4. **Không hardcode:** SLA đọc từ `state_machine.yaml`; mã lỗi, nhóm, mức độ, ngưỡng đọc từ `error_catalog.yaml` (`ErrorCatalog.severity_for`, `thresholds`); team đọc từ `routing.yaml`. Tham số của correlator (cửa sổ gộp, ngưỡng lan rộng) đặt trong file mới `ase/knowledge/detector.yaml`, có loader Pydantic.
5. **Idempotency:** dedup theo `event_id` (in-memory); bỏ qua transition lùi. ID incident deterministic theo thời gian mô phỏng: `INC-{yyyymmdd}-{seq:04d}`. Chạy 2 lần trên cùng dữ liệu → output giống hệt (so sha256).
6. **`error_message` là dữ liệu untrusted:** detector không bao giờ đọc nó để phân loại (CLAUDE.md §10.1).

## 3. Cấu trúc

```
ase/detector/
  __init__.py
  fields.py         # tầng trường T0–T3, FieldView, coverage
  context.py        # bảng phụ có "thời điểm biết" (payments, telemetry...)
  state_machine.py  # SessionTracker: dedup, trạng thái phiên, timer SLA
  classifier.py     # rule -> Classification
  correlator.py     # Classification -> Incident (mở/append/revision)
  output.py         # ghi detections.jsonl, incidents.jsonl, coverage.json
  main.py           # điều phối async; CLI `python -m ase.detector`
  __main__.py
ase/knowledge/detector.yaml   # tham số correlator, tầng trường
```

Pipeline: `JsonlSource` → dedup → `SessionTracker` → `Finding` → `Classifier` → `Classification` → `Correlator` → `Incident` → `output` (+ notifier nếu bật `--notify`).

Mọi contract là Pydantic, không truyền dict qua ranh giới module (CLAUDE.md §14):
- **`Finding`:** `session_id`, `station_id`, `cabinet_id`, `slot_id`, `step`, `kind` (`EXPLICIT_FAILURE` | `STEP_TIMEOUT` | `PAID_NOT_DELIVERED`), `error_code`, `status`, `occurred_at`, `detected_at`, `event_ids` (evidence).
- **`Classification`:** `Finding` + `category`, `severity`, `error_code` (có thể suy ra), `rule_id`, `confidence`, `needs_human_review`.
- Ghi ra **`ase.anomaly.transaction.v0.jsonl`** theo đúng ví dụ ở CLAUDE.md §9. Đây là topic dùng chung với đối tác; chỉ ghi file, không tích hợp.

## 4. Các thành phần

### 4.1 `SessionTracker` (state_machine.py)
- Mỗi phiên lưu: các bước đã thấy, `occurred_at` **cuối** của mỗi bước, status, phiên đã đóng chưa.
  - Phiên đóng khi bước 7 OK, hoặc gặp status trong `terminal_statuses`.
- Event đến sai thứ tự hoặc đến trễ: cập nhật theo bước. Event của bước nhỏ hơn bước đã đóng thì bỏ qua (không lùi trạng thái).
- **Timer SLA:** chờ bước k+1 tính từ event **cuối** của bước k (ví dụ PENDING → OK ở bước 5 thì tính từ OK). Hạn chót = mốc đó + SLA của bước k+1, lấy từ `state_machine.sla`.
  - Dùng heap theo hạn chót; sau mỗi event, pop mọi timer có hạn ≤ `clock.now()`. Timer đã cũ (phiên đã tiến tiếp) thì bỏ qua.
- **Phát `Finding`:**
  - Event FAILED/TIMEOUT → `EXPLICIT_FAILURE` (giữ `error_code`, có thể `None`).
  - Hết SLA mà không có event bước kế → `STEP_TIMEOUT`, ở bước đang chờ.
- **Đã kiểm chứng ở simulator:** disorder chỉ dời event trong giới hạn SLA, nên phiên bình thường không bao giờ bị timeout giả (commit `48768ba`). Tracker phải giữ đúng quy tắc tính "từ event cuối", nếu không sẽ báo nhầm ở phiên `pending_then_ok`.

### 4.2 `Classifier` (classifier.py)
Rule theo thứ tự ưu tiên; rule đầu tiên khớp thắng; mỗi rule có `rule_id` và danh sách trường cần.
1. **`R_PAID_NOT_DELIVERED`** (ưu tiên cao nhất, CLAUDE.md §6.3): payment `captured=true` đã biết tới `now`, và phiên timeout/failed ở **bước ≥ 6** (gồm `STEP_TIMEOUT` khi chờ bước 6) → `PAYMENT/P1`, `error_code=PAID_NOT_DELIVERED`. Cần T1 `payment.captured`.
2. **`R_CATALOG`:** có `error_code` → `default_category` của catalog; severity = `severity_for(code, temperature)`.
   - Nhiệt độ lấy từ telemetry khoang gần nhất ≤ `now` (cho `BAT_RETURN_FAULT`, CLAUDE.md §6.2).
   - Mã có `possible_categories` nhiều nhóm (`STEP_TIMEOUT`) thì đi tiếp các rule suy luận bên dưới.
3. **Suy luận khi không có mã** (FAILED/TIMEOUT với `error_code=None`, hoặc `STEP_TIMEOUT`), dùng telemetry khoang/tủ ≤ `now`:
   - bước 6, cửa không đổi (`door_state` vẫn CLOSED) → `HARDWARE`, `SLOT_DOOR_JAM`;
   - bước 6, cửa đã đóng mà `battery_detected=false` → `HARDWARE`, `SLOT_SENSOR_FAIL`;
   - tủ mất heartbeat quanh thời điểm lỗi (khoảng trống > 2 phút) → `HARDWARE`, `CABINET_DISCONNECT`;
   - bước 7, SOC xuất < `low_soc_pct` → `BATTERY_QUALITY`, `BAT_LOW_SOC`; pin thuộc lô có SOH < `low_soh_pct` → `BAT_LOW_SOH`;
   - bước 4, user `debt_flag` → `BUSINESS`, `ELIG_DEBT`; `package_expiry` < `now` → `ELIG_PACKAGE_EXPIRED`.
   - Mỗi suy luận có `confidence` < 1 (ví dụ 0,7).
4. **Không khớp rule nào** → `UNKNOWN`, `needs_human_review=True`. Correlator có thể gán lại theo cụm (mục 4.3).

### 4.3 `Correlator` (correlator.py)
- **Khoá gộp theo phạm vi tự nhiên của nhóm lỗi** (CLAUDE.md §6.3, §10 "Phạm vi quốc gia"):

  | Nhóm | Khoá gộp | Phạm vi trên alert |
  |---|---|---|
  | SYSTEM | `error_code` toàn mạng | toàn cầu nếu ≥ 2 nước, nếu không thì theo nước |
  | PAYMENT | `(error_code, payment_provider)`; thiếu provider (T0) thì `error_code` theo nước | nước của provider |
  | HARDWARE | `(error_code, cabinet_id)`, thiếu cabinet thì theo station | trạm / tủ / khoang |
  | BATTERY_QUALITY | `(error_code, lot_id)` nếu có T2, nếu không thì theo station | lô hoặc trạm |
  | BUSINESS | `(error_code, station_id)` | trạm |
  | USER | không tạo incident tức thời; vào tin gom 15 phút gửi CSKH (mục 4.5). Tỷ lệ USER tăng đột biến (`detector.yaml`) → incident P2 cho IT Ops | trạm |
  | UNKNOWN | `(step, station_id)` | trạm, `needs_human_review` |

- **Cửa sổ:** incident mở nhận thêm phiên nếu `occurred_at - last_seen_at ≤ window` (mặc định 5 phút, đặt trong `detector.yaml`; ghi đè theo nhóm qua `window_minutes_by_category`, hiện SYSTEM = 15 phút). Quá cửa sổ thì đóng; lỗi mới mở incident mới.
- **Gán theo cụm cho phiên không có mã:** phiên `UNKNOWN`, hoặc phiên suy luận có confidence thấp, nếu trùng bước và thời gian với một incident đang mở có mã (ví dụ `SESS_CREATE_5XX` không mã trong lúc deploy 503) thì gắn vào incident đó, với `rule_id=R_CLUSTER`. Đây là cơ chế chính để rule-only xử lý khoảng 10% phiên thiếu `error_code`.
- **Revision:**
  - Tạo incident → `revision=1`. Chỉ incident **P1** gửi alert đầu tiên **ngay lập tức** (SF5); P2/P3 đi theo tin gom 15 phút (§4.5).
  - Thêm phiên chỉ cập nhật `n_sessions`, `n_stations`, `countries`, `last_seen_at`; **không** tăng revision.
  - **Chỉ tăng revision khi severity tăng** (CLAUDE.md §8), để giữ SF8 ≤ 1,2.
- **Evidence:** `event_id` thật của tối đa 5 phiên đầu. Không bao giờ tạo ID không có trong luồng (SF9).

### 4.4 Bảng phụ theo thời gian (context.py)
- Nạp Parquet của `data/<profile>/` một lần.
- Tra cứu theo khoá kèm điều kiện thời gian ≤ `now`:
  - payment theo `session_id`;
  - telemetry khoang theo `(cabinet_id, slot_id)`;
  - heartbeat theo `cabinet_id`.
- Dùng `polars` + index dict trong bộ nhớ. `eval` có khoảng 1,4 triệu dòng telemetry: lọc trước theo `cabinet_id`, rồi tìm bằng bisect.
- `country` / `timezone` suy từ bảng `stations`. Không suy được thì mặc định `VN` và ghi vào coverage (CLAUDE.md §10).

### 4.5 Lịch gửi thông báo (dispatch.py) — chốt 06/11, CLAUDE.md §8 "Lịch gửi thông báo"
- **P1 gửi ngay:** incident P1 mới (hoặc lên P1) → `NotifierService.notify` ngay, như cũ.
- **P2/P3 gom theo team:** cửa sổ 15 phút theo `SimClock` (`notify.batch_interval_minutes`, căn mốc tròn UTC :00/:15/…). Khi clock vượt cuối cửa sổ: với mỗi team có lỗi, dựng **1 tin gom** (model Pydantic `TeamBatch`) gồm mọi lỗi của team trong cửa sổ, nhóm theo incident (mã GD, giờ địa phương trạm, bước, mã lỗi, phạm vi). Team rỗng thì không gửi. Hết file thì flush cửa sổ cuối.
- **USER → CSKH:** lỗi USER không tạo alert tức thời; vào tin gom của team `CSKH` mỗi cửa sổ. Sửa `routing.yaml`: `USER: {team: "CSKH", alert: true}` và cập nhật test notifier đang kỳ vọng USER không gửi.
- **Báo cáo ngày:** 18:00 giờ VN (`notify.daily_report_local_time`, `notify.daily_report_timezone: Asia/Ho_Chi_Minh`), khoảng 18:00 hôm trước → 18:00 hôm nay; toàn bộ lỗi mọi nhóm; đếm theo nhóm/nước/trạm + CSV đầy đủ; gửi quản lý + mọi team; `llm_summary` = `None` tới Sprint 1.
- Gửi qua notifier: mail theo team (bảng tối đa `notify.max_rows_mail` = 200 dòng, kèm CSV đầy đủ) + card mock Teams đúng kênh team (chỉ số + top 10 + link dashboard). Idempotency: `batch:{team}:{window_start}`, `daily:{date}`.
- Ghi `batches.jsonl` và `daily_reports.jsonl` để evaluator và dashboard dùng.

## 5. Output (`data/<profile>/`)

| File | Nội dung | Dùng cho |
|---|---|---|
| `detections.jsonl` | 1 dòng mỗi phiên được phát hiện lỗi: `session_id`, `detected_at` (thời gian mô phỏng), `category`, `severity`, `error_code`, `rule_id`, `incident_id` | Evaluator: SF2, SF3, SF4, SF5 |
| `incidents.jsonl` | Mỗi incident, mỗi revision 1 dòng (model `Incident` ở [ase/common/models.py](ase/common/models.py)), kèm `alert_sent_at` | SF7, SF8; tab Incident của dashboard |
| `ase.anomaly.transaction.v0.jsonl` | Bản xuất theo schema CLAUDE.md §9 | Đối tác (chỉ ghi file) |
| `coverage.json` | `available_fields`, rule bị tắt và lý do, số phiên phải suy `country` mặc định, số event trùng / đến trễ đã xử lý | Báo cáo ablation, Gate 1 |

Tab Incident của dashboard (đang để trống) đọc `incidents.jsonl` nếu có. Thay đổi nhỏ: thêm loader trong `ase/dashboard/data.py`, không đọc ground truth.

## 6. Tầng trường và field ablation (fields.py)
- `available_fields`: tập tên trường theo CLAUDE.md §10.1. Preset `T0`, `T0+T1`, `T0+T1+T2`, `ALL` khai báo trong `detector.yaml`.
- `FieldView` bọc event và các bảng phụ: trường không có trong `available_fields` thì trả `None`, như thể dữ liệu không có trường đó.
  - Ví dụ ở T0: `error_code`, `cabinet_id`, `slot_id`, `payment` đều là `None`.
- Mỗi rule khai báo các trường nó cần. Thiếu trường thì rule **tự tắt** và được ghi vào `coverage.json`, không crash.
- CLI: `--fields T0` / `--fields T0+T1` / … Output ghi vào thư mục con `data/<profile>/ablation/<preset>/`, để evaluator chạy cả 4 preset.

## 7. CLI, Makefile, notifier
- `python -m ase.detector --profile dev [--fields ALL] [--notify] [--speed N]`.
  - Mặc định không notify và replay nhanh nhất có thể.
  - `--speed 1` để đo SF5 theo thời gian thực trên `smoke`.
- **Makefile:**
  - `detect`: `--profile $(PROFILE)`;
  - `detect-ablation`: chạy 4 preset;
  - `e2e`: `sim` → `detect --notify`, mặc định trên `smoke`.
  - `make e2e PROFILE=smoke` là việc bước 7; nối vào đây cho xong luôn.
- **`--notify`:** P1 gửi ngay qua `NotifierService.notify` ([ase/notifier/service.py](ase/notifier/service.py)); P2/P3/USER gửi theo tin gom 15 phút và báo cáo ngày (mục 4.5). Chạy bất đồng bộ, không chặn vòng xử lý event.
  - Trần `max_alerts_per_run` (20) vẫn áp dụng. Chạy `dev` với notify sẽ chạm trần; đúng ý đồ: demo dùng `smoke`.
- In tóm tắt cuối mỗi lần chạy: số event, số trùng, số đến trễ, số phiên phát hiện theo category, số incident theo category, rule bị tắt.

## 8. Test (`tests/unit/test_detector.py`)
- **Tracker:**
  - timeout đúng tại SLA, tính từ event cuối của bước trước;
  - PENDING → OK ở bước 5 không timeout;
  - event trùng bị bỏ;
  - transition lùi bị bỏ;
  - hết file thì flush timer;
  - không gọi `datetime.now()` (monkeypatch cho nổ lỗi nếu bị gọi).
- **Classifier:**
  - `PAID_NOT_DELIVERED` thắng mọi rule khi có `captured=true`;
  - payment có `callback_at` sau `now` thì **chưa** được dùng (chống nhìn trước tương lai);
  - `BAT_RETURN_FAULT` thành P1 khi nhiệt > 55 °C;
  - mỗi suy luận theo telemetry ra đúng mã;
  - không khớp rule nào → `UNKNOWN` + `needs_human_review`.
- **Correlator:**
  - 3 nước cùng `SESS_CREATE_5XX` → 1 incident toàn cầu;
  - `PH_WALLET_1` → 1 incident PAYMENT, chỉ PH;
  - `SLOT_DOOR_JAM` ở 2 tủ khác nhau → 2 incident;
  - USER rải rác → không có incident;
  - thêm phiên không tăng revision, severity tăng thì tăng;
  - phiên không mã trong cửa sổ deploy được gắn vào incident deploy (`R_CLUSTER`).
- **Tầng trường:** ở T0, các rule cần T1+ bị tắt và có trong `coverage.json`; pipeline vẫn chạy hết file.
- **Trên dữ liệu `smoke` sinh vào `tmp_path`:**
  - chạy 2 lần → sha256 output giống hệt;
  - không đọc ground truth (monkeypatch);
  - mọi `evidence` là `event_id` có thật trong file;
  - test được đọc ground truth để kiểm: mỗi `scenario_instance_id` có ≥ 1 incident; phiên `pending_then_ok` không bị phát hiện.
- **Hiệu năng:** `dev` < 30 giây (đánh dấu là test chậm, nếu cần thì tách khỏi `make check`). In thông lượng (event/giây) ở tóm tắt cuối mỗi lần chạy; mục tiêu ≥ 3.000 event/giây trên laptop, so với ước tính thô khoảng 400 event/giây lúc cao điểm khi chạy thật.
- **Lịch gửi:**
  - P1 gửi ngay (trước khi cửa sổ đóng); P2/P3 không gửi ngay;
  - cửa sổ 15 phút căn đúng mốc; mỗi team tối đa 1 tin/cửa sổ; team rỗng không gửi;
  - mỗi lỗi P2/P3/USER nằm trong **đúng 1** tin gom, đúng team (USER → CSKH);
  - báo cáo ngày cắt đúng 18:00 giờ VN, chứa mọi lỗi trong 24 giờ, kể cả USER;
  - chạy lại không gửi trùng (outbox); mail cắt ở `max_rows_mail` nhưng CSV đủ dòng; không gọi LLM.

## 9. Dừng lại hỏi chủ dự án khi
- Cần đổi field của `SwapEvent`, `Incident` hoặc các bảng trong `ase/common/models.py` (chỉ được **thêm** field).
- Cần sửa simulator hoặc `scenarios.yaml`, kể cả khi phát hiện dữ liệu khiến rule không phân biệt được: báo lại, không tự sửa dữ liệu cho dễ.
- `deploy_503` không gộp được thành 1 incident nếu không thêm độ trễ trước alert đầu tiên (sẽ đánh đổi SF5).
- Detector chạy `dev` quá 30 giây sau khi đã tối ưu tra cứu bảng phụ.
