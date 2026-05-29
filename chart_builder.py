"""
실거래가 차트 — 개별 거래를 시간순으로 연결한 꺾은선 그래프.
입력: prepare_raw_chart_data() 가 만든 실거래 DataFrame
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Iterable

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

MANWON_PER_EOK = 10_000
X_COL = "계약일자_표시"
LINE_WIDTH = 1.5
MARKER_SIZE = 4


def _manwon_to_eok_str(manwon: float) -> str:
    return f"{float(manwon) / MANWON_PER_EOK:.1f}억"


def _format_contract_date_short(contract_ymd: object) -> str:
    """YYYYMMDD → '22.04.09'"""
    text = str(contract_ymd).strip()
    if len(text) >= 8 and text[:8].isdigit():
        y, m, d = text[:4], text[4:6], text[6:8]
        return f"{y[2:]}.{m}.{d}"
    ts = pd.Timestamp(contract_ymd)
    return f"{ts.year % 100:02d}.{ts.month:02d}.{ts.day:02d}"


def _format_eok_label(manwon: float) -> str:
    eok = manwon / MANWON_PER_EOK
    if abs(eok - round(eok)) < 0.05:
        return f"{int(round(eok))}억"
    return f"{eok:.1f}억"


def _yaxis_ticks_eok(y_series: pd.Series) -> tuple[list[float], list[str], float, float]:
    y_min = float(y_series.min())
    y_max = float(y_series.max())
    span = max(y_max - y_min, MANWON_PER_EOK)

    if span <= 30_000:
        step = 5_000
    elif span <= 80_000:
        step = 10_000
    elif span <= 200_000:
        step = 50_000
    else:
        step = 100_000

    tick_start = math.floor(y_min / step) * step
    tick_end = math.ceil(y_max / step) * step
    tickvals = list(range(int(tick_start), int(tick_end) + 1, int(step)))

    while len(tickvals) > 10 and step < 500_000:
        step *= 2
        tick_start = math.floor(y_min / step) * step
        tick_end = math.ceil(y_max / step) * step
        tickvals = list(range(int(tick_start), int(tick_end) + 1, int(step)))

    ticktext = [_format_eok_label(v) for v in tickvals]
    pad = step * 0.15
    return tickvals, ticktext, tick_start - pad, tick_end + pad


def build_price_chart(
    chart_df: pd.DataFrame,
    selected_labels: Iterable[str],
    y_axis_title: str = "거래금액",
    chart_height: int = 600,
    label_formatter: Callable[[str], str] | None = None,
) -> go.Figure:
    """
    chart_df: 실거래 원본 (차트라벨, 계약일자_표시, 거래금액(만원), 계약일자)
    시리즈별로 날짜순 정렬 후 개별 거래를 연결한 꺾은선 1개.
    """
    labels = list(selected_labels)
    fmt = label_formatter or (lambda s: s)

    if chart_df.empty or X_COL not in chart_df.columns:
        fig = go.Figure()
        fig.update_layout(
            title="선택한 단지(평형)에 해당하는 거래 데이터가 없습니다.",
            template="plotly_white",
            height=chart_height,
        )
        return fig

    plot_df = chart_df.copy()
    plot_df[X_COL] = pd.to_datetime(plot_df[X_COL])
    plot_df = plot_df.dropna(subset=["거래금액(만원)", X_COL])

    fig = go.Figure()
    palette = px.colors.qualitative.Plotly

    for idx, label in enumerate(labels):
        sub = plot_df.loc[plot_df["차트라벨"] == label].sort_values(X_COL)
        if sub.empty:
            continue
        color = palette[idx % len(palette)]
        display_name = fmt(label)

        hover_dates = sub["계약일자"].map(_format_contract_date_short)
        hover_prices = sub["거래금액(만원)"].map(_manwon_to_eok_str)

        fig.add_trace(
            go.Scatter(
                x=sub[X_COL],
                y=sub["거래금액(만원)"],
                mode="lines+markers",
                name=display_name,
                line=dict(width=LINE_WIDTH, color=color),
                marker=dict(
                    size=MARKER_SIZE,
                    color=color,
                    line=dict(width=0.5, color="white"),
                ),
                customdata=list(zip(hover_dates, hover_prices)),
                hovertemplate=(
                    f"<b>{display_name}</b><br>"
                    "거래일: %{customdata[0]}<br>"
                    "금액: %{customdata[1]}<extra></extra>"
                ),
            )
        )

    tickvals, ticktext, y_lo, y_hi = _yaxis_ticks_eok(plot_df["거래금액(만원)"])

    fig.update_layout(
        template="plotly_white",
        hovermode="closest",
        title=None,
        yaxis=dict(
            title=y_axis_title,
            tickmode="array",
            tickvals=tickvals,
            ticktext=ticktext,
            range=[y_lo, y_hi],
            showgrid=True,
            gridcolor="#b8c4d4",
            griddash="dash",
            gridwidth=1.2,
            zeroline=False,
        ),
        legend=dict(
            title=dict(text="단지 (평형)"),
            orientation="h",
            yanchor="bottom",
            y=1.03,
            xanchor="left",
            x=0,
            font=dict(size=11),
        ),
        margin=dict(l=48, r=24, t=56, b=88),
        height=chart_height,
        plot_bgcolor="#ffffff",
        paper_bgcolor="#ffffff",
    )
    fig.update_xaxes(
        title=dict(text="계약일"),
        tickformat="%Y-%m",
        showgrid=True,
        gridcolor="#eef2f7",
        rangeslider_visible=True,
        rangeslider=dict(thickness=0.08, bgcolor="#f1f5f9"),
    )
    fig.update_yaxes(showspikes=False)
    return fig
