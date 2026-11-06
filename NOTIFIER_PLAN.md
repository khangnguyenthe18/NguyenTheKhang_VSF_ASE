# NOTIFIER_PLAN.md — Notifier tối thiểu: Mailpit + mock Teams

> Plan thực thi cho CLAUDE.md §15 bước 4. Đọc CLAUDE.md (§3, §6.1, §8, §14) trước khi code.
> **Cập nhật 06/11 (thay quyết định trước): bỏ Gmail thật, dùng Mailpit.** Mục 1–6 bên dưới là phần đã làm xong với Gmail. Chỗ nào nói Gmail / App Password / `thekhang20042@gmail.com` thì **mục 7 thay thế**. Việc cần làm tiếp nằm ở **mục 7**.

## 1. Bối cảnh

- Repo hiện có: `ase/common` (models, time, ids, config, transport), `ase/knowledge` (YAML + loader), `ase/simulator` (xong MOCK_PLAN bước 1–12, commit `48768ba`). **Chưa có** detector, model `Incident`, `ase/notifier`, `ase/feedback`.
- Notifier làm **trước** detector, demo bằng **incident mẫu**. Detector viết sau sẽ sinh đúng model `Incident` ở mục 3.2 và gọi cùng notifier.
- Người nhận mail: `thekhang20042@gmail.com`. Người gửi: một **Gmail riêng cho ASE** do chủ dự án tự tạo (xác minh 2 bước + App Password).
- Nút feedback trong mail và card là link tới `http://localhost:8000/...`, nên chỉ bấm được trên chính laptop chạy mock Teams. Như vậy là đủ cho demo.

## 2. Điều kiện môi trường (chủ dự án tự làm, cần trước khi chạy)

1. **Cài uv.** Máy hiện **chưa có `uv`**, nên `make check` / `make sim` đang hỏng.
   - Chạy `curl -LsSf https://astral.sh/uv/install.sh | sh`, mở terminal mới, rồi `uv sync`.
   - Không `pip install` thẳng vào `.venv`, vì làm lệch `uv.lock`.
2. **Tạo Gmail ASE.** Bật xác minh 2 bước, rồi tạo App Password tại `myaccount.google.com/apppasswords`.
3. **Điền `.env`** (đã có trong `.gitignore`, không commit):
   ```
   ASE_SMTP_USER=<gmail ASE>
   ASE_SMTP_PASSWORD=<app password 16 ký tự>
   ASE_MAIL_TO=thekhang20042@gmail.com
   ```

## 3. Thay đổi code

### 3.1 Dependencies
Thêm bằng `uv add aiosmtplib jinja2 httpx tenacity aiosqlite fastapi uvicorn` (không sửa tay `pyproject.toml`).

### 3.2 `ase/common/models.py`: thêm model `Incident`
Đây là contract giữa detector và notifier. Chỉ **thêm** model, không đổi các model cũ (MOCK_PLAN §3.9). Timestamp phải tz-aware: dùng lại `_require_aware` đã có trong file.

| Field | Kiểu | Ghi chú |
|---|---|---|
| `incident_id` | `str` | vd `INC-20261104-0007` |
| `revision` | `int ≥ 1` | tăng khi severity tăng hoặc LLM bổ sung |
| `category` / `severity` | `Category` / `Severity` | |
| `error_code` | `str \| None` | |
| `step` | `Step` | |
| `station_id`, `cabinet_id`, `slot_id` | `str`, `str \| None`, `str \| None` | trạm đại diện |
| `country`, `timezone` | `str`, `str` (IANA) | timezone của trạm đại diện; incident toàn cầu ghi `country="GLOBAL"` + liệt kê `countries` |
| `countries` | `list[str]` | |
| `session_ids` | `list[str]` (≥ 1) | phiên đầu tiên dùng cho subject |
| `first_seen_at`, `last_seen_at` | `datetime` (UTC) | |
| `n_sessions`, `n_stations`, `window_minutes` | `int` | dòng "Phạm vi" |
| `confidence` | `float` 0–1 | |
| `probable_cause` | `str` (≤ 500) | |
| `evidence`, `suggested_actions` | `list[str]` (≤ 5) | |
| `runbook_ids` | `list[str]` | |
| `needs_human_review` | `bool` | |

### 3.3 `ase/common/config.py`: thêm vào `Settings` (prefix `ASE_`)
- `smtp_host="smtp.gmail.com"`, `smtp_port=587`, `smtp_user: str | None`, `smtp_password: SecretStr | None`.
- `mail_from` (mặc định = `smtp_user`), `mail_to: str | None`.
- `teams_url="http://localhost:8000"`, `feedback_base_url="http://localhost:8000"`.
- `notifier_db = data_dir / "notifier.sqlite"`, `preview_dir = data_dir / "outbox_preview"`.
- `notify_mode: Literal["dry_run", "live"] = "dry_run"`, `max_alerts_per_run: int = 20` (chặn bão mail khi có bug).

Cập nhật `.env.example`: thêm các khoá `ASE_SMTP_*`, `ASE_MAIL_TO`, `ASE_NOTIFY_MODE`, giá trị để trống.

### 3.4 `ase/notifier/`
Mọi kênh gửi nằm sau một interface `Notifier` (Protocol, async `send(alert) -> SendResult`). Code nghiệp vụ không gọi `datetime.now()`: inject `Clock` từ `ase/common/transport.py` (CLAUDE.md §3.9).

| File | Nội dung |
|---|---|
| `router.py` | Dùng `Routing.route_for(category, country)` có sẵn ở `ase/knowledge/loader.py` (đọc `routing.yaml`). `USER` → không gửi (`alert: false`). `UNKNOWN` → IT Ops + `needs_human_review`. Trả `Alert` (Pydantic: incident + team + channel slug). |
| `render.py` + `templates/` | Jinja2 (`autoescape` cho HTML). Subject đúng **CLAUDE.md §8**: `[ASE] Cảnh báo giao dịch lỗi có thể liên quan đến {nhãn nhóm} _ {session_id} _ {HH:mm:ss dd/MM/yyyy}`. Body text + HTML theo mẫu §8: giờ địa phương qua `to_local` / `format_offset_for` (`ase/common/time.py`), có dòng "Quốc gia", 3 nút link feedback. Nhãn nhóm tiếng Việt lấy từ map trong code của notifier (§8). Mô tả bước lấy từ `state_machine.yaml` / `error_catalog.yaml`. `error_message` của event **không** đưa vào mail. |
| `adaptive_card.py` | Adaptive Card 1.5 rút gọn: tiêu đề, FactSet (mã GD, giờ, quốc gia, trạm/tủ/khoang, mã lỗi, phân loại, phạm vi); P1 dùng `style: attention` + dòng "@Người trực"; 3 `Action.OpenUrl` tới `{feedback_base_url}/feedback/{incident_id}/{confirm\|wrong_category\|resolved}`. |
| `email_smtp.py` | aiosmtplib, STARTTLS 587, MIME multipart (text + html). Ở chế độ `dry_run`: ghi `preview_dir/{incident_id}-r{revision}.html` + `.eml`, không kết nối SMTP. Thiếu `smtp_user` / `smtp_password` / `mail_to` ở `live` → lỗi rõ ràng (`NotifierConfigError`), log, không crash pipeline. |
| `teams_mock_client.py` | httpx async POST card tới `{teams_url}/webhook/{channel}`. Chạy cả ở `dry_run` (mock local, vô hại). |
| `retry.py` | tenacity: exponential backoff + jitter, tối đa 4 lần; tôn trọng `Retry-After` khi 429; không retry 4xx khác. SMTP: retry lỗi tạm thời (4xx SMTP, timeout), không retry lỗi xác thực 535. |
| `outbox.py` | aiosqlite, bảng `outbox(key PRIMARY KEY, incident_id, revision, channel, status, attempts, last_error, created_at, sent_at)`. Khoá idempotency `"{incident_id}:{revision}:{channel}"`: đã `SENT` thì bỏ qua. Đếm số alert gửi trong lần chạy, vượt `max_alerts_per_run` thì dừng gửi và log cảnh báo. |
| `service.py` | `notify(incident)`: router → render → outbox → gửi mail + card → cập nhật outbox. Log structlog có `incident_id`, `session_id`. |
| `demo.py` | `python -m ase.notifier.demo [--live]`. Gửi 3 incident mẫu, dữ liệu cố định, ID ổn định: (1) SYSTEM toàn cầu VN+PH+ID, `SESS_CREATE_5XX` bước 3 (deploy 503), P2; (2) PAYMENT chỉ PH, `PH_WALLET_1`, `PAY_GATEWAY_TIMEOUT`, trạm `Asia/Manila` → giờ GMT+8; (3) PAYMENT P1 `PAID_NOT_DELIVERED`, VN. |

### 3.5 `ase/notifier/mock_teams/app.py` (FastAPI, kiêm feedback API tối giản)
- `POST /webhook/{channel}`: nhận card JSON, lưu SQLite (`cards`: id, channel, incident_id, revision, card_json, received_at). Cùng `incident_id` thì revision mới **cập nhật** card cũ (CLAUDE.md §8: incident đang mở thì append, không thêm card mới).
- `GET /`: trang HTML kiểu Teams, sidebar các channel (IT Ops, Field Ops, Payment Ops, Business Ops, Battery Ops), card render bằng `adaptivecards` (cdn.jsdelivr.net, **version cố định**). Mỗi card hiện trạng thái feedback mới nhất.
- `GET /feedback/{incident_id}/{action}`: `action ∈ {confirm, wrong_category, resolved}`, lưu bảng `feedback(incident_id, action, at)`, trả trang "Đã ghi nhận". Bản đầy đủ của feedback API thuộc bước 9.
- Dùng chung file `notifier_db`. Thời gian ghi nhận lấy từ `WallClock` (đây là tầng I/O, không phải logic replay).

### 3.6 Makefile
```
teams:        $(UV_RUN) uvicorn ase.notifier.mock_teams.app:app --port 8000
notify-demo:  $(UV_RUN) python -m ase.notifier.demo $(if $(LIVE),--live,)
```

### 3.7 CLAUDE.md
Ghi quyết định vào §5 (dòng Mail/Teams) và §16 "Đã chốt": *"Mail: Gmail SMTP thật (tài khoản ASE riêng, App Password trong `.env`) thay MailHog; Teams: mock Teams. Không cần Docker cho notifier."* Không sửa các mục khác.

## 4. Test (`tests/unit/test_notifier.py`; không bao giờ gửi mail thật)
- Subject đúng định dạng §8; giờ hiển thị đúng với `Asia/Ho_Chi_Minh` (GMT+7), `Asia/Manila` (GMT+8), `Asia/Kolkata` (GMT+5:30).
- Router: `USER` không tạo alert; `UNKNOWN` → IT Ops + `needs_human_review`; `country_overrides` được áp.
- Card: JSON có đủ mã GD, giờ địa phương, 3 nút feedback; P1 có `attention`.
- Outbox: cùng `incident_id:revision` chỉ gửi 1 lần; revision mới gửi lại; vượt `max_alerts_per_run` thì dừng.
- Retry (httpx `MockTransport`): 429 + `Retry-After` được chờ rồi thành công; 400 không retry. Patch thời gian ngủ để test nhanh.
- SMTP: dùng sender giả, hoặc monkeypatch `aiosmtplib.send`. `dry_run` ghi file preview; `live` thiếu cấu hình → `NotifierConfigError`.
- Mock Teams qua `httpx.ASGITransport`: POST card → `GET /` chứa card đúng channel; revision mới thay card cũ; feedback được lưu.
- `make check` (ruff + ruff format + mypy strict + pytest) phải xanh.

## 5. Kiểm chứng end-to-end
1. `make check` xanh.
2. `make teams` (terminal riêng) → mở `http://localhost:8000`.
3. `make notify-demo` → 3 file preview trong `data/outbox_preview/`; 3 card hiện ở channel IT Ops và Payment Ops; card P1 nổi bật.
4. `make notify-demo LIVE=1` → 3 mail `[ASE] …` tới `thekhang20042@gmail.com`, giờ đúng địa phương (card PH ghi GMT+8).
5. Bấm "Xác nhận đúng" trong mail hoặc trên card → trang "Đã ghi nhận"; card hiện trạng thái mới.
6. Chạy lại `make notify-demo LIVE=1` → không có mail / card trùng.

## 6. Dừng lại hỏi chủ dự án khi
- Cần đổi hoặc xoá field của model đã có, hoặc của `Incident` sau khi detector dùng.
- Muốn trỏ notifier sang SMTP thật (công ty hay bất kỳ), hoặc gửi dữ liệu masked (CP2) ra ngoài máy.

## 7. Cập nhật 06/11: Mailpit thay Gmail

### 7.1 Quyết định và lý do
- **Bỏ hẳn Gmail thật.** Mail đi tới **Mailpit**: SMTP giả lập kèm web UI, chạy bằng Docker trên laptop.
  - Lý do: mạng công ty chặn Gmail. Khi mang vào dự án thật, hệ thống sẽ gửi bằng **mail server và mạng của công ty**.
- **Code giữ SMTP chung.** Chuyển sang SMTP công ty sau này **chỉ bằng `.env`** (host, port, kiểu TLS, user, password), không sửa code. Không có gì đặc thù Gmail trong code.
- Chọn Mailpit thay MailHog: cùng cổng (1025 SMTP, 8025 web), vẫn được bảo trì, giao diện tốt hơn. MailHog đã ngừng phát triển.
- Teams giữ nguyên mock Teams.

### 7.2 Điều kiện môi trường (chủ dự án tự làm)
- Bật Docker daemon: `sudo systemctl enable --now docker`, rồi `sudo usermod -aG docker $USER`, đăng xuất / đăng nhập lại, kiểm tra bằng `docker run --rm hello-world`.
- Không cần tài khoản Gmail, App Password hay `.env` để chạy demo.

### 7.3 Thay đổi code
1. **`docker-compose.yml`** (mới, ở gốc repo): chỉ một service `mailpit`.
   - Image `axllent/mailpit` **ghim version cụ thể** (tra tag hiện hành trên Docker Hub; không dùng `latest`).
   - Cổng `127.0.0.1:1025:1025` và `127.0.0.1:8025:8025`: chỉ bind localhost.
   - Không lưu volume: mail mất khi xoá container, chấp nhận cho demo.
2. **`ase/common/config.py`:**
   - Mặc định trỏ Mailpit: `smtp_host="localhost"`, `smtp_port=1025`.
   - Thêm `smtp_tls: Literal["none", "starttls", "ssl"] = "none"`.
   - Mặc định `mail_from="ase-alerts@ase.local"`, `mail_to="ops@ase.local"`.
   - `smtp_user`, `smtp_password` **không bắt buộc**.
   - Bỏ việc suy `mail_from` từ `smtp_user` nếu không còn cần.
3. **`ase/notifier/email_smtp.py`:**
   - `smtp_tls` ánh xạ sang tham số aiosmtplib: `none` → không TLS; `starttls` → `start_tls=True`; `ssl` → `use_tls=True`. Cổng 465 dùng `ssl`.
   - Chỉ gửi `username` / `password` khi `smtp_user` có giá trị.
   - **An toàn:** có `smtp_user` mà `smtp_tls="none"` và `smtp_host` không phải `localhost` / `127.0.0.1` → `NotifierConfigError`, để không gửi mật khẩu dạng rõ qua mạng.
   - Ở chế độ `live` chỉ bắt buộc `mail_from` và `mail_to`; thông báo lỗi liệt kê đúng khoá còn thiếu.
   - Mailpit chưa chạy (`SMTPConnectError` / `ConnectionRefusedError`): log rõ "Mailpit chưa chạy? `make mail-up`".
   - **Thử kết nối nhanh 1 lần cho mỗi lần chạy:** nếu không kết nối được thì mọi mail còn lại của lần chạy ghi `FAILED`, không retry đủ 4 lần cho từng incident. Outbox gửi lại ở lần chạy sau như hiện tại.
4. **`notify_mode`:** mặc định đổi thành `live`, vì đích mặc định là Mailpit local và vô hại.
   - Giữ `dry_run` (ghi file preview) cho trường hợp không có Docker.
   - Khoá outbox vẫn tách theo chế độ như hiện tại.
5. **Feedback an toàn với trình quét link** (cần cho dự án thật). Mailpit có tính năng "Link check", Microsoft Defender Safe Links của công ty cũng tự mở link. Nếu `GET` link ghi feedback thì sẽ có feedback giả.
   - `GET /feedback/{incident_id}/{action}`: chỉ trả trang xác nhận, có nút submit, **không ghi gì**.
   - `POST /feedback/{incident_id}/{action}`: mới ghi SQLite và trả "Đã ghi nhận".
   - Nút trong mail và card vẫn là link `GET` như cũ.
6. **`ase/notifier/demo.py`:** sửa help text của `--live` / `--dry-run` (bỏ chữ Gmail).
7. **Makefile:**
   - `mail-up`: `docker compose up -d mailpit`;
   - `mail-down`: `docker compose down`;
   - `notify-demo`: chạy live tới Mailpit; `make notify-demo DRY=1` để ghi preview;
   - **bỏ biến `LIVE`**.
8. **`.env.example`:** bỏ mọi chỗ nhắc Gmail. Ghi các khoá `ASE_SMTP_HOST/PORT/TLS/USER/PASSWORD`, `ASE_MAIL_FROM/TO`, `ASE_NOTIFY_MODE`, để trống, kèm comment: "mặc định Mailpit local; dự án thật điền SMTP công ty". Không đưa email cá nhân nào vào repo.
9. **CLAUDE.md §5 và §16:** thay dòng "Gmail SMTP thật…" bằng: *"Mail: aiosmtplib → **Mailpit** (Docker, container duy nhất) khi dev/demo; dự án thật trỏ sang SMTP công ty chỉ bằng `.env` (host/port/TLS/auth). Teams: mock Teams."* Sửa luôn dòng Mail ở mục Tech stack ưu tiên 1.
10. **Dọn tài liệu:** NOTIFIER_PLAN mục 1–6 giữ làm lịch sử, mục này là bản hiện hành. Xoá test hoặc tên hàm nào còn chứa "gmail" nếu chỉ là tên.

### 7.4 Test
- Unit: `smtp_tls` ánh xạ đúng tham số aiosmtplib (`none` / `starttls` / `ssl`); không có `smtp_user` thì không truyền auth; có user + `none` + host ngoài localhost → `NotifierConfigError`; thiếu `mail_to` ở `live` → lỗi rõ.
- Kết nối hỏng: lần chạy dừng thử sau lần kết nối thất bại đầu tiên; mail ghi `FAILED`; lần chạy sau gửi lại được.
- Feedback: `GET` không ghi bản ghi nào; `POST` ghi đúng 1 bản ghi.
- **E2E Mailpit** (đánh dấu, tự skip nếu `localhost:1025` đóng): chạy `run_demo`, rồi gọi API Mailpit `GET http://localhost:8025/api/v1/messages`.
  - Có 3 mail, subject bắt đầu `[ASE]`.
  - Mail PH ghi `GMT+8`.
  - Chạy lại → vẫn 3 mail (không trùng).
  - Dọn bằng `DELETE /api/v1/messages` trước test.
- `make check` xanh. Test mặc định không cần Docker (e2e tự skip).

### 7.5 Kiểm chứng end-to-end
1. `make mail-up`, mở `http://localhost:8025`: Mailpit trống.
2. `make teams` (terminal riêng), mở `http://localhost:8000`.
3. `make notify-demo` → Mailpit có 3 mail `[ASE] …` (HTML + text), giờ địa phương đúng; mock Teams có 3 card.
4. Bấm "Xác nhận đúng" trong mail → trang xác nhận → bấm nút → "Đã ghi nhận"; card đổi trạng thái.
5. Chạy lại `make notify-demo` → không có mail hoặc card trùng.
6. `make mail-down`, rồi chạy `make notify-demo` → lỗi rõ "Mailpit chưa chạy", không treo lâu, không crash.
