"""
국토교통부 아파트 전월세 실거래가 — 수집·캐시·환산 전세가·정제
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import requests

import config
from data_service import (
    API_SLEEP_SEC,
    TargetDict,
    ProgressCallback,
    _DERIVED_DASHBOARD_COLUMNS,
    _as_list,
    _region_label,
    add_pyeong_columns,
    assign_pyeong_group_for_cache,
    assign_pyeong_group_from_m2,
    classify_row_at_ingest,
    enrich_chart_columns,
    filter_by_targets,
    generate_month_range,
    get_data_start_ymd,
    is_allowed_area_m2,
    normalize_raw_dataframe,
    parse_targets,
    validate_service_key,
)

RENT_API_URL = (
    "http://apis.data.go.kr/1613000/RTMSDataSvcAptRent/"
    "getRTMSDataSvcAptRent"
)

BASE_DIR = Path(__file__).resolve().parent
RENT_CACHE_CSV = BASE_DIR / "all_combined_rent_data.csv"

# 월세 40만원 = 전세 1억 → 만원 단위 월세 × 250
MONTHLY_RENT_TO_DEPOSIT_FACTOR = 250

RENT_FIELD_MAP = {
    "aptNm": "아파트",
    "아파트": "아파트",
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
    "deposit": "보증금(만원)",
    "monthlyRent": "월세(만원)",
    "contractType": "계약구분",
    "contractTerm": "계약기간",
    "useRRRight": "갱신요구권사용",
    "useRRRightYN": "갱신요구권사용여부",
    "renewReqYn": "갱신요구권사용여부",
    "preDeposit": "종전계약보증금",
    "preMonthlyRent": "종전계약월세",
}


def _text(elem: ET.Element | None) -> str:
    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()


def _parse_amount(value: str) -> float | None:
    import re

    cleaned = re.sub(r"[^\d.]", "", value or "")
    return float(cleaned) if cleaned else None


def _item_to_row(item: ET.Element) -> dict[str, str]:
    row: dict[str, str] = {}
    for child in item:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        col = RENT_FIELD_MAP.get(tag)
        if col:
            row[col] = _text(child)
    return row


def compute_converted_jeonse_deposit(
    deposit_manwon: float | None,
    monthly_manwon: float | None,
) -> float | None:
    """전환보증금(만원) = 보증금 + (월세 × 250)"""
    if deposit_manwon is None and monthly_manwon is None:
        return None
    deposit = 0.0 if deposit_manwon is None or pd.isna(deposit_manwon) else float(deposit_manwon)
    monthly = 0.0 if monthly_manwon is None or pd.isna(monthly_manwon) else float(monthly_manwon)
    total = deposit + monthly * MONTHLY_RENT_TO_DEPOSIT_FACTOR
    return total if total > 0 else None


def classify_rent_row_at_ingest(row: dict[str, str]) -> dict[str, str] | None:
    """면적 분류 후 환산 전세가를 계산해 반환. 비허용 면적은 폐기."""
    classified = classify_row_at_ingest(row)
    if classified is None:
        return None

    deposit = _parse_amount(classified.get("보증금(만원)", ""))
    monthly = _parse_amount(classified.get("월세(만원)", ""))
    converted = compute_converted_jeonse_deposit(deposit, monthly)
    if converted is None:
        return None

    classified["보증금(만원)"] = str(deposit or 0)
    classified["월세(만원)"] = str(monthly or 0)
    classified["환산보증금(만원)"] = str(converted)
    classified["거래금액(만원)"] = str(converted)
    return classified


def fetch_apt_rent_data(
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
        response = requests.get(RENT_API_URL, params=params, timeout=60)
        if response.status_code == 403:
            raise RuntimeError(
                "전월세 API 권한 없음(403). 공공데이터포털에서 "
                "'국토교통부_아파트 전월세 실거래가 자료' 활용 신청 후 "
                "config.py의 SERVICE_KEY로 다시 수집해 주세요."
            )
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
            classified = classify_rent_row_at_ingest(row)
            if classified:
                all_rows.append(classified)

        total_count = _text(root.find(".//totalCount"))
        if total_count and page_no * page_size >= int(total_count):
            break
        if len(items) < page_size:
            break
        page_no += 1

    return pd.DataFrame(all_rows)


def normalize_rent_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """전월세 캐시용 정제 + 환산보증금 재계산."""
    if df.empty:
        return df
    out = normalize_raw_dataframe(df)
    for col in ("보증금(만원)", "월세(만원)"):
        if col in out.columns:
            out[col] = pd.to_numeric(
                out[col].astype(str).str.replace(",", "", regex=False),
                errors="coerce",
            ).fillna(0)
    out["환산보증금(만원)"] = out.apply(
        lambda r: compute_converted_jeonse_deposit(
            r.get("보증금(만원)"), r.get("월세(만원)")
        ),
        axis=1,
    )
    out["거래금액(만원)"] = out["환산보증금(만원)"]
    return out


def enforce_strict_pyeong_on_rent_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = normalize_rent_dataframe(df)
    out = filter_new_market_rent_contracts(out)
    targets = parse_targets(getattr(config, "TARGET_APARTMENTS", []))
    out["평형그룹"] = out.apply(
        lambda r: assign_pyeong_group_for_cache(
            r["전용면적(㎡)"],
            dong=r["법정동"],
            apt=r["아파트"],
            targets=targets,
        ),
        axis=1,
    )
    out = out[out["평형그룹"].notna()].copy()
    out = out.dropna(subset=["환산보증금(만원)", "거래금액(만원)"])
    return out


def filter_new_market_rent_contracts(df: pd.DataFrame) -> pd.DataFrame:
    """
    전월세 노이즈 제거:
    - 계약구분: '신규'만 유지 (NaN/공백은 과거 데이터로 간주해 유지)
    - 갱신요구권사용(여부): '사용'은 삭제 (NaN/공백은 유지)
    """
    if df.empty:
        return df
    out = df.copy()

    if "계약구분" in out.columns:
        raw_contract = out["계약구분"]
        contract = raw_contract.astype(str).str.strip()
        keep_new_or_empty = raw_contract.isna() | contract.eq("") | contract.eq("nan") | contract.eq("신규")
        out = out[keep_new_or_empty].copy()

    rr_cols = [c for c in ("갱신요구권사용여부", "갱신요구권사용") if c in out.columns]
    for col in rr_cols:
        raw_rr = out[col]
        rr = raw_rr.astype(str).str.strip()
        keep_not_used = raw_rr.isna() | rr.eq("") | rr.eq("nan") | ~rr.eq("사용")
        out = out[keep_not_used].copy()

    return out


def _cached_keys(df: pd.DataFrame) -> set[tuple[str, str]]:
    if df.empty or "조회지역코드" not in df.columns or "조회계약년월" not in df.columns:
        return set()
    return set(
        zip(
            df["조회지역코드"].astype(str),
            df["조회계약년월"].astype(str),
        )
    )


def load_cached_rent_data() -> pd.DataFrame:
    if not RENT_CACHE_CSV.exists():
        return pd.DataFrame()
    return pd.read_csv(RENT_CACHE_CSV, encoding="utf-8-sig", low_memory=False)


def save_cached_rent_data(df: pd.DataFrame) -> None:
    df.to_csv(RENT_CACHE_CSV, encoding="utf-8-sig", index=False)


def clear_rent_cache_file() -> None:
    if RENT_CACHE_CSV.exists():
        RENT_CACHE_CSV.unlink()


def update_rent_cache(
    progress: ProgressCallback = None,
    force_rebuild: bool = False,
) -> pd.DataFrame:
    service_key = validate_service_key()
    lawd_codes = _as_list(config.LAWD_CD)
    all_months = generate_month_range(get_data_start_ymd())

    if force_rebuild:
        clear_rent_cache_file()
        cached = pd.DataFrame()
        existing: set[tuple[str, str]] = set()
    else:
        cached = load_cached_rent_data()
        cached = (
            enforce_strict_pyeong_on_rent_dataframe(cached)
            if not cached.empty
            else cached
        )
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
        msg = f"[전월세] {region} {deal_ymd[:4]}.{deal_ymd[4:]}월 수집 중..."
        if progress:
            progress(idx / max(total_tasks, 1), msg)

        try:
            chunk = fetch_apt_rent_data(service_key, lawd_cd, deal_ymd)
        except Exception as exc:
            if progress:
                progress(idx / max(total_tasks, 1), f"[전월세] 오류({deal_ymd}): {exc}")
            time.sleep(1.0)
            continue

        if not chunk.empty:
            chunk["조회지역코드"] = lawd_cd
            chunk["조회계약년월"] = deal_ymd
            new_frames.append(chunk)

        time.sleep(API_SLEEP_SEC)

    if new_frames:
        new_df = enforce_strict_pyeong_on_rent_dataframe(
            pd.concat(new_frames, ignore_index=True)
        )
        if not new_df.empty:
            cached = (
                pd.concat([cached, new_df], ignore_index=True)
                if not cached.empty
                else new_df
            )

    if not cached.empty:
        cached = enforce_strict_pyeong_on_rent_dataframe(cached)
        cached = cached.drop_duplicates(
            subset=[
                "조회지역코드",
                "조회계약년월",
                "아파트",
                "계약일자",
                "보증금(만원)",
                "월세(만원)",
                "전용면적(㎡)",
                "층",
            ],
            keep="last",
        )
        save_cached_rent_data(cached)

    if progress and total_tasks == 0:
        progress(1.0, "전월세 캐시가 최신 상태입니다.")
    elif progress:
        progress(1.0, f"[전월세] 완료 - 총 {len(cached):,}건")

    return cached


def rebuild_rent_cache_from_scratch(progress: ProgressCallback = None) -> pd.DataFrame:
    clear_rent_cache_file()
    return update_rent_cache(progress=progress, force_rebuild=True)


def prepare_rent_dashboard_data(
    raw_df: pd.DataFrame,
    targets: list[TargetDict],
) -> pd.DataFrame:
    if raw_df.empty:
        return raw_df
    base = normalize_rent_dataframe(raw_df)
    drop_cols = [c for c in _DERIVED_DASHBOARD_COLUMNS if c in base.columns]
    if drop_cols:
        base = base.drop(columns=drop_cols)
    base = enforce_strict_pyeong_on_rent_dataframe(base)
    filtered = filter_by_targets(base, targets)
    filtered = add_pyeong_columns(filtered)
    if "환산보증금(만원)" in filtered.columns:
        filtered["거래금액(만원)"] = filtered["환산보증금(만원)"]
    return enrich_chart_columns(filtered)


def rent_cache_status() -> dict:
    cached = load_cached_rent_data()
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
        "exists": RENT_CACHE_CSV.exists(),
        "rows": len(cached),
        "filled_slots": filled,
        "total_slots": total_slots,
        "period": period,
        "path": str(RENT_CACHE_CSV),
    }
