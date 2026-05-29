"""
아파트 실거래가 대시보드 (Streamlit) — 매매 + 전월세
실행: streamlit run app.py
"""

from __future__ import annotations

import hashlib

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

import config
from chart_builder import build_price_chart
from data_service import (
    _as_list,
    cache_status,
    default_chart_selection,
    get_apartment_select_column,
    load_cached_data,
    parse_target_pyeong,
    parse_targets,
    prepare_dashboard_data,
    rebuild_cache_from_scratch,
    sort_chart_labels,
    update_cache,
    validate_service_key,
)
from rent_service import (
    load_cached_rent_data,
    prepare_rent_dashboard_data,
    rebuild_rent_cache_from_scratch,
    rent_cache_status,
    update_rent_cache,
)

_DATA_CACHE_VERSION = "v22_checkbox_grid_ui"

NEAREST_TOLERANCE_DAYS = 180

# 삼부 UI 표기 (내부 value는 24평형/34평형 유지)
_SAMBU_APT = "삼부"
_SAMBU_PYEONG_DISPLAY = getattr(
    config,
    "SAMBU_PYEONG_DISPLAY",
    {"24평형": "27평", "34평형": "29평"},
)
_JUGONG5_LABEL = getattr(config, "JAMSIL_JUGONG5_LABEL", "잠실주공5단지")
_JUGONG5_APT_NAME = getattr(config, "JAMSIL_JUGONG5_APT_NAME", "주공아파트 5단지")
_JUGONG5_PYEONG_DISPLAY = getattr(
    config,
    "JAMSIL_JUGONG5_PYEONG_DISPLAY",
    {"34평형": "34평"},
)

# UI 선택지 고정 우선순위
_APT_PRIORITY_KEYWORDS = [
    "잠실주공5단지",
    "잠실주공5",
    "삼부",
    "원베일리",
    "퍼스티지",
    "그랑자이",
    "리더스원",
    "신반포2차",
    "신반포2",
]
_PYEONG_PRIORITY = {"24평형": 0, "34평형": 1}

_PAGE_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700&display=swap');
html, body, [class*="css"] {
    font-family: 'Noto Sans KR', sans-serif;
}
.main-header {
    font-size: 1.75rem;
    font-weight: 700;
    color: #1a1a2e;
    margin-bottom: 0.2rem;
}
.sub-header {
    font-size: 0.95rem;
    color: #6b7280;
    margin-bottom: 1.2rem;
}
div[data-testid="stSidebar"] {
    background-color: #f8fafc;
}

/* 상단 탭 UI 강화 */
.stTabs [data-baseweb="tab-list"] {
    gap: 0.5rem;
    padding-bottom: 0.4rem;
    border-bottom: 1px solid #e5e7eb;
}

.stTabs [data-baseweb="tab-list"] button {
    font-size: 1.2rem;
    font-weight: 700;
    color: #334155;
    padding: 0.8rem 1.1rem;
    border-radius: 0.6rem 0.6rem 0 0;
    background: #f8fafc;
    transition: all 0.15s ease-in-out;
}

.stTabs [data-baseweb="tab-list"] button:hover {
    color: #0f172a;
    background: #eef2ff;
}

.stTabs [data-baseweb="tab-list"] button[aria-selected="true"] {
    color: #1d4ed8;
    background: #e0e7ff;
    border-bottom: 4px solid #1d4ed8;
}
</style>
"""


@st.cache_data(show_spinner="매매 데이터 불러오는 중...")
def get_prepared_sale_data(_cache_version: str = _DATA_CACHE_VERSION) -> pd.DataFrame:
    raw = load_cached_data()
    targets = parse_targets(getattr(config, "TARGET_APARTMENTS", []))
    return prepare_dashboard_data(raw, targets)


@st.cache_data(show_spinner="전월세 데이터 불러오는 중...")
def get_prepared_rent_data(_cache_version: str = _DATA_CACHE_VERSION) -> pd.DataFrame:
    raw = load_cached_rent_data()
    targets = parse_targets(getattr(config, "TARGET_APARTMENTS", []))
    return prepare_rent_dashboard_data(raw, targets)


def prepare_chart_comparison_data(
    df: pd.DataFrame,
    selected_labels: list[str],
    tolerance_days: int = NEAREST_TOLERANCE_DAYS,
) -> pd.DataFrame:
    if not selected_labels:
        return pd.DataFrame()

    cols = ["차트라벨", "계약일자_표시", "거래금액(만원)", "계약일자"]
    chart_df = df.loc[df["차트라벨"].isin(selected_labels), cols].copy()
    chart_df = chart_df.dropna(subset=["거래금액(만원)", "계약일자_표시"])
    if chart_df.empty:
        return chart_df

    chart_df["계약일자_표시"] = pd.to_datetime(chart_df["계약일자_표시"])
    master = (
        chart_df["계약일자_표시"]
        .drop_duplicates()
        .sort_values()
        .to_frame(name="기준일")
    )
    tolerance = pd.Timedelta(days=tolerance_days)
    parts: list[pd.DataFrame] = []

    for label in selected_labels:
        sub = chart_df.loc[chart_df["차트라벨"] == label].copy()
        if sub.empty:
            continue
        sub = (
            sub.sort_values("계약일자_표시")
            .drop_duplicates(subset=["계약일자_표시"], keep="last")
        )
        trades = sub.rename(columns={"계약일자_표시": "실제거래일_표시"})[
            ["실제거래일_표시", "거래금액(만원)", "계약일자"]
        ].sort_values("실제거래일_표시")

        merged = pd.merge_asof(
            master.sort_values("기준일"),
            trades,
            left_on="기준일",
            right_on="실제거래일_표시",
            direction="nearest",
            tolerance=tolerance,
        )
        merged["차트라벨"] = label
        parts.append(merged)

    if not parts:
        return pd.DataFrame()

    return pd.concat(parts, ignore_index=True).dropna(subset=["거래금액(만원)"])


@st.cache_data(show_spinner=False)
def get_sorted_chart_options(
    _cache_version: str = _DATA_CACHE_VERSION,
    market: str = "sale",
) -> list[str]:
    df = get_prepared_sale_data() if market == "sale" else get_prepared_rent_data()
    if df.empty:
        return []
    targets = parse_targets(getattr(config, "TARGET_APARTMENTS", []))
    labels = df["차트라벨"].dropna().unique().tolist()
    return sort_chart_labels(labels, targets)


@st.cache_data(show_spinner=False)
def build_chart_cached(
    _df: pd.DataFrame,
    selected_labels: tuple[str, ...],
    y_axis_title: str,
    chart_height: int,
    _cache_version: str = _DATA_CACHE_VERSION,
):
    labels = list(selected_labels)
    if not labels:
        return build_price_chart(
            pd.DataFrame(),
            labels,
            y_axis_title=y_axis_title,
            chart_height=chart_height,
        )
    aligned = prepare_chart_comparison_data(_df, labels)
    return build_price_chart(
        aligned,
        labels,
        y_axis_title=y_axis_title,
        chart_height=chart_height,
    )


def _clear_data_caches() -> None:
    get_prepared_sale_data.clear()
    get_prepared_rent_data.clear()
    get_sorted_chart_options.clear()
    build_chart_cached.clear()


def _format_amount_korean(manwon: object) -> str:
    if manwon is None or pd.isna(manwon):
        return "-"
    v = int(round(float(manwon)))
    eok = v // 10000
    rest = v % 10000
    return f"{eok}억원" if rest == 0 else f"{eok}억 {rest}만원"


def _manwon_to_eok_str(manwon: float) -> str:
    return f"{float(manwon) / 10000:.1f}억"


def _eok_str(value_eok: float) -> str:
    return f"{float(value_eok):.1f}억"


def _apt_rank(name: str) -> tuple[int, str]:
    text = str(name).strip()
    for idx, keyword in enumerate(_APT_PRIORITY_KEYWORDS):
        if keyword in text:
            # 신반포2차/신반포2를 동일 우선순위(5등)로 처리
            fixed_idx = 4 if ("신반포2차" in keyword or "신반포2" in keyword) else idx
            return fixed_idx, text
    return 999, text


def _is_jamsil_jugong5_apt(apt_name: str | None) -> bool:
    if not apt_name:
        return False
    text = str(apt_name)
    return _JUGONG5_LABEL in text or _JUGONG5_APT_NAME in text


def _format_pyeong_for_apt(apt_name: str | None, pyeong: str) -> str:
    """단지별 UI 평형 표기. value는 24평형/34평형 유지."""
    if apt_name and _is_jamsil_jugong5_apt(apt_name):
        return _JUGONG5_PYEONG_DISPLAY.get(pyeong, pyeong)
    if apt_name and _SAMBU_APT in str(apt_name):
        return _SAMBU_PYEONG_DISPLAY.get(pyeong, pyeong)
    return pyeong


def _format_chart_label_display(label: str) -> str:
    """사이드바 multiselect용 — 단지별 평형 표기 커스텀."""
    apt, pyeong = _extract_label_parts(label)
    if _is_jamsil_jugong5_apt(apt):
        display_p = _JUGONG5_PYEONG_DISPLAY.get(pyeong, pyeong)
        return f"{apt} ({display_p})"
    if _SAMBU_APT in apt:
        display_p = _SAMBU_PYEONG_DISPLAY.get(pyeong, pyeong)
        return f"{apt} ({display_p})"
    return label


def _extract_label_parts(label: str) -> tuple[str, str]:
    text = str(label).strip()
    if " (" in text and text.endswith(")"):
        apt, p = text.rsplit(" (", 1)
        return apt.strip(), p.rstrip(")").strip()
    return text, ""


def sort_chart_labels_for_ui(labels: list[str]) -> list[str]:
    def sort_key(lb: str) -> tuple[int, str, int, str]:
        apt, p = _extract_label_parts(lb)
        apt_order, apt_name = _apt_rank(apt)
        p_order = _PYEONG_PRIORITY.get(p, 999)
        return (apt_order, apt_name, p_order, lb)

    return sorted(labels, key=sort_key)


def sort_apartment_options_for_ui(apts: list[str]) -> list[str]:
    return sorted(apts, key=lambda name: _apt_rank(name))


def _get_series_labels_from_df(df: pd.DataFrame) -> list[str]:
    """준비된 DataFrame에서 차트 시리즈(차트라벨) 목록 생성."""
    if df.empty or "차트라벨" not in df.columns:
        return []
    targets = parse_targets(getattr(config, "TARGET_APARTMENTS", []))
    labels = df["차트라벨"].dropna().astype(str).unique().tolist()
    return sort_chart_labels_for_ui(sort_chart_labels(labels, targets))


def _series_checkbox_key(key_prefix: str, chart_label: str) -> str:
    digest = hashlib.sha256(chart_label.encode("utf-8")).hexdigest()[:16]
    return f"{key_prefix}_cb_{digest}"


def _build_apt_series_map(series_labels: list[str]) -> dict[str, list[str]]:
    """아파트명 → 해당 단지의 차트라벨 목록."""
    apt_map: dict[str, list[str]] = {}
    for label in series_labels:
        apt, _ = _extract_label_parts(label)
        apt_map.setdefault(apt, []).append(label)
    for apt in apt_map:
        apt_map[apt] = sorted(
            apt_map[apt],
            key=lambda lb: _PYEONG_PRIORITY.get(_extract_label_parts(lb)[1], 999),
        )
    return apt_map


def _sync_checkbox_states(
    key_prefix: str,
    all_series: list[str],
    selected: set[str],
) -> None:
    for label in all_series:
        st.session_state[_series_checkbox_key(key_prefix, label)] = label in selected


def _apply_series_selection(
    key_prefix: str,
    all_series: list[str],
    selected: set[str],
) -> None:
    selected_key = f"{key_prefix}_selected"
    st.session_state[selected_key] = list(selected)
    _sync_checkbox_states(key_prefix, all_series, selected)


def _render_apartment_series_selector(
    df: pd.DataFrame,
    *,
    key_prefix: str,
    default_labels: list[str] | None = None,
) -> list[str]:
    """
    매매·전월세 탭 — 4개 핵심 버튼 + 아파트별 체크박스 그리드.
    반환값은 차트라벨(내부 value) 리스트.
    """
    all_series = _get_series_labels_from_df(df)
    if not all_series:
        st.warning("표시할 단지·평형 데이터가 없습니다.")
        return []

    selected_key = f"{key_prefix}_selected"
    apt_map = _build_apt_series_map(all_series)
    apt_list = sort_apartment_options_for_ui(list(apt_map.keys()))

    if selected_key not in st.session_state:
        initial = {lb for lb in (default_labels or []) if lb in all_series}
        if not initial:
            initial = set(all_series)
        _apply_series_selection(key_prefix, all_series, initial)

    st.markdown("##### 🏠 비교할 단지 · 평형")

    btn1, btn2, btn3, btn4 = st.columns(4)
    with btn1:
        if st.button("전체 단지 선택", key=f"{key_prefix}_btn_all", use_container_width=True):
            _apply_series_selection(key_prefix, all_series, set(all_series))
            st.rerun()
    with btn2:
        if st.button("24평형 선택", key=f"{key_prefix}_btn_24", use_container_width=True):
            picked = {
                lb
                for lb in all_series
                if _extract_label_parts(lb)[1] == "24평형"
            }
            _apply_series_selection(key_prefix, all_series, picked)
            st.rerun()
    with btn3:
        if st.button("34평형 선택", key=f"{key_prefix}_btn_34", use_container_width=True):
            picked = {
                lb
                for lb in all_series
                if _extract_label_parts(lb)[1] == "34평형"
            }
            _apply_series_selection(key_prefix, all_series, picked)
            st.rerun()
    with btn4:
        if st.button("전체 선택 해제", key=f"{key_prefix}_btn_clear", use_container_width=True):
            _apply_series_selection(key_prefix, all_series, set())
            st.rerun()

    grid_cols = 4
    for row_start in range(0, len(apt_list), grid_cols):
        row_apts = apt_list[row_start : row_start + grid_cols]
        cols = st.columns(len(row_apts))
        for col, apt in zip(cols, row_apts):
            with col:
                st.markdown(f"**{apt}**")
                for label in apt_map[apt]:
                    _, pyeong = _extract_label_parts(label)
                    display_pyeong = _format_pyeong_for_apt(apt, pyeong)
                    cb_key = _series_checkbox_key(key_prefix, label)
                    if cb_key not in st.session_state:
                        selected_now = set(st.session_state.get(selected_key, []))
                        st.session_state[cb_key] = label in selected_now
                    st.checkbox(display_pyeong, key=cb_key)

    selected_set: set[str] = set()
    for label in all_series:
        if st.session_state.get(_series_checkbox_key(key_prefix, label), False):
            selected_set.add(label)
    st.session_state[selected_key] = list(selected_set)

    if selected_set:
        summary = ", ".join(_format_chart_label_display(lb) for lb in sorted(selected_set))
        st.caption(f"선택됨 ({len(selected_set)}개): {summary}")

    return list(selected_set)


def _render_gap_analysis_tab(sale_df: pd.DataFrame) -> None:
    st.caption(
        "기준/비교 단지를 각각 선택하면, **가장 가까운 과거 거래일** 기준으로 "
        "매매가 갭 차액(기준-비교) 추이를 계산합니다."
    )
    if sale_df.empty:
        st.info("매매 데이터가 없어 갭 분석을 표시할 수 없습니다.")
        return

    apt_col = get_apartment_select_column(sale_df)
    apt_options = sort_apartment_options_for_ui(
        sale_df[apt_col].dropna().astype(str).unique().tolist()
    )
    if not apt_options:
        st.info("매매 아파트 목록이 비어 있습니다.")
        return

    col1, col2 = st.columns(2)
    with col1:
        base_apt = st.selectbox(
            "기준 아파트",
            options=apt_options,
            index=None,
            placeholder="기준 아파트를 선택하세요",
            key="gap_base_apt",
        )
        base_pyeong_options = (
            sorted(
                sale_df.loc[sale_df[apt_col] == base_apt, "평형그룹"]
                .dropna()
                .astype(str)
                .unique()
                .tolist(),
                key=lambda p: _PYEONG_PRIORITY.get(p, 999),
            )
            if base_apt
            else []
        )
        base_pyeong = st.selectbox(
            "기준 평형",
            options=base_pyeong_options,
            index=None,
            placeholder="기준 평형을 선택하세요",
            key="gap_base_pyeong",
            disabled=not base_apt,
            format_func=lambda p: _format_pyeong_for_apt(base_apt, p),
        )

    with col2:
        compare_apt = st.selectbox(
            "비교 아파트",
            options=apt_options,
            index=None,
            placeholder="비교 아파트를 선택하세요",
            key="gap_compare_apt",
        )
        compare_pyeong_options = (
            sorted(
                sale_df.loc[sale_df[apt_col] == compare_apt, "평형그룹"]
                .dropna()
                .astype(str)
                .unique()
                .tolist(),
                key=lambda p: _PYEONG_PRIORITY.get(p, 999),
            )
            if compare_apt
            else []
        )
        compare_pyeong = st.selectbox(
            "비교 평형",
            options=compare_pyeong_options,
            index=None,
            placeholder="비교 평형을 선택하세요",
            key="gap_compare_pyeong",
            disabled=not compare_apt,
            format_func=lambda p: _format_pyeong_for_apt(compare_apt, p),
        )

    if not all([base_apt, base_pyeong, compare_apt, compare_pyeong]):
        st.info("좌/우의 아파트와 평형을 모두 선택하면 갭 분석 차트가 표시됩니다.")
        return

    base_df = sale_df[
        (sale_df[apt_col] == base_apt) & (sale_df["평형그룹"] == base_pyeong)
    ][["계약일자_표시", "계약일자", "거래금액(만원)"]].copy()
    compare_df = sale_df[
        (sale_df[apt_col] == compare_apt) & (sale_df["평형그룹"] == compare_pyeong)
    ][["계약일자_표시", "계약일자", "거래금액(만원)"]].copy()

    if base_df.empty or compare_df.empty:
        st.warning("선택한 조합에 해당하는 거래 데이터가 부족합니다.")
        return

    base_df = (
        base_df.dropna(subset=["계약일자_표시", "거래금액(만원)"])
        .sort_values("계약일자_표시")
        .drop_duplicates(subset=["계약일자_표시"], keep="last")
        .rename(
            columns={
                "계약일자_표시": "기준일",
                "계약일자": "기준계약일자",
                "거래금액(만원)": "기준매매가(만원)",
            }
        )
    )
    compare_df = (
        compare_df.dropna(subset=["계약일자_표시", "거래금액(만원)"])
        .sort_values("계약일자_표시")
        .drop_duplicates(subset=["계약일자_표시"], keep="last")
        .rename(
            columns={
                "계약일자_표시": "비교일",
                "계약일자": "비교계약일자",
                "거래금액(만원)": "비교매매가(만원)",
            }
        )
    )

    merged = pd.merge_asof(
        base_df,
        compare_df,
        left_on="기준일",
        right_on="비교일",
        direction="backward",
    )
    merged = merged.dropna(subset=["비교매매가(만원)"]).copy()
    if merged.empty:
        st.warning("가까운 과거 거래일 기준으로 매칭된 비교 데이터가 없습니다.")
        return

    merged["갭차액(만원)"] = merged["기준매매가(만원)"] - merged["비교매매가(만원)"]
    # 원본 단위(만원) → 억 단위 실수로 변환
    merged["갭차액(억)"] = merged["갭차액(만원)"] / 10000.0
    mean_gap_eok = float(merged["갭차액(억)"].mean())
    merged["갭차액(억표기)"] = merged["갭차액(억)"].apply(_eok_str)

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=merged["기준일"],
            y=merged["갭차액(억)"],
            mode="lines+markers",
            name="갭 차액",
            line=dict(width=1.5, color="#2563eb"),
            marker=dict(size=5, color="#2563eb"),
            customdata=merged[["갭차액(억표기)", "기준계약일자", "비교계약일자"]].to_numpy(),
            hovertemplate=(
                "갭 차액: %{customdata[0]}<br>"
                "기준 거래일: %{customdata[1]}<br>"
                "비교 최근 거래일: %{customdata[2]}<extra></extra>"
            ),
        )
    )
    fig.add_hline(
        y=mean_gap_eok,
        line_dash="dash",
        line_color="#ef4444",
        annotation_text=f"역사적 평균 갭: {_eok_str(mean_gap_eok)}",
        annotation_position="top left",
    )
    fig.update_layout(
        template="plotly_white",
        title=(
            f"{base_apt} ({_format_pyeong_for_apt(base_apt, base_pyeong)}) vs "
            f"{compare_apt} ({_format_pyeong_for_apt(compare_apt, compare_pyeong)}) 갭 추이"
        ),
        height=600,
        hovermode="x unified",
        margin=dict(l=48, r=24, t=72, b=88),
        yaxis=dict(title="갭 차액", tickformat=".1f", ticksuffix="억"),
        xaxis=dict(
            title="",
            tickformat="%Y-%m",
            rangeslider_visible=True,
            rangeslider=dict(thickness=0.08, bgcolor="#f1f5f9"),
        ),
    )
    st.plotly_chart(fig, use_container_width=True, key="sale_gap_chart")

    # 차트 아래: 기준/비교 거래 내역 좌우 분할
    base_table_df = sale_df[
        (sale_df[apt_col] == base_apt) & (sale_df["평형그룹"] == base_pyeong)
    ].copy()
    compare_table_df = sale_df[
        (sale_df[apt_col] == compare_apt) & (sale_df["평형그룹"] == compare_pyeong)
    ].copy()

    table_cols = [
        "계약일자",
        "아파트",
        "평형그룹",
        "전용면적(㎡)",
        "거래금액(만원)",
        "층",
    ]
    table_cols = [c for c in table_cols if c in sale_df.columns]

    def _build_gap_table(df: pd.DataFrame) -> pd.DataFrame:
        out = df[table_cols].copy()
        if "거래금액(만원)" in out.columns:
            out["거래금액"] = out["거래금액(만원)"].apply(_format_amount_korean)
            out = out.drop(columns=["거래금액(만원)"])
        return out.sort_values("계약일자", ascending=False)

    left_col, right_col = st.columns(2)
    with left_col:
        st.markdown("#### 기준 아파트 거래 내역")
        st.dataframe(
            _build_gap_table(base_table_df),
            use_container_width=True,
            hide_index=True,
        )
    with right_col:
        st.markdown("#### 비교 아파트 거래 내역")
        st.dataframe(
            _build_gap_table(compare_table_df),
            use_container_width=True,
            hide_index=True,
        )


def _render_sidebar(
    sale_status: dict,
    rent_status: dict,
) -> None:
    with st.sidebar:
        st.subheader("📥 데이터 수집")

        if st.button("🔄 데이터 업데이트", use_container_width=True, type="primary"):
            try:
                validate_service_key()
                progress = st.progress(0, text="준비 중...")
                status_text = st.empty()

                def on_progress(ratio: float, msg: str) -> None:
                    progress.progress(min(ratio, 1.0), text=msg)
                    status_text.caption(msg)

                update_cache(on_progress)
                update_rent_cache(on_progress)
                _clear_data_caches()
                progress.progress(1.0, text="완료!")
                st.success("매매·전월세 업데이트 완료")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

        if st.button("♻️ 캐시 초기화 후 전체 재수집", use_container_width=True):
            try:
                validate_service_key()
                progress = st.progress(0, text="캐시 삭제 중...")
                status_text = st.empty()

                def on_progress(ratio: float, msg: str) -> None:
                    progress.progress(min(ratio, 1.0), text=msg)
                    status_text.caption(msg)

                rebuild_cache_from_scratch(on_progress)
                rebuild_rent_cache_from_scratch(on_progress)
                _clear_data_caches()
                progress.progress(1.0, text="완료!")
                st.success("매매·전월세 전체 재수집 완료")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

        st.caption(f"{len(_as_list(config.LAWD_CD))}개 구역 · 누락 월만 추가 수집")

        st.divider()
        st.header("⚙️ 설정")

        st.caption("**매매 캐시**")
        if sale_status["exists"]:
            pct = sale_status["filled_slots"] / max(sale_status["total_slots"], 1) * 100
            st.progress(min(pct / 100, 1.0))
            st.caption(
                f"{sale_status['period']} · {sale_status['rows']:,}건 · "
                f"{sale_status['filled_slots']}/{sale_status['total_slots']}"
            )
        else:
            st.warning("매매 캐시 없음")

        st.caption("**전월세 캐시**")
        if rent_status["exists"]:
            pct_r = rent_status["filled_slots"] / max(rent_status["total_slots"], 1) * 100
            st.progress(min(pct_r / 100, 1.0))
            st.caption(
                f"{rent_status['period']} · {rent_status['rows']:,}건 · "
                f"{rent_status['filled_slots']}/{rent_status['total_slots']}"
            )
        else:
            st.warning("전월세 캐시 없음")


def _render_metrics(view: pd.DataFrame, series_count: int) -> None:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("선택 시리즈", f"{series_count}개")
    m2.metric("거래 건수", f"{len(view):,}건")
    if not view.empty:
        m3.metric("기간", f"{view['계약일자'].min()[:4]}~{view['계약일자'].max()[:4]}")
        latest = view["계약일자"].max()
        m4.metric("최근 거래", f"{latest[:4]}.{latest[4:6]}.{latest[6:]}")
    else:
        m3.metric("기간", "-")
        m4.metric("최근 거래", "-")


def _render_trade_table(view: pd.DataFrame, *, is_rent: bool = False) -> None:
    title = "📋 거래 내역"
    with st.expander(title, expanded=False):
        if is_rent:
            display_cols = [
                c
                for c in [
                    "계약일자",
                    "아파트",
                    "평형그룹",
                    "전용면적(㎡)",
                    "보증금(만원)",
                    "월세(만원)",
                    "환산보증금(만원)",
                    "층",
                ]
                if c in view.columns
            ]
        else:
            display_cols = [
                c
                for c in [
                    "계약일자",
                    "아파트",
                    "평형그룹",
                    "전용면적(㎡)",
                    "거래금액(만원)",
                    "층",
                ]
                if c in view.columns
            ]

        display_df = view[display_cols].copy()
        if "평형그룹" in display_df.columns:
            apt_name_col = (
                "타겟명"
                if "타겟명" in view.columns
                else ("아파트" if "아파트" in view.columns else None)
            )
            if apt_name_col:
                display_df["평형그룹"] = [
                    _format_pyeong_for_apt(row.get(apt_name_col), str(row["평형그룹"]))
                    for _, row in view.iterrows()
                ]
        if is_rent:
            if "보증금(만원)" in display_df.columns:
                display_df["보증금"] = display_df["보증금(만원)"].apply(_format_amount_korean)
                display_df = display_df.drop(columns=["보증금(만원)"])
            if "월세(만원)" in display_df.columns:
                display_df["월세"] = display_df["월세(만원)"].apply(
                    lambda v: "-" if pd.isna(v) or float(v) == 0 else f"{int(round(float(v)))}만원"
                )
                display_df = display_df.drop(columns=["월세(만원)"])
            if "환산보증금(만원)" in display_df.columns:
                display_df["환산 전세가"] = display_df["환산보증금(만원)"].apply(
                    _format_amount_korean
                )
                display_df = display_df.drop(columns=["환산보증금(만원)"])
        elif "거래금액(만원)" in display_df.columns:
            display_df["거래금액"] = display_df["거래금액(만원)"].apply(_format_amount_korean)
            display_df = display_df.drop(columns=["거래금액(만원)"])

        st.dataframe(
            display_df.sort_values("계약일자", ascending=False),
            use_container_width=True,
            hide_index=True,
        )


def _render_market_tab(
    df: pd.DataFrame,
    *,
    is_rent: bool,
    chart_key: str,
    chart_height: int,
    selector_key: str,
    default_labels: list[str],
) -> None:
    selected_series = _render_apartment_series_selector(
        df,
        key_prefix=selector_key,
        default_labels=default_labels,
    )
    if not selected_series:
        st.warning("위 체크박스에서 **단지·평형**을 1개 이상 선택해 주세요.")
        return

    st.divider()

    view = df[df["차트라벨"].isin(selected_series)]
    _render_metrics(view, len(selected_series))

    if is_rent:
        st.caption(
            "월세는 **보증금 + (월세×250)** 으로 환산 전세가(억)를 계산해 표시합니다. "
            "통합 툴팁·세로선·±6개월 nearest 비교가 동일하게 적용됩니다."
        )
    else:
        st.caption(
            "마우스를 올리면 세로선과 **단일 말풍선**에 각 단지의 **가장 가까운 거래(±6개월 이내)** 가 "
            "`단지명 / 매매가 / 실제거래일 / 최고가 대비 %` 로 고액순 표시됩니다."
        )
    st.divider()

    if view.empty:
        st.warning("선택한 단지(평형)에 거래 데이터가 없습니다.")
        return

    y_title = "환산 전세가" if is_rent else "거래금액"
    fig = build_chart_cached(df, tuple(selected_series), y_title, chart_height)
    st.plotly_chart(fig, use_container_width=True, key=chart_key)
    _render_trade_table(view, is_rent=is_rent)


def main() -> None:
    st.set_page_config(
        page_title="아파트 실거래가 대시보드",
        page_icon="🏢",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(_PAGE_CSS, unsafe_allow_html=True)

    st.markdown('<p class="main-header">🏢 아파트 실거래가 대시보드</p>', unsafe_allow_html=True)

    sale_status = cache_status()
    rent_status = rent_cache_status()
    sale_df = get_prepared_sale_data()
    rent_df = get_prepared_rent_data()

    config_pyeong = parse_target_pyeong(getattr(config, "TARGET_PYEONG", None))
    sale_series_options = _get_series_labels_from_df(sale_df)
    rent_series_options = _get_series_labels_from_df(rent_df)
    sale_default_labels = default_chart_selection(sale_series_options, config_pyeong)
    rent_default_labels = default_chart_selection(rent_series_options, config_pyeong)

    _render_sidebar(sale_status, rent_status)

    if (not sale_status["exists"] or sale_df.empty) and (
        not rent_status["exists"] or rent_df.empty
    ):
        st.info(
            "👈 사이드바 **「데이터 업데이트」**로 2014년~현재 매매·전월세 데이터를 수집하세요.\n\n"
            "첫 수집은 시간이 걸릴 수 있으며, 이후에는 캐시를 사용합니다."
        )
        return

    tab_sale, tab_gap, tab_rent = st.tabs(
        ["매매 실거래가", "매매가 갭 분석", "전월세 실거래가"]
    )

    with tab_sale:
        if not sale_status["exists"] or sale_df.empty:
            st.info("매매 캐시가 없습니다. 사이드바에서 데이터를 수집해 주세요.")
        else:
            _render_market_tab(
                sale_df,
                is_rent=False,
                chart_key="sale_price_chart",
                chart_height=600,
                selector_key="sale",
                default_labels=sale_default_labels,
            )

    with tab_gap:
        if not sale_status["exists"] or sale_df.empty:
            st.info("매매 캐시가 없어 갭 분석을 표시할 수 없습니다.")
        else:
            _render_gap_analysis_tab(sale_df)

    with tab_rent:
        if not rent_status["exists"] or rent_df.empty:
            st.info(
                "전월세 캐시가 없습니다. 사이드바 **「데이터 업데이트」**로 수집해 주세요.\n\n"
                "403 오류가 나면 공공데이터포털에서 **「국토교통부_아파트 전월세 실거래가 자료」** "
                "API 활용 신청이 필요합니다. (매매 API와 별도)"
            )
        else:
            _render_market_tab(
                rent_df,
                is_rent=True,
                chart_key="rent_price_chart",
                chart_height=700,
                selector_key="rent",
                default_labels=rent_default_labels,
            )


if __name__ == "__main__":
    main()
