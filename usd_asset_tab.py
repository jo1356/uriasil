"""
달러 환산 자산가치 탭 — 래미안퍼스티지 34평형 매매 집중 분석.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

MANWON_PER_EOK = 10_000
USD_PER_MAN = 10_000  # 만 달러 = 10,000 USD
TARGET_APT = "래미안퍼스티지"
TARGET_PYEONG = "34평형"
FX_START = "2014-01-01"
INDEX_BASE_YEAR = 2014


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
    return sale_df.loc[mask].copy()


def _parse_contract_datetime(series: pd.Series) -> pd.Series:
    s = series.astype(str).str.replace(r"\D", "", regex=True)
    return pd.to_datetime(s, format="%Y%m%d", errors="coerce")


def _attach_value_indices(df: pd.DataFrame) -> pd.DataFrame:
    """2014년 최초 거래 가격을 원화·달러 지수 100 기준으로 설정."""
    out = df.copy()
    y2014 = out[out["contract_dt"].dt.year == INDEX_BASE_YEAR].sort_values("contract_dt")
    base = y2014.iloc[0] if not y2014.empty else out.sort_values("contract_dt").iloc[0]

    krw0 = float(base["거래금액(만원)"])
    usd0 = float(base["달러환산가(USD)"])
    manwon = pd.to_numeric(out["거래금액(만원)"], errors="coerce")
    usd = pd.to_numeric(out["달러환산가(USD)"], errors="coerce")

    out["원화지수"] = (manwon / krw0 * 100.0) if krw0 else 100.0
    out["달러지수"] = (usd / usd0 * 100.0) if usd0 else 100.0
    out["지수기준일"] = base["계약일자_표시"]
    return out


@st.cache_data(ttl=300, show_spinner=False)
def build_raemian_usd_series(
    sale_df: pd.DataFrame,
    _data_file_fp: str = "",
) -> pd.DataFrame:
    """매매 데이터 + 환율 병합 + 달러 환산가 + 가치 지수."""
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
    return _attach_value_indices(out)


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
    """구간 내 최초·최종 거래 기준 절대값 수익률."""
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
        "krw_text": _format_krw_delta(m1 - m0, krw_pct),
        "usd_text": _format_usd_delta(u1 - u0, usd_pct),
        "first_label": first["계약일자_표시"],
        "last_label": last["계약일자_표시"],
    }


def _tooltip_rows(period_df: pd.DataFrame) -> list[list[str]]:
    rows: list[list[str]] = []
    for _, row in period_df.iterrows():
        manwon = float(row["거래금액(만원)"])
        usd = float(row["달러환산가(USD)"])
        rows.append(
            [
                str(row["계약일자_표시"]),
                f"{manwon / MANWON_PER_EOK:.1f}억",
                f"${usd / USD_PER_MAN:.1f}만",
                f"{float(row['원화지수']):.1f}",
                f"{float(row['달러지수']):.1f}",
                f"환율 {float(row['krw_per_usd']):,.2f}원/USD",
            ]
        )
    return rows


def build_usd_index_chart(period_df: pd.DataFrame, *, index_base_date: str = "") -> go.Figure:
    """단일 Y축 — 원화·달러 가치 지수(2014=100), 툴팁은 절대 금액."""
    title_suffix = f" (기준: {index_base_date})" if index_base_date else ""
    fig = go.Figure()

    if period_df.empty:
        fig.update_layout(
            title=f"{TARGET_APT} {TARGET_PYEONG} — 가치 지수{title_suffix}",
            height=700,
        )
        fig.update_xaxes(title_text="계약일", rangeslider_visible=False)
        fig.update_yaxes(title_text="가치 지수 (2014=100)")
        return fig

    x = period_df["contract_dt"]
    custom = _tooltip_rows(period_df)
    krw_hover = (
        "%{customdata[0]}<br>"
        "원화: %{customdata[1]}<br>"
        "달러: %{customdata[2]}<br>"
        "원화 지수: %{customdata[3]}<br>"
        "달러 지수: %{customdata[4]}<br>"
        "%{customdata[5]}<extra></extra>"
    )

    fig.add_trace(
        go.Scatter(
            x=x,
            y=period_df["원화지수"],
            mode="lines+markers",
            name="원화 지수",
            line=dict(color="#2563eb", width=2),
            marker=dict(size=6),
            customdata=custom,
            hovertemplate=krw_hover,
        ),
    )
    fig.add_trace(
        go.Scatter(
            x=x,
            y=period_df["달러지수"],
            mode="lines+markers",
            name="달러 지수",
            line=dict(color="#dc2626", width=2, dash="dot"),
            marker=dict(size=6),
            customdata=custom,
            hovertemplate=krw_hover,
        ),
    )

    fig.update_layout(
        title=f"{TARGET_APT} {TARGET_PYEONG} — 가치 지수 (2014=100){title_suffix}",
        height=700,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=60, r=60, t=80, b=50),
    )
    fig.update_xaxes(title_text="계약일", rangeslider_visible=False)
    fig.update_yaxes(title_text="가치 지수 (2014=100)", tickformat=".0f")
    return fig


_PERIOD_SLIDER_KEY = "usd_asset_period_range"


def _filter_by_period(df: pd.DataFrame, start_d: date, end_d: date) -> pd.DataFrame:
    mask = (df["contract_dt"].dt.date >= start_d) & (df["contract_dt"].dt.date <= end_d)
    return df.loc[mask].copy()


def _render_roi_metrics(roi: dict, trade_count: int) -> None:
    """슬라이더 구간 절대값 수익률 — 큰 글씨로 표시."""
    st.divider()
    st.subheader("구간 수익률")
    st.markdown(
        f"**{roi['first_label']}** → **{roi['last_label']}** "
        f"({trade_count:,}건)"
    )
    col_krw, col_usd = st.columns(2)
    with col_krw:
        st.markdown(
            f'<p style="font-size:0.95rem;color:#64748b;margin-bottom:0.25rem;">'
            f"원화 변동</p>"
            f'<p style="font-size:2rem;font-weight:700;margin:0;line-height:1.2;">'
            f"{roi['krw_text']}</p>",
            unsafe_allow_html=True,
        )
    with col_usd:
        st.markdown(
            f'<p style="font-size:0.95rem;color:#64748b;margin-bottom:0.25rem;">'
            f"달러 변동</p>"
            f'<p style="font-size:2rem;font-weight:700;margin:0;line-height:1.2;">'
            f"{roi['usd_text']}</p>",
            unsafe_allow_html=True,
        )


def render_usd_asset_tab(sale_df: pd.DataFrame, *, data_file_fp: str = "") -> None:
    """달러 환산 자산가치 탭 본문."""
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

    index_base = str(df["지수기준일"].iloc[0]) if "지수기준일" in df.columns else ""
    min_d: date = df["contract_dt"].min().date()
    max_d: date = df["contract_dt"].max().date()
    default_range = (min_d, max_d)

    if _PERIOD_SLIDER_KEY not in st.session_state:
        st.session_state[_PERIOD_SLIDER_KEY] = default_range

    start_d, end_d = st.session_state[_PERIOD_SLIDER_KEY]
    chart_period = _filter_by_period(df, start_d, end_d)

    st.plotly_chart(
        build_usd_index_chart(chart_period, index_base_date=index_base),
        use_container_width=True,
        key="usd_asset_index_chart",
    )

    st.caption(
        f"{TARGET_APT} · {TARGET_PYEONG} · "
        f"지수 기준: {index_base} (2014년 최초 거래=100) · yfinance 환율 병합"
    )

    start_d, end_d = st.slider(
        "분석 기간",
        min_value=min_d,
        max_value=max_d,
        value=(start_d, end_d),
        format="YYYY-MM-DD",
        key=_PERIOD_SLIDER_KEY,
        help="선택 구간의 차트·수익률이 함께 갱신됩니다.",
    )

    period = _filter_by_period(df, start_d, end_d)
    if period.empty:
        st.info("선택한 기간에 거래가 없습니다. 슬라이더 구간을 조정해 주세요.")
        return

    _render_roi_metrics(_compute_period_roi(period), len(period))
