"""
전월세 캐시 매매 혼입 검증 — 9개 단지 전체 Sanity Check + P90 이상치 샘플.

실행: python scripts/verify_rent_sanity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from data_service import load_cached_data, parse_targets, prepare_dashboard_data
from rent_service import (
    RENT_SALE_LEAK_MAX_MANWON,
    load_cached_rent_data,
    prepare_rent_dashboard_data,
    purge_rent_sale_cross_contamination,
)

# app.add_outlier_flags import without Streamlit
from app import add_outlier_flags


def _eok(manwon: float | None) -> str:
    if manwon is None or pd.isna(manwon):
        return "N/A"
    return f"{float(manwon) / 10_000:.1f}억"


def main() -> int:
    targets = parse_targets(getattr(config, "TARGET_APARTMENTS", []))
    apt_names = list(getattr(config, "DASHBOARD_ALLOWED_COMPLEX_LABELS", []))

    sale_raw = load_cached_data()
    rent_raw = load_cached_rent_data()
    rent_clean = purge_rent_sale_cross_contamination(rent_raw, sale_raw)

    sale = prepare_dashboard_data(sale_raw, targets)
    rent = prepare_rent_dashboard_data(rent_clean, targets)

    print("=" * 60)
    print("  전월세 Sanity Check (9개 단지)")
    print("=" * 60)
    print(f"  매매 raw: {len(sale_raw):,}건 | 전월세 raw: {len(rent_raw):,}건")
    print(f"  purge 후 전월세: {len(rent_clean):,}건")
    print("-" * 60)

    failed = False
    for apt in apt_names:
        r = rent[rent["타겟명"] == apt] if "타겟명" in rent.columns else rent.iloc[0:0]
        s = sale[sale["타겟명"] == apt] if "타겟명" in sale.columns else sale.iloc[0:0]

        rmax = float(r["환산보증금(만원)"].max()) if len(r) else None
        smax = float(s["거래금액(만원)"].max()) if len(s) else None

        flag = ""
        if rmax is not None and rmax > RENT_SALE_LEAK_MAX_MANWON:
            flag = " [FAIL: 40억+ 전월세]"
            failed = True
        elif rmax and smax and rmax >= smax * 0.95:
            flag = " [FAIL: 매매가와 유사]"
            failed = True
        elif len(r) == 0:
            flag = " [WARN: 전월세 데이터 없음]"

        print(f"[검증] {apt} 전월세 최고가: {_eok(rmax)} | 매매 최고: {_eok(smax)}{flag}")

    print("-" * 60)
    over = rent_clean[
        pd.to_numeric(rent_clean.get("환산보증금(만원)"), errors="coerce")
        > RENT_SALE_LEAK_MAX_MANWON
    ]
    print(f"  전체 40억+ 전월세 행: {len(over)}건")

    if not rent.empty:
        flagged = add_outlier_flags(rent, is_rent=True)
        outlier_n = int(flagged["is_outlier"].fillna(False).sum())
        print(f"  P90 이상치(임대세대) 플래그: {outlier_n}건 / {len(flagged):,}건")
        wb = flagged[
            (flagged["타겟명"] == "원베일리") & (flagged["평형그룹"] == "24평형")
        ]
        if len(wb):
            normal = wb[~wb["is_outlier"].fillna(False)]
            print(
                f"  원베일리 24평 정상 전세 거래: {len(normal)}건 "
                f"(회색 제외, 최고 {_eok(normal['환산보증금(만원)'].max() if len(normal) else None)})"
            )

    print("=" * 60)
    if failed:
        print("  RESULT: FAIL - rent data contamination suspected")
        return 1
    print("  RESULT: PASS - rent data clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
