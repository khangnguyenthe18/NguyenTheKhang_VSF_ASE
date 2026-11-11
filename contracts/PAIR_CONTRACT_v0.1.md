# Pair contract v0.2 — Nguyễn Thế Khang (ASE) × Võ Doanh Nhân

> **v0.2** (trả lời 7 điểm của Nhân trước I1): mã điểm đổi pin trong dữ liệu chung đã che; bộ dữ liệu chung là profile `pair` (thêm 2 kịch bản sụt traffic theo tỉnh, request lặp lại) + đáp án mức lần lặp `scenario_instances.csv`; `baseline` theo tỉnh × giờ hoặc trạm × ngày (thêm trường tuỳ chọn `scope.province`); lead time đo theo lần quét. Schema vẫn `0.1` (chỉ thêm trường tuỳ chọn); tên file giữ nguyên để không vỡ đường dẫn.

Bản hợp nhất duy nhất, thay hai bản v0 riêng của mỗi người. Mọi thay đổi sửa trực tiếp file này
(trong repo chung) và cả hai cùng xác nhận ở mục 7.

## 1. Hai bài toán giao nhau ở đâu, giá trị cộng hưởng là gì

| | Khang (ASE) | Nhân |
|---|---|---|
| Tầng | **KPI và quyết định**: success factor của luồng đổi pin, health score dịch vụ critical, open point + đề xuất, cảnh báo/ticket cho OPS | **Tín hiệu từ log**: hành vi bất thường LLM phát hiện trên log stream |
| Dữ liệu | Danh sách giao dịch đổi pin (1 dòng/giao dịch) | Log hệ thống / thiết bị / người dùng |
| Đầu ra | Mail + Teams + ticket (kênh push duy nhất), báo cáo tuần/tháng | Log stream + thống kê (pull) |

Giao nhau: cùng hệ thống BSS VN, cùng khoá tương quan (mã trạm, mã tủ, connector, mã giao dịch
eMSP, thời gian). Giá trị cộng hưởng:
1. Tín hiệu log của Nhân giải thích và báo sớm sự cố mà KPI của Khang mới thấy hậu quả.
2. Mức nền (baseline) của Khang làm ngưỡng cho cảnh báo "sụt giảm theo khung giờ" của Nhân.
3. Một kênh thông báo, không cảnh báo trùng.

## 2. Ai làm phần nào, ranh giới ở đâu

| Hạng mục | Khang | Nhân |
|---|---|---|
| Mock Danh sách giao dịch + ground truth (VN) | Chịu trách nhiệm | Dùng chung seed |
| Mock log có cài sẵn VinSOC scan, request lặp lại, sụt traffic theo khung giờ, deploy làm latency tăng dần | — | Chịu trách nhiệm |
| Detector realtime trên log (EWMA, latency, kịch bản deploy lỗi) | **Nhường cho Nhân** (bản detector event 7 bước của Khang đã đóng băng) | Chịu trách nhiệm |
| KPI success factor, health score, open point, báo cáo tuần/tháng | Chịu trách nhiệm | — |
| Gộp sự cố từ giao dịch lỗi, cảnh báo Mail/Teams, ticket ManageEngine | Chịu trách nhiệm | Gửi qua Khang bằng `notify=true` |
| Log stream view + dashboard thống kê | — | Chịu trách nhiệm |
| Schema chung (mục 3), contract test | Cùng sở hữu | Cùng sở hữu |

Mỗi bên chạy được độc lập khi bên kia ngừng.

## 3. Trao đổi gì, định dạng nào

**Một schema chung** `ase.anomaly.event` v0.1 cho cả hai chiều:
[`contracts/schemas/ase.anomaly.event.v0.1.json`](schemas/ase.anomaly.event.v0.1.json),
mẫu ở [`contracts/fixtures/`](fixtures/), contract test `tests/contract/test_anomaly_event_contract.py`. Định dạng danh sách giao dịch dùng chung cho I1: [`DATA_FORMAT_v0.1.md`](DATA_FORMAT_v0.1.md).

| `kind` | Chiều | Nội dung | `event_id` |
|---|---|---|---|
| `transaction_incident` | Khang → Nhân | Sự cố gộp từ giao dịch lỗi: phạm vi, mã lỗi chuẩn, mức High/Low, mã giao dịch liên quan | `INC-*` |
| `baseline` | Khang → Nhân | Mức nền 7 ngày theo **tỉnh × giờ** (`scope.province`) hoặc **trạm × ngày**: số giao dịch, tỷ lệ lỗi, health score. Trạm × giờ quá thưa ở mật độ thật (~0,5 GD/giờ/trạm, ~60% giờ cao điểm không có GD) | `BASE-*` |
| `behavior` | Nhân → Khang | Hành vi bất thường trên log: metric quan sát / nền / z-score, phạm vi, mức tin cậy | `BHV-*` |

- **Truyền tải:** file JSONL `data/<profile>/ase.anomaly.event.v0.1.jsonl` (mỗi dòng một sự kiện).
  **Không dùng Kafka** cho tới khi cả hai cùng cần (xem lại sau CP2).
- Thời gian ISO-8601 UTC. Mã lỗi là mã của bảng mã lỗi chuẩn, giữ nguyên văn.
- Thêm field = không phá vỡ (consumer bỏ qua field lạ). Đổi/xoá field = tăng major, cả hai đồng ý.
- Nhân đặt `notify=true` khi cần báo người; Khang gửi trong lần quét 30 phút kế tiếp (không gửi riêng).

## 4. Phiên bản dữ liệu chung

| Mục | Chốt |
|---|---|
| Phạm vi | **VN** (theo góp ý Gate 0) |
| Profile | **`pair`** = `eval` (30 ngày từ 05/10/2026, 20 nền + 10 test, 650 điểm lấy mẫu theo tỉnh, ~1.450 giao dịch/ngày theo mật độ thật ≈ 2,9 triệu GD/tháng toàn VN, seed 2042 / test 9142) **+ 2 kịch bản có dấu hiệu trên log**: `traffic_drop` (giao dịch một tỉnh đông giảm ~60% trong 3 giờ, 6 lần) và `repeated_request` (một khách tạo 5–9 giao dịch lặp tại một tủ, D6031, 6 lần). `eval` giữ nguyên để báo cáo số đo của ASE |
| Che mã | Mã trạm giữ tỉnh, 5 số giả (không trùng điểm thật); toạ độ làm tròn ~5 km. Chỉ áp cho dữ liệu dùng chung; agent không che mã khi chạy với dữ liệu thật |
| File gửi Nhân (I1) | `swap_transactions.parquet` (+ `.jsonl`), `txn_ground_truth.csv` (đáp án từng giao dịch), **`scenario_instances.csv`** (đáp án từng lần lặp: kịch bản, phạm vi, trạm/tủ/tỉnh, bắt đầu/kết thúc UTC, số GD ảnh hưởng), `error_catalog_v1.yaml` (bảng mã lỗi chuẩn, giữ nguyên văn). Sinh lại: `make sim PROFILE=pair` |
| Lý do không dùng 60 ngày × 8.000 phiên | Chất lượng đánh giá phụ thuộc số case lỗi, không phải tổng số; 30 ngày × 650 điểm đủ nhiều lần lặp mỗi kịch bản trong tập test và sinh + quét + báo cáo chạy khoảng 1 phút trên laptop. Cần quy mô toàn quốc thì dùng profile `scale` (toàn bộ điểm, 1 ngày ≈ 96 nghìn GD) |
| Log của Nhân | Sinh theo cùng seed và cùng mã trạm/tủ/giao dịch của `swap_transactions.parquet`; khối lượng log do Nhân chọn |

## 5. Mốc tích hợp

| Điểm | Ngày | Việc | Xong khi |
|---|---|---|---|
| I1 Dữ liệu nguồn | 18/11 (CP1) | Log của Nhân sinh trên cùng trạm/tủ/giao dịch, cùng seed | Một kịch bản có cả giao dịch lỗi và dấu hiệu log khớp mã + thời gian |
| I2 Trao đổi sự kiện | 22/11 | Khang ghi `transaction_incident` + `baseline`; Nhân đọc | Contract test xanh ở cả hai repo |
| I3 Bằng chứng | 25/11 (CP2) | Khang đọc `behavior`, đưa `BHV-*` vào sự cố, open point; Nhân dùng `baseline` làm ngưỡng | Thông báo của Khang có `BHV-*` thật |
| I4 Cảnh báo | 02/12 | `notify=true` của Nhân đi qua kênh của Khang | Cảnh báo trùng = 0 |
| I5 Đánh giá | 09/12 (CP3) | Đo cộng hưởng (mục 6) | Có bảng A/B/C |

## 6. Đo cộng hưởng (cùng seed): (A) Khang một mình · (B) Nhân một mình · (C) kết hợp

| Metric | Cách đo | Ngưỡng (C so với A hoặc B) |
|---|---|---|
| Lead time | Lần quét 30 phút đầu tiên OPS được báo cho các kịch bản có dấu hiệu log (deploy làm latency tăng dần → giao dịch lỗi). Nhịp 30 phút giữ theo yêu cầu mentor (chống spam), nên đo theo số lần quét | C sớm hơn A **≥ 1 lần quét** trên ≥ 80% các kịch bản đó |
| Bằng chứng nguyên nhân | % sự cố/open point có `BHV-*` hợp lệ và khớp kịch bản | C ≥ 50%; A = 0% theo định nghĩa |
| Độ chính xác cảnh báo "sụt giảm theo khung giờ" của Nhân | Precision khi dùng `baseline` của Khang so với ngưỡng cố định | C cao hơn B ≥ 10 điểm %, recall không giảm |
| Nhiễu | Số cảnh báo trùng giữa hai hệ cho cùng sự cố | 0 |

## 7. Xác nhận

| | Khang | Nhân |
|---|---|---|
| Đồng ý bản v0.2 | ☐ | ☐ |
| Ngày | | |

Ghi chú: standup trước ghi "tách riêng 2 dự án" — nghĩa đúng là **hai hệ chạy độc lập, tích hợp
qua contract này**; standup từ nay ghi theo mốc I1–I5.
