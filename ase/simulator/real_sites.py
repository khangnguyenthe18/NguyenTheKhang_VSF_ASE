"""Toạ độ tủ đổi pin VinFast thật (VN) cho trạm mô phỏng của profile `demo`.

Vì sao: dashboard hiển thị lớp 43.297 tủ thật; nếu trạm mô phỏng chỉ đặt quanh tâm thành phố thì
trên bản đồ không trùng điểm nào. Profile bật `real_sites` lấy mẫu tủ thật theo tỷ lệ số tủ từng
tỉnh, gán toạ độ + mã tủ (`site_code`) cho trạm VN. Chỉ dùng để trình diễn; file nguồn không nằm
trong repo nên thiếu file thì quay về cách đặt theo thành phố.
"""

from __future__ import annotations

import gzip
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import orjson

REAL_SITES_PATH = (
    Path(__file__).resolve().parents[1] / "dashboard" / "static" / "vinfast_bss.json.gz"
)
_CODE_RE = re.compile(r"^[A-Z]+\.([A-Z]+)\d+$")


@dataclass(frozen=True)
class RealSite:
    code: str
    name: str
    lat: float
    lon: float
    province: str  # mã tỉnh trong mã tủ, vd `B.HCM01234` → `HCM`


def load_real_sites(path: Path = REAL_SITES_PATH) -> list[RealSite] | None:
    """Đọc `[lat, lon, code, name, addr, (country)]`, CHỈ giữ tủ VN; None nếu thiếu file.

    File dashboard có thể chứa thêm nước khác (cột thứ 6); dòng không có cột này là tủ VN.
    Sắp theo mã để deterministic.
    """
    if not path.exists():
        return None
    rows = orjson.loads(gzip.decompress(path.read_bytes()))
    sites: list[RealSite] = []
    for lat, lon, code, name, *rest in rows:
        country = str(rest[1]).upper() if len(rest) > 1 and rest[1] else "VN"
        if country != "VN":
            continue
        m = _CODE_RE.match(str(code))
        sites.append(
            RealSite(
                code=str(code),
                name=str(name),
                lat=float(lat),
                lon=float(lon),
                province=m.group(1) if m else "?",
            )
        )
    sites.sort(key=lambda s: s.code)
    return sites


def allocate(counts: dict[str, int], n: int) -> dict[str, int]:
    """Chia `n` trạm cho các tỉnh theo tỷ lệ số tủ (largest remainder), hoà thì theo mã tỉnh."""
    total = sum(counts.values())
    if n <= 0 or total == 0:
        return {}
    quotas = {p: n * c / total for p, c in counts.items()}
    alloc = {p: int(q) for p, q in quotas.items()}
    rest = n - sum(alloc.values())
    for p in sorted(quotas, key=lambda p: (-(quotas[p] - alloc[p]), p))[:rest]:
        alloc[p] += 1
    return {p: k for p, k in alloc.items() if k > 0}


def sample_sites(sites: list[RealSite], n: int, rng: random.Random) -> list[RealSite]:
    """Lấy `n` tủ khác nhau, phân tầng theo tỉnh; thứ tự: tỉnh nhiều tủ trước."""
    if n > len(sites):
        raise ValueError(f"cần {n} trạm nhưng chỉ có {len(sites)} tủ thật")
    by_prov: dict[str, list[RealSite]] = defaultdict(list)
    for s in sites:
        by_prov[s.province].append(s)
    alloc = allocate({p: len(v) for p, v in by_prov.items()}, n)
    out: list[RealSite] = []
    for p in sorted(alloc, key=lambda p: (-len(by_prov[p]), p)):
        out.extend(rng.sample(by_prov[p], alloc[p]))
    return out
