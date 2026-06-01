"""Print Jamsil Jugong 5-danji sale/rent row counts (raw + dashboard)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

import config
from data_service import (
    is_jamsil_jugong5_apartment,
    load_cached_data,
    parse_targets,
    prepare_dashboard_data,
)
from rent_service import load_cached_rent_data


def _jamsil_mask(df: pd.DataFrame) -> pd.Series:
    if df.empty or "아파트" not in df.columns:
        return pd.Series(dtype=bool)
    dong = df.get("법정동", pd.Series("", index=df.index)).fillna("").astype(str)
    apt = df["아파트"].fillna("").astype(str)
    return pd.Series(
        [is_jamsil_jugong5_apartment(d, a) for d, a in zip(dong, apt, strict=False)],
        index=df.index,
    )


def main() -> None:
    targets = parse_targets(config.TARGET_APARTMENTS)
    sale_raw = load_cached_data()
    rent_raw = load_cached_rent_data()

    sale_j = sale_raw[_jamsil_mask(sale_raw)]
    rent_j = rent_raw[_jamsil_mask(rent_raw)]

    # flexible contains check (user-requested style)
    apt_col = "아파트"
    if apt_col in sale_raw.columns:
        loose_s = sale_raw[apt_col].astype(str).str.replace(" ", "", regex=False).str.contains(
            "잠실주공", na=False
        ) | sale_raw[apt_col].astype(str).str.replace(" ", "", regex=False).str.contains(
            "주공아파트5", na=False
        )
        loose_r = rent_raw[apt_col].astype(str).str.replace(" ", "", regex=False).str.contains(
            "잠실주공", na=False
        ) | rent_raw[apt_col].astype(str).str.replace(" ", "", regex=False).str.contains(
            "주공아파트5", na=False
        )
        print(f"loose match sale: {loose_s.sum()}, rent: {loose_r.sum()}")

    sale_dash = prepare_dashboard_data(sale_raw, targets)
    rent_dash = prepare_dashboard_data(rent_raw, targets)
    label = getattr(config, "JAMSIL_JUGONG5_LABEL", "잠실주공5단지")
    sd = sale_dash[sale_dash["타겟명"] == label] if "타겟명" in sale_dash.columns else sale_dash
    rd = rent_dash[rent_dash["타겟명"] == label] if "타겟명" in rent_dash.columns else rent_dash

    print(f"잠실주공5단지 매매: {len(sale_j):,}건 (대시보드: {len(sd):,}건)")
    print(f"잠실주공5단지 전월세: {len(rent_j):,}건 (대시보드: {len(rd):,}건)")
    print(f"송파구(11710) 전체 매매: {(sale_raw['조회지역코드'].astype(str)=='11710').sum():,}건")
    print(f"송파구(11710) 전체 전월세: {(rent_raw['조회지역코드'].astype(str)=='11710').sum():,}건")


if __name__ == "__main__":
    main()
