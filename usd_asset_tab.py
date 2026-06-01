"""
달러 환산 자산가치 탭 — 래미안퍼스티지 34평형 매매 집중 분석.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

MANWON_PER_EOK = 10_000
TARGET_APT = "래미안퍼스티지"
TARGET_PYEONG = "34평형"
FX_START = "2014-01-01"


@st.cache_data(ttl=3600, show_spinner="원/달러 환율 데이터 불러오는 중...")
def load_usdkrw_daily(start: str = FX_START) -> pd.DataFrame:
    """yfinance KRW=X — 일별 원/달러 환율 (1 USD당 KRW)."""
    import yfinance as yf

    raw = yf.download("KRW=X", start=start, progress=False, auto_adjust=True)
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["date", "krw_per_usd"])

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [str(c[0]) if isinstance(c, tuple) else str(c) for c in raw.columns]

    close_col = "Close" if "Close" in raw.columns else raw.columns[0]
    fx = raw[[close_col]].reset_index()
    date_col = "Date" if "Date" in fx.columns else fx.columns[0]
    fx = fx.rename(columns={date_col: "date", close_col: "krw_per_usd"})
    fx["date"] = pd.to_datetime(fx["date"]).dt.normalize()
    fx["krw_per_usd"] = pd.to_numeric(fx["krw_per_usd"], errors="coerce")
    fx = fx.dropna(subset=["krw_per_usd"]).sort_values("date")
    return fx[["date", "krw_per_usd"]]


def _apt_column(df: pd.DataFrame) -> str:
    if "아파트" in df.columns:
        return "아파트"
    if "아파트명" in df.columns:
        return "아파트명"
    raise KeyError("아파트/아파트명 컬럼이 없습니다.")


def filter_raemian_firstige_34(sale_df: pd.DataFrame) -> pd.DataFrame:
    """래미안퍼스티지 34평형 매매만."""
    if sale_df.empty:
        return sale_df.iloc[0:0].copy()
    apt_col = _apt_column(sale_df)
    mask = sale_df[apt_col].astype(str).str.contains(TARGET_APT, na=False)
    if "평형그룹" in sale_df.columns:
        mask &= sale_df["평형그룹"].astype(str) == TARGET_PYEONG
    out = sale_df.loc[mask].copy()
    return out


def _parse_contract_datetime(series: pd.Series) -> pd.Series:
    s = series.astype(str).str.replace(r"\D", "", regex=True)
    return pd.to_datetime(s, format="%Y%m%d", errors="coerce")


@st.cache_data(ttl=300, show_spinner=False)
def build_raemian_usd_series(
    sale_df: pd.DataFrame,
    _data_file_fp: str = "",
) -> pd.DataFrame:
    """매매 데이터 + 환율 병합 + 달러 환산가."""
    base = filter_raemian_firstige_34(sale_df)
    if base.empty:
        return base

    fx = load_usdkrw_daily(FX_START)
    if fx.empty:
        return base

    out = base.copy()
    out["contract_dt"] = _parse_contract_datetime(out["계약일자"])
    out = out.dropna(subset=["contract_dt", "거래금액(만원)"])
    out = out.sort_values("contract_dt").reset_index(drop=True)

    trade_dates = pd.DataFrame(
        {"date": pd.to_datetime(out["contract_dt"]).astype("datetime64[ns]")}
    )
    fx_sorted = fx.sort_values("date").copy()
    fx_sorted["date"] = pd.to_datetime(fx_sorted["date"]).astype("datetime64[ns]")
    merged = pd.merge_asof(
        trade_dates.sort_values("date"),
        fx_sorted,
        on="date",
        direction="backward",
    )
    out["krw_per_usd"] = merged["krw_per_usd"].values
    out["거래금액(원)"] = pd.to_numeric(out["거래금액(만원)"], errors="coerce") * MANWON_PER_EOK
    out["달러환산가(USD)"] = out["거래금액(원)"] / out["krw_per_usd"]
    out["계약일자_표시"] = out["contract_dt"].dt.strftime("%Y-%m-%d")
    return out


def _format_krw_delta(manwon_delta: float, pct: float) -> str:
    eok = manwon_delta / MANWON_PER_EOK
    sign = "+" if eok >= 0 else ""
    pct_sign = "+" if pct >= 0 else ""
    return f"{sign}{eok:.1f}억 ({pct_sign}{pct:.1f}%)"


def _format_usd_delta(usd_delta: float, pct: float) -> str:
    sign = "+" if usd_delta >= 0 else ""
    pct_sign = "+" if pct >= 0 else ""
    return f"{sign}${usd_delta:,.0f} ({pct_sign}{pct:.1f}%)"


def _compute_period_roi(period_df: pd.DataFrame) -> dict:
    """구간 내 최초·최종 거래 기준 수익률."""
    if period_df.empty or len(period_df) < 1:
        return {}
    ordered = period_df.sort_values("contract_dt")
    first = ordered.iloc[0]
    last = ordered.iloc[-1]

    m0 = float(first["거래금액(만원)"])
    m1 = float(last["거래금액(만원)"])
    u0 = float(first["달러환산가(USD)"])
    u1 = float(last["달러환산가(USD)"])

    krw_pct = ((m1 / m0) - 1.0) * 100.0 if m0 else 0.0
    usd_pct = ((u1 / u0) - 1.0) * 100.0 if u0 else 0.0

    return {
        "first_date": first["contract_dt"],
        "last_date": last["contract_dt"],
        "krw_text": _format_krw_delta(m1 - m0, krw_pct),
        "usd_text": _format_usd_delta(u1 - u0, usd_pct),
        "first_label": first["계약일자_표시"],
        "last_label": last["계약일자_표시"],
    }


def build_usd_dual_axis_chart(period_df: pd.DataFrame) -> go.Figure:
    """원화(억)·달러($만) 듀얼 Y축 차트."""
    fig = make_subplots(specs=[[{"secondary_y": True}]])

    x = period_df["contract_dt"]
    manwon = pd.to_numeric(period_df["거래금액(만원)"], errors="coerce")
    usd = pd.to_numeric(period_df["달러환산가(USD)"], errors="coerce")
    eok = manwon / MANWON_PER_EOK
    usd_10k = usd / 10_000

    custom = [
        [
            str(row["계약일자_표시"]),
            f"{float(row['거래금액(만원)']):,.0f}만원 "
            f"({float(row['거래금액(만원)']) / MANWON_PER_EOK:.1f}억)",
            f"${float(row['달러환산가(USD)']):,.0f}",
            f"환율 {float(row['krw_per_usd']):,.2f}원/USD",
        ]
        for _, row in period_df.iterrows()
    ]

    fig.add_trace(
        go.Scatter(
            x=x,
            y=eok,
            mode="lines+markers",
            name="원화 (억원)",
            line=dict(color="#2563eb", width=2),
            marker=dict(size=6),
            customdata=custom,
            hovertemplate=(
                "%{customdata[0]}<br>"
                "원화: %{customdata[1]}<br>"
                "달러: %{customdata[2]}<br>"
                "%{customdata[3]}<extra></extra>"
            ),
        ),
        secondary_y=False,
    )

    fig.add_trace(
        go.Scatter(
            x=x,
            y=usd_10k,
            mode="lines+markers",
            name="달러 ($만)",
            line=dict(color="#dc2626", width=2, dash="dot"),
            marker=dict(size=6),
            customdata=custom,
            hovertemplate=(
                "%{customdata[0]}<br>"
                "원화: %{customdata[1]}<br>"
                "달러: %{customdata[2]}<br>"
                "%{customdata[3]}<extra></extra>"
            ),
        ),
        secondary_y=True,
    )

    fig.update_layout(
        title=f"{TARGET_APT} {TARGET_PYEONG} — 원화 vs 달러 환산 매매가",
        height=700,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=60, r=60, t=80, b=50),
    )
    fig.update_xaxes(title_text="계약일")
    fig.update_yaxes(title_text="원화 (억원)", secondary_y=False, tickformat=".1f")
    fig.update_yaxes(title_text="달러 ($만)", secondary_y=True, tickformat=".0f")

    return fig


def render_usd_asset_tab(sale_df: pd.DataFrame, *, data_file_fp: str = "") -> None:
    """달러 환산 자산가치 탭 본문."""
    st.caption(
        f"**{TARGET_APT}** · **{TARGET_PYEONG}** 매매 실거래만 분석합니다. "
        "원/달러 환율(yfinance)을 계약일 기준으로 병합해 달러 환산가를 계산합니다."
    )

    try:
        df = build_raemian_usd_series(sale_df, data_file_fp)
    except Exception as exc:
        st.error(f"환율·매매 데이터 처리 오류: {exc}")
        return

    if df.empty:
        st.warning(
            f"{TARGET_APT} {TARGET_PYEONG} 매매 데이터가 없습니다. "
            "사이드바에서 데이터를 수집해 주세요."
        )
        return

    min_d: date = df["contract_dt"].min().date()
    max_d: date = df["contract_dt"].max().date()

    st.subheader("📅 분석 기간")
    start_d, end_d = st.slider(
        "기간 선택 (최초 거래 ↔ 최종 거래 수익률 계산 구간)",
        min_value=min_d,
        max_value=max_d,
        value=(min_d, max_d),
        format="YYYY-MM-DD",
    )

    mask = (df["contract_dt"].dt.date >= start_d) & (df["contract_dt"].dt.date <= end_d)
    period = df.loc[mask].copy()

    if period.empty:
        st.info("선택한 기간에 거래가 없습니다. 슬라이더 구간을 조정해 주세요.")
        return

    roi = _compute_period_roi(period)

    st.subheader("📊 구간 수익률")
    st.markdown(
        f"**{roi['first_label']}** → **{roi['last_label']}** "
        f"({len(period):,}건)"
    )

    col_krw, col_usd = st.columns(2)
    with col_krw:
        st.metric(
            label="원화 변동",
            value=roi["krw_text"],
            help="구간 내 시간순 최초 거래 대비 최종 거래 (만원 → 억원 표기)",
        )
    with col_usd:
        st.metric(
            label="달러 변동",
            value=roi["usd_text"],
            help="동일 구간 달러 환산가 변동",
        )

    st.subheader("📈 가격 추이 (원화 · 달러)")
    st.plotly_chart(
        build_usd_dual_axis_chart(period),
        use_container_width=True,
        key="usd_asset_dual_chart",
    )
