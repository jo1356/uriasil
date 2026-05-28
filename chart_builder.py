"""
실거래가 라인 차트 — 통합 툴팁(세로선 + 단일 말풍선) 전용 빌더.
입력: app.prepare_chart_comparison_data() 가 만든 정렬·nearest 매핑 DataFrame
"""

from __future__ import annotations

import math
from typing import Iterable

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

MANWON_PER_EOK = 10_000
HOVER_LINE_SEP = " / "
X_COL = "기준일"


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


def _format_hover_line(
    label: str,
    manwon: float,
    contract_ymd: object,
    pct: int,
) -> str:
    return HOVER_LINE_SEP.join(
        [
            label,
            _manwon_to_eok_str(manwon),
            _format_contract_date_short(contract_ymd),
            f"{pct}%",
        ]
    )


def _build_hover_block_for_timeline(day_df: pd.DataFrame) -> str:
    """기준일(row)마다 nearest로 모인 단지들 — 고액순·최고가 대비 % 재계산."""
    valid = day_df.dropna(subset=["거래금액(만원)"])
    if valid.empty:
        return ""

    max_manwon = float(valid["거래금액(만원)"].max())
    if max_manwon <= 0:
        return ""

    rows = valid.sort_values("거래금액(만원)", ascending=False)
    lines: list[str] = []
    for _, row in rows.iterrows():
        manwon = float(row["거래금액(만원)"])
        pct = int(round(manwon / max_manwon * 100))
        contract = row.get("계약일자") or row.get("실제거래일_표시")
        lines.append(
            _format_hover_line(str(row["차트라벨"]), manwon, contract, pct)
        )
    return "<br>".join(lines)


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
    aligned_df: pd.DataFrame,
    selected_labels: Iterable[str],
    y_axis_title: str = "거래금액",
    chart_height: int = 600,
) -> go.Figure:
    """
    aligned_df: prepare_chart_comparison_data() 결과
      - 기준일: 마스터 타임라인 X
      - 실제거래일_표시 / 계약일자: nearest로 매칭된 실제 거래
      - 거래금액(만원): 해당 시점 비교용 가격
    """
    labels = list(selected_labels)
    chart_df = aligned_df.copy()

    if chart_df.empty or X_COL not in chart_df.columns:
        fig = go.Figure()
        fig.update_layout(
            title="선택한 단지(평형)에 해당하는 거래 데이터가 없습니다.",
            template="plotly_white",
            height=chart_height,
        )
        return fig

    chart_df[X_COL] = pd.to_datetime(chart_df[X_COL])
    chart_df = chart_df.dropna(subset=["거래금액(만원)", X_COL])

    fig = go.Figure()
    palette = px.colors.qualitative.Plotly

    for idx, label in enumerate(labels):
        sub = chart_df[chart_df["차트라벨"] == label].sort_values(X_COL)
        if sub.empty:
            continue
        color = palette[idx % len(palette)]
        fig.add_trace(
            go.Scatter(
                x=sub[X_COL],
                y=sub["거래금액(만원)"],
                mode="lines+markers",
                name=label,
                hoverinfo="skip",
                line=dict(width=1.5, color=color),
                marker=dict(size=5, color=color, line=dict(width=0.5, color="white")),
            )
        )

    anchor_x: list[pd.Timestamp] = []
    anchor_y: list[float] = []
    anchor_blocks: list[str] = []

    for ref_date, day_df in chart_df.groupby(X_COL, sort=True):
        block = _build_hover_block_for_timeline(day_df)
        if not block:
            continue
        anchor_x.append(pd.Timestamp(ref_date))
        anchor_y.append(float(day_df["거래금액(만원)"].max()))
        anchor_blocks.append(block)

    if anchor_x:
        fig.add_trace(
            go.Scatter(
                x=anchor_x,
                y=anchor_y,
                mode="markers",
                name="",
                showlegend=False,
                customdata=[[b] for b in anchor_blocks],
                hovertemplate="%{customdata[0]}<extra></extra>",
                marker=dict(size=16, opacity=0, color="rgba(0,0,0,0)"),
                hoverlabel=dict(
                    align="left",
                    bgcolor="#ffffff",
                    bordercolor="#d1d5db",
                    font=dict(size=12, color="#1f2937"),
                    namelength=0,
                ),
            )
        )

    tickvals, ticktext, y_lo, y_hi = _yaxis_ticks_eok(chart_df["거래금액(만원)"])

    fig.update_layout(
        template="plotly_white",
        hovermode="x unified",
        hoverdistance=80,
        spikedistance=1000,
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
            title="단지 (평형)",
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
        title="",
        tickformat="%Y-%m",
        showgrid=True,
        gridcolor="#eef2f7",
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        spikecolor="#64748b",
        spikethickness=1,
        spikedash="solid",
        rangeslider_visible=True,
        rangeslider=dict(thickness=0.08, bgcolor="#f1f5f9"),
    )
    fig.update_yaxes(showspikes=False)
    return fig
