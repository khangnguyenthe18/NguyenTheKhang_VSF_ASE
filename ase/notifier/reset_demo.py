"""`python -m ase.notifier.reset_demo`: xoá trạng thái "đã gửi" của agent để chạy demo lại.

Vì sao cần: outbox chống gửi trùng theo `scan:*` / `report:*`. Khi Mailpit khởi động lại (mất thư)
hoặc muốn demo lại, chạy lại agent sẽ bỏ qua mọi lần quét đã gửi. Lệnh này chỉ xoá các dòng outbox
`scan:*`, `report:*` và card kênh `ops` của mock Teams; không đụng dữ liệu nào khác.
"""

from __future__ import annotations

import sqlite3

from ase.common.config import get_settings


def main() -> None:
    db = get_settings().notifier_db
    if not db.exists():
        print(f"chưa có {db}: không có gì để xoá")
        return
    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        n_out = n_cards = 0
        if "outbox" in tables:
            n_out = conn.execute(
                "DELETE FROM outbox WHERE incident_id LIKE 'scan:%' OR incident_id LIKE 'report:%'"
            ).rowcount
        if "cards" in tables:
            n_cards = conn.execute("DELETE FROM cards WHERE channel = 'ops'").rowcount
        conn.commit()
    print(f"đã xoá {n_out} dòng outbox (scan/report) và {n_cards} card kênh OPS trong {db}")


if __name__ == "__main__":
    main()
