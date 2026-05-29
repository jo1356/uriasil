"""
실거래가 차트 — 개별 거래 꺾은선 + 통합 툴팁(세로선·단지 간 nearest 비교).
line_df: prepare_raw_chart_data() — 모든 개별 거래 연결
tooltip_df: prepare_chart_comparison_data() — 기준일별 통합 툴팁

Plotly는 동일 datetime X에 여러 점이 있으면 선·마커가 겹쳐 보일 수 있어,
전체 거래를 날짜순 정렬한 뒤 고유 순번(_x_idx)을 X축으로 사용한다.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Iterable

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

MANWON_PER_EOK = 10_000
HOVER_LINE_SEP = " / "
LINE_X_COL = "계약일자_표시"
SEQ_X_COL = "_x_idx"
TOOLTIP_X_COL = "기준일"
LINE_SORT_COLS = ("계약일자", "계약일자_표시")
LINE_WIDTH = 1.5
MARKER_SIZE = 5
MAX_X_TICKS = 12


def _sort_line_rows(df: pd.DataFrame) -> pd.DataFrame:
    sort_cols = [c for c in LINE_SORT_COLS if c in df.columns]
    if not sort_cols:
        return df.sort_values(LINE_X_COL, kind="mergesort")
    return df.sort_values(sort_cols, kind="mergesort")


def _assign_sequential_x(plot_df: pd.DataFrame) -> pd.DataFrame:
    """날짜순 정렬 후 거래마다 고유 X 순번 부여 (집계·병합 없음)."""
    out = _sort_line_rows(plot_df).reset_index(drop=True)
    out[SEQ_X_COL] = range(len(out))
    return out


def _format_xaxis_date_label(row: pd.Series) -> str:
    contract = row.get("계약일자")
    if contract is not None and not pd.isna(contract):
        return _format_contract_date_short(contract)
    ts = pd.Timestamp(row[LINE_X_COL])
    return f"{ts.year % 100:02d}.{ts.month:02d}.{ts.day:02d}"


def _build_xaxis_date_ticks(plot_df: pd.DataFrame) -> tuple[list[int], list[str]]:
    """X축 눈금 — 순번 위치에 실제 계약일 라벨."""
    n = len(plot_df)
    if n == 0:
        return [], []

    if n <= MAX_X_TICKS:
        indices = list(range(n))
    else:
        step = max(1, (n - 1) // (MAX_X_TICKS - 1))
        indices = list(range(0, n, step))
        if indices[-1] != n - 1:
            indices.append(n - 1)

    ticktext = [
        _format_xaxis_date_label(plot_df.iloc[i]) for i in indices
    ]
    return indices, ticktext


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


def _build_hover_block_for_timeline(
    day_df: pd.DataFrame,
    label_formatter: Callable[[str], str] | None = None,
) -> str:
    """기준일(row)마다 nearest로 모인 단지들 — 고액순·최고가 대비 % 재계산."""
    fmt = label_formatter or (lambda s: s)
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
            _format_hover_line(fmt(str(row["차트라벨"])), manwon, contract, pct)
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


def _tooltip_rows_for_trade(
    tooltip_df: pd.DataFrame,
    ref_date: pd.Timestamp,
) -> pd.DataFrame:
    if tooltip_df.empty or TOOLTIP_X_COL not in tooltip_df.columns:
        return pd.DataFrame()
    ref = pd.Timestamp(ref_date).normalize()
    dates = pd.to_datetime(tooltip_df[TOOLTIP_X_COL]).dt.normalize()
    return tooltip_df.loc[dates == ref]


def build_price_chart(
    line_df: pd.DataFrame,
    selected_labels: Iterable[str],
    y_axis_title: str = "거래금액",
    chart_height: int = 600,
    label_formatter: Callable[[str], str] | None = None,
    tooltip_df: pd.DataFrame | None = None,
) -> go.Figure:
    """
    line_df: 실거래 원본 — 날짜순 꺾은선 (집계 없음)
    tooltip_df: nearest 매핑 — 기준일별 통합 툴팁용

    X축은 Plotly datetime 겹침을 피하기 위해 거래 순번(0..N-1)을 사용하고,
    tick 라벨만 실제 계약일로 표시한다.
    """
    labels = list(selected_labels)
    fmt = label_formatter or (lambda s: s)

    if line_df.empty or LINE_X_COL not in line_df.columns:
        fig = go.Figure()
        fig.update_layout(
            title="선택한 단지(평형)에 해당하는 거래 데이터가 없습니다.",
            template="plotly_white",
            height=chart_height,
        )
        return fig

    plot_df = line_df.copy()
    plot_df[LINE_X_COL] = pd.to_datetime(plot_df[LINE_X_COL])
    plot_df = plot_df.dropna(subset=["거래금액(만원)", LINE_X_COL])
    plot_df = _assign_sequential_x(plot_df)

    if tooltip_df is not None and not tooltip_df.empty:
        hover_source = tooltip_df.copy()
        hover_source[TOOLTIP_X_COL] = pd.to_datetime(hover_source[TOOLTIP_X_COL])
    else:
        hover_source = None

    fig = go.Figure()
    palette = px.colors.qualitative.Plotly

    for idx, label in enumerate(labels):
        sub = plot_df.loc[plot_df["차트라벨"] == label].sort_values(SEQ_X_COL)
        if sub.empty:
            continue
        color = palette[idx % len(palette)]
        display_name = fmt(label)
        fig.add_trace(
            go.Scatter(
                x=sub[SEQ_X_COL],
                y=sub["거래금액(만원)"],
                mode="lines+markers",
                name=display_name,
                hoverinfo="skip",
                connectgaps=True,
                line=dict(width=LINE_WIDTH, color=color),
                marker=dict(
                    size=MARKER_SIZE,
                    color=color,
                    line=dict(width=0.5, color="white"),
                ),
            )
        )

    anchor_x: list[int] = []
    anchor_y: list[float] = []
    anchor_blocks: list[str] = []

    for _, row in plot_df.iterrows():
        x_pos = int(row[SEQ_X_COL])
        ref_date = pd.Timestamp(row[LINE_X_COL])
        if hover_source is not None:
            day_df = _tooltip_rows_for_trade(hover_source, ref_date)
        else:
            day_df = pd.DataFrame(
                [
                    {
                        "차트라벨": row["차트라벨"],
                        "거래금액(만원)": row["거래금액(만원)"],
                        "계약일자": row.get("계약일자"),
                    }
                ]
            )
        block = _build_hover_block_for_timeline(day_df, label_formatter=fmt)
        if not block:
            continue
        anchor_x.append(x_pos)
        anchor_y.append(float(row["거래금액(만원)"]))
        anchor_blocks.append(block)

    if anchor_x:
        fig.add_trace(
            go.Scatter(
                x=anchor_x,
                y=anchor_y,
                mode="markers",
                name="통합 툴팁",
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

    x_tickvals, x_ticktext = _build_xaxis_date_ticks(plot_df)
    tickvals, ticktext, y_lo, y_hi = _yaxis_ticks_eok(plot_df["거래금액(만원)"])
    x_max = max(len(plot_df) - 1, 0)

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
        title=dict(text="계약일 (거래 순서)"),
        type="linear",
        tickmode="array",
        tickvals=x_tickvals,
        ticktext=x_ticktext,
        range=[-0.5, x_max + 0.5] if x_max else None,
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
