"""
국토교통부 아파트 실거래가 — 수집·캐시·정제·차트 생성
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
import os
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd
import requests

import config

API_URL = (
    "http://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/"
    "getRTMSDataSvcAptTradeDev"
)
PLACEHOLDER_KEY = "여기에_발급받은_API_인증키를_입력하세요"
PYEONG_FROM_M2 = 0.3025
API_SLEEP_SEC = 0.35

BASE_DIR = Path(__file__).resolve().parent
CACHE_CSV = BASE_DIR / "all_combined_data.csv"

FIELD_MAP = {
    "aptNm": "아파트",
    "아파트": "아파트",
    "dealAmount": "거래금액(만원)",
    "거래금액": "거래금액(만원)",
    "dealYear": "계약년",
    "년": "계약년",
    "dealMonth": "계약월",
    "월": "계약월",
    "dealDay": "계약일",
    "일": "계약일",
    "excluUseAr": "전용면적(㎡)",
    "전용면적": "전용면적(㎡)",
    "floor": "층",
    "층": "층",
    "buildYear": "건축년도",
    "건축년도": "건축년도",
    "umdNm": "법정동",
    "법정동": "법정동",
    "jibun": "지번",
    "지번": "지번",
    "roadNm": "도로명",
    "도로명": "도로명",
    "sggCd": "지역코드",
    "지역코드": "지역코드",
}

# 허용 평형 (그 외 32·43·49평 등은 모두 제외)
ALLOWED_PYEONG_GROUPS = ["24평형", "34평형"]

# 초정밀 전용면적(㎡) 구간 — [min, max) 만 허용, 그 외 전부 삭제
# 24평형: 59㎡ 내외 | 34평형: 84㎡ 내외 (32평 70~74㎡ 등 완전 차단)
AREA_M2_STRICT_RULES: list[tuple[str, float, float]] = [
    ("24평형", 57.0, 63.0),
    ("34평형", 82.0, 87.0),
]

TargetDict = dict[str, str]
ProgressCallback = Callable[[float, str], None] | None


def _as_list(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def parse_targets(raw: object) -> list[TargetDict]:
    if not isinstance(raw, (list, tuple)) or not raw:
        return []
    targets: list[TargetDict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        dong = str(item.get("dong", "")).strip()
        name = str(item.get("name", "")).strip()
        label = str(item.get("label", "")).strip() or name
        if dong and name:
            targets.append({"dong": dong, "name": name, "label": label})
    return targets


def target_label(target: TargetDict) -> str:
    return f"{target['dong']} · {target['name']}"


def parse_target_pyeong(raw: object) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        value = raw.strip()
        return [value] if value else None
    if isinstance(raw, (list, tuple)):
        groups = [str(v).strip() for v in raw if str(v).strip()]
        return groups or None
    return None


def all_pyeong_labels() -> list[str]:
    return ALLOWED_PYEONG_GROUPS.copy()


def calc_pyeong_from_m2(area_m2: float) -> float | None:
    """전용면적(㎡) → 평 (×0.3025)"""
    if pd.isna(area_m2) or area_m2 <= 0:
        return None
    return float(area_m2) * PYEONG_FROM_M2


def assign_pyeong_group_from_m2(area_m2: float) -> str | None:
    """
    전용면적(㎡)으로 24/34평형만 반환. 그 외(32평 70~74㎡ 등)는 None(삭제).
    - 24평형: 57.0 ≤ ㎡ < 63.0
    - 34평형: 82.0 ≤ ㎡ < 87.0
    """
    if area_m2 is None or pd.isna(area_m2):
        return None
    m2 = float(area_m2)
    if 57.0 <= m2 < 63.0:
        return "24평형"
    if 82.0 <= m2 < 87.0:
        return "34평형"
    return None


def is_allowed_area_m2(area_m2: float, group: str) -> bool:
    """평형그룹과 전용면적(㎡)이 규칙에 일치하는지 검증."""
    if area_m2 is None or pd.isna(area_m2) or group not in ALLOWED_PYEONG_GROUPS:
        return False
    m2 = float(area_m2)
    for label, lo, hi in AREA_M2_STRICT_RULES:
        if label == group:
            return lo <= m2 < hi
    return False


def get_data_start_ymd() -> str:
    return str(getattr(config, "DATA_START_YMD", "201401")).strip()


def generate_month_range(start_ymd: str, end: datetime | None = None) -> list[str]:
    """YYYYMM 목록 생성 (시작월 ~ 현재월)."""
    end = end or datetime.now()
    start_year = int(start_ymd[:4])
    start_month = int(start_ymd[4:6])
    months: list[str] = []
    year, month = start_year, start_month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year}{month:02d}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def validate_service_key() -> str:
    key = get_service_key()
    if not key or key == PLACEHOLDER_KEY:
        raise ValueError(
            "SERVICE_KEY가 비어 있습니다. Streamlit Cloud는 st.secrets['SERVICE_KEY'], "
            "로컬은 환경변수 SERVICE_KEY 또는 config.py의 SERVICE_KEY를 설정해 주세요."
        )
    return key


def get_service_key() -> str:
    """
    우선순위:
    1) Streamlit secrets
    2) 환경변수 SERVICE_KEY
    3) config.py SERVICE_KEY
    """
    try:
        import streamlit as st  # type: ignore

        if hasattr(st, "secrets"):
            secret = str(st.secrets.get("SERVICE_KEY", "")).strip()
            if secret:
                return secret
    except Exception:
        pass

    env_key = os.getenv("SERVICE_KEY", "").strip()
    if env_key:
        return env_key
    return str(getattr(config, "SERVICE_KEY", "")).strip()


def format_chart_label(apt_name: str, pyeong_group: str) -> str:
    """아실 스타일 표기: 잠실주공5 (34평형)"""
    return f"{apt_name} ({pyeong_group})"


def add_pyeong_columns(df: pd.DataFrame) -> pd.DataFrame:
    """초정밀 ㎡ 구간으로 24·34평형만 남기고 나머지는 전부 삭제합니다."""
    if df.empty:
        return df
    out = df.copy()
    if "전용면적(㎡)" not in out.columns:
        return out.iloc[0:0].copy()

    out["전용면적(㎡)"] = pd.to_numeric(out["전용면적(㎡)"], errors="coerce")
    out["평형그룹"] = out["전용면적(㎡)"].apply(assign_pyeong_group_from_m2)

    # 32평(70~74㎡)·90㎡+ 등 비허용 면적 즉시 삭제
    out = out[out["평형그룹"].isin(ALLOWED_PYEONG_GROUPS)].copy()
    if out.empty:
        return out

    out["전용평수"] = (out["전용면적(㎡)"] * PYEONG_FROM_M2).round(2)
    out["전용면적(평)"] = out["전용평수"]

    return finalize_pyeong_dataframe(out)


def finalize_pyeong_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    평형그룹·차트라벨을 24/34평형만 남기도록 최종 정제.
    (32평형 등 잘못된 라벨 문자열 완전 제거)
    """
    if df.empty:
        return df

    out = df.copy()
    out["평형그룹"] = out["평형그룹"].where(
        out["평형그룹"].isin(ALLOWED_PYEONG_GROUPS), None
    )
    out = out.dropna(subset=["평형그룹"])

    if "전용면적(㎡)" in out.columns:
        out["전용면적(㎡)"] = pd.to_numeric(out["전용면적(㎡)"], errors="coerce")
        out = out[
            out.apply(
                lambda r: is_allowed_area_m2(r["전용면적(㎡)"], r["평형그룹"]),
                axis=1,
            )
        ].copy()

    display = out["타겟명"] if "타겟명" in out.columns else out["아파트"]
    out["차트라벨"] = [
        format_chart_label(str(n).strip(), str(p))
        for n, p in zip(display, out["평형그룹"])
    ]

    # 24평형·34평형 라벨만 허용 (32평형 등 문자열 완전 차단)
    out = out[
        out["차트라벨"].str.fullmatch(r".+ \((24평형|34평형)\)", na=False)
    ].copy()
    return out


def enrich_chart_columns(df: pd.DataFrame) -> pd.DataFrame:
    """차트 렌더링용 컬럼을 미리 계산해 선택 변경 시 즉시 반응하도록 합니다."""
    if df.empty:
        return df
    out = df.copy()
    if "계약일자_표시" not in out.columns:
        out["계약일자_표시"] = pd.to_datetime(
            out["계약일자"], format="%Y%m%d", errors="coerce"
        )
    if "층표시" not in out.columns:
        floors = out.get("층", pd.Series("-", index=out.index)).fillna("-").astype(str)
        out["층표시"] = floors.where(floors.str.endswith("층"), floors + "층")
    if "면적표시" not in out.columns:
        out["면적표시"] = out.apply(
            lambda r: (
                f"{r['전용면적(㎡)']:.2f}㎡ · {r['전용평수']:.1f}평"
                if pd.notna(r.get("전용면적(㎡)"))
                else "-"
            ),
            axis=1,
        )
    return out


def _text(elem: ET.Element | None) -> str:
    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()


def _parse_amount(value: str) -> float | None:
    cleaned = re.sub(r"[^\d.]", "", value or "")
    return float(cleaned) if cleaned else None


def _parse_area_m2(value: object) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    cleaned = re.sub(r"[^\d.]", "", str(value).strip())
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def classify_row_at_ingest(row: dict[str, str]) -> dict[str, str] | None:
    """
    API 행 1건 — 전용면적(㎡)으로 평형그룹을 즉시 부여. 비허용 면적은 None(폐기).
    """
    m2 = _parse_area_m2(row.get("전용면적(㎡)"))
    if m2 is None:
        return None
    group = assign_pyeong_group_from_m2(m2)
    if group is None:
        return None
    row = dict(row)
    row["전용면적(㎡)"] = str(m2)
    row["평형그룹"] = group
    return row


# 캐시에 남아 있을 수 있는 파생 컬럼 — 항상 전용면적(㎡)에서 재계산
_DERIVED_DASHBOARD_COLUMNS = (
    "평형그룹",
    "차트라벨",
    "전용평수",
    "전용면적(평)",
    "면적표시",
    "계약일자_표시",
    "층표시",
)


def _item_to_row(item: ET.Element) -> dict[str, str]:
    row: dict[str, str] = {}
    for child in item:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        col = FIELD_MAP.get(tag)
        if col:
            row[col] = _text(child)
    return row


def fetch_apt_trade_data(
    service_key: str,
    lawd_cd: str,
    deal_ymd: str,
    page_size: int = 1000,
) -> pd.DataFrame:
    all_rows: list[dict[str, str]] = []
    page_no = 1
    while True:
        params = {
            "serviceKey": service_key,
            "LAWD_CD": lawd_cd,
            "DEAL_YMD": deal_ymd,
            "pageNo": page_no,
            "numOfRows": page_size,
        }
        response = requests.get(API_URL, params=params, timeout=60)
        response.raise_for_status()
        root = ET.fromstring(response.content)

        auth_error = _text(root.find(".//returnAuthMsg"))
        if auth_error:
            raise RuntimeError(f"인증키 오류: {auth_error}")

        result_code = _text(root.find(".//resultCode"))
        result_msg = _text(root.find(".//resultMsg"))
        if result_code and result_code not in ("00", "000"):
            raise RuntimeError(f"API 오류 ({result_code}): {result_msg}")

        items = root.findall(".//item")
        if not items:
            break
        for item in items:
            row = _item_to_row(item)
            if not row:
                continue
            classified = classify_row_at_ingest(row)
            if classified:
                all_rows.append(classified)

        total_count = _text(root.find(".//totalCount"))
        if total_count and page_no * page_size >= int(total_count):
            break
        if len(items) < page_size:
            break
        page_no += 1

    return pd.DataFrame(all_rows)


def normalize_raw_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """캐시용 기본 정제 (타겟 필터 전)."""
    if df.empty:
        return df
    out = df.copy()
    for col in ("계약년", "계약월", "계약일"):
        if col not in out.columns:
            out[col] = ""
    out["계약년"] = out["계약년"].astype(str).str.replace(r"\D", "", regex=True)
    out["계약월"] = out["계약월"].astype(str).str.replace(r"\D", "", regex=True)
    out["계약일"] = out["계약일"].astype(str).str.replace(r"\D", "", regex=True)
    out["계약월"] = out["계약월"].str.zfill(2).str[-2:]
    day = out["계약일"].str.replace(r"\D", "", regex=True)
    day = day.where(day.str.len() > 0, "01").str.zfill(2).str[-2:]
    out["계약일"] = day
    out["계약일자"] = out["계약년"] + out["계약월"] + out["계약일"]
    out["거래금액(만원)"] = out.get("거래금액(만원)", pd.Series(dtype=object)).apply(
        lambda x: _parse_amount(str(x))
    )
    if "전용면적(㎡)" in out.columns:
        out["전용면적(㎡)"] = pd.to_numeric(
            out["전용면적(㎡)"].astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        )
    return out


def enforce_strict_pyeong_on_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """전용면적(㎡) 기준으로 평형그룹을 재부여·검증하고 비허용 행을 삭제합니다."""
    if df.empty:
        return df
    out = normalize_raw_dataframe(df)
    out["평형그룹"] = out["전용면적(㎡)"].apply(assign_pyeong_group_from_m2)
    out = out[out["평형그룹"].isin(ALLOWED_PYEONG_GROUPS)].copy()
    if out.empty:
        return out
    out = out[
        out.apply(
            lambda r: is_allowed_area_m2(r["전용면적(㎡)"], r["평형그룹"]),
            axis=1,
        )
    ].copy()
    return out


def clear_cache_file() -> None:
    if CACHE_CSV.exists():
        CACHE_CSV.unlink()


def filter_by_targets(df: pd.DataFrame, targets: list[TargetDict]) -> pd.DataFrame:
    if df.empty or not {"법정동", "아파트"}.issubset(df.columns):
        return df.iloc[0:0].copy()

    pieces: list[pd.DataFrame] = []
    for target in targets:
        dong, name = target["dong"], target["name"]
        display_name = target.get("label") or name
        label = target_label(target)
        mask = (
            df["법정동"].astype(str).str.contains(dong, case=False, na=False)
            & df["아파트"].astype(str).str.contains(name, case=False, na=False)
        )
        chunk = df.loc[mask].copy()
        if chunk.empty:
            continue
        chunk["타겟라벨"] = label
        chunk["타겟동"] = dong
        chunk["타겟명"] = display_name
        pieces.append(chunk)
    return pd.concat(pieces, ignore_index=True) if pieces else df.iloc[0:0].copy()


def _cached_keys(df: pd.DataFrame) -> set[tuple[str, str]]:
    if df.empty or "조회지역코드" not in df.columns or "조회계약년월" not in df.columns:
        return set()
    return set(
        zip(
            df["조회지역코드"].astype(str),
            df["조회계약년월"].astype(str),
        )
    )


def load_cached_data() -> pd.DataFrame:
    if not CACHE_CSV.exists():
        return pd.DataFrame()
    return pd.read_csv(CACHE_CSV, encoding="utf-8-sig", low_memory=False)


def save_cached_data(df: pd.DataFrame) -> None:
    df.to_csv(CACHE_CSV, index=False, encoding="utf-8-sig")


def _region_label(lawd_cd: str, index: int) -> str:
    regions = _as_list(getattr(config, "REGION_NAME", []))
    return regions[index] if index < len(regions) else lawd_cd


def rebuild_cache_from_scratch(progress: ProgressCallback = None) -> pd.DataFrame:
    """기존 캐시를 삭제하고 API에서 전 구간을 새로 수집합니다."""
    clear_cache_file()
    return update_cache(progress=progress, force_rebuild=True)


def update_cache(
    progress: ProgressCallback = None,
    force_rebuild: bool = False,
) -> pd.DataFrame:
    """
    캐시 CSV를 읽고, 누락된 (지역×월)만 API로 추가 수집합니다.
    force_rebuild=True 이면 캐시를 비우고 2014-01~현재월 전체를 재수집합니다.
    수집 시점마다 전용면적(㎡)으로 24/34평형만 분류·저장합니다.
    """
    service_key = validate_service_key()
    lawd_codes = _as_list(config.LAWD_CD)
    all_months = generate_month_range(get_data_start_ymd())

    if force_rebuild:
        clear_cache_file()
        cached = pd.DataFrame()
        existing: set[tuple[str, str]] = set()
    else:
        cached = load_cached_data()
        cached = enforce_strict_pyeong_on_dataframe(cached) if not cached.empty else cached
        existing = _cached_keys(cached)

    tasks: list[tuple[str, str]] = []
    for lawd_cd in lawd_codes:
        for deal_ymd in all_months:
            if (lawd_cd, deal_ymd) not in existing:
                tasks.append((lawd_cd, deal_ymd))

    total_tasks = len(tasks)
    new_frames: list[pd.DataFrame] = []

    for idx, (lawd_cd, deal_ymd) in enumerate(tasks, start=1):
        region = _region_label(lawd_cd, lawd_codes.index(lawd_cd))
        msg = f"{region} {deal_ymd[:4]}.{deal_ymd[4:]}월 수집 중..."
        if progress:
            progress(idx / max(total_tasks, 1), msg)

        try:
            chunk = fetch_apt_trade_data(service_key, lawd_cd, deal_ymd)
        except Exception as exc:
            if progress:
                progress(idx / max(total_tasks, 1), f"오류({deal_ymd}): {exc}")
            time.sleep(1.0)
            continue

        if not chunk.empty:
            chunk["조회지역코드"] = lawd_cd
            chunk["조회계약년월"] = deal_ymd
            new_frames.append(chunk)

        time.sleep(API_SLEEP_SEC)

    if new_frames:
        new_df = enforce_strict_pyeong_on_dataframe(
            pd.concat(new_frames, ignore_index=True)
        )
        if not new_df.empty:
            cached = (
                pd.concat([cached, new_df], ignore_index=True)
                if not cached.empty
                else new_df
            )

    if not cached.empty:
        cached = enforce_strict_pyeong_on_dataframe(cached)
        cached = cached.drop_duplicates(
            subset=["조회지역코드", "조회계약년월", "아파트", "계약일자", "거래금액(만원)", "전용면적(㎡)", "층"],
            keep="last",
        )
        save_cached_data(cached)

    if progress and total_tasks == 0:
        progress(1.0, "캐시가 최신 상태입니다.")
    elif progress:
        progress(1.0, f"완료 - 총 {len(cached):,}건")

    return cached


def prepare_dashboard_data(
    raw_df: pd.DataFrame,
    targets: list[TargetDict],
) -> pd.DataFrame:
    """대시보드용: 타겟 필터 + 평형 통합 + 차트용 컬럼 선계산."""
    if raw_df.empty:
        return raw_df
    base = normalize_raw_dataframe(raw_df)
    drop_cols = [c for c in _DERIVED_DASHBOARD_COLUMNS if c in base.columns]
    if drop_cols:
        base = base.drop(columns=drop_cols)
    base = enforce_strict_pyeong_on_dataframe(base)
    filtered = filter_by_targets(base, targets)
    filtered = add_pyeong_columns(filtered)  # 전용면적(㎡)에서 24·34평형 재확정
    return enrich_chart_columns(filtered)


def sort_chart_labels(labels: list[str], targets: list[TargetDict]) -> list[str]:
    """config 단지 순서 → 평형 순으로 범례/선택 목록 정렬."""
    pyeong_rank = {name: i for i, name in enumerate(all_pyeong_labels())}
    target_names = [t["name"] for t in targets]

    def sort_key(label: str) -> tuple[int, int, str]:
        apt_part = label.rsplit(" (", 1)[0] if " (" in label else label
        pyeong_part = label.rsplit(" (", 1)[-1].rstrip(")") if " (" in label else ""
        apt_rank = next(
            (i for i, name in enumerate(target_names) if name in apt_part),
            999,
        )
        return (apt_rank, pyeong_rank.get(pyeong_part, 999), label)

    return sorted(labels, key=sort_key)


def default_chart_selection(
    all_labels: list[str],
    target_pyeong: list[str] | None,
) -> list[str]:
    """기본값: 리스트에 있는 모든 단지(24·34평) 조합."""
    if not all_labels:
        return []
    if not target_pyeong:
        return all_labels
    selected = [lb for lb in all_labels if any(f"({p})" in lb for p in target_pyeong)]
    return selected or all_labels


MANWON_PER_EOK = 10_000  # 1억원 = 10,000만원


def _format_eok_label(manwon: float) -> str:
    """만원 → '35억', '35.5억' 형태 라벨."""
    eok = manwon / MANWON_PER_EOK
    if abs(eok - round(eok)) < 0.05:
        return f"{int(round(eok))}억"
    return f"{eok:.1f}억"


def _yaxis_ticks_eok(y_series: pd.Series) -> tuple[list[float], list[str], float, float]:
    """
    세로축 눈금(만원 기준 tickvals + 억 단위 ticktext)과 y축 범위를 계산합니다.
    데이터는 만원 단위 그대로 두고 라벨만 억으로 표시합니다.
    """
    import math

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
    y_range = [tick_start - pad, tick_end + pad]
    return tickvals, ticktext, y_range[0], y_range[1]


def _parse_chart_label(label: str) -> tuple[str, str]:
    """'삼부 (24평형)' → ('삼부', '24평형')"""
    text = str(label).strip()
    if " (" in text and text.endswith(")"):
        apt, pyeong = text.rsplit(" (", 1)
        return apt.strip(), pyeong.rstrip(")").strip()
    return text, ""


def _floor_display(floor: object) -> str:
    text = str(floor).strip() if pd.notna(floor) else "-"
    if text in ("", "-", "nan"):
        return "-"
    return text if text.endswith("층") else f"{text}층"


def _area_display(row: pd.Series) -> str:
    m2 = row.get("전용면적(㎡)")
    pyeong = row.get("평형그룹", "")
    if pd.notna(m2):
        return f"{float(m2):.2f}㎡ ({pyeong})"
    return f"- ({pyeong})"


def cache_status() -> dict:
    cached = load_cached_data()
    months = generate_month_range(get_data_start_ymd())
    lawd_codes = _as_list(config.LAWD_CD)
    total_slots = len(months) * len(lawd_codes)
    filled = len(_cached_keys(cached)) if not cached.empty else 0
    period = (
        f"{get_data_start_ymd()[:4]}.{get_data_start_ymd()[4:6]} ~ "
        f"{months[-1][:4]}.{months[-1][4:6]}"
        if months
        else ""
    )
    return {
        "exists": CACHE_CSV.exists(),
        "rows": len(cached),
        "filled_slots": filled,
        "total_slots": total_slots,
        "period": period,
        "path": str(CACHE_CSV),
    }
