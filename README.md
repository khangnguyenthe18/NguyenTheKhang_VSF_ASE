# ASE – AI Agent hỗ trợ vận hành đổi pin (mock VN)

Agent đọc **Danh sách giao dịch đổi pin** (read-only), quét 30 phút/lần, gộp giao dịch lỗi thành sự cố, gửi 1 mail + 1 card Teams mỗi lần quét cho đội OPS, và lập báo cáo tuần/tháng: KPI success factor, Top N lỗi, health score dịch vụ critical, open point. Dữ liệu là mock do simulator sinh (có ground truth); không kết nối hệ thống thật. LLM (khi bật) chỉ dùng V-LLM nội bộ.

## Cài đặt

Cần Python 3.12, [`uv`](https://docs.astral.sh/uv/), Docker (cho Mailpit).

```bash
uv sync
cp .env.example .env          # mặc định: Mailpit local, rule-only (ASE_LLM_PROVIDER=none)
make check                    # ruff + mypy strict + pytest
```

## Chạy lại số đo trong báo cáo (khoảng 1 phút)

```bash
make sim PROFILE=eval             # sinh dữ liệu mock (seed cố định → cùng số liệu)
make agent-eval PROFILE=eval      # quét 30 phút/lần + đo SF1–SF10 trên tập test
#   → data/eval/agent/eval/report.md
make report PROFILE=eval FROM=2026-10-01 TO=2026-10-31 TOP=5   # báo cáo tháng 10
#   → data/eval/reports/*.txt
```

Lưu lượng mock bám thực tế: ≈ 2,9 triệu giao dịch/tháng cho các điểm đổi pin VN, phân bổ theo tỉnh/thành (`txn_traffic` trong `ase/simulator/profiles.yaml`). Profile lấy mẫu điểm thật theo tỉnh: `smoke` (150 điểm, 1 ngày, CI), `dev` (450 điểm, 14 ngày, chỉnh rule), `eval` (650 điểm, 30 ngày, 10 ngày cuối là tập test), `demo` (3.000 điểm, 3 ngày, trình diễn), `scale` (toàn quốc, 1 ngày ≈ 96 nghìn giao dịch, chỉ đo hiệu năng: `make sim PROFILE=scale && make agent PROFILE=scale`).

Demo lại từ đầu (sau khi Mailpit khởi động lại): `make notify-reset`. Xem card Teams: `http://localhost:8000/teams`.

## Xem mail và dashboard

```bash
make mail-up                              # Mailpit: http://localhost:8025
make agent PROFILE=smoke NOTIFY=1         # gửi mail + card cho từng lần quét có lỗi
make dashboard                            # http://localhost:8000 → Danh sách giao dịch đổi pin
```

## Cấu hình (không sửa code)

| File | Nội dung |
|---|---|
| `ase/knowledge/error_catalog_v1.yaml` | Bảng mã lỗi chuẩn (chỉ đọc, giữ nguyên văn) |
| `ase/knowledge/detector.yaml` (khối `scan`) | Nhịp quét, ngưỡng kẹt, ngưỡng gộp sự cố |
| `ase/knowledge/kpi.yaml` | KPI, health score, dịch vụ critical, open point, lịch báo cáo |
| `ase/simulator/txn_scenarios.yaml` | Kịch bản mock |
| `contracts/` | Pair contract v0.1 + schema sự kiện chung với đối tác |
