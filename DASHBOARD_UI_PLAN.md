# DASHBOARD_UI_PLAN.md — Đổi giao diện dashboard theo kiểu console IoT (widget grid)

> Plan thực thi. Đọc DASHBOARD_PLAN.md trước; plan này **chỉ đổi giao diện**, giữ nguyên API, số liệu và các nguyên tắc ở DASHBOARD_PLAN §2: read-only, không dùng ground truth ở tab vận hành, escape dữ liệu, `as_of` thay cho giờ thật.
> **Ưu tiên (chủ dự án đổi 06/11): làm NGAY, trước detector**, để demo luồng cho mentor rồi mới hỏi về DETECTOR_PLAN.md. Hạn mục tiêu: **07/11**. Giới hạn phạm vi: chỉ trang Tổng quan + bảng Cảnh báo + chuyển các tab còn lại sang thẻ trắng; bỏ qua mục "phát lại". Lý do: còn phải làm detector cho Gate 1 (11/11).
> Mẫu tham khảo: ảnh chụp dashboard ThingsBoard do chủ dự án gửi (06/11). Chỉ học **bố cục và phong cách**. **Không** dùng logo, tên hay asset của ThingsBoard.

## 1. Phong cách từ ảnh mẫu
- **Nền và widget:**
  - nền xám rất nhạt (`#eef0f3`), widget là **thẻ trắng**, bo góc 8px, đổ bóng nhẹ, khoảng cách 8–12px;
  - bố cục lưới nhiều cột, chiều cao các thẻ đều nhau theo hàng.
- **Đầu mỗi widget:**
  - tiêu đề đậm cỡ lớn (khoảng 22px);
  - dòng phụ nhỏ cho biết khoảng thời gian, kiểu "Realtime - last 1 hour". Ở đây dùng *"Tại as_of 22:55 01/11 (GMT+7) – 1 giờ trước"*;
  - góc phải có các icon: tìm kiếm, chọn cột, phóng to toàn màn hình.
- **Bản đồ:** nền sáng, marker có **nhãn dạng viên thuốc** (pill) ghi tên thiết bị; màu marker và viền nhãn theo trạng thái; nút zoom và nút chọn lớp bản đồ ở góc trái.
- **Bảng thực thể:**
  - cột có mũi tên sắp xếp;
  - chấm tròn màu + chữ cho trạng thái;
  - số căn trái, gọn;
  - chân bảng có phân trang: "Items per page", "1 – 3 of 3", nút trang đầu / trước / sau / cuối.
- **Biểu đồ theo thời gian:**
  - nhiều series, mỗi series một màu;
  - **đường ngưỡng ngang** (đỏ = mức nguy hiểm, vàng = mức cảnh báo), có chấm tròn và nhãn giá trị ở mép phải;
  - chú thích dưới biểu đồ dạng bảng: tên series có chấm màu, cột "Avg" căn phải.
- **Biểu đồ vùng (area):** các series chồng màu trong suốt, trục 0–100%.
- **Bảng cảnh báo (Alarms):**
  - cột: Start time (giảm dần), Originator, Type, Severity, Status;
  - severity tô màu chữ: Critical = đỏ, Major = cam, Minor = vàng;
  - Status 2 dòng, ví dụ "Active / Unacknowledged";
  - cuối mỗi dòng: nút "…" xem chi tiết và nút ✓ xác nhận.

## 2. Ánh xạ sang BSS

### 2.1 Trang Tổng quan (`/dashboard`): bố cục giống ảnh mẫu
Hàng KPI nhỏ hiện có giữ lại ở trên cùng, thu gọn thành 1 dải thẻ. Bên dưới là lưới 2 hàng:

| Hàng | Trái (khoảng 2/3 rộng) | Phải (khoảng 1/3 rộng) |
|---|---|---|
| 1 | **Bản đồ trạm:** marker có nhãn `VN-ST-001`…, màu xanh lá = online, xanh dương = có tủ đang có lỗi, cam = tỷ lệ lỗi vượt ngưỡng, đỏ = mất heartbeat hoặc có alert P1. Bấm marker mở popup tóm tắt + link chi tiết trạm | **Bảng Trạm:** Tên, Trạng thái (chấm + "Online" / "Offline"), Pin sẵn sàng %, Nhiệt độ max °C, Tỷ lệ lỗi hôm nay %. Sắp xếp, tìm kiếm, phân trang |
| 2 | Hai widget cạnh nhau: (a) **Nhiệt độ tủ, 1 giờ trước `as_of`**: mỗi tủ của trạm đang chọn một series, đường ngưỡng vàng = `temp_warn_1_c` (55 °C), đỏ = `temp_warn_2_c` (60 °C), đọc từ knowledge; chú thích có Avg. (b) **Pin sẵn sàng theo thời gian, 6 giờ trước**: area chart % khoang sẵn sàng theo trạm | **Cảnh báo (Alarms)** (mục 2.3) |

Bộ chọn trạm cho widget nhiệt độ: mặc định trạm có nhiệt độ cao nhất. Đổi trạm thì chỉ widget đó gọi lại API.

### 2.2 Các tab khác
Giữ nội dung hiện có, chỉ chuyển sang cùng kiểu thẻ trắng và cùng kiểu header widget.
- **Trạm & Tủ:**
  - lưới 12 khoang dùng màu viên thuốc như marker;
  - thêm widget nhiệt độ tủ (cùng component với Tổng quan) cho trạm đang xem.
- **Giao dịch:** phễu, heatmap và latency mỗi cái một thẻ; bảng phiên dùng component bảng chung, có phân trang.
- **Thanh toán / Pin:** biểu đồ theo thời gian có đường ngưỡng, ví dụ SOC thấp = `low_soc_pct`, SOH thấp = `low_soh_pct`.

### 2.3 Bảng Cảnh báo
- **Nguồn dữ liệu** (chưa có detector nên phải chạy được khi thiếu nguồn 1):
  - `incidents.jsonl` của detector **nếu có** (DETECTOR_PLAN §5); không có thì bỏ qua, không báo lỗi;
  - trạng thái gửi và feedback trong `notifier.sqlite`;
  - **cảnh báo thiết bị** tính tại `as_of`: tủ mất heartbeat, nhiệt > 55 °C hoặc > 60 °C. Loại này ghi Originator là mã tủ.
- **Ánh xạ cột:**
  - Originator = trạm / tủ;
  - Type = mô tả mã lỗi trong catalog (tiếng Việt);
  - Severity: P1 → Critical (đỏ), P2 → Major (cam), P3 → Minor (vàng).
- **Status** lấy từ feedback mới nhất:
  - chưa có feedback → "Active / Chưa xác nhận";
  - `confirm` hoặc `wrong_category` → "Active / Đã xác nhận";
  - `resolved` → "Đã xử lý".
- **Nút:**
  - "…" mở chi tiết incident hoặc phiên đầu tiên;
  - ✓ dẫn tới trang xác nhận feedback hiện có, dạng GET → form POST (giữ cơ chế chống trình quét link). Dashboard **không** tự ghi gì.

## 3. Kỹ thuật
- **Component dùng chung** trong `ase/dashboard/static/dashboard.js` và macro Jinja2 `templates/widgets.html.j2`:
  - `widgetCard(title, subtitle, actions)`;
  - `entityTable(columns, rows, {sortable, searchable, pageSize})`: sắp xếp, tìm kiếm, phân trang phía client, dữ liệu render bằng `textContent`;
  - `timeSeriesChart(series, thresholds)`: ECharts `markLine` cho đường ngưỡng, chú thích Avg tự tính;
  - `areaChart(series)`;
  - `stationMap(points)`: Leaflet, marker `divIcon` có nhãn pill.
- **Thư viện:** giữ ECharts `5.5.1` và Leaflet `1.9.4` (CDN, ghim version). Không thêm framework JS. Icon dùng SVG inline tự vẽ, không lấy icon set của ThingsBoard.
- **Phóng to toàn màn hình:** Fullscreen API trên thẻ widget, biểu đồ gọi `resize()` khi vào hoặc ra.
- **API mới** (chỉ đọc, cùng tham số `profile`, `as_of`, `country`):
  - `GET /api/dashboard/timeseries/cabinet-temperature?station_id=&hours=1`: mỗi tủ một series, đã lấy mẫu xuống tối đa khoảng 120 điểm;
  - `GET /api/dashboard/timeseries/ready-batteries?hours=6&step_minutes=15`: tính lại bằng `compute_ready_batteries_by_cabinet` tại từng mốc;
  - `GET /api/dashboard/alarms`: hợp nhất 3 nguồn ở mục 2.3, sắp xếp theo Start time giảm dần.
- **Phát lại (tuỳ chọn, nếu còn thời gian):** nút ▶ trên thanh trên cùng, tăng `as_of` theo bước (ví dụ 1 phút mô phỏng mỗi 2 giây) và tải lại các widget, để demo cảm giác realtime. Không gọi giờ thật.
- **Responsive:** dưới 1200px lưới về 1 cột, bản đồ cao tối thiểu 320px; không có thanh cuộn ngang.

## 4. Test (`tests/unit/test_dashboard.py`, bổ sung)
- Các API mới trả 200 trên `smoke`, đúng model Pydantic. Series nhiệt độ có ngưỡng 55 và 60 lấy từ knowledge, không hardcode.
- Alarms:
  - severity ánh xạ đúng P1 / P2 / P3;
  - Status đổi theo feedback;
  - cảnh báo thiết bị xuất hiện khi tủ mất heartbeat tại `as_of` (dữ liệu tự dựng).
- Ground truth vẫn không được đọc ở mọi API mới (dùng lại test monkeypatch hiện có).
- HTML các trang: có các container widget mong đợi; `dashboard.js` không dùng `innerHTML` với dữ liệu (test grep hiện có, nếu chưa có thì thêm).
- `make check` xanh.

## 5. Kiểm chứng
1. `make sim PROFILE=dev`, rồi `make dashboard`, mở `http://localhost:8000/dashboard`.
2. So với ảnh mẫu:
   - hàng 1: bản đồ + bảng trạm;
   - hàng 2: biểu đồ nhiệt có 2 đường ngưỡng + area chart pin sẵn sàng + bảng cảnh báo;
   - các thẻ trắng trên nền xám, tiêu đề và dòng phụ thời gian, icon ở góc.
3. Bấm phóng to từng widget; sắp xếp và tìm kiếm trong bảng trạm; chuyển trang trong bảng cảnh báo.
4. Chạy `make notify-demo` rồi bấm ✓ trên một cảnh báo → xác nhận → Status đổi thành "Đã xác nhận".
5. Thu hẹp cửa sổ dưới 1200px → lưới về 1 cột, không cuộn ngang.

## 6. Kịch bản demo cho mentor (chưa có detector)

### 6.1 Lệnh chuẩn bị
Thêm target `make demo` vào Makefile:
1. `make sim PROFILE=dev`;
2. `make mail-up`;
3. xoá outbox và card cũ: chỉ `data/notifier.sqlite` và `data/outbox_preview/`, có in thông báo;
4. chạy `make notify-demo`.

Server (`make dashboard`) chạy ở terminal riêng. In ra các URL cần mở.

### 6.2 Trình tự demo (khoảng 10 phút)

| # | Màn hình | Nói gì |
|---|---|---|
| 1 | Dashboard Tổng quan | Mạng lưới 5 trạm ở 3 nước; KPI theo nước; bản đồ; giờ hiển thị theo giờ địa phương của trạm |
| 2 | Kéo `as_of` tới đợt `deploy_503` | Lỗi `SESS_CREATE_5XX` dồn ở cả 3 nước cùng lúc |
| 3 | Chi tiết trạm `VN-ST-001` | Khoang 07 tủ CAB-03 kẹt cửa, heartbeat chập chờn, biểu đồ nhiệt có đường ngưỡng |
| 4 | Tab Thanh toán | Đối soát: phiên đã trừ tiền nhưng không nhả pin |
| 5 | Mailpit (`localhost:8025`) | Mail cảnh báo theo mẫu, giờ địa phương (mail PH ghi GMT+8) |
| 6 | Mock Teams (`localhost:8000`) | Card theo channel, P1 nổi bật |
| 7 | Bấm "Xác nhận đúng" trong mail | Trang xác nhận → Đã ghi nhận; bảng Cảnh báo đổi Status |
| 8 | Nói rõ phần chưa làm | Incident hiện đang là **mẫu**; detector (đang làm) sẽ sinh incident thật từ luồng event. Sau đó xin ý kiến mentor về DETECTOR_PLAN.md và về dữ liệu masked |

### 6.3 Ghi chú cho buổi demo
Giao diện phải ghi rõ đâu là dữ liệu mẫu: incident từ `notify-demo` có nhãn "mẫu". Không để mentor hiểu nhầm là detector đã chạy.

## 7. Dừng lại hỏi chủ dự án khi
- Muốn dùng ThingsBoard thật (Docker) thay vì tự làm giao diện.
- Cần thêm dependency (Python hoặc JS framework), hoặc đổi số liệu / API hiện có thay vì chỉ thêm mới.
