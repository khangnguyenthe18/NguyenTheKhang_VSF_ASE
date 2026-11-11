# Định dạng Danh sách giao dịch đổi pin (mock) — cho điểm tích hợp I1

Kèm `PAIR_CONTRACT_v0.1.md`. Mục đích: để log của đối tác dùng **cùng mã trạm / tủ / connector / giao dịch và cùng thời gian** với danh sách giao dịch. File này chỉ mô tả định dạng; kịch bản mock của mỗi bên do bên đó tự làm.

**File:** `data/<profile>/swap_transactions.parquet` (bản JSONL: `swap_transactions.jsonl`, mỗi dòng 1 giao dịch), sinh bằng `make sim PROFILE=<profile>`. Profile dùng chung: `eval` (VN, 11 trạm, 30 ngày từ 05/10/2026, seed 2042). Mọi thời gian là UTC (ISO-8601); giờ hiển thị theo `timezone` của trạm.

| Cột | Kiểu | Ví dụ | Ghi chú |
|---|---|---|---|
| `station_id` | chuỗi | `B.HCM24156` | Mã trạm `B.<TỈNH><5 số>` |
| `cabinet_id` | chuỗi | `B.HCM24156.002` | Mã tủ = mã trạm + `.<3 số>` |
| `cabinet_serial` | chuỗi | `BSS-30319410305241` | S/N tủ |
| `cabinet_type` | chuỗi | `CC_06_P` / `CC_12_P` | 6 hoặc 12 connector |
| `emsp_txn_id` | chuỗi 24 hex | `7cda8cd78a1e2243cd402062` | **Khoá chính**, dùng làm `related_txn_ids` trong schema chung |
| `cabinet_txn_id` | chuỗi số | `12014103` | Mã giao dịch trên tủ |
| `started_at` | thời gian UTC | `2026-10-18T23:46:32Z` | "Ngày thực hiện" |
| `status` | `CREATED` · `BATTERY_IN` · `COMPLETED` · `FAILED` | | Tạo mới · Đưa pin vào · Hoàn thành · Thất bại (trạng thái cuối) |
| `error_code` | chuỗi / rỗng | `D6031` | Lý do lỗi, mã của bảng mã lỗi chuẩn; chỉ có khi `FAILED` |
| `slot_in`, `slot_out` | chuỗi | `8` | Connector pin cũ / pin mới |
| `battery_in_id`, `battery_out_id` | chuỗi | `BAT…` (32 ký tự) | S/N pin cũ / pin mới |
| `soh_in`, `soc_in`, `soh_out`, `soc_out` | số 0–100 | | |
| `battery_in_at`, `battery_out_at` | thời gian UTC | | Thời gian pin vào / pin ra |
| `updated_at` | thời gian UTC | | Lần đổi trạng thái cuối |
| `country`, `timezone` | chuỗi | `VN`, `Asia/Ho_Chi_Minh` | |
| `user_id` | chuỗi băm | `u_cb4fa5b26d71` | Không chứa thông tin cá nhân |

**Khớp với log (I1):** mỗi dòng log liên quan tới giao dịch nên có `emsp_txn_id` (hoặc `cabinet_txn_id`), `station_id`, `cabinet_id` và thời gian UTC nằm trong khoảng `started_at` … `updated_at` của giao dịch đó. Log không gắn với giao dịch (vd VinSOC scan, sụt traffic theo khung giờ) chỉ cần `station_id` + thời gian.
