"""
아파트 실거래가 대시보드 (Streamlit) — 매매 + 전월세
실행: streamlit run app.py
"""

from __future__ import annotations

import hashlib
import html
import os
import subprocess
import sys
import time
from pathlib import Path

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
    get_data_cache_fingerprint,
    load_cached_data,
    parse_target_pyeong,
    parse_targets,
    prepare_dashboard_data,
    sort_chart_labels,
    validate_service_key,
)
from rent_service import (
    load_cached_rent_data,
    prepare_rent_dashboard_data,
    rent_cache_status,
)

_PROJECT_DIR = Path(__file__).resolve().parent
_DATA_CACHE_VERSION = "v48_rent_purge_all_apts"
_UX_SELECTION_VERSION = "default_24pyeong_v1"
_DEFAULT_PYEONG_GROUPS = ["24평형"]

_OUTLIER_P90_QUANTILE = 0.90
_OUTLIER_P90_RATIO = 0.55
_OUTLIER_MIN_GROUP_SIZE = 3
_OUTLIER_ROW_BG = "#f0f0f0"
_OUTLIER_ROW_COLOR = "#888888"

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
_SINBANPO2_LABEL = getattr(config, "SINBANPO2_LABEL", "신반포2차")
_SINBANPO2_APT_NAME = getattr(config, "SINBANPO2_APT_NAME", "신반포2")
_SINBANPO2_PYEONG_DISPLAY = getattr(
    config,
    "SINBANPO2_PYEONG_DISPLAY",
    {"24평형": "22평", "34평형": "35평"},
)
_GAEPO_WOOSUNG_LABEL = getattr(config, "GAEPO_WOOSUNG_LABEL", "개포우성 1,2차")
_GAEPO_WOOSUNG_PYEONG_DISPLAY = getattr(
    config,
    "GAEPO_WOOSUNG_PYEONG_DISPLAY",
    {"24평형": "31평", "34평형": "44평"},
)
_SINHYUNDAI_LABEL = getattr(config, "SINHYUNDAI_LABEL", "신현대")
_SINHYUNDAI_PYEONG_DISPLAY = getattr(
    config,
    "SINHYUNDAI_PYEONG_DISPLAY",
    {"34평형": "34평"},
)

# 사이드바·차트 범례 공통 단지 노출 순서 (표시명 기준)
_SIDEBAR_APT_ORDER = getattr(
    config,
    "DASHBOARD_ALLOWED_COMPLEX_LABELS",
    [
        "원베일리",
        "퍼스티지",
        "리더스원",
        "그랑자이",
        "신현대",
        "신반포2차",
        "개포우성 1,2차",
        "잠실주공5단지",
        "삼부",
    ],
)
# API/내부 명칭 → 사이드바 표시명
_SIDEBAR_APT_ALIASES: dict[str, str] = {
    "신반포2": "신반포2차",
    "잠실주공5": "잠실주공5단지",
    "주공아파트 5단지": "잠실주공5단지",
}
_PYEONG_PRIORITY = {"24평형": 0, "34평형": 1}
_SIDEBAR_UI_VERSION = "인라인 HTML v3"

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
/* 사이드바 내부 최상단 여백만 축소 */
[data-testid="stSidebarUserContent"] {
    padding-top: 1rem !important;
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


def _render_sidebar_apt_title(apt_name: str, *, is_first: bool = False) -> None:
    """사이드바 단지명 — 인라인 스타일만 사용 (글로벌 CSS 없음)."""
    safe_name = html.escape(str(apt_name))
    margin_top = "4px" if is_first else "5px"
    st.sidebar.markdown(
        f"<div style='margin-top: {margin_top}; margin-bottom: -5px; "
        f"font-weight: bold; font-size: 15px; color: #1e293b;'>{safe_name}</div>",
        unsafe_allow_html=True,
    )


def _render_sidebar_apt_separator() -> None:
    """단지 그룹 구분선 — 인라인 hr."""
    st.sidebar.markdown(
        "<hr style='margin-top: 8px; margin-bottom: 8px; border: none; "
        "border-top: 1px solid #e6e6e6;'>",
        unsafe_allow_html=True,
    )


def _sidebar_pyeong_uses_right_column(pyeong: str) -> bool:
    """34평형(및 UI 34·35·44평)은 col2(우측) 고정 — 24평형만 col1."""
    return pyeong == "34평형"


@st.cache_data(show_spinner="매매 데이터 불러오는 중...", ttl=300)
def get_prepared_sale_data(
    _cache_version: str = _DATA_CACHE_VERSION,
    _data_file_fp: str = "",
) -> pd.DataFrame:
    raw = load_cached_data()
    targets = parse_targets(getattr(config, "TARGET_APARTMENTS", []))
    return add_outlier_flags(prepare_dashboard_data(raw, targets), is_rent=False)


@st.cache_data(show_spinner="전월세 데이터 불러오는 중...", ttl=300)
def get_prepared_rent_data(
    _cache_version: str = _DATA_CACHE_VERSION,
    _data_file_fp: str = "",
) -> pd.DataFrame:
    raw = load_cached_rent_data()
    targets = parse_targets(getattr(config, "TARGET_APARTMENTS", []))
    return add_outlier_flags(prepare_rent_dashboard_data(raw, targets), is_rent=True)


def _data_file_fingerprint() -> str:
    """캐시 CSV 변경 시 Streamlit 메모리 캐시 자동 무효화."""
    return get_data_cache_fingerprint(app_cache_version=_DATA_CACHE_VERSION)


def _group_p90(series: pd.Series) -> float:
    """그룹 거래 건수가 충분할 때만 P90(90백분위) 반환."""
    valid = series.dropna()
    if len(valid) <= _OUTLIER_MIN_GROUP_SIZE:
        return float("nan")
    return float(valid.quantile(_OUTLIER_P90_QUANTILE))


def add_outlier_flags(df: pd.DataFrame, *, is_rent: bool) -> pd.DataFrame:
    """
    임대세대 등 특수 저가 거래 플래그 — 행 삭제 없이 is_outlier 컬럼만 추가.
    전월세: [계약연도, 단지명, 평형] 그룹 P90(90%)의 55% 미만.
    연도 그룹 건수 ≤3이면 [단지명, 평형] 전체 기간 P90으로 대체, 그것도 부족하면 미판정.
    """
    if df.empty:
        out = df.copy()
        out["is_outlier"] = False
        return out

    out = df.copy()
    if not is_rent:
        out["is_outlier"] = False
        return out

    if "환산보증금(만원)" in out.columns:
        price = pd.to_numeric(out["환산보증금(만원)"], errors="coerce")
    else:
        price = pd.to_numeric(out.get("거래금액(만원)"), errors="coerce")

    apt_col = get_apartment_select_column(out)
    if "평형그룹" not in out.columns:
        out["is_outlier"] = False
        return out

    if "계약년" in out.columns:
        contract_year = out["계약년"].astype(str)
    elif "계약일자" in out.columns:
        contract_year = out["계약일자"].astype(str).str[:4]
    elif "계약일자_표시" in out.columns:
        contract_year = pd.to_datetime(out["계약일자_표시"], errors="coerce").dt.year.astype("Int64").astype(str)
    else:
        out["is_outlier"] = False
        return out

    group_keys = pd.DataFrame(
        {
            "_outlier_year": contract_year,
            "_outlier_apt": out[apt_col].astype(str),
            "_outlier_pyeong": out["평형그룹"].astype(str),
            "_outlier_price": price,
        },
        index=out.index,
    )
    year_group = ["_outlier_year", "_outlier_apt", "_outlier_pyeong"]
    apt_pyeong_group = ["_outlier_apt", "_outlier_pyeong"]

    p90_by_year = group_keys.groupby(year_group, dropna=False)["_outlier_price"].transform(_group_p90)
    p90_all_period = group_keys.groupby(apt_pyeong_group, dropna=False)["_outlier_price"].transform(
        _group_p90
    )
    reference_p90 = p90_by_year.fillna(p90_all_period)

    out["is_outlier"] = (
        price.notna()
        & reference_p90.notna()
        & (price < reference_p90 * _OUTLIER_P90_RATIO)
    )
    return out


def _chart_df_excluding_outliers(df: pd.DataFrame) -> pd.DataFrame:
    """차트용 — is_outlier == False 만."""
    if df.empty or "is_outlier" not in df.columns:
        return df
    return df.loc[~df["is_outlier"].fillna(False)]


def _style_outlier_table_rows(row: pd.Series, outlier_flags: pd.Series) -> list[str]:
    if bool(outlier_flags.get(row.name, False)):
        css = f"background-color: {_OUTLIER_ROW_BG}; color: {_OUTLIER_ROW_COLOR};"
        return [css] * len(row)
    return [""] * len(row)


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
    ordered_labels = _ordered_chart_labels(list(selected_labels))
    master = (
        chart_df["계약일자_표시"]
        .drop_duplicates()
        .sort_values()
        .to_frame(name="기준일")
    )
    tolerance = pd.Timedelta(days=tolerance_days)
    parts: list[pd.DataFrame] = []

    for label in ordered_labels:
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


@st.cache_data(show_spinner=False, ttl=300)
def get_sorted_chart_options(
    _cache_version: str = _DATA_CACHE_VERSION,
    _data_file_fp: str = "",
    market: str = "sale",
) -> list[str]:
    df = (
        get_prepared_sale_data(_cache_version, _data_file_fp)
        if market == "sale"
        else get_prepared_rent_data(_cache_version, _data_file_fp)
    )
    if df.empty:
        return []
    targets = parse_targets(getattr(config, "TARGET_APARTMENTS", []))
    labels = df["차트라벨"].dropna().unique().tolist()
    return sort_chart_labels(labels, targets)


def prepare_raw_chart_data(
    df: pd.DataFrame,
    selected_labels: list[str],
) -> pd.DataFrame:
    """차트용 실거래 원본 — 개별 계약일·금액 그대로 (집계/nearest 없음)."""
    if not selected_labels:
        return pd.DataFrame()
    cols = ["차트라벨", "계약일자_표시", "거래금액(만원)", "계약일자"]
    use_cols = [c for c in cols if c in df.columns]
    out = df.loc[df["차트라벨"].isin(selected_labels), use_cols].copy()
    out["거래금액(만원)"] = pd.to_numeric(out["거래금액(만원)"], errors="coerce")
    out = out.dropna(subset=["거래금액(만원)", "계약일자_표시"])
    if out.empty:
        return out
    out["계약일자_표시"] = pd.to_datetime(out["계약일자_표시"])
    out["아파트명"] = [
        _canonical_sidebar_apt(_extract_label_parts(str(lb))[0])
        for lb in out["차트라벨"]
    ]
    label_order = _ordered_chart_labels(list(selected_labels))
    out = _apply_apt_display_categorical(
        out,
        apt_col="아파트명",
        label_col="차트라벨",
        label_order=label_order,
    )
    return (
        out.sort_values(["아파트명", "차트라벨", "계약일자_표시"])
        .reset_index(drop=True)
    )


@st.cache_data(show_spinner=False, ttl=300)
def _prepare_raw_chart_df(
    data_source: str,
    _df: pd.DataFrame,
    selected_labels: tuple[str, ...],
    _cache_version: str = _DATA_CACHE_VERSION,
    _data_file_fp: str = "",
) -> pd.DataFrame:
    """차트용 실거래 DataFrame 캐시."""
    if not selected_labels:
        return pd.DataFrame()
    return prepare_raw_chart_data(_df, list(selected_labels))


@st.cache_data(show_spinner=False, ttl=300)
def _prepare_comparison_chart_df(
    data_source: str,
    _df: pd.DataFrame,
    selected_labels: tuple[str, ...],
    _cache_version: str = _DATA_CACHE_VERSION,
    _data_file_fp: str = "",
) -> pd.DataFrame:
    """통합 툴팁용 nearest 매핑 DataFrame 캐시."""
    if not selected_labels:
        return pd.DataFrame()
    return prepare_chart_comparison_data(_df, list(selected_labels))


def _clear_data_caches() -> None:
    get_prepared_sale_data.clear()
    get_prepared_rent_data.clear()
    get_sorted_chart_options.clear()
    _prepare_raw_chart_df.clear()
    _prepare_comparison_chart_df.clear()


def _subprocess_env_with_service_key() -> dict[str, str]:
    env = os.environ.copy()
    try:
        secret = str(st.secrets.get("SERVICE_KEY", "")).strip()
        if secret:
            env["SERVICE_KEY"] = secret
    except Exception:
        pass
    return env


def _is_pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return int(code.value) == still_active
            return False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _init_incremental_update_session() -> None:
    if "incremental_update_running" not in st.session_state:
        st.session_state.incremental_update_running = False
    if "incremental_update_pid" not in st.session_state:
        st.session_state.incremental_update_pid = None


def _start_subprocess_fetch(*extra_args: str) -> None:
    """Streamlit과 분리된 OS 프로세스에서 fetch_data.py 실행."""
    from update_status import UPDATE_LOG_FILE, reset_update_status

    reset_update_status("별도 프로세스에서 수집을 시작합니다...")
    log_path = UPDATE_LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w", encoding="utf-8")
    cmd = [sys.executable, "-u", str(_PROJECT_DIR / "fetch_data.py"), *extra_args]
    proc = subprocess.Popen(
        cmd,
        cwd=str(_PROJECT_DIR),
        env=_subprocess_env_with_service_key(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    log_file.close()
    st.session_state.incremental_update_pid = proc.pid
    st.session_state.incremental_update_running = True


_UPDATE_SPINNER_MSG = "최근 2개월 누락 데이터를 확인하고 수집 중입니다..."


def _poll_incremental_update() -> None:
    """별도 프로세스 수집 진행 — 사이드바에는 심플한 안내만 표시."""
    from update_status import read_update_status

    _init_incremental_update_session()
    if not st.session_state.incremental_update_running:
        return

    status = read_update_status()
    pid = st.session_state.get("incremental_update_pid")
    proc_alive = _is_pid_alive(pid)
    running = bool(status.get("running")) or proc_alive

    if running and not status.get("done"):
        with st.spinner(_UPDATE_SPINNER_MSG):
            time.sleep(1.5)
        st.rerun()
        return

    st.session_state.incremental_update_running = False
    st.session_state.incremental_update_pid = None
    _clear_data_caches()
    err = status.get("error")
    if err:
        st.error(str(err))
    elif status.get("done"):
        st.success("매매·전월세 업데이트 완료")
    st.rerun()


def _clear_series_selection_session() -> None:
    for key in list(st.session_state.keys()):
        if key.startswith("series_cb_") or key == "series_selected":
            del st.session_state[key]


def _reset_ui_session_if_data_version_changed() -> None:
    """데이터/표기 버전 변경 시 사이드바 체크박스 세션 초기화."""
    version_key = "_app_data_version"
    if st.session_state.get(version_key) == _DATA_CACHE_VERSION:
        return
    _clear_series_selection_session()
    st.session_state[version_key] = _DATA_CACHE_VERSION


def _reset_ui_session_if_selection_policy_changed() -> None:
    """초기 24평형 기본 선택 등 UX 정책 변경 시 1회 세션 초기화."""
    version_key = "_ux_selection_version"
    if st.session_state.get(version_key) == _UX_SELECTION_VERSION:
        return
    _clear_series_selection_session()
    st.session_state[version_key] = _UX_SELECTION_VERSION


def _labels_for_pyeong_groups(all_series: list[str], groups: list[str]) -> set[str]:
    group_set = set(groups)
    return {
        lb
        for lb in all_series
        if _extract_label_parts(lb)[1] in group_set
    }


# [34평형 선택] 마스터 버튼 — (단지명, UI평형 또는 내부 34평형) 고정 목록
_MASTER_34_PYEONG_SELECTION: list[tuple[str, str]] = [
    ("원베일리", "34평형"),
    ("퍼스티지", "34평형"),
    ("리더스원", "34평형"),
    ("그랑자이", "34평형"),
    ("삼부", "29평"),
    ("신현대", "34평"),
    ("신반포2차", "35평"),
    ("개포우성 1,2차", "31평"),
    ("개포우성 1,2차", "44평"),
]


def _label_matches_master_34_target(
    apt: str,
    internal_pyeong: str,
    target_apt: str,
    target_pyeong: str,
) -> bool:
    """마스터 목록 (단지, 평형) — UI 표기·내부 평형그룹 모두 매칭."""
    if _canonical_sidebar_apt(apt) != _canonical_sidebar_apt(target_apt):
        return False
    display = _format_pyeong_for_apt(apt, internal_pyeong)
    return display == target_pyeong or internal_pyeong == target_pyeong


def _labels_for_34_pyeong_master(all_series: list[str]) -> set[str]:
    """34평형 마스터 — 고정 (단지·평형) 조합만 선택 (데이터 라벨은 변경하지 않음)."""
    picked: set[str] = set()
    for lb in all_series:
        apt, internal = _extract_label_parts(lb)
        for target_apt, target_pyeong in _MASTER_34_PYEONG_SELECTION:
            if _label_matches_master_34_target(apt, internal, target_apt, target_pyeong):
                picked.add(lb)
                break
    return picked


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


def _canonical_sidebar_apt(name: str) -> str:
    """사이드바 정렬용 표시명으로 통일 (API명·별칭 → 타겟 label)."""
    text = str(name).strip()
    return _SIDEBAR_APT_ALIASES.get(text, text)


def _apt_display_index(apt_name: str) -> int:
    canonical = _canonical_sidebar_apt(apt_name)
    try:
        return _SIDEBAR_APT_ORDER.index(canonical)
    except ValueError:
        return len(_SIDEBAR_APT_ORDER)


def _apt_sidebar_sort_key(name: str) -> tuple[int, str]:
    return _apt_display_index(name), str(name).strip()


def _apt_rank(name: str) -> tuple[int, str]:
    return _apt_sidebar_sort_key(name)


def _ordered_chart_labels(labels: list[str]) -> list[str]:
    """차트라벨 — 단지 고정 순서 → 24평형 → 34평형."""
    def sort_key(lb: str) -> tuple[int, int, str]:
        apt, p = _extract_label_parts(lb)
        return (_apt_display_index(apt), _PYEONG_PRIORITY.get(p, 999), lb)

    return sorted(labels, key=sort_key)


def _apply_apt_display_categorical(
    df: pd.DataFrame,
    apt_col: str = "아파트명",
    *,
    label_col: str | None = "차트라벨",
    label_order: list[str] | None = None,
) -> pd.DataFrame:
    """단지·차트라벨 Categorical — sidebar/chart/table 공통 정렬."""
    if df.empty or apt_col not in df.columns:
        return df
    out = df.copy()
    categories = list(_SIDEBAR_APT_ORDER)
    extras = [a for a in out[apt_col].astype(str).unique() if a not in categories]
    out[apt_col] = pd.Categorical(
        out[apt_col].astype(str),
        categories=categories + extras,
        ordered=True,
    )
    if label_col and label_col in out.columns:
        order = label_order or _ordered_chart_labels(out[label_col].astype(str).unique().tolist())
        out[label_col] = pd.Categorical(
            out[label_col].astype(str),
            categories=order,
            ordered=True,
        )
    return out


def _is_jamsil_jugong5_apt(apt_name: str | None) -> bool:
    if not apt_name:
        return False
    text = str(apt_name)
    return _JUGONG5_LABEL in text or _JUGONG5_APT_NAME in text


def _is_sinbanpo2_apt(apt_name: str | None) -> bool:
    if not apt_name:
        return False
    text = str(apt_name)
    return _SINBANPO2_LABEL in text or text.strip() == _SINBANPO2_APT_NAME


def _is_gaepo_woosung_apt(apt_name: str | None) -> bool:
    if not apt_name:
        return False
    return _GAEPO_WOOSUNG_LABEL in str(apt_name)


def _is_sinhyundai_apt(apt_name: str | None) -> bool:
    if not apt_name:
        return False
    return _SINHYUNDAI_LABEL in str(apt_name)


def _format_pyeong_for_apt(apt_name: str | None, pyeong: str) -> str:
    """단지별 UI 평형 표기. value는 24평형/34평형 유지."""
    if apt_name and _is_jamsil_jugong5_apt(apt_name):
        return _JUGONG5_PYEONG_DISPLAY.get(pyeong, pyeong)
    if apt_name and _SAMBU_APT in str(apt_name):
        return _SAMBU_PYEONG_DISPLAY.get(pyeong, pyeong)
    if apt_name and _is_sinbanpo2_apt(apt_name):
        return _SINBANPO2_PYEONG_DISPLAY.get(pyeong, pyeong)
    if apt_name and _is_gaepo_woosung_apt(apt_name):
        return _GAEPO_WOOSUNG_PYEONG_DISPLAY.get(pyeong, pyeong)
    if apt_name and _is_sinhyundai_apt(apt_name):
        return _SINHYUNDAI_PYEONG_DISPLAY.get(pyeong, pyeong)
    return pyeong


def _extract_label_parts(label: str) -> tuple[str, str]:
    text = str(label).strip()
    if " (" in text and text.endswith(")"):
        apt, p = text.rsplit(" (", 1)
        return apt.strip(), p.rstrip(")").strip()
    return text, ""


def _format_chart_label_display(label: str) -> str:
    """차트 범례·툴팁 — 단지별 평형 표기 커스텀."""
    apt, pyeong = _extract_label_parts(label)
    if _is_jamsil_jugong5_apt(apt):
        display_p = _JUGONG5_PYEONG_DISPLAY.get(pyeong, pyeong)
        return f"{apt} ({display_p})"
    if _SAMBU_APT in apt:
        display_p = _SAMBU_PYEONG_DISPLAY.get(pyeong, pyeong)
        return f"{apt} ({display_p})"
    if _is_sinbanpo2_apt(apt):
        display_p = _SINBANPO2_PYEONG_DISPLAY.get(pyeong, pyeong)
        return f"{apt} ({display_p})"
    if _is_gaepo_woosung_apt(apt):
        display_p = _GAEPO_WOOSUNG_PYEONG_DISPLAY.get(pyeong, pyeong)
        return f"{apt} ({display_p})"
    if _is_sinhyundai_apt(apt):
        display_p = _SINHYUNDAI_PYEONG_DISPLAY.get(pyeong, pyeong)
        return f"{apt} ({display_p})"
    return label


def build_chart_cached(
    df: pd.DataFrame,
    selected_labels: tuple[str, ...],
    y_axis_title: str,
    chart_height: int,
    data_source: str,
    *,
    data_file_fp: str = "",
) -> go.Figure:
    """실거래 원본은 캐시, Plotly figure·표시 라벨은 매 실행마다 생성."""
    labels = _ordered_chart_labels(list(selected_labels))
    raw_chart = _prepare_raw_chart_df(
        data_source,
        df,
        tuple(labels),
        _DATA_CACHE_VERSION,
        data_file_fp,
    )
    tooltip_df = _prepare_comparison_chart_df(
        data_source,
        df,
        tuple(labels),
        _DATA_CACHE_VERSION,
        data_file_fp,
    )
    return build_price_chart(
        raw_chart,
        labels,
        y_axis_title=y_axis_title,
        chart_height=chart_height,
        label_formatter=_format_chart_label_display,
        tooltip_df=tooltip_df,
    )


def sort_chart_labels_for_ui(labels: list[str]) -> list[str]:
    return _ordered_chart_labels(labels)


def sort_apartment_options_for_ui(apts: list[str]) -> list[str]:
    """DASHBOARD_ALLOWED_COMPLEX_LABELS 순서 고정 (가나다 정렬 없음)."""
    apt_set = set(apts)
    ordered = [a for a in _SIDEBAR_APT_ORDER if a in apt_set]
    seen = set(ordered)
    for apt in apts:
        if apt not in seen:
            ordered.append(apt)
            seen.add(apt)
    return ordered


def _gap_analysis_apt_options(sale_df: pd.DataFrame) -> list[str]:
    """갭 분석 — 실데이터 단지 + 대시보드 고정 단지(신현대·개포우성 등) 병합."""
    apt_col = get_apartment_select_column(sale_df)
    from_data: list[str] = []
    if not sale_df.empty and apt_col in sale_df.columns:
        from_data = sale_df[apt_col].dropna().astype(str).unique().tolist()
    dashboard = list(
        getattr(config, "DASHBOARD_ALLOWED_COMPLEX_LABELS", _SIDEBAR_APT_ORDER)
    )
    combined: list[str] = list(from_data)
    seen = set(combined)
    for apt in dashboard:
        if apt not in seen:
            combined.append(apt)
            seen.add(apt)
    return sort_apartment_options_for_ui(combined)


def _mask_gap_apt_rows(df: pd.DataFrame, apt_col: str, apt_name: str) -> pd.Series:
    """갭 분석 — 표시명·타겟명·차트라벨 기준 단지 행 매칭."""
    if df.empty:
        return pd.Series(False, index=df.index)
    apt_s = df[apt_col].fillna("").astype(str)
    canonical = _canonical_sidebar_apt(apt_name)
    mask = (apt_s == str(apt_name)) | (apt_s.map(_canonical_sidebar_apt) == canonical)
    if "차트라벨" in df.columns:
        prefix = f"{apt_name} ("
        mask = mask | df["차트라벨"].astype(str).str.startswith(prefix)
    return mask


def _gap_pyeong_options_for_apt(
    sale_df: pd.DataFrame,
    apt_col: str,
    apt_name: str | None,
) -> list[str]:
    """갭 분석 평형 선택지 — 실데이터 + config 단일 평형 단지 fallback."""
    if not apt_name:
        return []
    pyeongs: list[str] = []
    if not sale_df.empty and "평형그룹" in sale_df.columns:
        pyeongs = (
            sale_df.loc[_mask_gap_apt_rows(sale_df, apt_col, apt_name), "평형그룹"]
            .dropna()
            .astype(str)
            .unique()
            .tolist()
        )
    config_opts = getattr(config, "SIDEBAR_APT_PYEONG_OPTIONS", {}).get(str(apt_name), [])
    extras: list[str] = []
    if _is_jamsil_jugong5_apt(apt_name) or _is_sinhyundai_apt(apt_name):
        extras = ["34평형"]
    merged: list[str] = []
    seen: set[str] = set()
    for p in pyeongs + list(config_opts) + extras:
        if p and p not in seen:
            seen.add(p)
            merged.append(p)
    return sorted(merged, key=lambda p: _PYEONG_PRIORITY.get(p, 999))


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


def _config_defined_series_labels() -> list[str]:
    """config SIDEBAR_APT_PYEONG_OPTIONS — 데이터 없어도 사이드바 체크박스 노출."""
    options = getattr(config, "SIDEBAR_APT_PYEONG_OPTIONS", {})
    labels: list[str] = []
    for apt, pyeongs in options.items():
        for pyeong in pyeongs:
            labels.append(f"{apt} ({pyeong})")
    return labels


def _default_dashboard_series_labels() -> list[str]:
    """사이드바 9개 단지 — 데이터 유무와 관계없이 체크박스 노출."""
    labels: list[str] = []
    sidebar_opts = getattr(config, "SIDEBAR_APT_PYEONG_OPTIONS", {})
    for apt in _SIDEBAR_APT_ORDER:
        if apt in sidebar_opts:
            pyeongs = list(sidebar_opts[apt])
        elif _is_jamsil_jugong5_apt(apt) or _is_sinhyundai_apt(apt):
            pyeongs = ["34평형"]
        else:
            pyeongs = ["24평형", "34평형"]
        for pg in pyeongs:
            labels.append(f"{apt} ({pg})")
    return labels


def _merge_series_with_config(all_series: list[str]) -> list[str]:
    """실데이터 시리즈 + config 고정 9개 단지·평형을 합쳐 사이드바 렌더."""
    merged: list[str] = list(all_series)
    seen = set(merged)
    for label in _config_defined_series_labels() + _default_dashboard_series_labels():
        if label not in seen:
            merged.append(label)
            seen.add(label)
    return sort_chart_labels_for_ui(merged)


def _merge_series_labels(sale_df: pd.DataFrame, rent_df: pd.DataFrame) -> list[str]:
    """매매·전월세 공통 사이드바 선택지 (차트라벨 합집합 + config 고정 항목)."""
    sale_labels = _get_series_labels_from_df(sale_df)
    rent_labels = _get_series_labels_from_df(rent_df)
    merged: list[str] = []
    seen: set[str] = set()
    for lb in sale_labels + rent_labels:
        if lb not in seen:
            seen.add(lb)
            merged.append(lb)
    return _merge_series_with_config(merged)


def _render_sidebar_series_selector(
    sale_df: pd.DataFrame,
    rent_df: pd.DataFrame,
    *,
    default_labels: list[str] | None = None,
) -> list[str]:
    """
    사이드바 — 4개 핵심 버튼(2x2) + 아파트별 체크박스.
    반환값은 차트라벨(내부 value) 리스트 (매매·전월세 탭 공통).
    """
    key_prefix = "series"
    all_series = _merge_series_labels(sale_df, rent_df)
    if not all_series:
        return []

    selected_key = f"{key_prefix}_selected"
    apt_map = _build_apt_series_map(all_series)
    apt_list = list(_SIDEBAR_APT_ORDER)

    if selected_key not in st.session_state:
        initial = {lb for lb in (default_labels or []) if lb in all_series}
        if not initial:
            initial = _labels_for_pyeong_groups(all_series, _DEFAULT_PYEONG_GROUPS)
        _apply_series_selection(key_prefix, all_series, initial)

    st.subheader("🏠 비교할 단지 · 평형")

    row1c1, row1c2 = st.columns(2)
    with row1c1:
        if st.button("전체 단지 선택", key=f"{key_prefix}_btn_all", use_container_width=True):
            _apply_series_selection(key_prefix, all_series, set(all_series))
            st.rerun()
    with row1c2:
        if st.button("전체 선택 해제", key=f"{key_prefix}_btn_clear", use_container_width=True):
            _apply_series_selection(key_prefix, all_series, set())
            st.rerun()

    row2c1, row2c2 = st.columns(2)
    with row2c1:
        if st.button("24평형 선택", key=f"{key_prefix}_btn_24", use_container_width=True):
            picked = {lb for lb in all_series if _extract_label_parts(lb)[1] == "24평형"}
            _apply_series_selection(key_prefix, all_series, picked)
            st.rerun()
    with row2c2:
        if st.button("34평형 선택", key=f"{key_prefix}_btn_34", use_container_width=True):
            picked = _labels_for_34_pyeong_master(all_series)
            _apply_series_selection(key_prefix, all_series, picked)
            st.rerun()

    st.caption("단지별 평형")
    for apt_idx, apt in enumerate(apt_list):
        _render_sidebar_apt_title(apt, is_first=(apt_idx == 0))
        labels = apt_map.get(apt, [])
        if not labels:
            labels = [
                lb
                for lb in _default_dashboard_series_labels()
                if _extract_label_parts(lb)[0] == apt
            ]
        if not labels:
            continue
        if len(labels) == 1:
            label = labels[0]
            _, pyeong = _extract_label_parts(label)
            display_pyeong = _format_pyeong_for_apt(apt, pyeong)
            cb_key = _series_checkbox_key(key_prefix, label)
            if cb_key not in st.session_state:
                selected_now = set(st.session_state.get(selected_key, []))
                st.session_state[cb_key] = label in selected_now
            col1, col2 = st.sidebar.columns(2)
            target_col = col2 if _sidebar_pyeong_uses_right_column(pyeong) else col1
            with target_col:
                st.checkbox(display_pyeong, key=cb_key)
        else:
            col1, col2 = st.sidebar.columns(2)
            sorted_labels = sorted(
                labels,
                key=lambda lb: _PYEONG_PRIORITY.get(_extract_label_parts(lb)[1], 999),
            )
            for label in sorted_labels[:2]:
                _, pyeong = _extract_label_parts(label)
                display_pyeong = _format_pyeong_for_apt(apt, pyeong)
                cb_key = _series_checkbox_key(key_prefix, label)
                if cb_key not in st.session_state:
                    selected_now = set(st.session_state.get(selected_key, []))
                    st.session_state[cb_key] = label in selected_now
                target_col = col2 if _sidebar_pyeong_uses_right_column(pyeong) else col1
                with target_col:
                    st.checkbox(display_pyeong, key=cb_key)
        if apt_idx < len(apt_list) - 1:
            _render_sidebar_apt_separator()

    selected_set: set[str] = set()
    for label in all_series:
        if st.session_state.get(_series_checkbox_key(key_prefix, label), False):
            selected_set.add(label)
    st.session_state[selected_key] = list(selected_set)

    if selected_set:
        st.caption(f"✓ {len(selected_set)}개 선택")

    st.sidebar.caption(f"UI Version: {_SIDEBAR_UI_VERSION}")

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
    apt_options = _gap_analysis_apt_options(sale_df)
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
        base_pyeong_options = _gap_pyeong_options_for_apt(sale_df, apt_col, base_apt)
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
        compare_pyeong_options = _gap_pyeong_options_for_apt(sale_df, apt_col, compare_apt)
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

    apt_mask_base = _mask_gap_apt_rows(sale_df, apt_col, base_apt)
    apt_mask_compare = _mask_gap_apt_rows(sale_df, apt_col, compare_apt)
    base_df = sale_df[
        apt_mask_base & (sale_df["평형그룹"] == base_pyeong)
    ][["계약일자_표시", "계약일자", "거래금액(만원)"]].copy()
    compare_df = sale_df[
        apt_mask_compare & (sale_df["평형그룹"] == compare_pyeong)
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
    base_table_df = sale_df[apt_mask_base & (sale_df["평형그룹"] == base_pyeong)].copy()
    compare_table_df = sale_df[
        apt_mask_compare & (sale_df["평형그룹"] == compare_pyeong)
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
    sale_df: pd.DataFrame,
    rent_df: pd.DataFrame,
    default_labels: list[str],
) -> list[str]:
    with st.sidebar:
        selected_series = _render_sidebar_series_selector(
            sale_df,
            rent_df,
            default_labels=default_labels,
        )

        st.divider()
        st.subheader("📥 데이터 수집")

        _poll_incremental_update()

        _init_incremental_update_session()
        update_disabled = bool(st.session_state.incremental_update_running)

        if st.button(
            "🔄 데이터 업데이트",
            use_container_width=True,
            type="primary",
            disabled=update_disabled,
        ):
            try:
                validate_service_key()
                _start_subprocess_fetch()
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

        st.caption(
            f"{len(_as_list(config.LAWD_CD))}개 구역 · "
            "누락 월 보충 + 최근 2개월 자동 재수집"
        )

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

        st.divider()
        st.caption("⚠️ 전체 재수집 (오래 걸림)")
        if st.button(
            "♻️ 캐시 초기화 후 전체 재수집",
            use_container_width=True,
            disabled=update_disabled,
        ):
            try:
                validate_service_key()
                _start_subprocess_fetch("--rebuild")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    return selected_series


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


def _sort_trade_table_by_latest_contract(view: pd.DataFrame) -> pd.DataFrame:
    """거래 내역 표 — 계약일자 기준 최신순."""
    if view.empty:
        return view
    out = view.copy()
    if "계약일자" in out.columns:
        sort_key = pd.to_numeric(
            out["계약일자"].astype(str).str.replace(r"\D", "", regex=True),
            errors="coerce",
        )
    elif "계약일자_표시" in out.columns:
        sort_key = pd.to_datetime(out["계약일자_표시"], errors="coerce")
    else:
        return out
    return (
        out.assign(_sort_key=sort_key)
        .sort_values("_sort_key", ascending=False, na_position="last")
        .drop(columns=["_sort_key"])
    )


def _render_trade_table(view: pd.DataFrame, *, is_rent: bool = False) -> None:
    title = "📋 거래 내역"
    with st.expander(title, expanded=False):
        if is_rent:
            pyeong_cols = ["평형"] if "평형" in view.columns else (["평형그룹"] if "평형그룹" in view.columns else [])
            display_cols = [
                c
                for c in [
                    "계약일자",
                    "아파트",
                    *pyeong_cols,
                    "전용면적(㎡)",
                    "보증금(만원)",
                    "월세(만원)",
                    "환산보증금(만원)",
                    "층",
                ]
                if c in view.columns
            ]
        else:
            pyeong_cols = ["평형"] if "평형" in view.columns else (["평형그룹"] if "평형그룹" in view.columns else [])
            display_cols = [
                c
                for c in [
                    "계약일자",
                    "아파트",
                    *pyeong_cols,
                    "전용면적(㎡)",
                    "거래금액(만원)",
                    "층",
                ]
                if c in view.columns
            ]

        sort_view = _sort_trade_table_by_latest_contract(view)
        display_df = sort_view[display_cols].copy()
        if "평형" in display_df.columns:
            display_df = display_df.drop(columns=["평형그룹"], errors="ignore")
        elif "평형그룹" in display_df.columns:
            apt_name_col = (
                "타겟명"
                if "타겟명" in sort_view.columns
                else ("아파트" if "아파트" in sort_view.columns else None)
            )
            if apt_name_col:
                display_df["평형"] = [
                    _format_pyeong_for_apt(row.get(apt_name_col), str(row["평형그룹"]))
                    for _, row in sort_view.iterrows()
                ]
            display_df = display_df.drop(columns=["평형그룹"], errors="ignore")
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

        sorted_df = display_df
        outlier_flags = (
            sort_view.loc[sorted_df.index, "is_outlier"].fillna(False)
            if "is_outlier" in sort_view.columns
            else pd.Series(False, index=sorted_df.index)
        )
        styled = sorted_df.style.apply(
            lambda row: _style_outlier_table_rows(row, outlier_flags),
            axis=1,
        )
        st.dataframe(
            styled,
            use_container_width=True,
            hide_index=True,
        )


def _render_market_tab(
    df: pd.DataFrame,
    selected_series: list[str],
    *,
    is_rent: bool,
    chart_key: str,
    chart_height: int,
    data_source: str,
    data_file_fp: str = "",
) -> None:
    if not selected_series:
        st.warning("사이드바에서 **단지·평형**을 1개 이상 선택해 주세요.")
        return

    view = df[df["차트라벨"].isin(selected_series)]
    _render_metrics(view, len(selected_series))

    if is_rent:
        st.caption(
            "월세는 **보증금 + (월세×250)** 으로 환산 전세가(억)를 계산해 표시합니다. "
            "차트는 **개별 거래 꺾은선**이며, 마우스를 올리면 **±6개월 nearest** 기준으로 "
            "선택 단지들의 거래를 **단일 말풍선**에 고액순·% 비교 표시합니다."
        )
    else:
        st.caption(
            "모든 실거래를 **날짜순 꺾은선**으로 표시합니다 (평균·집계 없음). "
            "마우스를 올리면 세로선과 **단일 말풍선**에 각 단지의 **가장 가까운 거래(±6개월 이내)** 가 "
            "고액순·% 비교로 표시됩니다."
        )
    st.divider()

    if view.empty:
        st.warning("선택한 단지(평형)에 거래 데이터가 없습니다.")
        return

    y_title = "환산 전세가" if is_rent else "거래금액"
    chart_df = _chart_df_excluding_outliers(df)
    fig = build_chart_cached(
        chart_df,
        tuple(selected_series),
        y_title,
        chart_height,
        data_source=data_source,
        data_file_fp=data_file_fp,
    )
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

    _reset_ui_session_if_data_version_changed()
    _reset_ui_session_if_selection_policy_changed()

    data_file_fp = _data_file_fingerprint()
    sale_status = cache_status()
    rent_status = rent_cache_status()
    sale_df = get_prepared_sale_data(_DATA_CACHE_VERSION, data_file_fp)
    rent_df = get_prepared_rent_data(_DATA_CACHE_VERSION, data_file_fp)

    config_pyeong = parse_target_pyeong(getattr(config, "TARGET_PYEONG", None))
    merged_series_options = _merge_series_labels(sale_df, rent_df)
    default_labels = default_chart_selection(
        merged_series_options,
        config_pyeong,
        default_pyeong_groups=_DEFAULT_PYEONG_GROUPS,
    )

    selected_series = _render_sidebar(
        sale_status,
        rent_status,
        sale_df,
        rent_df,
        default_labels,
    )

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
                selected_series,
                is_rent=False,
                chart_key="sale_price_chart",
                chart_height=600,
                data_source="sale",
                data_file_fp=data_file_fp,
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
                selected_series,
                is_rent=True,
                chart_key="rent_price_chart",
                chart_height=700,
                data_source="rent",
                data_file_fp=data_file_fp,
            )


if __name__ == "__main__":
    main()
