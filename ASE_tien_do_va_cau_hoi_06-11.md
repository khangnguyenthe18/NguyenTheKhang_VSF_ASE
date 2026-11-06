# ASE — Tiến độ và câu hỏi cho mentor

AI Agent kiểm tra giao dịch lỗi đổi pin VinFast · Ngày 06/11/2026 

Đã chạy được trên dữ liệu mock: simulator, gửi cảnh báo (Mail + Teams) và dashboard vận hành. Phần phát hiện lỗi (detector) đã có kế hoạch chi tiết và làm tiếp từ 07/11.

## 1. Luồng hệ thống hiện tại

| Simulator → | Luồng event → | Detector (rule) → | LLM triage → | Thông báo → | OPS + Dashboard |
|---|---|---|---|---|---|
| mock 3 nước, có ground truth | JSONL, có trùng / trễ | realtime → incident | LLM gợi ý nguyên nhân | Mail + Teams | phản hồi, giám sát |

- Nguyên tắc: AI chỉ đọc và gợi ý, OPS quyết định; rule luôn gửi được cảnh báo kể cả khi LLM lỗi.

| Thành phần | Đã làm |
|---|---|
| Simulator (mock) | Hành trình 7 bước; 3 nước VN, PH, ID (múi giờ, tiền tệ, bảng giá thật); 12 kịch bản lỗi (deploy 503 cả 3 nước, ví PH ngừng, kẹt cửa khoang, đã trừ tiền không nhả pin, lô pin SOH thấp, quá nhiệt…); event trùng / sai thứ tự / đến trễ; profile smoke, dev, eval (tập test đã đóng băng), demo (40 trạm). Xong |
| Gửi cảnh báo | Mail theo mẫu [ASE] (giờ địa phương của trạm), card Teams theo kênh từng team; chống gửi trùng, retry; nút phản hồi OPS 2 bước (chống trình quét link). Đang dùng Mailpit + mock Teams; đổi sang SMTP công ty chỉ bằng cấu hình. Xong |
| Dashboard vận hành | Tổng quan (KPI theo nước, bản đồ Vietmap, bảng trạm, nhiệt độ tủ, pin sẵn sàng, cảnh báo), trạm và tủ, giao dịch (phễu 7 bước, phiên treo), thanh toán (đối soát), pin. Xong (demo) |
| Detector (rule) | Theo dõi phiên + timeout SLA (bắt lỗi im lặng), phân loại theo error catalog, gộp giao dịch thành incident; chạy được khi thiếu trường dữ liệu (field ablation T0–T3). Có kế hoạch, làm 07–10/11 |
| LLM triage | Gợi ý nguyên nhân / hướng xử lý theo incident; so sánh rule-only với rule + LLM. Sprint 1 |

## 2. Lịch gửi thông báo đã chốt

| Loại lỗi | Khi nào gửi | Gửi cho ai |
|---|---|---|
| P1 (an toàn / tiền / tài sản: đã trừ tiền không nhả pin, quá nhiệt…) | **Ngay** khi phát hiện (< 60 giây) | Đúng team xử lý (IT Ops, Field Ops, Payment Ops, Battery Ops…) |
| P2 / P3 | **Gom mỗi 15 phút**; mỗi team 1 tin/chu kỳ, chỉ gửi khi có lỗi | Đúng team xử lý |
| Lỗi do người dùng (sai QR, thẻ bị từ chối, khách huỷ…) | Gom mỗi 15 phút | CSKH |
| Báo cáo tổng hợp toàn bộ lỗi | **1 lần/ngày lúc 18:00** giờ VN | Quản lý vận hành + mọi team |

Gửi qua Mail + Teams. LLM gợi ý nguyên nhân theo incident (không gọi riêng cho từng lỗi người dùng) và viết 1 nhận xét cho báo cáo ngày; chu kỳ và giờ gửi chỉnh bằng cấu hình.

### Danh sách loại lỗi theo nhóm (error catalog v0, 50 mã)

Mức **P1** gửi ngay; P2/P3 gom mỗi 15 phút. Bước: 1 Mở app · 2 Chọn dịch vụ · 3 Quét QR · 4 Kiểm tra điều kiện · 5 Thanh toán · 6 Trả pin cũ · 7 Nhận pin mới.

**Thanh toán** (PAYMENT) → Payment Ops · 6 mã

| Mã lỗi | Lỗi | Bước | Mức |
|---|---|---|---|
| `PAY_CALLBACK_LOST` | Mất callback | 5 | **P1** |
| `PAY_DOUBLE_CHARGE` | Trừ tiền 2 lần | 5 | **P1** |
| `PAID_NOT_DELIVERED` | Trừ tiền nhưng không nhả pin | xuyên bước | **P1** |
| `DELIVERED_NOT_PAID` | Nhả pin nhưng chưa thu tiền | xuyên bước | **P1** |
| `PAY_GATEWAY_TIMEOUT` | Gateway timeout/lỗi | 5 | P2 |
| `PAY_CALLBACK_LATE` | Callback trễ | 5 | P2 |

**Thiết bị tủ / khoang** (HARDWARE) → Field Ops · 8 mã

| Mã lỗi | Lỗi | Bước | Mức |
|---|---|---|---|
| `SLOT_LOCK_FAIL` | Khoang không đóng/khoá | 6 | **P1** |
| `CABINET_OVERHEAT` | Tủ/khoang quá nhiệt | xuyên bước | **P1** |
| `STATION_OFFLINE` | Trạm offline/bảo trì | 2 | P2 |
| `CABINET_NO_ACK` | Tủ không phản hồi lệnh | 3 | P2 |
| `SLOT_DOOR_JAM` | Khoang không mở | 6 | P2 |
| `SLOT_SENSOR_FAIL` | Pin đặt vào không nhận | 6 | P2 |
| `CABINET_DISCONNECT` | Tủ mất kết nối giữa phiên | 6 | P2 |
| `SLOT_OUT_DOOR_JAM` | Khoang pin mới không mở | 7 | P2 |

**Chất lượng pin** (BATTERY_QUALITY) → Battery Ops · 5 mã

| Mã lỗi | Lỗi | Bước | Mức |
|---|---|---|---|
| `BAT_BMS_FAULT` | Pin mới lỗi BMS | 7 | **P1** |
| `BAT_RETURN_FAULT` | Pin cũ lỗi BMS/quá nhiệt | 6 | P2 (**P1** nếu > 55°C) |
| `BAT_LOW_SOC` | Pin xuất SOC thấp | 7 | P2 |
| `BAT_LOW_SOH` | Pin xuất SOH thấp | 7 | P2 |
| `BAT_PAIRING_FAIL` | Pin không ghép được với xe | 7 | P2 |

**Hệ thống** (SYSTEM) → IT Ops · 13 mã

| Mã lỗi | Lỗi | Bước | Mức |
|---|---|---|---|
| `PAY_WRONG_AMOUNT` | Tính sai tiền | 5 | **P1** |
| `ASSET_NOT_ASSIGNED` | Pin mới chưa gán cho khách | 7 | **P1** |
| `ASSET_MISMATCH` | Pin lệch sổ tài sản | xuyên bước | **P1** |
| `AUTH_FAILED` | Auth service lỗi | 1 | P2 |
| `APP_CRASH` | App crash/treo | 1–2 | P2 |
| `STATION_LIST_FAIL` | Không tải được danh sách trạm | 2 | P2 |
| `STATION_DATA_STALE` | App hiển thị sai số pin/trạng thái | 2 | P2 |
| `SESS_CREATE_5XX` | Session service lỗi | 3 | P2 |
| `SESS_DUPLICATE` | Phiên cũ chưa đóng | 3 | P2 |
| `ELIG_SERVICE_ERROR` | Eligibility service lỗi | 4 | P2 |
| `ELIG_FALSE_REJECT` | Đủ điều kiện nhưng bị từ chối | 4 | P2 |
| `SESSION_DANGLING` | Phiên không đóng, giữ khoang | xuyên bước | P2 |
| `SWAP_SLOW` | Thành công nhưng quá chậm | xuyên bước | P3 |

**Nghiệp vụ** (BUSINESS) → Business Ops · 9 mã

| Mã lỗi | Lỗi | Bước | Mức |
|---|---|---|---|
| `ELIG_BATTERY_NOT_OWNED` | Pin không thuộc hợp đồng | 4 | **P1** |
| `RETURN_FOREIGN_BATTERY` | Pin trả vào là pin lạ | 6 | **P1** |
| `FRAUD_PATTERN` | Hành vi đổi pin bất thường | xuyên bước | **P1** |
| `NO_CHARGED_BATTERY` | Hết pin đầy | 2–4 | P2 |
| `ACCOUNT_LOCKED` | Tài khoản bị khoá | 1 | P3 |
| `CABINET_BUSY` | Tủ đang phục vụ phiên khác | 3 | P3 |
| `ELIG_PACKAGE_EXPIRED` | Gói hết hạn | 4 | P3 |
| `ELIG_DEBT` | Nợ cước | 4 | P3 |
| `ELIG_QUOTA_EXCEEDED` | Vượt hạn mức | 4 | P3 |

**Người dùng** (USER) → CSKH · 8 mã

| Mã lỗi | Lỗi | Bước | Mức |
|---|---|---|---|
| `USR_NOT_COLLECTED` | Khách không lấy pin | 7 | P2 |
| `TOKEN_EXPIRED` | Hết phiên đăng nhập | 1 | P3 |
| `QR_INVALID` | QR sai/hết hạn | 3 | P3 |
| `QR_WRONG_STATION` | QR không khớp vị trí | 3 | P3 |
| `PAY_DECLINED` | Thẻ bị từ chối/không đủ số dư | 5 | P3 |
| `PAY_USER_CANCEL` | Khách huỷ | 5 | P3 |
| `USR_RETURN_TIMEOUT` | Khách không đặt pin kịp | 6 | P3 |
| `USR_RETRY_STORM` | User retry liên tục | xuyên bước | P3 |

**Chưa xác định** (UNKNOWN) → IT Ops (cần người xem) · 1 mã

| Mã lỗi | Lỗi | Bước | Mức |
|---|---|---|---|
| `STEP_TIMEOUT` | Kẹt bước quá SLA — rule/LLM xác định nhóm thật | xuyên bước | P2 |

## 3. Câu hỏi cần mentor trả lời

**Câu 1 — luồng kiến trúc BSS cần xác nhận:**

| Pin/Tủ BSS (CAN/RS485) → | Kết nối Edge (MQTT / WebSockets qua AWS IoT Core) → | Xử lý luồng trung tâm (Apache Kafka) → | Microservices quản lý (App VinFast / VGreen, Thanh toán, ERP/MES) |
|---|---|---|---|
| tầng thiết bị | tầng kết nối | tầng xử lý luồng | tầng nghiệp vụ |

| # | Câu hỏi | Vì sao cần |
|---|---|---|
| 1 | Luồng kiến trúc ở khung trên có đúng với hệ thống thật của VinFast không? | Kiểm chứng giả định thiết kế; chưa có nguồn công khai xác nhận. |
| 2 | Lịch gửi ở mục 2 đã đúng chưa (P1 gửi ngay; P2/P3 gom 15 phút theo team; lỗi người dùng gửi CSKH; báo cáo ngày 18:00)? Danh sách team và người nhận thật là ai? | Chốt trước khi làm phần gửi theo lịch. |
| 3 | Chỉ tiêu "cảnh báo < 60 giây" chỉ áp cho P1; P2/P3 tới tay team trong ≤ 15 phút. Có chấp nhận được? | Do gom lỗi 15 phút, chỉ tiêu SF5 cũ (< 60 giây cho mọi lỗi) cần điều chỉnh. |

