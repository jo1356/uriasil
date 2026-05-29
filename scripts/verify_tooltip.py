"""통합 툴팁·nearest 타임라인 검증."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from app import NEAREST_TOLERANCE_DAYS, prepare_chart_comparison_data
from app import prepare_raw_chart_data
from chart_builder import build_price_chart
from data_service import load_cached_data, parse_targets, prepare_dashboard_data
import config


def test_nearest_fills_missing_on_master_date() -> None:
    """기준일에 A만 거래돼도 B는 nearest로 툴팁에 포함."""
    raw = pd.DataFrame(
        [
            {
                "차트라벨": "A (24평형)",
                "계약일자_표시": "2020-01-15",
                "거래금액(만원)": 300_000,
                "계약일자": "20200115",
            },
            {
                "차트라벨": "B (34평형)",
                "계약일자_표시": "2020-04-20",
                "거래금액(만원)": 400_000,
                "계약일자": "20200420",
            },
        ]
    )
    aligned = prepare_chart_comparison_data(raw, ["A (24평형)", "B (34평형)"])
    jan = aligned[aligned["기준일"] == pd.Timestamp("2020-01-15")]
    labels_on_jan = set(jan["차트라벨"])
    assert "A (24평형)" in labels_on_jan
    assert "B (34평형)" in labels_on_jan, "B should appear via nearest on A's trade date"

    b_row = jan[jan["차트라벨"] == "B (34평형)"].iloc[0]
    gap = abs((b_row["기준일"] - b_row["실제거래일_표시"]).days)
    assert gap <= NEAREST_TOLERANCE_DAYS


def test_tolerance_excludes_far_trades() -> None:
    raw = pd.DataFrame(
        [
            {
                "차트라벨": "A (24평형)",
                "계약일자_표시": "2020-01-01",
                "거래금액(만원)": 300_000,
                "계약일자": "20200101",
            },
            {
                "차트라벨": "B (34평형)",
                "계약일자_표시": "2021-01-01",
                "거래금액(만원)": 400_000,
                "계약일자": "20210101",
            },
        ]
    )
    aligned = prepare_chart_comparison_data(raw, ["A (24평형)", "B (34평형)"])
    jan = aligned[aligned["기준일"] == pd.Timestamp("2020-01-01")]
    assert "B (34평형)" not in set(jan["차트라벨"]), "B is >180d away from 2020-01-01"


def test_chart_with_real_cache() -> None:
    raw = load_cached_data()
    if raw.empty:
        print("SKIP chart: no cache")
        return
    targets = parse_targets(getattr(config, "TARGET_APARTMENTS", []))
    prep = prepare_dashboard_data(raw, targets)
    labels = prep["차트라벨"].dropna().unique().tolist()[:4]
    raw_chart = prepare_raw_chart_data(prep, labels)
    assert not raw_chart.empty
    assert len(raw_chart) >= len(labels), "each series should have at least one point"
    fig = build_price_chart(raw_chart, labels)
    assert fig.layout.hovermode == "closest"
    lines = [t for t in fig.data if "lines" in (t.mode or "")]
    assert len(lines) >= 1
    assert fig.layout.legend.title.text == "단지 (평형)"
    assert len(fig.data) == len(lines), "one line trace per series, no extra trend traces"
    print("chart OK: raw points =", len(raw_chart), "line traces =", len(lines))


if __name__ == "__main__":
    test_nearest_fills_missing_on_master_date()
    print("nearest fill OK")
    test_tolerance_excludes_far_trades()
    print("tolerance OK")
    test_chart_with_real_cache()
