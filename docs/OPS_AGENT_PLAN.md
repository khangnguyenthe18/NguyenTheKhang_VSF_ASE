# OPS_AGENT_PLAN.md — ASE: AI Agent hỗ trợ vận hành, quét "Danh sách giao dịch đổi pin" 30 phút/lần

> **Phụ lục chi tiết** của `KE_HOACH.md` (bản 2 trang). Plan duyệt 10/11; cập nhật theo góp ý Gate 0: **chỉ VN**, **LLM = V-LLM nội bộ**, KPI + health score + báo cáo kỳ đã làm (xem mục "Cập nhật theo góp ý Gate 0" cuối file).

## Context

Chủ dự án đổi hướng ASE (09–10/11) từ "giám sát event realtime 7 bước" sang **AI Agent hỗ trợ vận hành** làm việc trên màn hình **Danh sách giao dịch đổi pin** của hệ thống thật (1 dòng/giao dịch). Năm mục tiêu:

1. Kiểm tra lỗi hệ thống, phân tích chuyên sâu.
2. Gửi cảnh báo cho OPS qua email, Teams, ticket.
3. Dùng ManageEngine (ME) bản trial làm hệ thống ticket.
4. Customer-facing, cải thiện chất lượng vận hành.
5. Báo cáo tổng kết tuần/tháng: Top 5/Top 10 lỗi, mỗi lỗi chiếm bao nhiêu % trên tổng số phiên lỗi (theo mẫu ảnh chủ dự án gửi).

**Quyết định đã chốt trong phiên này:**

| Chủ đề | Quyết định |
|---|---|
| Bảng mã lỗi | Bảng 66 mã chủ dự án giả lập (`ase/knowledge/error_catalog_v1.yaml`) là **chuẩn duy nhất, giữ nguyên văn**: không thêm cột, không đổi mức, không thêm mã. Ảnh Top 5 chỉ là **mẫu trình bày**, các mã trong ảnh không đưa vào bảng |
| Nhịp quét | Theo mentor: **quét Danh sách giao dịch 30 phút/lần**, mỗi lần tối đa một thông báo, để không spam OPS. Bỏ cảnh báo tức thời P1 và tin gom 15 phút |
| Người nhận | **Một đội OPS** nhận mọi cảnh báo và ticket |
| Báo cáo tuần/tháng | **Chỉ gửi mail tự động** (không làm trang dashboard) |
| Ticket | ManageEngine ServiceDesk Plus; chủ dự án đăng ký trial **sau** → trước mắt dùng ticket giả lập (`MockTicketClient`) |
| Mail | Hộp thư **giả lập** trên Mailpit (`ops@ase.local`, `cskh@ase.local`), không dùng địa chỉ thật |
| Dashboard | Đã dựng lại: trang Danh sách giao dịch + bộ lọc + xuất CSV; `/` mở thẳng dashboard |

**Hệ quả:** pipeline cũ (state machine 7 bước, correlator theo nhóm lỗi, MQTT/Kafka, P1/P2/P3, định tuyến theo category) không còn là đường chính. Giữ nguyên, không sửa, tới sau Gate 1 (11/11); việc xoá hẳn sẽ hỏi chủ dự án sau CP1.

## Kiến trúc mới

```
Nguồn giao dịch (TransactionSource)
  ├─ MockTransactionSource  : swap_transactions.parquet do simulator sinh (có ground truth)
  └─ ExportFileSource       : tệp "Xuất dữ liệu" CSV/XLSX của hệ thống thật (CP2)
        │   mỗi 30 phút (mốc :00/:30, theo Clock được inject)
        ▼
Scanner  → giao dịch Thất bại mới + giao dịch kẹt (Tạo mới / Đưa pin vào quá lâu)
        ▼
Analyzer (code, tất định) → tra bảng mã chuẩn, gộp sự cố, so với nền 7 ngày, khách bị ảnh hưởng
        ▼
Insight LLM (V-LLM nội bộ, 1 lần gọi/lần quét, tuỳ chọn) → nhận định + đề xuất, có evidence
        ▼
Notifier → 1 mail + 1 card Teams / lần quét (chỉ khi có lỗi) · ticket ManageEngine theo sự cố
        ▼
Báo cáo kỳ (tuần, tháng) → mail tự động Top N lỗi      ·      Evaluator (so ground truth)
```

Nguyên tắc giữ nguyên từ CLAUDE.md §3: chỉ đọc hệ thống vận hành; rule trước, LLM sau (LLM lỗi vẫn gửi được); thông báo và ticket do code gọi, không để LLM gọi; lưu UTC, hiển thị giờ địa phương trạm; không gọi `datetime.now()` trong logic (dùng `Clock`); idempotency qua outbox.

## Bước 0 — Chốt hiện trạng (10–11/11)

- Commit phần đang dở theo từng nhóm (không ghi ngày trong message): dựng lại dashboard; tắt UI mock Teams + sửa link mail; bảng mã chuẩn + tài liệu.
- Viết lại `docs/OPS_AGENT_PLAN.md` bằng nội dung plan này (bản hiện tại còn ghi "gom 15 phút", "nhiều team", các cột đề xuất đã bị bác).
- Cập nhật `CLAUDE.md`: §1 (mô tả dự án), §3, §4 (SF mới), §6 (trỏ tới bảng chuẩn), §8 (lịch 30 phút, một đội OPS, báo cáo tuần/tháng), §10 (mock dạng danh sách giao dịch), §15 (các giai đoạn dưới đây), §16.
- Gate 1 (11/11): trình bày skeleton và số đo hiện có; hướng mới đưa vào "Kế hoạch điều chỉnh". **Không đổi code pipeline cũ trước buổi Gate 1.**

## Giai đoạn 1 — Nền dữ liệu (11 → 14/11)

| # | Việc | File | Xong khi |
|---|---|---|---|
| 1.1 | Loader bảng mã chuẩn: đọc 5 cột, tra theo `(mã, loại tủ)`, gộp dòng trùng y hệt; mã không có → `None` (báo "ngoài bảng chuẩn"). Đổi tên file thành `error_catalog.yaml`, xoá catalog v0 | `ase/knowledge/loader.py`, `ase/knowledge/error_catalog.yaml` | Test: 136 dòng, 66 mã, chỉ High/Low; file không bị ghi |
| 1.2 | Model `SwapTransaction` (18 cột màn hình thật + `country`, `timezone`, `updated_at`), `TxnStatus`, `AlertLevel` (High/Low) | `ase/common/models.py` | Validator thời gian tz-aware |
| 1.3 | Simulator sinh thẳng danh sách giao dịch: mã trạm `B.<TỈNH><5 số>`, mã tủ `.<3 số>`, S/N tủ `BSS-…`, loại tủ CC_06_P/CC_12_P, mã eMSP 24 hex, mã trên tủ tăng dần, S/N pin `BAT…`. Lỗi chỉ dùng mã trong bảng chuẩn. Ghi `swap_transactions.{jsonl,parquet}` + `ground_truth.csv` (mã, mức, kịch bản, lần lặp) | `ase/simulator/txn_generator.py`, `scenarios.yaml`, `profiles.yaml`, `checks.py` | Tất định theo seed; profile smoke/dev/eval; tự kiểm số case tối thiểu |
| 1.4 | Dashboard đọc `swap_transactions.parquet` thay cho phần dựng từ event | `ase/dashboard/transactions.py` (`_build_base`), `data.py` | Trang giao dịch hiện mã thật dạng `B.HCM…`, lý do lỗi là mã chuẩn; test cũ vẫn xanh |

Dùng lại: `build_world` (`ase/simulator/entities.py`), `generate_arrivals`/`hourly_weights` (`traffic.py`), `sample_sites` (`real_sites.py`), `load_profiles`, `check_eval_thresholds` (`checks.py`), `_write_table` (`generator.py`).

**Kịch bản mock (khoá trước khi viết rule):** khoang kẹt khoá nhiều ngày (B6000, C6029, một tủ); mất điện một trạm (A0106/A0122 → B6025); tủ quá nhiệt rồi báo khói (A6009 → A0110); lô pin lỗi BMS (B605x theo lô); cổng thanh toán ngừng (D6036/D6037 nhiều trạm VN); tủ không gửi được giao dịch ở nhiều trạm (B0207); khách không đặt pin kịp, lỗi nền (D6031); pin lạ (D6033); mất pin (C6100); đã trả pin chưa nhận pin (D6038 sau khi pin vào); giao dịch kẹt không có mã; bẫy: giao dịch chậm nhưng hoàn thành.

## Giai đoạn 2 — Agent quét 30 phút + cảnh báo + báo cáo kỳ (15 → 18/11, CP1)

| # | Việc | File | Xong khi |
|---|---|---|---|
| 2.1 | `TransactionSource` (Protocol) + `MockTransactionSource`; `ScanState` (SQLite): mốc quét, giao dịch đã báo, sự cố đang mở | `ase/agent/source.py`, `state.py` | Quét lại cùng cửa sổ không báo trùng |
| 2.2 | Scanner: tại mốc :00/:30 lấy giao dịch trong cửa sổ nhìn lại (mặc định 60 phút), giữ **Thất bại chưa báo** + **kẹt** (`Tạo mới`/`Đưa pin vào` quá `stuck_after_minutes`) | `ase/agent/scanner.py`, `knowledge/detector.yaml` (khối `scan`) | Replay `dev` bằng `SimClock` < 30 giây |
| 2.3 | Analyzer (polars): đếm theo mã (tên + mức từ bảng); gộp sự cố theo tủ/connector, theo trạm, lan rộng nhiều trạm/nước, theo S/N pin; so tỷ lệ lỗi với cùng khung giờ 7 ngày trước; danh sách "đã trả pin, chưa nhận pin"; mã ngoài bảng | `ase/agent/analyzer.py` | Mỗi lần lặp kịch bản ↔ đúng 1 sự cố |
| 2.4 | Thông báo: **1 mail + 1 card Teams mỗi lần quét**, chỉ khi có lỗi; khoá idempotency `scan:{window_start}`; CSV đính kèm; link về dashboard đã lọc sẵn. Hộp thư giả lập: gửi từ `ase-agent@ase.local` tới `ops@ase.local` | `ase/notifier/scan_digest.py`, `templates/scan_email.{html,txt}.j2`, `routing.yaml` (một đội OPS + địa chỉ mail), `.env.example` | Mail tới `ops@ase.local` trong Mailpit, card vào mock Teams; không có lỗi thì không gửi |
| 2.5 | Ticket: `TicketClient` (Protocol) + `MockTicketClient` (SQLite). 1 ticket/sự cố, khoá `ticket:{incident_key}`; sự cố tái diễn ở lần quét sau → thêm ghi chú. Ngưỡng tạo ticket cấu hình được (mặc định: có mã High, hoặc ≥ 3 giao dịch, hoặc lặp 2 lần quét liên tiếp) | `ase/ticketing/{client,mock,service}.py`, `knowledge/ticketing.yaml` | Ticket trùng = 0; mail/card có mã ticket |
| 2.6 | **Báo cáo tuần/tháng** (mục tiêu 5) — xem dưới | `ase/report/period.py`, `templates/period_report.{html,txt}.j2` | Mail đúng mẫu ảnh; số liệu khớp bảng giao dịch |
| 2.7 | Evaluator mới trên ground truth giao dịch + `make agent`, `make report`, `make eval` | `ase/evaluator/txn_metrics.py`, `Makefile` | Bảng SF rule-only |

Dùng lại: `Outbox` (`ase/notifier/outbox.py`), `SmtpEmailNotifier`, `TeamsMockNotifier`, `http_retrying`/`smtp_retrying` (`retry.py`), `Clock`/`SimClock` (`ase/common/transport.py`), `format_local`/`format_offset_for` (`ase/common/time.py`), `query_transactions`/`to_csv`/`TxnFilters` (`ase/dashboard/transactions.py`), `dashboard_session_url` (`ase/notifier/render.py`).

### Báo cáo tuần/tháng (2.6)

- **Lịch (cấu hình trong `detector.yaml`, khối `report`):** tuần — 08:00 thứ Hai giờ VN, kỳ thứ Hai–Chủ nhật tuần trước; tháng — 08:00 ngày 1, kỳ tháng trước. Khoá idempotency `report:weekly:{ngày đầu kỳ}`, `report:monthly:{yyyy-mm}`. Chạy tay kỳ bất kỳ: `make report FROM=2026-10-01 TO=2026-10-28 TOP=5`.
- **Người nhận:** đội OPS (mail tự động, không có trang dashboard).
- **Nội dung, theo mẫu ảnh, một mục cho từng hệ thống BSS (hiện chỉ VN; thêm nước thì có thêm mục và mục toàn mạng):**
  > Top 5 lỗi đang chiếm số lượng lớn **82,37%** trong tổng số **7788** các phiên lỗi:
  > 1. `D6031` – BatteryInTimeout, chiếm **17,05%** (1328 case)
  > 2. …
- Tên lỗi lấy từ cột "Tên cảnh báo" của bảng chuẩn; mã ngoài bảng vẫn xếp hạng, ghi "mã ngoài bảng chuẩn"; giao dịch lỗi không có mã gom vào dòng "Không có mã lỗi".
- `top_n` mặc định 5, đổi thành 10 bằng cấu hình/tham số.
- Kèm: tổng giao dịch, tỷ lệ lỗi, so với kỳ trước, Top 5 trạm/tủ nhiều lỗi nhất, CSV đầy đủ theo mã.
- Mọi con số do code tính; LLM (giai đoạn 3) chỉ thêm 2–3 câu nhận xét.

## Giai đoạn 3 — LLM, ManageEngine thật, dữ liệu thật (19 → 25/11, CP2)

- **Insight LLM:** `ase/agent/insight.py` + `ase/triage/llm_client.py` (adapter **V-LLM nội bộ**, endpoint/model do mentor cấp; single-shot, structured output, `max_tokens` 800, ghi `data/llm_usage.jsonl`). Không dùng API bên ngoài (mentor không duyệt). 1 lần gọi mỗi lần quét có sự cố, 1 lần mỗi báo cáo kỳ. Nhận định phải trỏ tới mã lỗi/mã trạm/mã giao dịch có trong số liệu; không có evidence thì loại. `error_message` và mọi chuỗi tự do bọc `<untrusted_data>`. LLM lỗi → gửi bản rule.
- **`ManageEngineClient`** (chỉ làm khi chủ dự án đã có portal trial)**:** REST API v3 qua `httpx` + `tenacity`; ánh xạ trường trong `knowledge/ticketing.yaml` (subject, description, priority theo High/Low, group = OPS, trường tuỳ chỉnh `ASE Incident ID`). Chọn bằng `ASE_TICKET_PROVIDER=manageengine|mock`. Dữ liệu masked chỉ gửi lên ME khi `ASE_ALLOW_TICKET_ON_MASKED=true`. Đọc lại trạng thái ticket (chỉ đọc) để đo MTTA/MTTR.
- **`ExportFileSource`:** đọc tệp "Xuất dữ liệu" CSV/XLSX, ánh xạ cột tiếng Việt → trường bằng YAML, đổi giờ địa phương → UTC, báo cáo coverage (cột thiếu, dòng hỏng, mã ngoài bảng). Thêm dependency `fastexcel`.
- Gán nhãn 200–300 giao dịch lỗi thật, đo lại SF.

## Giai đoạn 4 — Customer-facing và chất lượng vận hành (26/11 → 09/12, CP3)

- Mục "Khách bị ảnh hưởng" trong mail mỗi lần quét + mail riêng cho CSKH (cùng nhịp 30 phút, chỉ khi có): khách đã trả pin chưa nhận pin; khách thất bại ≥ 2 lần trong ngày (chỉ `user_id` hash).
- Chỉ số vận hành từ ticket: MTTA, MTTR, % quá SLA, tỷ lệ tái diễn cùng tủ sau khi đóng ticket; đưa vào báo cáo tuần/tháng.
- Open point tuần: tủ lặp lỗi, mã lỗi tăng so với kỳ trước, SF chưa đạt.
- Báo cáo đánh giá cuối, tài liệu bàn giao, kịch bản demo.

## Chuyển sang mail và Teams thật (khi chủ dự án muốn)

Mặc định vẫn là giả lập (Mailpit + mock Teams). Thiết kế để chuyển sang thật **chỉ bằng cấu hình**, không sửa agent:

| Kênh | Cần gì từ chủ dự án | Việc phía code | Cách bật |
|---|---|---|---|
| **Mail thật** | Tài khoản SMTP (SMTP công ty, Microsoft 365, hoặc Gmail + mật khẩu ứng dụng) và địa chỉ nhận thật | Không cần viết thêm: `SmtpEmailNotifier` (`ase/notifier/email_smtp.py`) đã hỗ trợ host/port/TLS/đăng nhập | `.env`: `ASE_SMTP_HOST/PORT/USER/PASSWORD`, `ASE_MAIL_FROM`; người nhận trong `routing.yaml` |
| **Teams thật** | Một **URL webhook** của kênh Teams, tạo bằng Workflows ("Post to a channel when a webhook request is received"). Cần quyền tạo Workflow trong tenant — hiện chưa có (CLAUDE.md §5), phải xin IT hoặc dùng tenant riêng | Viết `TeamsWebhookNotifier` (~nửa ngày): bọc Adaptive Card hiện có vào khung tin nhắn rồi POST tới webhook, dùng lại `http_retrying`. Cùng interface `Notifier` với `TeamsMockNotifier` | `.env`: `ASE_TEAMS_PROVIDER=webhook`, `ASE_TEAMS_WEBHOOK_URL=…` |

Việc phải làm kèm khi bật kênh thật (đưa vào việc 2.4):
- **Chốt an toàn người nhận:** `ASE_NOTIFY_ALLOWED_RECIPIENTS` (danh sách địa chỉ/đuôi tên miền được phép). Địa chỉ ngoài danh sách bị chặn và ghi log, để chạy thử không gửi nhầm ra ngoài.
- **Dữ liệu masked/thật ra kênh ngoài:** cờ `ASE_ALLOW_EXTERNAL_NOTIFY_ON_MASKED` (mặc định `false`), cùng cơ chế với cờ LLM và ticket.
- **Nút phản hồi và link dashboard** trong mail/card đang trỏ `http://localhost:8000`: người nhận ở máy khác sẽ không mở được. Cần đặt `ASE_FEEDBACK_BASE_URL` thành địa chỉ máy chạy ASE mà OPS truy cập được (mạng nội bộ).
- **Chống spam vẫn giữ:** 1 mail + 1 card mỗi lần quét, giới hạn `max_alerts_per_run`.
- Trước khi bật thật lần đầu: chạy `ASE_NOTIFY_MODE=dry_run` để xem trước nội dung trong `data/outbox_preview/`, rồi gửi thử tới chính địa chỉ của chủ dự án.

Thời điểm đề xuất: mail thật có thể bật bất cứ lúc nào sau việc 2.4; Teams thật làm ở giai đoạn 3, khi có URL webhook.

## Success factor (đề xuất định nghĩa lại — duyệt cùng plan)

| # | Chỉ tiêu | Ngưỡng |
|---|---|---|
| SF1 | % giao dịch trong các cửa sổ quét được đọc | 100% |
| SF2 | Recall giao dịch lỗi + kẹt | ≥ 95% |
| SF3 | Precision | ≥ 90% |
| SF4 | Đúng mã + mức theo bảng; mỗi lần lặp kịch bản ↔ 1 sự cố | ≥ 85% |
| SF5 | Thời gian từ lúc lỗi tới thông báo, p95 | ≤ 35 phút (1 chu kỳ quét + xử lý) |
| SF6 | % sự cố LLM gợi ý đúng nguyên nhân (kịch bản) | ≥ 70% |
| SF7 | Thông báo hợp lệ, đủ mã giao dịch + giờ địa phương | 100% |
| SF8 | Số mail mỗi lần quét; ticket trùng | ≤ 1; 0 |
| SF9 | Ghi vào hệ thống vận hành; evidence không tồn tại; prompt injection thành công | 0 / 0% / 0% |
| SF10 | Báo cáo tuần/tháng: tổng % và số case khớp dữ liệu | 100% |

## Verification

- `make check` (ruff, mypy strict, pytest) xanh sau mỗi việc.
- `make sim PROFILE=dev` → có `swap_transactions.parquet`; mọi `error_code` nằm trong bảng chuẩn (test).
- `make agent PROFILE=dev`: replay bằng `SimClock`; kiểm tra mỗi mốc 30 phút tối đa 1 mail trong Mailpit (`http://localhost:8025`) và 1 card ở mock Teams (`ASE_MOCK_TEAMS_UI=true`); chạy lại lần hai không gửi thêm (idempotency).
- `make report FROM=… TO=… TOP=5`: đối chiếu tổng số phiên lỗi và từng dòng Top N với kết quả lọc "Thất bại" + xuất CSV trên dashboard cho cùng khoảng ngày.
- `make eval PROFILE=eval`: bảng SF1–SF5, SF7, SF8, SF10 rule-only; sau giai đoạn 3 thêm cột rule + LLM và chi phí.
- Ticket: test `MockTicketClient` (không trùng, tái diễn → ghi chú); ManageEngine thật kiểm tra tay trên portal trial.
- Dashboard: mở `http://localhost:8000`, trang giao dịch hiện mã `B.…` và mã lỗi chuẩn; bộ lọc và xuất dữ liệu vẫn đúng.

## Phụ thuộc bên ngoài (chủ dự án đã trả lời 10/11)

1. **ManageEngine: đăng ký sau.** Toàn bộ luồng ticket làm và demo trên `MockTicketClient`; `ManageEngineClient` chỉ viết khi chủ dự án có portal trial (việc ở giai đoạn 3 lùi theo, không chặn việc khác). Lưu ý trial thường khoảng 30 ngày: nên đăng ký sao cho còn hạn tới sau CP3 (09/12).
2. **Bảng mã lỗi đã đúng** (chụp từ tệp XLSX gốc): không cần đối chiếu thêm, không làm script import. Ở bước 0, xoá dòng chú thích "cần đối chiếu tệp gốc" trong đầu file `error_catalog_v1.yaml` (chỉ sửa chú thích, không đụng dữ liệu bảng).
3. **Mail giả lập:** không dùng địa chỉ thật. Tạo hộp thư giả trên Mailpit, khai báo trong `knowledge/routing.yaml`:
   - `ops@ase.local` — đội OPS: mail mỗi lần quét 30 phút, báo cáo tuần/tháng.
   - `cskh@ase.local` — CSKH: danh sách khách bị ảnh hưởng (giai đoạn 4).
   - `ase-agent@ase.local` — địa chỉ gửi.
   Xem mail tại Mailpit `http://localhost:8025` (`make mail-up`). Chuyển sang SMTP thật sau này chỉ bằng `.env` + `routing.yaml`.
4. Tệp "Xuất dữ liệu" mẫu của Danh sách giao dịch (CSV/XLSX): cần cho `ExportFileSource` ở giai đoạn 3; chưa có thì viết trên fixture giả cùng cột với màn hình.


## Cập nhật theo góp ý Gate 0 (10/11)

| Góp ý | Đã làm |
|---|---|
| Rubric sai khung | `RUBRIC.md`: 3 tiêu chí × mức 2–3–4, có số; teamwork tách riêng |
| Lệch đề: thiếu KPI, health score, open point từ KPI | `ase/report/period.py` + `knowledge/kpi.yaml`: KPI success factor, health score 4 dịch vụ critical, open point (health thấp, mã tăng so với kỳ trước, lỗi lặp tại tủ), báo cáo tuần/tháng tự động (`make report`, agent tự lập theo lịch) |
| Cắt phạm vi | Danh sách giao dịch chỉ sinh **VN** (`txn_stations`); kịch bản "ví PH" thay bằng "cổng thanh toán VN ngừng"; Kafka, detector realtime đóng băng và nhường cho Nhân |
| Không gọi API LLM ngoài | `ASE_LLM_PROVIDER=none` mặc định, chỉ hỗ trợ `vllm_internal`; `ASE_ALLOW_EXTERNAL_LLM_ON_MASKED=false` |
| Pair contract hai bản lệch | `contracts/PAIR_CONTRACT_v0.1.md` + schema chung `ase.anomaly.event` v0.1 + contract test |
| Tài liệu quá dài | `KE_HOACH.md` 2 trang; file này là phụ lục |
| Chưa có baseline thủ công OPS | Cách đo trong `RUBRIC.md` (đo trước CP2) |
