# MOCK_PLAN.md — Xây bộ mock hệ thống vận hành BSS (simulator ASE)

> File bàn giao tự chứa. Người/agent thực hiện đọc file này rồi đọc `CLAUDE.md` (nguồn spec). Nếu hai file mâu thuẫn, **CLAUDE.md thắng**; nếu cần lệch khỏi CLAUDE.md thì dừng lại và hỏi chủ dự án.

## 1. Mục tiêu
Tạo simulator sinh dữ liệu vận hành đổi pin VinFast có **ground truth cho mọi phiên**, để kiểm thử và đo ASE (SF1–SF9). Mốc: Gate 1 (11/11/2026) và CP1 (18/11/2026). Mock phải xong và **khoá tập test** trước khi ai đó viết rule cho detector.

Phạm vi = CLAUDE.md §15 **bước 1–2**: `common/` + `knowledge/` + `simulator/`.

**Spec nguồn trong CLAUDE.md:** §2 (hành trình 7 bước, SLA), §5.1 (transport, clock, quy tắc replay), §6 (category, error catalog, quy tắc phân biệt), §7 (data contract), §10 (đặc tả mock, tầng trường T0–T3, profile, đa quốc gia, bảng, scenario).

## 2. Phạm vi
**Làm:**
- `common/`: models, transport, clock, time, ids, config.
- `knowledge/`: `state_machine.yaml`, `error_catalog.yaml`, `routing.yaml`, `countries.yaml`.
- `simulator/`: entities, traffic theo giờ địa phương, FSM 7 bước, lỗi nền, 11 scenario §10, xáo trộn/nhân bản event, ground truth, checker.
- Profile `smoke` và `dev`. Profile `eval` chỉ có cấu hình và checker (chạy thật ở Sprint 1).

**Không làm:**
- Detector, notifier, LLM, feedback, Kafka.
- `app_logs` / `ase.logs.raw` (thuộc đối tác, DEFERRED, §10 và §16).
- Dữ liệu IN, TH, MY (chỉ VN, PH, ID).
- Bất cứ thứ gì thuộc phần đối tác (§9).

## 3. Ràng buộc bắt buộc
1. Thời gian lưu **UTC**, `occurred_at` luôn có tzinfo. Chỉ đổi sang giờ địa phương ở tầng hiển thị.
2. **Không hardcode GMT+7.** Mọi timezone, currency, bảng giá, provider đọc từ `knowledge/countries.yaml`.
3. **Không gọi `datetime.now()`** trong logic. Mọi thứ theo `Clock`/profile.
4. Deterministic: cùng `profile + seed` thì cùng output (kể cả hash file). Dùng 1 `random.Random(seed)` gốc và tách sub-stream theo khoá để thêm scenario không làm đổi traffic. Seed test khác seed dev.
5. **Giữ nguyên thứ tự dòng** khi xuất JSONL. Không sort theo `occurred_at` (§5.1).
6. Không hardcode error catalog trong code, luôn đọc từ YAML.
7. Mọi `error_code` sinh ra phải có trong `error_catalog.yaml`.
8. Code: async I/O, strict typing, Pydantic v2 cho mọi contract, ruff + mypy strict.
9. Chỉ thêm cột/field, không đổi tên hay xoá field của contract §7 và bảng §10.
10. **Import thống nhất `from ase.common...`** xuyên suốt repo (package gốc là `ase`, chạy bằng `python -m ase.simulator...`). Snippet trong CLAUDE.md §5.1 ghi `from common.models import ...` chỉ là minh hoạ; **không sửa CLAUDE.md**, chỉ dùng đường dẫn `ase.common` khi code.
11. `SwapEvent` giữ **đúng 11 trường** của §7 (`event_id, session_id, station_id, cabinet_id, slot_id, step, status, error_code, error_message, latency_ms, occurred_at`). Không nhét thêm trường T1–T3 vào event (xem mục 5.1).

## 3a. Quy ước dữ liệu đã chốt thêm (làm rõ giao thoa với CLAUDE.md)

**Category trong `error_catalog.yaml`.** Enum `Category` chỉ có 7 giá trị (§7), nhưng bảng §6.2 có 4 mã đặc biệt. Mỗi mã trong YAML có:
- `default_category`: luôn thuộc enum `Category`.
- `possible_categories`: danh sách (có thể rỗng) khi một mã có thể thuộc nhiều nhóm.
- `escalation_condition`: tuỳ chọn, điều kiện nâng mức độ.

| Mã | `default_category` | `possible_categories` | Mức độ |
|---|---|---|---|
| `STEP_TIMEOUT` | `UNKNOWN` | tất cả 6 nhóm thật | P2 |
| `BAT_PAIRING_FAIL` | `BATTERY_QUALITY` | `[BATTERY_QUALITY, SYSTEM]` | P2 |
| `SWAP_SLOW` | `SYSTEM` | `[SYSTEM, HARDWARE]` | P3 |
| `BAT_RETURN_FAULT` | `BATTERY_QUALITY` | — | P2; **P1** nếu `temperature` > 55°C (`escalation_condition` trong YAML, ngưỡng lấy từ khối `thresholds`) |

**Ngưỡng nhiệt của tủ đổi pin** (cảm biến nhiệt + BMS, chốt từ chủ dự án), đặt trong khối `thresholds` của `error_catalog.yaml`, không hardcode trong code:
| Nhiệt độ | Hành vi của hệ thống thật | Cách mô phỏng |
|---|---|---|
| > ~55°C | Cảnh báo lần 1 | `temperature` > 55 trong `cabinet_telemetry`, có `hw_error_code` cảnh báo mức 1. Phiên liên quan là `BAT_RETURN_FAULT` P1, hoặc `CABINET_OVERHEAT` P1 nếu nguyên nhân ở tủ/khoang |
| > ~60°C | Cảnh báo lần 2 **và tự ngắt nguồn tủ** | `hw_error_code` cảnh báo mức 2, tủ ngừng phục vụ trong một khoảng thời gian (cấu hình được): các phiên đang chạy hoặc mới đến ở tủ đó lỗi/kẹt, heartbeat tủ gián đoạn |

Dấu `~` nghĩa là xấp xỉ, nên các ngưỡng đặt dạng cấu hình (55 và 60) và simulator có thể thêm nhiễu nhỏ quanh ngưỡng. Tủ ngắt nguồn ở 60°C cũng là tín hiệu để đo ranh giới `HARDWARE` (tủ) với `BATTERY_QUALITY` (pin) trong correlator.

Rule/detector dùng `default_category`. `ground_truth.csv` thì luôn ghi **nhóm thật** (xem dưới).

**Ground truth luôn ghi nhóm thật, không bao giờ ghi `UNKNOWN`.** Cột `category` là nhóm của nguyên nhân gốc (một trong 6 nhóm thật). Nhờ vậy khi rule trả `UNKNOWN` mà LLM đoán đúng nhóm thật thì đo được đóng góp của LLM (rule-only vs rule + LLM, CLAUDE.md §4–§5).

**Phiên lỗi cố tình thiếu `error_code` để tạo bài toán cho Correlator + LLM.** Nếu mọi phiên lỗi đều có `error_code` 1–1 với catalog thì rule-only đạt ~100% SF4 và không có case `UNKNOWN` nào. Vì vậy simulator sinh hai loại phiên "khó":
1. Phiên `STEP_TIMEOUT` (im lặng, xem mục 8).
2. Một tỷ lệ nhỏ phiên lỗi bị bỏ `error_code` (`error_code=None`, event vẫn có `status` FAILED/TIMEOUT), chỉ còn dấu hiệu ở telemetry, latency, payment hoặc phân bố theo cụm. Tỷ lệ cấu hình trong `profiles.yaml` (`missing_error_code_rate`, mặc định đề xuất **10% của số phiên lỗi**, không phải 10% tổng giao dịch; tổng tỷ lệ lỗi nền vẫn là 3–5%). Chỉ áp cho scenario còn dấu vết gián tiếp để suy ra nhóm thật.

**Bảng giá và gói thuê (chính sách thật, `knowledge/countries.yaml` → `pricing`):**
- Phí đổi /pin/lần: VN 9.000 VND · PH 35 PHP · ID 6.000 IDR. Gói `RENT_1`, `RENT_2` (mọi nước), `FLEET` (vận doanh Green SM, **chỉ VN**). Không sinh user pin mua đứt (không dùng được dịch vụ đổi pin).
- `profiles.yaml` có `pricing_mode`, mặc định **`standard_fee`**: `payments.amount = swap_fee × số pin của gói` (`PricingSpec.standard_swap_amount`), **bỏ qua ưu đãi miễn phí** để bước 5 luôn có giao dịch tiền. `promo` (áp `promotions`, 20 lần miễn phí/tháng…) là tuỳ chọn sau.
- `PAY_WRONG_AMOUNT` = `amount` lệch `standard_swap_amount` của đúng nước/gói.
- `ELIG_QUOTA_EXCEEDED` vẫn là lỗi mock BIZ. Chính sách thật khi vượt 20 lần/tháng là **tính phí**, không từ chối; đừng sinh scenario dựa trên quota 20 lần.

**Quy ước `split` trong `ground_truth.csv`:**
| Profile | Quy ước |
|---|---|
| `smoke` (1 ngày) | toàn bộ `split = "smoke"` |
| `dev` (14 ngày) | 7 ngày đầu `baseline`, 7 ngày sau `dev` |
| `eval` (30 ngày) | 20 ngày đầu `baseline`, 10 ngày cuối `test` |

**Phiên bình thường** (`is_failure=false`, gồm cả bẫy PENDING→OK): `category`, `severity`, `error_code`, `scenario_instance_id` để **rỗng (`""`)**. Riêng bẫy PENDING→OK vẫn ghi tên scenario ở cột `scenario`; phiên thường không thuộc scenario nào thì `scenario` cũng rỗng.

## 4. Cấu trúc thư mục
```
pyproject.toml  uv.lock  Makefile  ruff.toml  .env.example
ase/
  common/{models,transport,time,ids,config}.py
  knowledge/{state_machine,error_catalog,routing,countries}.yaml
  simulator/{profiles.yaml,scenarios.yaml,entities,traffic,session_fsm,scenarios,
             telemetry,disorder,ground_truth,generator,checks,replay,__main__}.py
  schemas/ (+ fixtures/)
tests/unit/  tests/contract/
data/<profile>/   # sinh ra, không commit
```

## 5. Output mỗi lần chạy (`data/<profile>/`)
| File | Nội dung |
|---|---|
| `ase.swap.events.jsonl` | `SwapEvent` theo thứ tự đến (đã xáo trộn/nhân bản) |
| `stations/cabinets/slots/batteries/users.parquet` | master data, có `country`, `timezone` |
| `swap_sessions`, `payments`, `cabinet_telemetry` (.jsonl + .parquet) | trường T1–T3 |
| `ground_truth.csv` | cột §10 + `severity, error_code, scenario_instance_id, country, split` |
| `manifest.json` | profile, seed, git sha, số dòng + sha256 từng file, bảng đếm category × scenario × country |

`scenario_instance_id` dùng cho SF8 (ví dụ deploy lỗi 3 nước = 1 incident). `split` tách baseline/dev/test (quy ước ở mục 3a).

### 5.1 Quan hệ giữa events và các bảng T1–T3
- `ase.swap.events.jsonl` chỉ chứa `SwapEvent` với đúng 11 trường §7. Đây là nguồn duy nhất detector đọc theo luồng.
- Các trường tầng T1–T3 **không nằm trong event**, mà ở file riêng trong `data/<profile>/`, join theo khoá:

| Trường | File | Khoá join |
|---|---|---|
| `payment.captured`, `payment_provider`, `amount`, `currency` | `payments`, `swap_sessions` | `session_id` |
| `app_version`, `user_id` (hash) | `swap_sessions` | `session_id` |
| `battery_id`, `lot_id`, `soh` | `batteries` (qua `slot_in`/`slot_out` của `swap_sessions`) | `battery_id` |
| `soc`, `door_state`, `battery_detected`, `temperature`, `heartbeat_at`, `hw_error_code` | `cabinet_telemetry` | `cabinet_id` + `slot_id` + thời gian |
| `country`, `timezone` | `stations` | `station_id` |

Nhờ vậy field ablation (T0→T3) chỉ cần bật/tắt việc đọc và join các bảng phụ, không phải sinh lại dữ liệu. `latency_ms` là ngoại lệ vì nó nằm ngay trong event (§7).

## 6. Profile
| Profile | Trạm | Ngày | ~Phiên/ngày | Ghi chú |
|---|---|---|---|---|
| `smoke` | 2 (VN) | 1 | ~300 | CI, chỉ VN |
| `dev` | 5: VN 2, PH 1, ID 2 (WIB + WITA) | 14 | ~1.000 | viết rule hằng ngày |
| `eval` | 11: VN 7, PH 2, ID 2 (WIB + WITA) | 30 (20 baseline + 10 test) | ~1.500 | chạy thật ở Sprint 1 |

Mỗi trạm có 2–4 tủ, mỗi tủ 12 khoang. Pin thuộc 6 lô, 3 loại gói thuê, 4 app version, cao điểm 7–9h và 17–19h **giờ địa phương**, lỗi nền 3–5% (chủ yếu USER).
Ngưỡng `eval` (checker phải báo lỗi nếu không đạt): mỗi category ≥ 50 case lỗi trong test; mỗi scenario ≥ 2 lần trong test; mỗi tủ ≥ 7 ngày lịch sử trước test. Thiếu thì tăng `fail_prob`/`occurrences`, không tăng tổng số phiên.

Làm rõ cách áp dụng:
- "Mỗi category ≥ 50 case" chỉ áp cho **6 nhóm thật** (`SYSTEM, HARDWARE, PAYMENT, BUSINESS, USER, BATTERY_QUALITY`), đếm theo `ground_truth.category` trong phiên `split = "test"`. **Không** yêu cầu `UNKNOWN ≥ 50` vì ground truth không bao giờ ghi `UNKNOWN`.
- "Tủ ≥ 7 ngày lịch sử" đếm trên các ngày `split = "baseline"`.
- Phiên bình thường (category rỗng) không tính vào ngưỡng này.

## 7. Các bước triển khai
| # | Việc | File chính | Xong khi |
|---|---|---|---|
| 1 | Scaffold: `uv init`, deps (pydantic, pydantic-settings, pyyaml, orjson, polars, pyarrow, structlog, pytest, pytest-asyncio, ruff, mypy); Makefile `check`/`sim`; `.gitignore` bỏ `data/` | `pyproject.toml`, `Makefile`, `ruff.toml` | `make check` chạy được (rỗng) |
| 2 | `common/models.py` đúng §7 (có `latency_ms`) + model các bảng §10 | `ase/common/models.py` | Contract test: fixture hợp lệ parse được; `occurred_at` không tz bị từ chối |
| 3 | `transport.py` (SimClock, JsonlSource/Sink), `time.py`, `ids.py`, `config.py` | `ase/common/*` | SimClock không lùi; format `GMT+7`, `GMT+8`, `GMT+5:30` đúng |
| 4 | Knowledge YAML + loader Pydantic | `ase/knowledge/*.yaml` | Load được; mọi code trong §6.2 có mặt; mọi mã có `default_category` thuộc enum `Category` (4 mã đặc biệt theo mục 3a); mọi category có route |
| 5 | Entities + profiles | `entities.py`, `profiles.yaml` | `smoke` ra 2 trạm VN; `dev` ra VN2/PH1/ID2 (2 timezone) |
| 6 | Traffic theo giờ địa phương | `traffic.py` | Phân bố có cao điểm 7–9h, 17–19h local cho từng timezone |
| 7 | FSM 7 bước + payment + telemetry + lỗi nền | `session_fsm.py`, `telemetry.py` | Phiên OK đi đủ 7 bước; lỗi nền chỉ dùng code trong catalog |
| 8 | Ground truth + generator + JsonlSink → `make sim PROFILE=smoke` | `ground_truth.py`, `generator.py`, `__main__.py` | Có output smoke; 1 dòng GT/phiên; chạy 2 lần hash trùng |
| 9 | Framework injector + 11 scenario (mục 8) | `scenarios.py`, `scenarios.yaml` | Unit test cho từng scenario |
| 10 | Disorder (trùng, sai thứ tự, đến trễ) | `disorder.py` | Event trùng có cùng `event_id`; replay không làm clock lùi |
| 11 | Parquet/CSV/manifest + `checks.py` + `replay.py` | `generator.py`, `checks.py`, `replay.py` | `make sim PROFILE=dev` < 1 phút, in bảng đếm |
| 12 | Đóng băng tập scenario test (seed test + `split: test`) và commit | `scenarios.yaml` | Commit "freeze test scenarios" trước khi ai bắt đầu viết detector |

**Mốc nên đạt:** bước 1–4 trong 05/11, bước 5–8 trong 06/11, bước 9–12 trong 07/11. Còn 08–11/11 dành cho detector, notifier, evaluator, ablation.

## 8. Scenario cần inject (dấu vết phải quan sát được)
Mỗi lỗi phải làm thay đổi tín hiệu thật (error code, latency, telemetry), không chỉ gắn nhãn.

| Scenario | Nhóm | Cách thể hiện |
|---|---|---|
| Deploy lỗi session-service 503, 40', **cả VN+PH+ID** | SYSTEM | `SESS_CREATE_5XX` bước 3, `latency_ms` tăng dần trước khi fail; **1** `scenario_instance_id` cho cả 3 nước |
| App v3.2.1 lỗi quét QR | SYSTEM | Lỗi bước 3 chỉ với `app_version=3.2.1` |
| Khoang 07 tủ CAB-03 kẹt cửa nhiều ngày | HARDWARE | `SLOT_DOOR_JAM`, `door_state` không đổi, heartbeat chập chờn |
| `PH_WALLET_1` ngừng 20' (chỉ PH) | PAYMENT | `PAY_GATEWAY_TIMEOUT`/callback trễ; provider khác và nước khác bình thường |
| Hiển thị giờ trạm PH (+8) và ID-WITA (+8) | — | Có phiên lỗi ở các trạm này, `scenario=tz_display`, để kiểm SF7 |
| Đã trừ tiền nhưng không nhả pin | PAYMENT P1 | `captured=true`, không có event bước 6/7 (lỗi im lặng) |
| Lô pin SOH thấp | BATTERY_QUALITY | Pin xuất thuộc 1 `lot_id` có SOC/SOH thấp, `BAT_LOW_SOH` |
| Thiếu pin đầy giờ cao điểm | BUSINESS | Slot sẵn sàng về 0, `NO_CHARGED_BATTERY` bước 4 |
| PENDING rồi thành công | — (không lỗi) | Bước 5 PENDING lâu nhưng trong grace 90s rồi OK; `is_failure=false` (bẫy SF3) |
| `error_message` chứa prompt injection | — | Category giữ đúng lỗi thật (SF9) |
| Event trùng, sai thứ tự, đến trễ | — | `disorder.py`; ground truth theo phiên không đổi |

**Hai loại lỗi im lặng, cách biểu diễn trên `ase.swap.events.jsonl`:**
- `PAID_NOT_DELIVERED` (PAYMENT/P1, §6.3): bước 1–5 đều OK, bảng `payments` có `captured=true`, rồi phiên dừng. **Không có** event bước 6/7 nào (cả OK lẫn FAILED). Detector phải thấy qua timeout SLA của bước 6 kết hợp `payment.captured`.
- `STEP_TIMEOUT`: phiên phát event đến bước k (status `OK` hoặc `PENDING`) rồi biến mất hoàn toàn, không phát event bước kế tiếp và **không phát event `status="TIMEOUT"`**. Bắt buộc State Machine Tracker tự phát hiện bằng `SimClock` + SLA. Ground truth ghi nhóm thật của nguyên nhân (không phải `UNKNOWN`).
- Phân biệt với `status="TIMEOUT"`: đây là event mà tủ/gateway chủ động bắn ra, vẫn là event bình thường có `error_code`.
Simulator luôn sinh đủ T0–T3. Việc cắt trường để đo ablation thuộc về detector.

## 9. Kiểm chứng
- `make check`: ruff + mypy strict + pytest xanh.
- Unit test:
  - FSM chỉ đi transition có trong `state_machine.yaml`;
  - mỗi scenario sinh đúng `error_code`, scope, cửa sổ thời gian;
  - deploy 503 phủ cả 3 nước nhưng chỉ 1 `scenario_instance_id`;
  - `PH_WALLET_1` chỉ ảnh hưởng PH;
  - PENDING→OK có `is_failure=false`;
  - `PAID_NOT_DELIVERED` có `captured=true` và không có event bước 6/7;
  - mọi `error_code` sinh ra có trong catalog (`None` là hợp lệ với phiên cố tình thiếu mã);
  - cao điểm tính theo giờ địa phương;
  - `ground_truth.category` không bao giờ là `UNKNOWN`; phiên bình thường có `category/severity/error_code/scenario_instance_id` rỗng;
  - `STEP_TIMEOUT`: không có event bước kế tiếp và không có event `status="TIMEOUT"`;
  - tỷ lệ phiên lỗi thiếu `error_code` xấp xỉ `missing_error_code_rate`;
  - `split` đúng quy ước từng profile;
  - `SwapEvent` trong JSONL chỉ có đúng 11 trường §7;
  - `BAT_RETURN_FAULT` lên P1 khi nhiệt > 55°C, còn lại P2;
  - nhiệt > 60°C: có `hw_error_code` cảnh báo mức 2 và tủ ngừng phục vụ trong khoảng cấu hình.
- Determinism: `smoke` seed 42 chạy 2 lần, sha256 trong manifest trùng.
- Ground truth: số session trong `ground_truth.csv` bằng số `session_id` distinct trong events; mọi dòng event qua `SwapEvent.model_validate_json`, `occurred_at` có tz.
- `python -m ase.simulator.replay data/smoke/ase.swap.events.jsonl` chạy hết file, SimClock không lùi khi gặp event trễ.
- `make sim PROFILE=dev` dưới 1 phút và in bảng đếm category × scenario × country.

## 10. Khi nào phải dừng lại hỏi chủ dự án
- Cần đổi tên/xoá field của contract §7 hay bảng §10, hoặc lệch bất kỳ quyết định nào trong CLAUDE.md §16.
- Cần thêm nước ngoài VN/PH/ID, hoặc sinh `app_logs`.
- Cần đụng vào phần đối tác (§9) hoặc thêm Kafka/Redis/Postgres.
- Không đạt ngưỡng `eval` dù đã tăng `fail_prob`/`occurrences`.

## 11. Sau khi xong
Chuyển sang CLAUDE.md §15 bước 3 (`detector/`), có plan riêng. Không bắt đầu detector trước khi hoàn thành bước 12.
