# DASHBOARD_PLAN.md — Dashboard vận hành mạng lưới BSS

> Plan thực thi. Đọc CLAUDE.md (§2, §3, §6, §10, §14), MOCK_PLAN.md §5 và NOTIFIER_PLAN.md trước khi code.
> Quyết định của chủ dự án (06/11): dashboard trình bày **một hệ thống vận hành BSS chuẩn**, dựng thành **trang trong FastAPI**, chạy chung app với mock Teams.

## 1. Mục tiêu

Một console vận hành kiểu NOC (trung tâm giám sát) cho mạng lưới đổi pin, cho OPS trả lời nhanh các câu hỏi:
- Mạng lưới có khỏe không? Bao nhiêu trạm/tủ online, bao nhiêu pin sẵn sàng?
- Giao dịch đang chạy thế nào? Bao nhiêu lượt đổi, tỷ lệ thành công, nghẽn ở bước nào?
- Lỗi đang tập trung ở đâu: trạm, tủ, khoang, provider, lô pin?
- Cảnh báo nào đang mở, OPS đã phản hồi chưa?

Dữ liệu lấy từ output của simulator (`data/<profile>/`) và `data/notifier.sqlite`. Detector và evaluator chưa có, nên dashboard tính trực tiếp từ dữ liệu vận hành. Các trang Incident và Metric để chỗ sẵn, hiển thị trống cho tới khi có dữ liệu.

## 2. Nguyên tắc bắt buộc

1. **Read-only.** Dashboard chỉ đọc file và SQLite, không ghi gì. Ngoại lệ duy nhất là nút feedback hiện có của mock Teams.
2. **Màn hình vận hành không dùng ground truth.** OPS thật không biết ground truth, nên các tab vận hành không được đọc `ground_truth.csv`. Ground truth chỉ xuất hiện ở tab "Kiểm chứng (mock)", có nhãn rõ ràng. Viết test chặn điều này.
3. **Thời gian.** Lưu và tính toán ở UTC. Chỉ khi hiển thị mới đổi sang giờ địa phương của trạm, kèm offset (`GMT+8`), qua `ase/common/time.py`. "Hôm nay" là ngày địa phương của từng trạm.
4. **Không gọi `datetime.now()`.** "Bây giờ" của dashboard là tham số `as_of` (UTC). Mặc định `as_of` = `occurred_at` lớn nhất trong dữ liệu, vì mock data nằm trong quá khứ. Mọi chỉ số "realtime" (online, đang chạy, hôm nay) tính theo `as_of`.
5. **Không hardcode** quốc gia, timezone, currency, SLA, mã lỗi, team: đọc qua `ase/knowledge/loader.py` (`default_knowledge()`).
6. **Dữ liệu untrusted.** `error_message` có thể chứa prompt injection hoặc HTML (scenario `prompt_injection`). Mọi giá trị hiển thị phải escape (Jinja2 autoescape cho `.html.j2`, `textContent` phía JS, không dùng `innerHTML` với dữ liệu).

## 3. Kiến trúc

```
ase/dashboard/
  __init__.py
  data.py        # nạp data/<profile> (polars), cache theo mtime file
  metrics.py     # hàm thuần: DataFrame + as_of -> Pydantic model (dễ test)
  models.py      # Pydantic response models cho API
  api.py         # APIRouter /api/dashboard/... trả JSON
  pages.py       # APIRouter /dashboard/... trả HTML (Jinja2)
  templates/     # base.html.j2 + 1 template/tab
  static/        # dashboard.js, dashboard.css
```

- Gắn vào app hiện có: trong `create_app()` của `ase/notifier/mock_teams/app.py`, gọi `include_router` cho `api.router` và `pages.router`, rồi mount `static/`. Chạy bằng `make teams` như cũ. Thêm alias `make dashboard` gọi cùng lệnh.
- Thanh điều hướng chung: Dashboard và Mock Teams nằm trong cùng một app.
- Thư viện JS từ CDN, **version cố định**:
  - biểu đồ: ECharts `echarts@5.5.1` (cdn.jsdelivr.net);
  - bản đồ: Leaflet `leaflet@1.9.4` + tile OpenStreetMap.
- Không thêm dependency Python mới; dùng polars, FastAPI, Jinja2 đã có.
- **Cache dữ liệu:** `data.py` nạp Parquet một lần cho mỗi profile, giữ trong bộ nhớ, nạp lại khi mtime của `manifest.json` đổi. `eval` có khoảng 1,4 triệu dòng telemetry, nên phải dùng Parquet + polars lazy, không đọc JSONL. Mục tiêu: mỗi API dưới 1 giây sau khi đã cache.
- **Tham số chung của mọi API/trang:** `profile` (mặc định `dev`), `as_of` (UTC ISO, mặc định cuối dữ liệu), `country` (mặc định tất cả).

## 4. Bổ sung dữ liệu: toạ độ trạm (cần cho bản đồ)

Bảng `stations` chưa có toạ độ. Thêm 2 trường `lat`, `lon` vào model `Station` ([ase/common/models.py](ase/common/models.py)). Đây là thay đổi chỉ **thêm field**, đúng quy tắc MOCK_PLAN §3.9.

- `countries.yaml`: mỗi nước thêm danh sách thành phố kèm toạ độ tâm, ví dụ VN: Hà Nội, TP.HCM, Đà Nẵng; PH: Manila; ID: Jakarta (WIB), Makassar (WITA).
  - Trạm ID phải nằm ở thành phố có đúng timezone của trạm: `Asia/Jakarta` → Jakarta, `Asia/Makassar` → Makassar.
- `entities.py`: gán thành phố theo vòng, toạ độ = tâm + lệch ngẫu nhiên nhỏ.
  - Dùng **RNG substream riêng** `substream(seed, "geo", station_id)` để không làm đổi traffic, event, ground truth.
- **Kiểm chứng bắt buộc:** sinh lại `smoke` và `dev`. sha256 của `ase.swap.events.jsonl`, `ground_truth.csv`, `swap_sessions`, `payments`, `cabinet_telemetry` trong `manifest.json` phải **giữ nguyên** so với trước khi thêm toạ độ; chỉ `stations.parquet` thay đổi. Nếu khác → dừng lại, xem mục 9.

## 5. Các tab

Mỗi tab gồm 1 trang HTML và các API JSON tương ứng. Bộ lọc profile, quốc gia và thời điểm `as_of` nằm trên thanh trên cùng.

### 5.1 Tổng quan mạng lưới (`/dashboard`)
- **Hàng KPI**, chia theo quốc gia và tổng:
  - số trạm / tủ online;
  - pin sẵn sàng / tổng khoang;
  - lượt đổi hôm nay;
  - tỷ lệ thành công hôm nay;
  - thời gian đổi trung vị / p95;
  - doanh thu hôm nay, tách theo currency, không quy đổi;
  - số alert đang mở P1 / P2.
- **Bản đồ trạm** (Leaflet), màu theo trạng thái:
  - xanh = online, lỗi thấp;
  - vàng = tỷ lệ lỗi 1 giờ gần nhất vượt ngưỡng;
  - đỏ = có tủ mất heartbeat, hoặc có alert P1.
  - Bấm vào trạm để mở chi tiết trạm.
- **Biểu đồ lượt đổi theo giờ địa phương** 24 giờ gần nhất, tách OK / lỗi.
- **Top 5 điểm nóng:** trạm, tủ, khoang có nhiều phiên lỗi nhất trong 24 giờ.

### 5.2 Trạm & Tủ (`/dashboard/stations`, `/dashboard/stations/{station_id}`)
- **Bảng trạm:** quốc gia, timezone, số tủ, tủ online, pin sẵn sàng, lượt đổi và tỷ lệ lỗi hôm nay, heartbeat cuối (giờ địa phương).
- **Chi tiết trạm:** mỗi tủ là một lưới 12 khoang. Mỗi ô khoang hiện:
  - `door_state`, `battery_detected`, SOC, nhiệt độ;
  - tô màu: trống / sẵn sàng / đang sạc / lỗi / quá nhiệt.
  - Bên dưới: dòng thời gian heartbeat 24 giờ của từng tủ, khoảng mất heartbeat tô đỏ.

### 5.3 Giao dịch (`/dashboard/sessions`, `/dashboard/sessions/{session_id}`)
- **Phễu 7 bước:** số phiên đi tới từng bước và tỷ lệ rơi giữa các bước, trong khoảng thời gian chọn.
- **Ma trận bước × mã lỗi:** heatmap số event FAILED/TIMEOUT. Phiên có `error_code=None` hiện là "không có mã".
- **Latency p50/p95 theo bước**, kèm đường SLA lấy từ `state_machine.yaml`.
- **Phiên đang treo:** phiên chưa đóng mà đã quá SLA của bước hiện tại tại thời điểm `as_of`, tính theo cùng quy tắc tracker sẽ dùng: từ event *cuối* của bước trước.
- **Bảng phiên gần nhất**, có lọc theo trạng thái, trạm, bước.
- **Chi tiết phiên:** timeline các event (giờ địa phương, latency, status, mã lỗi), payment, telemetry khoang liên quan. `error_message` hiện dạng text đã escape, có nhãn "dữ liệu chưa kiểm chứng".

### 5.4 Thanh toán (`/dashboard/payments`)
- Theo provider và quốc gia: số giao dịch, tỷ lệ SUCCESS / FAILED / TIMEOUT / DECLINED / CANCELLED / VOIDED, số đang PENDING.
- Biểu đồ timeout theo thời gian của từng provider (ví dụ thấy rõ đợt `PH_WALLET_1` ngừng).
- **Đối soát (giao dịch rủi ro tiền):**
  - `captured=true` mà phiên FAILED/TIMEOUT hoặc không có event bước 6/7;
  - amount lệch bảng giá: so với `PricingSpec.standard_swap_amount` theo gói của user.
  - Mỗi dòng có link sang chi tiết phiên.
- Doanh thu theo ngày địa phương, tách theo currency.

### 5.5 Pin (`/dashboard/batteries`)
- Phân bố SOH theo `lot_id` (box plot); lô có SOH trung bình thấp được đánh dấu.
- SOC lúc xuất pin, ngưỡng thấp lấy từ catalog hoặc thresholds trong knowledge.
- Danh sách pin SOH thấp đã được xuất cho khách.
- Nhiệt độ cao nhất theo tủ; điểm vượt ngưỡng cảnh báo mức 1 / mức 2 (thresholds trong knowledge).

### 5.6 Cảnh báo (`/dashboard/alerts`)
- Từ `notifier.sqlite`:
  - bảng outbox: incident, revision, kênh, chế độ dry_run/live, trạng thái, số lần thử, lỗi cuối;
  - card mock Teams theo channel;
  - feedback mới nhất của OPS.
- Thống kê nhanh: số alert theo team / category / severity; tỷ lệ feedback "Sai phân loại".
- Link sang card trên mock Teams và sang chi tiết phiên đầu tiên của incident.

### 5.7 Để chỗ sẵn
- **Incident** (`/dashboard/incidents`): khi detector ghi incident ra file hoặc SQLite thì hiển thị. Hiện tại trang ghi "Chưa có dữ liệu detector". Không tự tạo incident trong dashboard.
- **Metric SF1–SF9** (`/dashboard/metrics`): đọc báo cáo của evaluator khi có. Hiện tại trang trống, có ghi chú.
- **Kiểm chứng (mock)** (`/dashboard/ground-truth`): tab duy nhất được đọc `ground_truth.csv`. Hiện số phiên lỗi theo category × scenario × quốc gia, và dòng thời gian các scenario instance. Có nhãn "chỉ có trong mock, OPS thật không thấy".

## 6. Định nghĩa chỉ số (ghi vào docstring `metrics.py`)

| Chỉ số | Cách tính tại thời điểm `as_of` |
|---|---|
| Tủ online | Có heartbeat trong 2 phút trước `as_of`. Ngưỡng 2 phút đặt trong config, không hardcode |
| Trạm online | Ít nhất 1 tủ online |
| Pin sẵn sàng | Khoang có lần đọc telemetry gần nhất `battery_detected=true` và SOC ≥ 90%, hoặc chưa có lần đọc nào (giả định đầy pin ban đầu). Ghi rõ đây là **xấp xỉ**, vì simulator chưa mô phỏng tồn kho pin theo khoang |
| Lượt đổi hôm nay | Số phiên có `started_at` trong ngày địa phương của trạm chứa `as_of` |
| Tỷ lệ thành công | Phiên `final_status=COMPLETED` / phiên đã kết thúc |
| Thời gian đổi | `ended_at - started_at` của phiên COMPLETED |
| Doanh thu | Tổng `amount` của payment `captured=true`, theo currency |
| Phiên đang treo | Như mục 5.3; SLA đọc từ `state_machine.yaml` |

## 7. Test (`tests/unit/test_dashboard.py`)
- **`metrics.py`** trên DataFrame nhỏ tự dựng:
  - tủ online / offline quanh ngưỡng 2 phút;
  - "hôm nay" đúng theo ngày địa phương: trạm `Asia/Manila` và `Asia/Ho_Chi_Minh` cùng một `as_of` UTC có thể khác ngày;
  - phễu 7 bước;
  - phiên treo tính từ event cuối của bước trước: phiên PENDING → OK ở bước 5 không bị tính treo sai;
  - đối soát bắt được `captured=true` mà không có bước 6/7;
  - doanh thu tách theo currency.
- **API** qua FastAPI `TestClient` trên dữ liệu `smoke` sinh vào `tmp_path`: mọi endpoint trả 200 và JSON đúng model; `as_of` lệch ra ngoài dữ liệu → 422 hoặc kẹp về biên, có test.
- **Không dùng ground truth:** dùng monkeypatch làm mọi lần đọc `ground_truth.csv` báo lỗi, rồi gọi toàn bộ API của các tab vận hành → tất cả vẫn 200.
- **Escape:** phiên của scenario `prompt_injection` hiện ở trang chi tiết phiên mà không có thẻ `<`, `</untrusted_data>` thô trong HTML.
- **Toạ độ:** test so sha256 các file event/ground truth trước và sau khi thêm `lat`/`lon` (mục 4).
- `make check` xanh.

## 8. Kiểm chứng end-to-end
1. `make check` xanh.
2. `make sim PROFILE=dev`, rồi `make dashboard`, mở `http://localhost:8000/dashboard`.
3. Tổng quan: hiện đủ 5 trạm `dev` trên bản đồ (VN 2, PH 1, ID 2 ở Jakarta + Makassar); KPI có số; giờ hiển thị kèm GMT+7 / GMT+8.
4. Kéo `as_of` tới ngày có scenario `deploy_503`: ma trận lỗi có cột `SESS_CREATE_5XX` nổi bật ở cả 3 nước. Tới đợt `ph_wallet_outage`: tab Thanh toán thấy timeout dồn ở `PH_WALLET_1`.
5. Chi tiết trạm `VN-ST-001`: tủ `CAB-03` khoang 07 hiện lỗi cửa, heartbeat chập chờn trong đợt `slot_door_jam`.
6. Chạy `make notify-demo` → tab Cảnh báo hiện 3 alert và trạng thái outbox.
7. Thử với `profile=eval`: các trang mở dưới khoảng 1–2 giây sau lần nạp đầu.

## 9. Dừng lại hỏi chủ dự án khi
- Thêm toạ độ làm đổi sha256 của event / ground truth (mục 4), hoặc cần đổi / xoá field có sẵn.
- Muốn dashboard ghi dữ liệu, hoặc gọi LLM.
- Cần thêm dependency Python mới, hoặc chuyển sang công cụ khác (Grafana, Streamlit…).
- Cần mô phỏng tồn kho pin theo khoang trong simulator để chỉ số "pin sẵn sàng" chính xác hơn.
