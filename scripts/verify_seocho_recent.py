"""Verify Seocho/Songpa rent rows for 2025-2026."""
from __future__ import annotations

import pandas as pd

from config import DASHBOARD_ALLOWED_COMPLEX_LABELS
from rent_service import load_cached_rent_data

SEOCHO = "11650"
SONGPA = "11710"

TARGETS = [
    "원베일리",
    "퍼스티지",
    "아크로리버파크",
    "래미안리더스원",
    "잠실주공5단지",
    "개포우성 1,2차",
]


def main() -> None:
    df = load_cached_rent_data()
    if df.empty:
        print("rent_data.csv is empty")
        return

    date_col = "계약일자" if "계약일자" in df.columns else None
    ym_col = "조회계약년월" if "조회계약년월" in df.columns else None
    apt_col = "아파트" if "아파트" in df.columns else "단지명"

    print(f"total rent rows: {len(df):,}")
    print(f"allowed labels in config: {len(DASHBOARD_ALLOWED_COMPLEX_LABELS)}")

    for lawd, region in [(SEOCHO, "서초구"), (SONGPA, "송파구")]:
        sub = df[df["조회지역코드"].astype(str) == lawd]
        print(f"\n[{region} {lawd}] rows={len(sub):,}")
        if ym_col:
            recent = sub[sub[ym_col].astype(str).str[:4].isin(["2025", "2026"])]
            print(f"  2025-2026 by 조회계약년월: {len(recent):,}")
        if date_col:
            dates = pd.to_datetime(sub[date_col], errors="coerce")
            recent_d = sub[dates.dt.year.isin([2025, 2026])]
            print(f"  2025-2026 by 계약일자: {len(recent_d):,}")
            if not recent_d.empty:
                print(f"  max contract date: {dates.max()}")

    print("\n[Target complexes 2025-2026]")
    for name in TARGETS:
        mask = df[apt_col].astype(str).str.contains(name.replace(" ", ""), na=False) | df[
            apt_col
        ].astype(str).str.contains(name.split()[0], na=False)
        sub = df[mask]
        if sub.empty:
            print(f"  {name}: NO DATA")
            continue
        if date_col:
            dates = pd.to_datetime(sub[date_col], errors="coerce")
            r25 = sub[dates.dt.year.isin([2025, 2026])]
            print(
                f"  {name}: total={len(sub):,}, 2025-2026={len(r25):,}, "
                f"max={dates.max().date() if dates.notna().any() else 'N/A'}, "
                f"region={sub['조회지역코드'].astype(str).unique().tolist()}"
            )
        else:
            print(f"  {name}: total={len(sub):,}")


if __name__ == "__main__":
    main()
