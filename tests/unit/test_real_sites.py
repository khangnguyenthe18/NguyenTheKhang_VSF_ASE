"""Lấy mẫu tủ đổi pin thật cho trạm `demo` (ase/simulator/real_sites.py)."""

from __future__ import annotations

import gzip
from pathlib import Path

import orjson
import pytest

from ase.knowledge.loader import load_knowledge
from ase.simulator.entities import build_world
from ase.simulator.profiles import load_profiles
from ase.simulator.real_sites import (
    REAL_SITES_PATH,
    RealSite,
    allocate,
    load_real_sites,
    sample_sites,
)
from ase.simulator.rng import substream


def _sites() -> list[RealSite]:
    out = [RealSite(f"B.HCM{i:05d}", "x", 10.0, 106.0, "HCM") for i in range(60)]
    out += [RealSite(f"B.HNO{i:05d}", "x", 21.0, 105.0, "HNO") for i in range(30)]
    out += [RealSite(f"B.CTO{i:05d}", "x", 10.0, 105.0, "CTO") for i in range(10)]
    return out


def test_allocate_proportional_and_exact_total() -> None:
    assert allocate({"HCM": 60, "HNO": 30, "CTO": 10}, 10) == {"HCM": 6, "HNO": 3, "CTO": 1}
    alloc = allocate({"A": 5, "B": 3, "C": 1}, 4)
    assert sum(alloc.values()) == 4 and alloc["A"] >= alloc["B"] >= alloc.get("C", 0)


def test_sample_sites_distinct_deterministic_stratified() -> None:
    a = sample_sites(_sites(), 10, substream(1, "real_sites"))
    b = sample_sites(_sites(), 10, substream(1, "real_sites"))
    assert a == b
    assert len({s.code for s in a}) == 10
    assert [s.province for s in a] == ["HCM"] * 6 + ["HNO"] * 3 + ["CTO"]


def test_sample_sites_too_many() -> None:
    with pytest.raises(ValueError):
        sample_sites(_sites(), 101, substream(1, "x"))


def test_load_real_sites_parses_province(tmp_path: Path) -> None:
    p = tmp_path / "bss.json.gz"
    rows = [
        [10.1, 106.2, "B.HCM00002", "TĐP b", "a"],
        [21.0, 105.8, "B.HNO00001", "TĐP a", "a"],
        [-6.3, 106.7, "B.ID2910", "Depot", "a", "ID"],  # nước khác: bỏ qua
        [14.6, 121.0, "B.PHMNL10001", "x", "a", "PH"],
    ]
    p.write_bytes(gzip.compress(orjson.dumps(rows)))
    sites = load_real_sites(p)
    assert sites is not None
    assert [(s.code, s.province) for s in sites] == [("B.HCM00002", "HCM"), ("B.HNO00001", "HNO")]
    assert load_real_sites(tmp_path / "khong-co.json.gz") is None


@pytest.mark.skipif(not REAL_SITES_PATH.exists(), reason="chưa có vinfast_bss.json.gz")
def test_demo_vn_stations_sit_on_real_cabinets() -> None:
    profiles = load_profiles()
    world = build_world(profiles.profiles["demo"], profiles.common, load_knowledge())
    real = {s.code: s for s in load_real_sites() or []}
    vn = [s for s in world.stations if s.country == "VN"]
    assert len(vn) == profiles.profiles["demo"].stations["VN"]
    for st in vn:
        assert st.site_code in real
        assert (st.lat, st.lon) == (real[st.site_code].lat, real[st.site_code].lon)
    assert all(s.site_code is None for s in world.stations if s.country != "VN")


def test_profiles_without_real_sites_unchanged() -> None:
    profiles = load_profiles()
    world = build_world(profiles.profiles["dev"], profiles.common, load_knowledge())
    assert all(s.site_code is None for s in world.stations)
