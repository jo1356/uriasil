"""초정밀 ㎡ 평형 필터 및 24/34 격리 검증."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from data_service import (
    AREA_M2_STRICT_RULES,
    assign_pyeong_group_from_m2,
    load_cached_data,
    parse_targets,
    prepare_dashboard_data,
)
import config


def test_assign_boundaries() -> None:
    assert assign_pyeong_group_from_m2(57.0) == "24평형"
    assert assign_pyeong_group_from_m2(62.99) == "24평형"
    assert assign_pyeong_group_from_m2(63.0) is None
    assert assign_pyeong_group_from_m2(82.0) == "34평형"
    assert assign_pyeong_group_from_m2(86.99) == "34평형"
    for m2 in (70.0, 72.0, 74.0, 73.5):
        assert assign_pyeong_group_from_m2(m2) is None


def test_cache_and_dashboard_isolation() -> None:
    raw = load_cached_data()
    assert not raw.empty, "cache missing"
    if "평형그룹" in raw.columns:
        m2 = pd.to_numeric(raw["전용면적(㎡)"], errors="coerce")
        bad = raw["평형그룹"] != m2.apply(assign_pyeong_group_from_m2)
        assert not bad.any(), f"cache mismatch {bad.sum()} rows"

    targets = parse_targets(config.TARGET_APARTMENTS)
    prep = prepare_dashboard_data(raw, targets)
    m2 = pd.to_numeric(prep["전용면적(㎡)"], errors="coerce")

    bad24 = prep[(prep["평형그룹"] == "24평형") & ~((m2 >= 57.0) & (m2 < 63.0))]
    bad34 = prep[(prep["평형그룹"] == "34평형") & ~((m2 >= 82.0) & (m2 < 87.0))]
    assert bad24.empty, f"84 in 24pyeong: {len(bad24)}"
    assert bad34.empty, f"59 in 34pyeong: {len(bad34)}"

    mid = prep[(m2 >= 70.0) & (m2 < 74.0)]
    assert mid.empty, f"32pyeong band remains: {len(mid)}"

    for g in ("24평형", "34평형"):
        sub = prep[prep["평형그룹"] == g]
        print(f"{g}: n={len(sub)} m2 {m2[sub.index].min():.2f}-{m2[sub.index].max():.2f}")

    leaders = prep[prep["차트라벨"].str.contains("리더스원", na=False)]
    l24 = leaders[leaders["평형그룹"] == "24평형"]
    l34 = leaders[leaders["평형그룹"] == "34평형"]
    if len(l24):
        assert l24["전용면적(㎡)"].between(57, 63).all()
    if len(l34):
        assert l34["전용면적(㎡)"].between(82, 87).all()
    print(f"leaders 24={len(l24)} 34={len(l34)} rules={AREA_M2_STRICT_RULES}")


if __name__ == "__main__":
    test_assign_boundaries()
    print("boundaries OK")
    test_cache_and_dashboard_isolation()
    print("ALL OK")
