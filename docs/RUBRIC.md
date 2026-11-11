# Rubric cụ thể hoá – ASE (nộp lại theo góp ý Gate 0)

Nguyễn Thế Khang · Nov 10, 2026

Thay rubric 100 điểm × 8 tiêu chí của Gate 0. Đúng khung chung: **3 tiêu chí × mức 2 – 3 – 4**, ô nào cũng có số. Cộng hưởng với cặp **không nằm trong rubric** (điểm teamwork tính riêng, xem `contracts/PAIR_CONTRACT_v0.1.md` §6). Dữ liệu: mock tới CP1; dữ liệu masked do mentor cấp ở CP2–CP3, chỉ chạy với V-LLM nội bộ.

## Tiêu chí 1 – Giá trị cho vận hành (success factor, KPI, critical service, stakeholder)

| Mức | Điều kiện (tất cả phải đạt) |
|---|---|
| **2** | Agent quét Danh sách giao dịch đổi pin 30 phút/lần trên mock VN. Báo cáo kỳ có KPI success factor (tỷ lệ đổi thành công, tỷ lệ lỗi, thời gian một lần đổi, khách bị ảnh hưởng) và Top N lỗi. Trên tập test mock: recall ≥ 90%, đúng mã + mức ≥ 80%. 1 buổi demo cho mentor |
| **3** | Trên tập test mock: recall ≥ 95%, precision ≥ 90%, ≥ 85% kịch bản gộp đúng 1 sự cố, lỗi → thông báo p95 ≤ 35 phút, báo cáo tuần/tháng khớp dữ liệu 100%. Health score cho ≥ 4 dịch vụ critical; open point sinh từ KPI. Trên mẫu masked đã gán nhãn (≥ 200 giao dịch lỗi): recall ≥ 85%, đúng mã ≥ 75%. ≥ 1 người OPS dùng thử báo cáo/cảnh báo, chấm "dùng được" ≥ 3/5 |
| **4** | Trên mẫu masked: recall ≥ 95%, đúng mã ≥ 85%. Thời gian OPS lập báo cáo Top N lỗi giảm ≥ 70% so với baseline thủ công **đo được** (mục "Baseline" dưới). ≥ 3 open point được OPS xác nhận đúng và có hành động |

## Tiêu chí 2 – Chất lượng kỹ thuật, an toàn, người khác chạy lại được

| Mức | Điều kiện |
|---|---|
| **2** | `make check` xanh (ruff, mypy strict, pytest). Chỉ dùng mock / masked do mentor cấp; 0 thao tác ghi vào hệ thống vận hành. Không gửi dữ liệu ra LLM/SaaS bên ngoài |
| **3** | Người khác clone repo, làm theo README chạy `make sim` → `make agent-eval` → `make report` ra **đúng các số trong báo cáo** trong ≤ 30 phút. Cùng seed → cùng số 100%. LLM chỉ là V-LLM nội bộ, chạy bất đồng bộ; thông báo vẫn gửi khi LLM lỗi (có test). An toàn: 0 thao tác ghi, 0% evidence không tồn tại, 0% prompt injection thành công trên bộ ≥ 20 ca |
| **4** | Contract test với đối tác chạy trong CI của cả hai repo. Có tài liệu bàn giao để OPS tự chạy agent và đọc báo cáo không cần người phát triển. Dữ liệu masked có báo cáo coverage (cột thiếu, mã ngoài bảng chuẩn) |

## Tiêu chí 3 – Tiến độ, quy trình, xử lý góp ý

| Mức | Điều kiện |
|---|---|
| **2** | ≥ 70% đầu việc đúng hạn trong file theo dõi; standup ≥ 80% số ngày; góp ý của mentor được xử lý trong ≤ 3 ngày |
| **3** | ≥ 80% đầu việc đúng hạn; mỗi việc có % hoàn thành; rủi ro mới ghi vào file theo dõi trong ≤ 1 ngày; góp ý xử lý trong ≤ 2 ngày và ghi rõ đã sửa gì (vd mục "Xử lý góp ý Gate 0" của báo cáo Gate 1) |
| **4** | ≥ 90% đầu việc đúng hạn; mọi điều chỉnh kế hoạch có số đo làm căn cứ; retro mỗi sprint có ≥ 2 hành động đo được và được kiểm lại ở sprint sau |

## Baseline thủ công của OPS (căn cứ cho Tiêu chí 1, mức 4)

Đo trước CP2, cùng 1–2 người OPS, theo trung vị của ≥ 3 lần:
1. **Báo cáo Top N lỗi tháng** (như mẫu báo cáo OPS đang gửi): thời gian từ lúc mở màn hình Danh sách giao dịch tới lúc có bảng Top 5 + % → so với `make report` (vài giây) và thời gian OPS đọc/duyệt bản do ASE lập.
2. **Phát hiện giao dịch lỗi**: thời gian từ lúc giao dịch thất bại tới lúc OPS biết (hiện chưa có số) → so với SF5 của agent (p95 28,8 phút trên mock).
Nếu không đo được (1) với OPS thật, mức 4 của Tiêu chí 1 coi như chưa đạt; không tự đặt số.
