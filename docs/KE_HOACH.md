# Kế hoạch – ASE: AI Agent hỗ trợ vận hành đổi pin VinFast

Nguyễn Thế Khang · Nov 10, 2026 · bản rút gọn 2 trang (chi tiết: Phụ lục `OPS_AGENT_PLAN.md`)

## 1. Vấn đề, người dùng, giá trị

OPS đang theo dõi màn hình **Danh sách giao dịch đổi pin** bằng tay: lọc "Thất bại", đếm theo mã lỗi, tự lập báo cáo Top 5 lỗi theo tháng. Không có KPI chung cho luồng đổi pin, không biết dịch vụ nào đang yếu cho tới khi lỗi đã nhiều.

**Phạm vi áp dụng:** ASE là AI Agent hỗ trợ vận hành; giai đoạn này áp dụng vào hệ thống BSS (nguồn dữ liệu: Danh sách giao dịch đổi pin), hướng lâu dài là nhiều hệ thống vận hành khác.

**Người dùng:** đội OPS trung tâm (nhận cảnh báo, ticket, báo cáo); CSKH (danh sách khách bị ảnh hưởng).

**ASE làm 5 việc**, chỉ đọc dữ liệu, AI chỉ gợi ý, OPS quyết định:
1. **Success factor của luồng đổi pin:** KPI (tỷ lệ đổi thành công, tỷ lệ lỗi, thời gian một lần đổi, khách đã trả pin chưa nhận pin, khách bị ảnh hưởng) và **health score** cho dịch vụ critical (tủ, trạm, callback thanh toán, đồng bộ giao dịch tủ → hệ thống).
2. **Monitoring tự động:** quét danh sách giao dịch 30 phút/lần, gộp giao dịch lỗi/kẹt thành sự cố theo mẫu phân bố so với mức nền.
3. **Cảnh báo OPS** qua email, Teams, ticket (ManageEngine trial): tối đa 1 mail + 1 card mỗi lần quét.
4. **Open point + đề xuất** sinh từ KPI và health score; customer-facing (khách bị ảnh hưởng).
5. **Báo cáo tuần/tháng** tự động: Top 5/10 lỗi, % trên tổng số phiên lỗi, KPI, health score, open point.

## 2. Phạm vi

| Trong phạm vi | Ngoài phạm vi |
|---|---|
| **VN**; Danh sách giao dịch (mock tới CP1, masked do mentor cấp ở CP2–CP3) | PH, ID, các nước khác (cấu hình sẵn, không sinh dữ liệu) |
| Bảng mã lỗi BSS chuẩn 66 mã (High/Low), dùng nguyên văn | Tự đặt nhóm lỗi / mức riêng |
| Rule + thống kê tất định; LLM = **V-LLM nội bộ**, bất đồng bộ, tách khỏi luồng quét | API LLM bên ngoài; LLM tự gửi thông báo hay tạo ticket |
| Mail (Mailpit → SMTP thật bằng cấu hình), mock Teams, ticket giả lập → ManageEngine | Ghi vào hệ thống vận hành; Kafka (hoãn) |
| Tín hiệu từ log là phần của Võ Doanh Nhân (pair contract v0.1) | Detector realtime trên log (nhường cho Nhân) |

## 3. Success metric

**KPI vận hành** (đầu ra cho OPS, không có ngưỡng "đạt"): như mục 1, tính trong báo cáo kỳ.

**Chỉ tiêu chất lượng của AI** (đo trên tập test mock có ground truth, sau đó trên mẫu masked gán nhãn):

| # | Chỉ tiêu | Ngưỡng | Baseline Gate 1 (mock, rule-only) |
|---|---|---|---|
| SF1 | Giao dịch được đọc | 100% | 100% |
| SF2 / SF3 | Recall / precision giao dịch lỗi + kẹt | ≥ 95% / ≥ 90% | 100% / 100% |
| SF4 | Đúng mã + mức; kịch bản ↔ 1 sự cố | ≥ 85% | 100%; **83,9%** (mật độ thật, tập test mới) |
| SF5 | Lỗi → thông báo, p95 | ≤ 35 phút | 28,5 phút |
| SF6 | Gợi ý đúng nguyên nhân (V-LLM) | ≥ 70% | chưa đo |
| SF7 / SF8 | Thông báo hợp lệ / ≤ 1 thông báo mỗi lần quét, ticket trùng 0 | 100% / ≤ 1 | 100% / 1 (ticket chưa có) |
| SF9 | Ghi vào hệ thống; evidence giả; prompt injection | 0 / 0% / 0% | chưa đo |
| SF10 | Báo cáo tuần/tháng khớp dữ liệu | 100% | 100% |

**Baseline thủ công của OPS** (đo trước CP2): thời gian lập báo cáo Top N tháng và thời gian từ lỗi tới lúc OPS biết. Đây là căn cứ chứng minh giá trị (Rubric, Tiêu chí 1 mức 4).

## 4. Kiến trúc

```
Danh sách giao dịch (mock · tệp "Xuất dữ liệu") → Scanner 30' → Analyzer (bảng mã chuẩn + mức nền)
   → thông báo (Mail + Teams) + ticket  ·  KPI + health score + open point → báo cáo tuần/tháng
   → V-LLM nội bộ (bất đồng bộ, chỉ diễn giải số đã tính)  ·  Evaluator (ground truth)
```

## 5. Lịch

| Mốc | Ngày | Nội dung |
|---|---|---|
| Gate 1 | 11/11 | Skeleton end-to-end, baseline SF1–SF10 (trừ SF6, SF9), KPI + health score + báo cáo kỳ, pair contract v0.1 |
| CP1 | 18/11 | SF4b ở mật độ thật ≥ 85%; ticket giả lập; lưu bền trạng thái quét; tập test mới; I1 với Nhân |
| CP2 | 25/11 | Dữ liệu masked (adapter tệp "Xuất dữ liệu"), gán nhãn ≥ 200 giao dịch lỗi; V-LLM; baseline OPS; I2–I3 |
| CP3 | 09/12 | ManageEngine; customer-facing (mail CSKH); MTTA/MTTR; đo cộng hưởng I5; bàn giao |

## 6. Rủi ro chính

| Rủi ro | Xử lý |
|---|---|
| Ở mật độ thật (2–3 GD/điểm/ngày) sự cố tủ để lại rất ít giao dịch → gộp sự cố khó (SF4b 83,9%; sai đều là sự cố chỉ 1 giao dịch lỗi) | Cộng dồn lỗi tủ qua 24 giờ, nâng sự cố lên mức trạm (đã làm); Sprint 1: thêm bằng chứng từ telemetry/alarm tủ; chốt cách tính lần lặp 1 giao dịch |
| Mock do mình thiết kế → số đo dễ tự khớp | Tập test seed khác, kịch bản khoá trước; đo lại trên mẫu masked gán nhãn; báo cáo ghi rõ giới hạn |
| Chưa biết cách lấy Danh sách giao dịch thật (chỉ có nút "Xuất dữ liệu"?) | Adapter đọc tệp CSV/XLSX; hỏi mentor tần suất xuất |
| Chưa có endpoint V-LLM | Rule-only chạy đủ; LLM chỉ là phần làm giàu |
| Dữ liệu masked có mã chưa nằm trong bảng chuẩn | Ghi "mã ngoài bảng chuẩn", đếm trong coverage, không tự gán mức; bảng chuẩn giữ nguyên |

Rubric: `RUBRIC.md`. Pair contract: `contracts/PAIR_CONTRACT_v0.1.md` (trong repo).
