"""Quick verification for 신현대 34평 integration."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

import config
from app import _format_pyeong_for_apt, _labels_for_34_pyeong_master
from data_service import (
    assign_pyeong_group_for_cache,
    filter_by_targets,
    is_allowed_area_m2,
    is_sinhyundai_apartment,
)


def main() -> None:
    df = pd.read_csv("data.csv")
    if "아파트" not in df.columns and "아파트명" in df.columns:
        df = df.rename(columns={"아파트명": "아파트"})

    matched = df[
        df.apply(
            lambda r: is_sinhyundai_apartment(r.get("법정동", "압구정동"), r["아파트"]),
            axis=1,
        )
    ]
    print(f"is_sinhyundai_apartment rows: {len(matched)}")

    filtered = filter_by_targets(df, config.TARGET_APARTMENTS)
    sh = filtered[filtered["아파트"] == "신현대"].copy()
    print(f"filter_by_targets 신현대 rows: {len(sh)}")

    sh["평형그룹"] = sh.apply(
        lambda r: assign_pyeong_group_for_cache(
            r["전용면적(㎡)"],
            dong=r["법정동"],
            apt=r["아파트"],
        ),
        axis=1,
    )
    kept = sh[sh["평형그룹"].notna()]
    print(f"With assigned pyeong: {len(kept)}")
    print(kept[["아파트", "전용면적(㎡)", "평형그룹"]].drop_duplicates().to_string(index=False))
    assert (kept["평형그룹"] == "34평형").all()
    assert kept["전용면적(㎡)"].between(107.0, 109.0).all()

    for _, row in kept.iterrows():
        assert is_allowed_area_m2(
            row["전용면적(㎡)"], "34평형", dong=row["법정동"], apt=row["아파트"]
        )

    assert _format_pyeong_for_apt("신현대", "34평형") == "34평"

    picked = _labels_for_34_pyeong_master(
        ["신현대 (34평형)", "원베일리 (34평형)", "개포우성 1,2차 (24평형)"]
    )
    assert "신현대 (34평형)" in picked

    print("All checks passed.")


if __name__ == "__main__":
    main()
