"""Sub-stream RNG: thêm scenario/bảng mới không làm đổi traffic (MOCK_PLAN §3.4).

Mỗi nguồn ngẫu nhiên có khoá riêng, nên số lần gọi của stream này không ảnh hưởng stream khác.
"""

from __future__ import annotations

import hashlib
import random


def substream(seed: int, *keys: object) -> random.Random:
    digest = hashlib.sha256(":".join([str(seed), *map(str, keys)]).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))
