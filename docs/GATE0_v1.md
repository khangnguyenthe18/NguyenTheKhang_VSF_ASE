# Kế hoạch Gate 0 (bản sửa sau đánh giá) – ASE: AI Agent hỗ trợ vận hành đổi pin VinFast

Nguyễn Thế Khang · Nov 11, 2026 · v1, thay bản 04/11 (`Gate0.md`, giữ làm v0) theo đánh giá Gate 0 của mentor (09/11)

**Những gì đã đổi so với v0** (đối chiếu từng góp ý ở Phụ lục A):

| Chủ đề | v0 (04/11) | v1 (11/11) |
|---|---|---|
| Đề tài | Phát hiện lỗi realtime trên event 7 bước, phân loại 7 nhóm, P1/P2/P3 | AI Agent hỗ trợ vận hành, **áp dụng đầu tiên cho hệ thống BSS** (nguồn dữ liệu: Danh sách giao dịch đổi pin), quét 30 phút/lần; hướng lâu dài là nhiều hệ thống |
| Success factor | 9 chỉ tiêu "AI kiểm tra được giao dịch lỗi" | **KPI của luồng đổi pin + health score dịch vụ critical + open point từ KPI**; SF1–SF10 chỉ là chỉ tiêu chất lượng của AI |
| Phạm vi | VN, PH, ID; MQTT/Kafka | **Chỉ VN**; không Kafka |
| LLM | Claude Haiku qua API ngoài | **V-LLM nội bộ**, mặc định rule-only, bất đồng bộ |
| Rubric | 100 điểm × 8 tiêu chí | 3 tiêu chí × mức 2–3–4, ô nào cũng có số |
| Pair contract | Hai bản lệch nhau | Một bản v0.1 trong repo, một schema chung, metric cộng hưởng có ngưỡng |
| Mock | 11 trạm, lưu lượng tự đặt | Điểm đổi pin thật, ≈ 2,9 triệu GD/tháng phân bổ theo tỉnh/thành |

---

## 1. Vấn đề, người dùng, giá trị

**Vấn đề.** OPS theo dõi màn hình Danh sách giao dịch đổi pin bằng tay: lọc "Thất bại", đếm theo mã lỗi, tự lập báo cáo Top 5 lỗi theo tháng. Không có KPI chung cho luồng đổi pin, không biết dịch vụ nào đang yếu cho tới khi lỗi đã nhiều, và giao dịch kẹt không có mã lỗi thì dễ bị bỏ sót.

**Giải pháp.** ASE là AI Agent hỗ trợ vận hành, áp dụng đầu tiên cho hệ thống BSS. Agent chỉ đọc Danh sách giao dịch, quét 30 phút/lần, gộp giao dịch lỗi/kẹt thành sự cố theo nơi xảy ra và so với mức nền, tính KPI và health score, gửi cảnh báo + ticket cho OPS, lập báo cáo tuần/tháng. AI chỉ gợi ý, OPS quyết định.

| Người dùng | Nhu cầu | Giá trị nhận được |
|---|---|---|
| Đội OPS trung tâm | Biết sớm sự cố, không bị spam | Tối đa 1 mail + 1 card Teams mỗi 30 phút, gộp theo sự cố; ticket cho sự cố cần xử lý |
| Quản lý vận hành | Biết luồng đổi pin khoẻ hay yếu ở đâu | KPI, health score dịch vụ critical, open point có đề xuất, báo cáo Top N lỗi tuần/tháng tự động |
| CSKH | Biết khách nào bị ảnh hưởng | Danh sách khách đã trả pin chưa nhận pin, khách thất bại ≥ 2 lần/ngày (mã khách đã băm) |

## 2. Phạm vi

| Trong phạm vi | Ngoài phạm vi |
|---|---|
| Hệ thống BSS (giai đoạn này) | Hệ thống vận hành khác: hướng phát triển sau CP3, thêm nguồn dữ liệu + bảng mã lỗi riêng, dùng lại phần quét, cảnh báo, KPI, báo cáo |
| **VN**; Danh sách giao dịch (mock tới CP1, masked do mentor cấp ở CP2–CP3) | PH, ID, các nước khác (cấu hình sẵn, không sinh dữ liệu) |
| Bảng mã lỗi BSS chuẩn (66 mã, mức High/Low), dùng nguyên văn | Tự đặt nhóm lỗi hay mức riêng |
| Rule + thống kê tất định; LLM = **V-LLM nội bộ**, bất đồng bộ, tách khỏi luồng quét | API LLM bên ngoài; LLM tự gửi thông báo hay tạo ticket |
| Mail (Mailpit → SMTP thật bằng cấu hình), mock Teams, ticket giả lập → ManageEngine trial | Ghi vào hệ thống vận hành BSS; Kafka |
| Báo cáo tuần/tháng qua mail; dashboard Danh sách giao dịch | Detector realtime trên log (nhường cho Võ Doanh Nhân) |

**Ràng buộc:** không kết nối production; dữ liệu masked chỉ chạy với V-LLM nội bộ; chưa có endpoint V-LLM thì chạy rule-only.

## 3. Success metric

**3.1 Success factor của luồng đổi pin** (đầu ra cho OPS, tính trong báo cáo kỳ):

| KPI | Định nghĩa |
|---|---|
| Tỷ lệ đổi pin thành công | Hoàn thành / tổng giao dịch |
| Tỷ lệ lỗi | (Thất bại + kẹt) / tổng giao dịch; Top N mã lỗi, % trên tổng phiên lỗi |
| Thời gian một lần đổi | p50 / p95 từ Tạo mới tới Hoàn thành |
| Khách đã trả pin, chưa nhận pin | Số giao dịch lỗi sau khi pin cũ đã vào tủ |
| Khách bị ảnh hưởng | Số khách có giao dịch lỗi; số khách thất bại ≥ 2 lần/ngày |

**Health score dịch vụ critical** = 100 − phạt theo % lỗi vượt mục tiêu − phạt theo % giao dịch có mã High; ≥ 90 Tốt, 70–90 Cần theo dõi, < 70 Kém. Bốn dịch vụ: tủ, trạm, callback thanh toán, đồng bộ giao dịch tủ → hệ thống. Công thức và ngưỡng trong `ase/knowledge/kpi.yaml`, không để LLM chấm.

**Open point** sinh từ KPI: dịch vụ health < 70, mã lỗi tăng ≥ 50% so với kỳ trước, tủ lặp lỗi. Mỗi open point có ảnh hưởng, bằng chứng (mã giao dịch thật), đề xuất; độ ưu tiên tính bằng công thức.

**3.2 Chỉ tiêu chất lượng của AI** (tập test mock có ground truth, sau đó mẫu masked gán nhãn; đo rule-only và rule + V-LLM):

| # | Chỉ tiêu | Ngưỡng | Baseline Gate 1 (mock, rule-only) |
|---|---|---|---|
| SF1 | Giao dịch được đọc | 100% | 100% |
| SF2 / SF3 | Recall / precision giao dịch lỗi + kẹt | ≥ 95% / ≥ 90% | 100% / 100% |
| SF4 | Đúng mã + mức; mỗi lần lặp kịch bản ↔ 1 sự cố | ≥ 85% | 100%; **83,9%** |
| SF5 | Lỗi → thông báo, p95 | ≤ 35 phút | 28,5 phút |
| SF6 | V-LLM gợi ý đúng nguyên nhân | ≥ 70% | chưa đo (chờ endpoint) |
| SF7 / SF8 | Thông báo hợp lệ / ≤ 1 thông báo mỗi lần quét, ticket trùng 0 | 100% / ≤ 1 | 100% / 1 (ticket chưa có) |
| SF9 | Ghi vào hệ thống; evidence giả; prompt injection thành công | 0 / 0% / 0% | chưa đo |
| SF10 | Báo cáo tuần/tháng khớp dữ liệu | 100% | 100% |

**Baseline thủ công của OPS** (đo trước CP2): thời gian lập báo cáo Top N tháng và thời gian từ lỗi tới lúc OPS biết. Đây là căn cứ chứng minh giá trị (Rubric, Tiêu chí 1 mức 4).

## 4. Kiến trúc sơ bộ

```
Danh sách giao dịch (mock parquet · tệp "Xuất dữ liệu" CSV/XLSX)
   → Scanner (mốc :00/:30) → Analyzer (bảng mã chuẩn + mức nền 7 ngày, tất định)
   → Notifier: 1 mail + 1 card / lần quét · Ticket: 1 ticket / sự cố
   → KPI + health score + open point → báo cáo tuần/tháng (mail)
   → V-LLM nội bộ (bất đồng bộ, chỉ diễn giải số đã tính) · Evaluator (so ground truth)
```

Nguyên tắc: chỉ đọc; rule trước, LLM sau (LLM lỗi vẫn gửi được thông báo); thông báo và ticket do code gọi; lưu UTC, hiển thị giờ địa phương của trạm; idempotency qua outbox.

**Stack:** Python 3.12 + `uv`, Pydantic v2, polars, SQLite (outbox, trạng thái), FastAPI (dashboard, mock Teams), aiosmtplib → Mailpit, Jinja2. Không LangChain, vector DB, Kafka.

---

## 5. Rubric cụ thể hoá

Đúng khung chung: **3 tiêu chí × mức 2 – 3 – 4**, ô nào cũng có số. Cộng hưởng với cặp **không nằm trong rubric** (teamwork tính riêng, mục 6.4).

**Tiêu chí 1 – Giá trị cho vận hành (success factor, KPI, critical service, stakeholder)**

| Mức | Điều kiện (tất cả phải đạt) |
|---|---|
| **2** | Agent quét Danh sách giao dịch 30 phút/lần trên mock VN. Báo cáo kỳ có KPI success factor và Top N lỗi. Trên tập test mock: recall ≥ 90%, đúng mã + mức ≥ 80%. 1 buổi demo cho mentor |
| **3** | Trên tập test mock: recall ≥ 95%, precision ≥ 90%, ≥ 85% kịch bản gộp đúng 1 sự cố, lỗi → thông báo p95 ≤ 35 phút, báo cáo khớp dữ liệu 100%. Health score cho ≥ 4 dịch vụ critical; open point sinh từ KPI. Trên mẫu masked gán nhãn (≥ 200 giao dịch lỗi): recall ≥ 85%, đúng mã ≥ 75%. ≥ 1 người OPS dùng thử, chấm "dùng được" ≥ 3/5 |
| **4** | Trên mẫu masked: recall ≥ 95%, đúng mã ≥ 85%. Thời gian OPS lập báo cáo Top N giảm ≥ 70% so với baseline thủ công đo được. ≥ 3 open point được OPS xác nhận đúng và có hành động |

**Tiêu chí 2 – Chất lượng kỹ thuật, an toàn, người khác chạy lại được**

| Mức | Điều kiện |
|---|---|
| **2** | `make check` xanh (ruff, mypy strict, pytest). Chỉ dùng mock / masked do mentor cấp; 0 thao tác ghi vào hệ thống vận hành; không gửi dữ liệu ra LLM/SaaS bên ngoài |
| **3** | Người khác làm theo README chạy `make sim` → `make agent-eval` → `make report` ra đúng các số trong báo cáo trong ≤ 30 phút; cùng seed → cùng số. Thông báo vẫn gửi khi LLM lỗi (có test). 0 thao tác ghi, 0% evidence không tồn tại, 0% prompt injection thành công trên ≥ 20 ca |
| **4** | Contract test với đối tác chạy trong CI của cả hai repo. Tài liệu bàn giao để OPS tự chạy agent, đọc báo cáo. Dữ liệu masked có báo cáo coverage (cột thiếu, mã ngoài bảng chuẩn) |

**Tiêu chí 3 – Tiến độ, quy trình, xử lý góp ý**

| Mức | Điều kiện |
|---|---|
| **2** | ≥ 70% đầu việc đúng hạn; standup ≥ 80% số ngày; góp ý của mentor xử lý trong ≤ 3 ngày |
| **3** | ≥ 80% đầu việc đúng hạn, mỗi việc có % hoàn thành; rủi ro mới ghi vào file theo dõi trong ≤ 1 ngày; góp ý xử lý trong ≤ 2 ngày và ghi rõ đã sửa gì (vd Phụ lục A) |
| **4** | ≥ 90% đầu việc đúng hạn; mọi điều chỉnh kế hoạch có số đo làm căn cứ; retro mỗi sprint có ≥ 2 hành động đo được, kiểm lại ở sprint sau |

**Cách đo baseline thủ công của OPS** (căn cứ cho Tiêu chí 1 mức 4): cùng 1–2 người OPS, trung vị ≥ 3 lần, đo (1) thời gian từ lúc mở Danh sách giao dịch tới lúc có bảng Top 5 + %, và (2) thời gian từ lúc giao dịch thất bại tới lúc OPS biết. Không đo được với OPS thật thì mức 4 coi như chưa đạt; không tự đặt số.

---

## 6. Pair contract v0.1 (với Võ Doanh Nhân)

Bản đầy đủ, một file duy nhất cho cả hai: `contracts/PAIR_CONTRACT_v0.1.md` (kèm `DATA_FORMAT_v0.1.md`, `schemas/`, `fixtures/`). Đã gửi Nhân, chờ xác nhận.

### 6.1 Hai bài toán giao nhau ở đâu, giá trị cộng hưởng là gì?

| | Khang (ASE) | Nhân |
|---|---|---|
| Tầng | **KPI và quyết định**: success factor, health score, open point, cảnh báo/ticket cho OPS | **Tín hiệu từ log**: hành vi bất thường LLM phát hiện trên log stream |
| Dữ liệu | Danh sách giao dịch đổi pin | Log hệ thống / thiết bị / người dùng |
| Đầu ra | Mail + Teams + ticket (kênh push duy nhất), báo cáo tuần/tháng | Log stream + thống kê (pull) |

Giao nhau: cùng hệ thống BSS VN, cùng khoá tương quan (mã trạm, mã tủ, connector, mã giao dịch eMSP, thời gian). Cộng hưởng: (1) tín hiệu log của Nhân giải thích và báo sớm sự cố mà KPI của Khang mới thấy hậu quả; (2) mức nền của Khang làm ngưỡng cho cảnh báo "sụt giảm theo khung giờ" của Nhân; (3) một kênh thông báo, không cảnh báo trùng.

### 6.2 Ai làm phần nào, ranh giới ở đâu?

| Hạng mục | Khang | Nhân |
|---|---|---|
| Mock Danh sách giao dịch + ground truth (VN) | Chịu trách nhiệm | Dùng chung seed |
| Mock log (VinSOC scan, request lặp lại, sụt traffic theo khung giờ, deploy làm latency tăng dần) | — | Chịu trách nhiệm |
| Detector realtime trên log | **Nhường cho Nhân** (detector event của Khang đã đóng băng) | Chịu trách nhiệm |
| KPI, health score, open point, báo cáo tuần/tháng | Chịu trách nhiệm | — |
| Gộp sự cố, cảnh báo Mail/Teams, ticket | Chịu trách nhiệm | Gửi qua Khang bằng `notify=true` |
| Log stream view + dashboard thống kê | — | Chịu trách nhiệm |
| Schema chung, contract test | Cùng sở hữu | Cùng sở hữu |

Mỗi bên chạy được độc lập khi bên kia ngừng.

### 6.3 Trao đổi gì, định dạng nào?

**Một schema chung** `ase.anomaly.event` v0.1 cho cả hai chiều, có fixture và contract test; truyền bằng file JSONL dùng chung, **không dùng Kafka** cho tới khi cả hai cùng cần (xem lại sau CP2).

| `kind` | Chiều | Nội dung |
|---|---|---|
| `transaction_incident` (`INC-*`) | Khang → Nhân | Sự cố gộp từ giao dịch lỗi: phạm vi, mã lỗi chuẩn, mức, mã giao dịch liên quan |
| `baseline` (`BASE-*`) | Khang → Nhân | Mức nền 7 ngày theo trạm × khung giờ: số giao dịch/giờ, tỷ lệ lỗi, health score |
| `behavior` (`BHV-*`) | Nhân → Khang | Hành vi bất thường trên log: quan sát / nền / z-score, phạm vi, độ tin cậy |

Thời gian ISO-8601 UTC. Thêm field không phá vỡ; đổi/xoá field tăng major, cả hai đồng ý. Nhân đặt `notify=true` khi cần báo người; Khang gửi trong lần quét 30 phút kế tiếp.

### 6.4 Khi nào tích hợp, đo cộng hưởng bằng gì?

| Điểm | Ngày | Xong khi |
|---|---|---|
| I1 Dữ liệu nguồn | 18/11 (CP1) | Một kịch bản có cả giao dịch lỗi và dấu hiệu log khớp mã + thời gian |
| I2 Trao đổi sự kiện | 22/11 | Contract test xanh ở cả hai repo |
| I3 Bằng chứng | 25/11 (CP2) | Thông báo của Khang có `BHV-*` thật; Nhân dùng `baseline` làm ngưỡng |
| I4 Cảnh báo | 02/12 | `notify=true` đi qua kênh của Khang; cảnh báo trùng = 0 |
| I5 Đánh giá | 09/12 (CP3) | Có bảng A/B/C dưới |

Ablation cùng seed: (A) Khang một mình · (B) Nhân một mình · (C) kết hợp.

| Metric | Ngưỡng |
|---|---|
| Lead time trên kịch bản có dấu hiệu log | C sớm hơn A ≥ 10 phút trên ≥ 80% kịch bản đó |
| Sự cố/open point có `BHV-*` hợp lệ, khớp kịch bản | C ≥ 50% (A = 0% theo định nghĩa) |
| Precision cảnh báo "sụt giảm theo khung giờ" của Nhân khi dùng `baseline` | C cao hơn B ≥ 10 điểm %, recall không giảm |
| Cảnh báo trùng giữa hai hệ | 0 |

---

## 7. Đặc tả bộ dữ liệu mockup

Simulator sinh **thẳng Danh sách giao dịch** đúng định dạng màn hình thật, tái lập được theo seed (tập test dùng seed khác `dev`), ghi `swap_transactions.{jsonl,parquet}` + `txn_ground_truth.csv` (mọi giao dịch, kể cả không lỗi). Mọi thời gian lưu UTC, hiển thị theo `Asia/Ho_Chi_Minh`.

**Cột** (như màn hình; chi tiết `contracts/DATA_FORMAT_v0.1.md`): mã trạm `B.<TỈNH><5 số>`, mã tủ `<mã trạm>.<3 số>`, S/N tủ `BSS-…`, loại tủ CC_06_P (6 connector) / CC_12_P (12), mã GD eMSP (24 hex, khoá chính), mã GD trên tủ, ngày thực hiện, trạng thái (Tạo mới · Đưa pin vào · Hoàn thành · Thất bại), lý do lỗi (mã của bảng chuẩn), connector + S/N + SOH/SOC pin cũ và mới, thời gian pin vào/ra; thêm `country`, `timezone`, mã khách đã băm.

**Lưu lượng bám thực tế:** ≈ **2,9 triệu GD/tháng** cho toàn bộ điểm đổi pin VN (theo chủ dự án), phân bổ theo tỉnh/thành: trạm đặt tại điểm đổi pin VinFast thật, lấy mẫu theo tỉnh; tỉnh nhiều điểm thì mỗi điểm cũng đông hơn (hệ số `province_alpha` 0,5 là **giả định**, cần OPS xác nhận). Bình quân 2–3 GD/điểm/ngày; cao điểm 7–9h và 17–19h giờ VN. Telemetry tủ (heartbeat, nhiệt độ) sinh khớp với giao dịch.

| Profile | Dùng cho | Điểm đổi pin | Ngày | Giao dịch |
|---|---|---|---|---|
| `smoke` | CI, test | 150 | 1 | ~330 |
| `dev` | Viết và chỉnh rule | 450 | 14 | ~14 nghìn |
| `eval` | Đo SF1–SF10 | 650 | 30 (20 nền + 10 test) | ~44 nghìn; tập test ~14,5 nghìn |
| `demo` | Trình diễn dashboard, mail (không đo SF) | 3.000 | 3 | ~20 nghìn |
| `scale` | Đo hiệu năng toàn quốc | toàn bộ | 1 | ~96 nghìn |

**Lỗi nền:** 4% giao dịch, chỉ mã mức Low (BatteryInTimeout, pin không hợp lệ, SOH không đạt, callback thanh toán lỗi/timeout).

**Kịch bản inject** (tập kịch bản **khoá trước khi viết rule**; mọi mã lấy từ bảng chuẩn, generator kiểm tra; mỗi kịch bản lặp nhiều lần, xen kẽ tập nền và tập test):

| Kịch bản | Phạm vi | Mã / dấu hiệu | Kiểm chứng |
|---|---|---|---|
| Khoang kẹt khoá nhiều ngày | 1 tủ, 3 ngày | B6000, C6029 | SF4, open point |
| Mất điện lưới | 1 trạm, 24 giờ | A0122 → B6025 | SF4b (cả trạm = 1 sự cố) |
| Tủ quá nhiệt rồi báo khói | 1 tủ, 24 giờ | A6009 → A0110; telemetry nhiệt độ tăng | SF4, mã High |
| Cổng thanh toán ngừng | Nhiều trạm VN, 25 phút | D6036, D6037 | SF4b (lan rộng = 1 sự cố), health score |
| Tủ không gửi được giao dịch | Toàn mạng, 40 phút | B0207 | SF4b, health score |
| Mất pin trong khoang | 1 tủ, 24 giờ | C6100 | SF4 |
| Đã trả pin, chưa nhận pin | 1 tủ, 24 giờ | D6038 sau khi pin vào | KPI khách bị ảnh hưởng, CSKH |
| Kẹt ở "Đưa pin vào" / "Tạo mới" | 1 tủ / 1 trạm, 24 giờ | Không có mã | SF2 (lỗi im lặng) |
| **Bẫy:** chậm nhưng hoàn thành | 1 tủ, 24 giờ | Không có lỗi | SF3 |

Mã ngoài bảng chuẩn trong dữ liệu masked: agent giữ nguyên mã, ghi "mã ngoài bảng chuẩn", không tự gán mức.

---

## 8. Rủi ro và phụ thuộc

**Giả định rủi ro nhất:** chỉ với các cột của màn hình Danh sách giao dịch và bảng mã lỗi chuẩn, ở lưu lượng thực tế, agent phát hiện đủ lỗi, gộp được sự cố và tính được KPI. Kiểm chứng ở Gate 1: phát hiện và KPI đạt; gộp sự cố đạt 83,9%, sai đều ở sự cố chỉ để lại 1 giao dịch lỗi (`GATE1_REPORT.md` §4).

| Rủi ro | Ảnh hưởng | Giảm thiểu |
|---|---|---|
| Ở mật độ thật, sự cố tủ để lại rất ít giao dịch | Gộp sự cố khó, SF4b < 85% | Cộng dồn lỗi tủ qua 24 giờ, nâng lên mức trạm (đã làm); thêm bằng chứng từ telemetry/alarm tủ; chốt với mentor cách tính lần lặp 1 giao dịch |
| Mock do mình thiết kế → số đo dễ tự khớp | Đánh giá sai năng lực thật | Seed test riêng, kịch bản khoá trước, lưu lượng theo số thật; mỗi lần đổi rule lớn đo trên tập test mới chưa xem; đo lại trên mẫu masked gán nhãn |
| Chưa biết cách lấy Danh sách giao dịch thật | Không chạy được trên dữ liệu thật | Adapter đọc tệp "Xuất dữ liệu" CSV/XLSX; hỏi mentor tần suất xuất |
| Dữ liệu masked có mã ngoài bảng chuẩn, cột khác mock | Mất phân loại, lệch KPI | Báo cáo coverage; ánh xạ cột bằng YAML; bảng chuẩn giữ nguyên |
| Tỷ lệ lỗi nền và phân bổ theo tỉnh thật khác mock | Mức nền, ngưỡng sai | Mức nền học từ lịch sử thật; xin số liệu theo tỉnh để hiệu chỉnh |
| LLM bịa nguyên nhân hoặc evidence; prompt injection | OPS tin sai | Evidence phải trỏ tới mã có thật (kiểm bằng code); dữ liệu bọc `<untrusted_data>`; LLM không có tool ghi; thông báo do code gửi |
| V-LLM chậm, lỗi hoặc chưa có | Thiếu phần làm giàu | Rule-only chạy đủ; LLM bất đồng bộ, không chặn thông báo |
| Spam OPS | OPS bỏ qua cảnh báo | 30 phút/lần, ≤ 1 mail + 1 card, gộp theo sự cố, 1 ticket/sự cố, idempotency |
| Tiến độ đối tác trễ | Mốc I1–I5 trượt | Mỗi bên chạy độc lập bằng fixture; tích hợp lần lượt |

| Phụ thuộc | Trạng thái / phương án |
|---|---|
| V-LLM nội bộ (endpoint, API, quota) | Chờ mentor; chưa có thì rule-only |
| Dữ liệu masked + tệp "Xuất dữ liệu" mẫu | Chờ mentor; viết adapter trên fixture cùng cột |
| Người gán nhãn mẫu masked | Đã có |
| ManageEngine trial | Chủ dự án đăng ký sau; trước đó dùng ticket giả lập |
| Mail / Teams thật | Mailpit + mock Teams; chuyển thật bằng cấu hình khi có SMTP / webhook |
| Một người OPS cho baseline thủ công và dùng thử | Cần sắp xếp trước CP2 |
| Nhân xác nhận pair contract v0.1 | Đã gửi |

---

## Phụ lục A. Đối chiếu góp ý Gate 0

| Góp ý Gate 0 (09/11) | Đã sửa | Ở đâu |
|---|---|---|
| Rubric sai khung (100 điểm, 8 tiêu chí), thiếu tiêu chí 3, teamwork nằm trong rubric | 3 tiêu chí × mức 2–3–4, mọi ô có số; tiêu chí 3 theo tiến độ và xử lý góp ý; teamwork tách riêng | Mục 5, `RUBRIC.md` |
| Lệch đề: success factor bị định nghĩa lại thành "kiểm tra giao dịch lỗi"; thiếu KPI, health score; open point chưa từ KPI | Tách hai lớp: KPI + health score 4 dịch vụ critical + open point từ KPI; SF1–SF10 là chỉ tiêu chất lượng của AI | Mục 3, `ase/report/`, `kpi.yaml` |
| Phạm vi quá rộng: cắt còn VN, hoãn Kafka, đa quốc gia | Chỉ VN; Kafka và pipeline event đóng băng | Mục 2 |
| Không duyệt gửi dữ liệu lên API LLM ngoài; dùng model nội bộ, tách khỏi luồng chính | V-LLM nội bộ; mặc định rule-only; thông báo không chờ LLM; cấu hình chặn LLM bên ngoài | Mục 2, 4 |
| Nhường detector realtime cho Nhân | Ghi rõ trong pair contract | Mục 6.2 |
| Pair contract hai bản lệch; thiếu schema chung, chiều baseline, metric cộng hưởng có ngưỡng | Một bản v0.1, một schema chung có contract test, chiều `baseline`, mốc I1–I5, 4 metric có ngưỡng | Mục 6, `contracts/` |
| Chưa có baseline thủ công của OPS | Cách đo, đo trước CP2 | Mục 3.2, 5 |
| Ngưỡng đo trên simulator tự thiết kế dễ tự khớp | Seed test riêng, kịch bản khoá trước, lưu lượng theo số thật, đo trên tập test mới; đo lại trên masked | Mục 7, 8 |
| Tài liệu quá dài | Kế hoạch (mục 1–4) gọn trong 2 trang; chi tiết vào phụ lục `OPS_AGENT_PLAN.md` | — |
