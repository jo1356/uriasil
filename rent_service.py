"""
국토교통부 아파트 전월세 실거래가 — 수집·캐시·환산 전세가·정제
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

import config
from data_service import (
    API_MAX_PAGES,
    API_SLEEP_SEC,
    TRANSACTION_TYPE_RENT,
    TRANSACTION_TYPE_SALE,
    TargetDict,
    ProgressCallback,
    _DERIVED_DASHBOARD_COLUMNS,
    _as_list,
    _log_api_fetch_error,
    _log_row_parse_error,
    _parse_api_xml_root,
    _region_label,
    _requests_get_with_retries,
    _safe_parse_int,
    _safe_assign_pyeong_group_for_row,
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
RENT_CACHE_CSV = BASE_DIR / "rent_data.csv"
LEGACY_RENT_CACHE_CSV = BASE_DIR / "all_combined_rent_data.csv"

RENT_SALE_LEAK_MAX_MANWON = 400_000  # 40억 — 전월세 캐시 매매가 혼입 상한(만원)

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
    try:
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
        classified["거래유형"] = TRANSACTION_TYPE_RENT
        return classified
    except Exception as exc:
        _log_row_parse_error("classify_rent_ingest", exc)
        return None


def fetch_apt_rent_data(
    service_key: str,
    lawd_cd: str,
    deal_ymd: str,
    page_size: int = 1000,
) -> pd.DataFrame:
    all_rows: list[dict[str, str]] = []
    page_no = 1
    ctx = f"rent {lawd_cd}/{deal_ymd}"

    while page_no <= API_MAX_PAGES:
        page_ctx = f"{ctx} page={page_no}"
        try:
            params = {
                "serviceKey": service_key,
                "LAWD_CD": lawd_cd,
                "DEAL_YMD": deal_ymd,
                "pageNo": page_no,
                "numOfRows": page_size,
            }
            response = _requests_get_with_retries(RENT_API_URL, params, context=page_ctx)
            if response is None:
                break

            if response.status_code == 403:
                _log_api_fetch_error(
                    page_ctx,
                    RuntimeError(
                        "전월세 API 권한 없음(403). 공공데이터포털에서 "
                        "'국토교통부_아파트 전월세 실거래가 자료' 활용 신청 후 "
                        "SERVICE_KEY로 다시 수집해 주세요."
                    ),
                )
                break

            root = _parse_api_xml_root(response.content, context=page_ctx)
            if root is None:
                break

            auth_error = _text(root.find(".//returnAuthMsg"))
            if auth_error:
                _log_api_fetch_error(page_ctx, RuntimeError(f"인증키 오류: {auth_error}"))
                break

            result_code = _text(root.find(".//resultCode"))
            result_msg = _text(root.find(".//resultMsg"))
            if result_code and result_code not in ("00", "000"):
                _log_api_fetch_error(
                    page_ctx,
                    RuntimeError(f"API 오류 ({result_code}): {result_msg}"),
                )
                break

            items = root.findall(".//item") or []
            if not items:
                break

            for item in items:
                try:
                    row = _item_to_row(item)
                    if not row:
                        continue
                    classified = classify_rent_row_at_ingest(row)
                    if classified:
                        all_rows.append(classified)
                except Exception as exc:
                    _log_row_parse_error("fetch_rent", exc)
                    continue

            total_count = _safe_parse_int(_text(root.find(".//totalCount")))
            if total_count is not None and page_no * page_size >= total_count:
                break
            if len(items) < page_size:
                break
            page_no += 1
        except Exception as exc:
            _log_api_fetch_error(page_ctx, exc)
            break

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
        lambda r: _safe_assign_pyeong_group_for_row(r, targets=targets),
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


def _transaction_fingerprint(
    row: pd.Series | dict,
    *,
    amount_col: str,
    apt_col: str = "아파트",
) -> tuple[str, str, float, int]:
    if isinstance(row, dict):
        getter = row.get
    else:
        getter = row.get
    apt = str(getter(apt_col, "") or getter("타겟명", "") or "").strip()
    date = str(getter("계약일자", "") or "").strip()
    try:
        m2 = round(float(getter("전용면적(㎡)", 0)), 2)
    except (TypeError, ValueError):
        m2 = 0.0
    try:
        amt = int(float(getter(amount_col, 0) or 0))
    except (TypeError, ValueError):
        amt = 0
    return (apt, date, m2, amt)


def purge_rent_sale_cross_contamination(
    rent_df: pd.DataFrame,
    sale_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    전월세 캐시에서 매매가 혼입 행 제거.
    - (단지, 계약일, 면적, 금액)이 매매 캐시와 동일하고 월세=0인 행
    - 환산보증금 40억(400,000만원) 초과 행
    """
    if rent_df.empty:
        return rent_df
    out = filter_rent_transactions(rent_df.copy())

    if sale_df is None:
        from data_service import load_cached_data

        sale_df = load_cached_data()

    sale_keys: set[tuple[str, str, float, int]] = set()
    if sale_df is not None and not sale_df.empty:
        prep_sale = sale_df.copy()
        if "타겟명" not in prep_sale.columns:
            from data_service import filter_by_targets, normalize_raw_dataframe, parse_targets

            prep_sale = filter_by_targets(
                normalize_raw_dataframe(prep_sale),
                parse_targets(getattr(config, "TARGET_APARTMENTS", [])),
            )
        for _, row in prep_sale.iterrows():
            sale_keys.add(
                _transaction_fingerprint(row, amount_col="거래금액(만원)", apt_col="타겟명")
            )
            sale_keys.add(
                _transaction_fingerprint(row, amount_col="거래금액(만원)", apt_col="아파트")
            )

    keep_idx: list[object] = []
    removed = 0
    for idx, row in out.iterrows():
        monthly = pd.to_numeric(row.get("월세(만원)"), errors="coerce")
        monthly_f = 0.0 if pd.isna(monthly) else float(monthly)
        conv = pd.to_numeric(row.get("환산보증금(만원)"), errors="coerce")
        conv_f = float(conv) if conv is not None and not pd.isna(conv) else 0.0

        if conv_f > RENT_SALE_LEAK_MAX_MANWON:
            removed += 1
            continue

        if monthly_f == 0.0 and sale_keys:
            fp_apt = _transaction_fingerprint(row, amount_col="환산보증금(만원)", apt_col="아파트")
            fp_target = _transaction_fingerprint(
                row, amount_col="환산보증금(만원)", apt_col="타겟명"
            )
            if fp_apt in sale_keys or fp_target in sale_keys:
                removed += 1
                continue

        keep_idx.append(idx)

    if removed:
        print(f"[purge] removed {removed} contaminated rent rows", flush=True)
    return out.loc[keep_idx].copy() if keep_idx else out.iloc[0:0].copy()


def filter_rent_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """전월세 캐시 — 매매 행 제거 + 거래유형 고정."""
    if df.empty:
        return df
    out = df.copy()
    if "거래유형" in out.columns:
        out = out[out["거래유형"].astype(str).str.strip() != TRANSACTION_TYPE_SALE].copy()
    out["거래유형"] = TRANSACTION_TYPE_RENT
    return out


def _resolve_rent_cache_path() -> Path:
    if RENT_CACHE_CSV.exists():
        return RENT_CACHE_CSV
    if LEGACY_RENT_CACHE_CSV.exists():
        return LEGACY_RENT_CACHE_CSV
    return RENT_CACHE_CSV


def load_cached_rent_data() -> pd.DataFrame:
    path = _resolve_rent_cache_path()
    if not path.exists():
        return pd.DataFrame()
    raw = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    return purge_rent_sale_cross_contamination(filter_rent_transactions(raw))


def save_cached_rent_data(df: pd.DataFrame) -> None:
    out = purge_rent_sale_cross_contamination(filter_rent_transactions(df))
    out.to_csv(RENT_CACHE_CSV, encoding="utf-8-sig", index=False)


def clear_rent_cache_file() -> None:
    for path in (RENT_CACHE_CSV, LEGACY_RENT_CACHE_CSV):
        if path.exists():
            path.unlink()


def update_rent_cache(
    progress: ProgressCallback = None,
    force_rebuild: bool = False,
) -> pd.DataFrame:
    service_key = validate_service_key()
    lawd_codes = _as_list(config.LAWD_CD)
    all_months = generate_month_range(get_data_start_ymd())

    from data_service import (
        clear_slot_manifest,
        crawl_version_changed,
        drop_cache_slots,
        get_filled_slots,
        mark_slots_fetched,
        plan_incremental_update_tasks,
        reprocess_rent_cache,
        _write_crawl_version_stamp,
    )

    if crawl_version_changed() and not force_rebuild:
        try:
            reprocess_rent_cache(import_supplemental=False)
            _write_crawl_version_stamp()
        except Exception as exc:
            _log_row_parse_error("version_reprocess_rent", exc)

    refresh_slots: set[tuple[str, str]] = set()

    if force_rebuild:
        clear_rent_cache_file()
        clear_slot_manifest("rent")
        cached = pd.DataFrame()
        tasks = [(lawd, ym) for lawd in lawd_codes for ym in all_months]
    else:
        cached = load_cached_rent_data()
        cached = (
            enforce_strict_pyeong_on_rent_dataframe(cached)
            if not cached.empty
            else cached
        )
        tasks, refresh_slots = plan_incremental_update_tasks(
            cached=cached,
            kind="rent",
            lawd_codes=lawd_codes,
            all_months=all_months,
            recent_n=1,
        )
        if refresh_slots:
            cached = drop_cache_slots(cached, refresh_slots)

    total_tasks = len(tasks)
    new_frames: list[pd.DataFrame] = []
    prev_flush_year: str | None = None

    try:
        print(
            f"[START] [전월세] {len(lawd_codes)}개 구 x {len(all_months)}개월 = "
            f"{total_tasks} 슬롯 ({'전체 재수집' if force_rebuild else '차분(누락+현재월)'})",
            flush=True,
        )
        for i, cd in enumerate(lawd_codes):
            print(f"  - {_region_label(cd, i)} ({cd})", flush=True)
    except Exception:
        pass

    def _flush_rent_frames(*, reason: str = "") -> None:
        nonlocal cached, new_frames
        if not new_frames:
            return
        try:
            new_df = enforce_strict_pyeong_on_rent_dataframe(
                pd.concat(new_frames, ignore_index=True)
            )
            if not new_df.empty:
                cached = (
                    pd.concat([cached, new_df], ignore_index=True)
                    if not cached.empty
                    else new_df
                )
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
            new_frames = []
            try:
                tag = f" ({reason})" if reason else ""
                print(
                    f"[SAVE] [전월세] 중간 저장{tag} - 누적 {len(cached):,}건 -> rent_data.csv",
                    flush=True,
                )
            except Exception:
                pass
        except Exception as exc:
            _log_row_parse_error("flush_rent_frames", exc)

    for idx, (lawd_cd, deal_ymd) in enumerate(tasks, start=1):
        region = _region_label(lawd_cd, lawd_codes.index(lawd_cd))
        year, month = deal_ymd[:4], deal_ymd[4:6]
        msg = f"[전월세] {region} {year}.{month}월 수집 중..."
        if progress:
            progress(idx / max(total_tasks, 1), msg)

        chunk_rows = 0
        try:
            chunk = fetch_apt_rent_data(service_key, lawd_cd, deal_ymd)
            if chunk is not None and not chunk.empty:
                chunk = chunk.copy()
                chunk["조회지역코드"] = lawd_cd
                chunk["조회계약년월"] = deal_ymd
                chunk_rows = len(chunk)
                new_frames.append(chunk)
        except Exception as exc:
            _log_api_fetch_error(f"[전월세] {region} {deal_ymd}", exc)
            print(f"[ERR] [{year}년 {month}월] {region} 전월세 오류 - skip ({exc})", flush=True)
            if progress:
                progress(
                    idx / max(total_tasks, 1),
                    f"[전월세] 오류({deal_ymd}): {exc} — 다음 월로 진행",
                )
        else:
            total_rows = len(cached) + sum(len(f) for f in new_frames)
            print(
                f"[OK] [{year}년 {int(month)}월] {region} 전월세 {chunk_rows}건 수집 "
                f"(누적 ~{total_rows:,}건, {idx}/{total_tasks})",
                flush=True,
            )
        finally:
            mark_slots_fetched("rent", [(lawd_cd, deal_ymd)])
            time.sleep(API_SLEEP_SEC)

        next_lawd = tasks[idx][0] if idx < len(tasks) else None
        if next_lawd is not None and next_lawd != lawd_cd:
            _flush_rent_frames(reason=f"{region} 완료")

        if idx % 10 == 0:
            _flush_rent_frames(reason=f"{idx}/{total_tasks} 슬롯")

        if (
            force_rebuild
            and month == "12"
            and lawd_cd == lawd_codes[-1]
            and prev_flush_year != year
        ):
            _flush_rent_frames(reason=f"{year}년 완료")
            prev_flush_year = year

    if new_frames:
        _flush_rent_frames(reason="최종 병합")

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

    from data_service import reprocess_rent_cache

    reprocess_rent_cache(import_supplemental=False)
    cached = load_cached_rent_data()

    if progress and total_tasks == 0:
        progress(1.0, "전월세 캐시가 최신 상태입니다. (로컬 재처리·보충 반영 완료)")
    elif progress:
        progress(1.0, f"[전월세] 완료 - 총 {len(cached):,}건")

    print(f"[DONE] [전월세] 수집 완료 - 총 {len(cached):,}건", flush=True)
    return cached


def rebuild_rent_cache_from_scratch(progress: ProgressCallback = None) -> pd.DataFrame:
    from data_service import clear_slot_manifest

    clear_rent_cache_file()
    clear_slot_manifest("rent")
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
    base = filter_new_market_rent_contracts(base)
    filtered = filter_by_targets(base, targets)
    if filtered.empty:
        return filtered
    filtered = add_pyeong_columns(filtered)
    if "환산보증금(만원)" in filtered.columns:
        filtered["거래금액(만원)"] = filtered["환산보증금(만원)"]
    return enrich_chart_columns(filtered)


def rent_cache_status() -> dict:
    cached = load_cached_rent_data()
    months = generate_month_range(get_data_start_ymd())
    lawd_codes = _as_list(config.LAWD_CD)
    total_slots = len(months) * len(lawd_codes)
    from data_service import get_filled_slots

    filled = len(get_filled_slots(cached, "rent"))
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
