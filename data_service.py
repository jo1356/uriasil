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
API_REQUEST_TIMEOUT = 15
API_MAX_RETRIES = 3
API_MAX_PAGES = 200

BASE_DIR = Path(__file__).resolve().parent
SALE_CACHE_CSV = BASE_DIR / "sales_data.csv"
LEGACY_SALE_CACHE_CSV = BASE_DIR / "all_combined_data.csv"
CACHE_CSV = SALE_CACHE_CSV
CRAWL_VERSION_FILE = BASE_DIR / ".crawl_data_version"

TRANSACTION_TYPE_SALE = "매매"
TRANSACTION_TYPE_RENT = "전월세"

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

# UI 평형 문자열 ↔ 내부 평형그룹
PYEONG_STRING_TO_GROUP: dict[str, str] = {
    "24평": "24평형",
    "34평": "34평형",
    "31평": "24평형",
    "44평": "34평형",
    "27평": "24평형",
    "29평": "34평형",
    "22평": "24평형",
    "35평": "34평형",
}
ALLOWED_DISPLAY_PYEONG = frozenset(PYEONG_STRING_TO_GROUP.keys())

# 초정밀 전용면적(㎡) 구간 — [min, max) 만 허용, 그 외 전부 삭제
# 24평형: 59㎡ 내외 | 34평형: 84㎡ 내외 (32평 70~74㎡ 등 완전 차단)
AREA_M2_STRICT_RULES: list[tuple[str, float, float]] = [
    ("24평형", 57.0, 63.0),
    ("34평형", 82.0, 87.0),
]

TargetDict = dict[str, str | bool]
ProgressCallback = Callable[[float, str], None] | None

_GAEPO_WOOSUNG_APT_RE = re.compile(
    str(getattr(config, "GAEPO_WOOSUNG_APT_REGEX", r"개포\s*우성\s*[12]|개포우성\s*[12]차?")),
    re.IGNORECASE,
)
_SINHYUNDAI_APT_RE = re.compile(
    str(getattr(config, "SINHYUNDAI_APT_REGEX", r"신현대(?:9|11|12)?|현대\s*(?:9|11|12)\s*차?")),
    re.IGNORECASE,
)


def _dong_matches(dong: str, allowed: str | list[str]) -> bool:
    d = str(dong)
    items = [allowed] if isinstance(allowed, str) else list(allowed)
    return any(str(item) in d for item in items if str(item).strip())


def _as_list(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def _safe_str(value: object, default: str = "") -> str:
    """NaN/None/비문자 → 안전한 str (매칭·contains 전처리용)."""
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in ("nan", "none", "<na>", "nat"):
        return default
    return text


def _log_row_parse_error(context: str, exc: Exception) -> None:
    print(f"Error parsing row ({context}): {exc}")


def _log_api_fetch_error(context: str, exc: Exception) -> None:
    print(f"Error fetching API ({context}): {exc}")


def _safe_parse_int(value: object) -> int | None:
    try:
        text = str(value or "").strip()
        if not text:
            return None
        return int(text)
    except (TypeError, ValueError):
        return None


def _requests_get_with_retries(
    url: str,
    params: dict[str, object],
    *,
    context: str,
) -> requests.Response | None:
    """requests.get — timeout·최대 3회 재시도 후 None."""
    for attempt in range(1, API_MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=API_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return response
        except Exception as exc:
            _log_api_fetch_error(f"{context} (attempt {attempt}/{API_MAX_RETRIES})", exc)
            if attempt >= API_MAX_RETRIES:
                return None
            time.sleep(min(attempt, 3))
    return None


def _parse_api_xml_root(content: bytes | None, *, context: str) -> ET.Element | None:
    """API XML 본문 파싱 — 실패 시 None."""
    if not content:
        return None
    try:
        return ET.fromstring(content)
    except Exception as exc:
        _log_api_fetch_error(f"XML parse {context}", exc)
        return None


def _safe_assign_pyeong_group_for_row(
    row: pd.Series | dict[str, object],
    targets: list[TargetDict] | None = None,
) -> str | None:
    """DataFrame 행 → 평형그룹. 오류 시 None 반환(수집 계속)."""
    try:
        getter = row.get if hasattr(row, "get") else lambda k, d="": row[k]  # type: ignore[index]
        dong = _safe_str(getter("법정동", ""))
        apt = _safe_str(getter("아파트", ""))
        m2 = _parse_area_m2(getter("전용면적(㎡)", None))
        if m2 is None:
            return None
        if targets is None:
            targets = parse_targets(getattr(config, "TARGET_APARTMENTS", []))
        return assign_pyeong_group_for_cache(
            m2,
            dong=dong,
            apt=apt,
            targets=targets,
        )
    except Exception as exc:
        _log_row_parse_error("assign_pyeong", exc)
        return None


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
        exact_name = bool(item.get("exact_name"))
        match_all_sinhyundai = bool(item.get("match_all_sinhyundai"))
        match_all_gaepo_woosung = bool(item.get("match_all_gaepo_woosung"))
        if dong and name:
            entry: TargetDict = {"dong": dong, "name": name, "label": label}
            if exact_name:
                entry["exact_name"] = True
            if match_all_sinhyundai:
                entry["match_all_sinhyundai"] = True
            if match_all_gaepo_woosung:
                entry["match_all_gaepo_woosung"] = True
            targets.append(entry)
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


def _area_in_range(m2: float, lo: float, hi: float, *, inclusive_max: bool) -> bool:
    if inclusive_max:
        return lo <= m2 <= hi
    return lo <= m2 < hi


def is_area_in_collection_whitelist(
    area_m2: float,
    *,
    dong: str = "",
    apt: str = "",
) -> bool:
    """수집 단계 면적 whitelist — 일반 59/84㎡ + 개포우성 127㎡ + 신현대 107㎡."""
    m2 = _parse_area_m2(area_m2)
    if m2 is None:
        return False
    dong_s = _safe_str(dong)
    apt_s = _safe_str(apt)

    if is_gaepo_woosung_apartment(dong_s, apt_s):
        return assign_gaepo_woosung_pyeong_group(m2) is not None
    if is_sinhyundai_apartment(dong_s, apt_s):
        return assign_sinhyundai_pyeong_group(m2) is not None
    if is_sambu_apartment(dong_s, apt_s):
        return assign_sambu_pyeong_group(m2) is not None
    if is_jamsil_jugong5_apartment(dong_s, apt_s):
        return assign_jamsil_jugong5_pyeong_group(m2) is not None
    if is_sinbanpo2_apartment(dong_s, apt_s):
        return assign_sinbanpo2_pyeong_group(m2) is not None

    for rule in getattr(config, "COLLECTION_AREA_WHITELIST", []):
        if rule.get("kind") != "standard":
            continue
        lo = float(rule["min_m2"])
        hi = float(rule["max_m2"])
        if _area_in_range(m2, lo, hi, inclusive_max=bool(rule.get("inclusive_max"))):
            return True
    return False


def display_pyeong_for_apartment(apt_name: str, pyeong_group: str) -> str:
    """UI 표시용 평형 — 평형그룹(24/34평형)은 그대로 두고 표시명만 반환."""
    apt = str(apt_name or "").strip()
    pg = str(pyeong_group or "").strip()
    gw = str(getattr(config, "GAEPO_WOOSUNG_LABEL", "개포우성 1,2차"))
    if gw in apt:
        return str(getattr(config, "GAEPO_WOOSUNG_PYEONG_DISPLAY", {}).get(pg, pg))
    sh = str(getattr(config, "SINHYUNDAI_LABEL", "신현대"))
    if sh in apt:
        return str(getattr(config, "SINHYUNDAI_PYEONG_DISPLAY", {}).get(pg, pg))
    sb2 = str(getattr(config, "SINBANPO2_LABEL", "신반포2차"))
    sb_name = str(getattr(config, "SINBANPO2_APT_NAME", "신반포2"))
    if sb2 in apt or apt == sb_name:
        return str(getattr(config, "SINBANPO2_PYEONG_DISPLAY", {}).get(pg, pg))
    j5 = str(getattr(config, "JAMSIL_JUGONG5_LABEL", "잠실주공5단지"))
    j5_name = str(getattr(config, "JAMSIL_JUGONG5_APT_NAME", "주공아파트 5단지"))
    if j5 in apt or j5_name in apt:
        return str(getattr(config, "JAMSIL_JUGONG5_PYEONG_DISPLAY", {}).get(pg, pg))
    sambu = str(getattr(config, "SAMBU_APT_NAME", "삼부"))
    if sambu in apt:
        return str(getattr(config, "SAMBU_PYEONG_DISPLAY", {}).get(pg, pg))
    return pg


def calc_pyeong_from_m2(area_m2: float) -> float | None:
    """전용면적(㎡) → 평 (×0.3025)"""
    if pd.isna(area_m2) or area_m2 <= 0:
        return None
    return float(area_m2) * PYEONG_FROM_M2


def is_sambu_apartment(dong: str, apt: str) -> bool:
    """여의도동 삼부 단지 여부 (국토부 API 명칭 '삼부')."""
    sambu_dong = str(getattr(config, "SAMBU_DONG", "여의도동"))
    sambu_name = str(getattr(config, "SAMBU_APT_NAME", "삼부"))
    return sambu_dong in str(dong) and str(apt).strip() == sambu_name


def get_sambu_area_rules() -> list[tuple[str, float, float]]:
    raw = getattr(
        config,
        "SAMBU_AREA_RULES",
        [("24평형", 77.0, 78.0), ("34평형", 92.0, 93.0)],
    )
    return [(str(label), float(lo), float(hi)) for label, lo, hi in raw]


def assign_sambu_pyeong_group(area_m2: float) -> str | None:
    """삼부 전용: 77㎡대→24평형, 92㎡대→34평형, 그 외 제외."""
    if area_m2 is None or pd.isna(area_m2):
        return None
    m2 = float(area_m2)
    for label, lo, hi in get_sambu_area_rules():
        if lo <= m2 < hi:
            return label
    return None


def is_jamsil_jugong5_apartment(dong: str, apt: str) -> bool:
    """잠실동 주공아파트 5단지 (국토부 API 공식 명칭)."""
    j_dong = str(getattr(config, "JAMSIL_JUGONG5_DONG", "잠실동"))
    j_name = str(getattr(config, "JAMSIL_JUGONG5_APT_NAME", "주공아파트 5단지"))
    return j_dong in str(dong) and str(apt).strip() == j_name


def get_jamsil_jugong5_area_rules() -> list[tuple[str, float, float]]:
    raw = getattr(
        config,
        "JAMSIL_JUGONG5_AREA_RULES",
        [("34평형", 76.0, 77.0)],
    )
    return [(str(label), float(lo), float(hi)) for label, lo, hi in raw]


def assign_jamsil_jugong5_pyeong_group(area_m2: float) -> str | None:
    """잠실주공5 전용: 76㎡대만 34평형, 그 외(81·82㎡ 등) 제외."""
    if area_m2 is None or pd.isna(area_m2):
        return None
    m2 = float(area_m2)
    for label, lo, hi in get_jamsil_jugong5_area_rules():
        if lo <= m2 < hi:
            return label
    return None


def resolve_apt_for_pyeong_rules(
    row: pd.Series | dict,
    *,
    apt_col: str = "아파트",
    target_col: str = "타겟명",
) -> str:
    """평형 분류용 API 단지명 — 아파트 컬럼 우선, 없으면 타겟 표시명."""
    if isinstance(row, dict):
        api_name = str(row.get(apt_col, "") or "").strip()
        display = str(row.get(target_col, "") or "").strip()
    else:
        api_name = str(row.get(apt_col, "") or "").strip()
        display = str(row.get(target_col, "") or "").strip()
    if api_name:
        return api_name
    sb_label = str(getattr(config, "SINBANPO2_LABEL", "신반포2차"))
    sb_name = str(getattr(config, "SINBANPO2_APT_NAME", "신반포2"))
    if display == sb_label:
        return sb_name
    gw_label = str(getattr(config, "GAEPO_WOOSUNG_LABEL", "개포우성 1,2차"))
    if display == gw_label:
        return api_name or gw_label
    sh_label = str(getattr(config, "SINHYUNDAI_LABEL", "신현대"))
    if display == sh_label:
        return api_name or sh_label
    return display


def is_sinbanpo2_apartment(dong: str, apt: str) -> bool:
    """잠원동 신반포2(차) — API 명칭 '신반포2' 또는 표시명 '신반포2차'."""
    sb_dong = str(getattr(config, "SINBANPO2_DONG", "잠원동"))
    sb_name = str(getattr(config, "SINBANPO2_APT_NAME", "신반포2"))
    sb_label = str(getattr(config, "SINBANPO2_LABEL", "신반포2차"))
    apt_s = str(apt).strip()
    if sb_dong not in str(dong):
        return False
    return apt_s in (sb_name, sb_label)


def get_sinbanpo2_area_rules() -> list[tuple[str, float, float]]:
    raw = getattr(
        config,
        "SINBANPO2_AREA_RULES",
        [("24평형", 68.0, 69.0), ("34평형", 107.0, 108.0)],
    )
    return [(str(label), float(lo), float(hi)) for label, lo, hi in raw]


def assign_sinbanpo2_pyeong_group(area_m2: float) -> str | None:
    """신반포2차 전용: 68㎡대→24평형, 107㎡대→34평형, 그 외 제외."""
    if area_m2 is None or pd.isna(area_m2):
        return None
    m2 = float(area_m2)
    for label, lo, hi in get_sinbanpo2_area_rules():
        if lo <= m2 < hi:
            return label
    return None


def is_gaepo_woosung_apartment(dong: str, apt: str) -> bool:
    """개포우성 1·2차 — API: 개포우성1/2, 개포 우성 1 등, 법정동 대치동·개포동."""
    try:
        dongs = getattr(config, "GAEPO_WOOSUNG_DONGS", None)
        if dongs is None:
            dongs = [str(getattr(config, "GAEPO_WOOSUNG_DONG", "개포동"))]
        dong_s = _safe_str(dong)
        if not _dong_matches(dong_s, dongs):
            return False
        apt_text = _safe_str(apt)
        if not apt_text:
            return False
        apt_norm = re.sub(r"\s+", "", apt_text)
        gw_label = str(getattr(config, "GAEPO_WOOSUNG_LABEL", "개포우성 1,2차"))
        if apt_norm == gw_label.replace(" ", ""):
            return True
        if _GAEPO_WOOSUNG_APT_RE.search(apt_text):
            return True
        return "개포우성" in apt_norm
    except Exception:
        return False


def get_gaepo_woosung_area_rules() -> list[tuple[str, float, float]]:
    raw = getattr(
        config,
        "GAEPO_WOOSUNG_AREA_RULES",
        [("24평형", 84.0, 85.0), ("34평형", 127.0, 129.0)],
    )
    return [(str(label), float(lo), float(hi)) for label, lo, hi in raw]


def assign_gaepo_woosung_pyeong_group(area_m2: float) -> str | None:
    """개포우성 1,2차: 84~85㎡→24평형(31평 UI), 127~129㎡→34평형(44평 UI)."""
    m2 = _parse_area_m2(area_m2)
    if m2 is None:
        return None
    for label, lo, hi in get_gaepo_woosung_area_rules():
        if lo <= m2 <= hi:
            return label
    return None


def is_sinhyundai_apartment(dong: str, apt: str) -> bool:
    """압구정동 신현대·현대9/11/12차(띄어쓰기·신현대9차 등 API 명칭 포함)."""
    try:
        sh_dong = str(getattr(config, "SINHYUNDAI_DONG", "압구정동"))
        dong_s = _safe_str(dong)
        if sh_dong not in dong_s:
            return False
        apt_text = _safe_str(apt)
        if not apt_text:
            return False
        if apt_text == "신현대":
            return True
        if _SINHYUNDAI_APT_RE.search(apt_text):
            return True
        apt_norm = re.sub(r"\s+", "", apt_text)
        for n in (9, 11, 12):
            if apt_norm.startswith(f"신현대{n}차") or apt_norm.startswith(f"현대{n}차"):
                return True
        return False
    except Exception:
        return False


def get_sinhyundai_area_rules() -> list[tuple[str, float, float]]:
    raw = getattr(
        config,
        "SINHYUNDAI_AREA_RULES",
        [("34평형", 107.0, 109.0)],
    )
    return [(str(label), float(lo), float(hi)) for label, lo, hi in raw]


def assign_sinhyundai_pyeong_group(area_m2: float) -> str | None:
    """신현대: 107~109㎡→34평형(34평 UI)만."""
    m2 = _parse_area_m2(area_m2)
    if m2 is None:
        return None
    for label, lo, hi in get_sinhyundai_area_rules():
        if lo <= m2 <= hi:
            return label
    return None


def area_m2_to_pyeong_string(
    area_m2: float,
    *,
    dong: str = "",
    apt: str = "",
) -> str | None:
    """
    전용면적(㎡) → UI 평형 문자열 ('24평', '34평', '44평' 등).
    개포우성 127~129㎡ → '44평', 신현대 107~109㎡ → '34평' 명시 매핑.
    """
    m2 = _parse_area_m2(area_m2)
    if m2 is None:
        return None
    dong_s = _safe_str(dong)
    apt_s = _safe_str(apt)
    display_apt = apt_s or dong_s

    if is_gaepo_woosung_apartment(dong_s, apt_s):
        if 84.0 <= m2 <= 85.0:
            return "31평"
        if 127.0 <= m2 <= 129.0:
            return "44평"
        return None

    if is_sinhyundai_apartment(dong_s, apt_s):
        if 107.0 <= m2 <= 109.0:
            return "34평"
        return None

    if is_sambu_apartment(dong_s, apt_s):
        group = assign_sambu_pyeong_group(m2)
        return display_pyeong_for_apartment(display_apt, group) if group else None
    if is_jamsil_jugong5_apartment(dong_s, apt_s):
        group = assign_jamsil_jugong5_pyeong_group(m2)
        return display_pyeong_for_apartment(display_apt, group) if group else None
    if is_sinbanpo2_apartment(dong_s, apt_s):
        group = assign_sinbanpo2_pyeong_group(m2)
        return display_pyeong_for_apartment(display_apt, group) if group else None

    if 57.0 <= m2 < 63.0:
        return "24평"
    if 82.0 <= m2 < 87.0:
        return "34평"
    return None


def pyeong_string_to_group(display_pyeong: str) -> str | None:
    return PYEONG_STRING_TO_GROUP.get(str(display_pyeong or "").strip())


def assign_pyeong_group_from_m2(
    area_m2: float,
    *,
    dong: str = "",
    apt: str = "",
) -> str | None:
    """
    전용면적(㎡)으로 24/34평형 반환. 삼부·잠실주공5·신반포2차는 전용 예외 규칙 적용.
    - 일반 24평형: 57.0 ≤ ㎡ < 63.0
    - 일반 34평형: 82.0 ≤ ㎡ < 87.0
    """
    if is_sambu_apartment(dong, apt):
        return assign_sambu_pyeong_group(area_m2)
    if is_jamsil_jugong5_apartment(dong, apt):
        return assign_jamsil_jugong5_pyeong_group(area_m2)
    if is_sinbanpo2_apartment(dong, apt):
        return assign_sinbanpo2_pyeong_group(area_m2)
    if is_gaepo_woosung_apartment(dong, apt):
        return assign_gaepo_woosung_pyeong_group(area_m2)
    if is_sinhyundai_apartment(dong, apt):
        return assign_sinhyundai_pyeong_group(area_m2)
    m2 = _parse_area_m2(area_m2)
    if m2 is None:
        return None
    if 57.0 <= m2 < 63.0:
        return "24평형"
    if 82.0 <= m2 < 87.0:
        return "34평형"
    return None


def assign_pyeong_group_for_cache(
    area_m2: float,
    *,
    dong: str = "",
    apt: str = "",
    targets: list[TargetDict] | None = None,
) -> str | None:
    """캐시 저장용 평형그룹 — area_m2_to_pyeong_string() 기반."""
    try:
        m2 = _parse_area_m2(area_m2)
        if m2 is None:
            return None
        dong_s = _safe_str(dong)
        apt_s = _safe_str(apt)
        display = area_m2_to_pyeong_string(m2, dong=dong_s, apt=apt_s)
        if display is None:
            return None
        group = pyeong_string_to_group(display)
        if group is None:
            return None
        if is_allowed_area_m2(m2, group, dong=dong_s, apt=apt_s):
            return group
        return None
    except Exception as exc:
        _log_row_parse_error("pyeong_cache", exc)
        return None


def is_allowed_area_m2(
    area_m2: float,
    group: str,
    *,
    dong: str = "",
    apt: str = "",
) -> bool:
    """평형그룹과 전용면적(㎡)이 규칙에 일치하는지 검증."""
    if area_m2 is None or pd.isna(area_m2) or group not in ALLOWED_PYEONG_GROUPS:
        return False
    m2 = float(area_m2)
    if is_sambu_apartment(dong, apt):
        for label, lo, hi in get_sambu_area_rules():
            if label == group:
                return lo <= m2 < hi
        return False
    if is_jamsil_jugong5_apartment(dong, apt):
        for label, lo, hi in get_jamsil_jugong5_area_rules():
            if label == group:
                return lo <= m2 < hi
        return False
    if is_sinbanpo2_apartment(dong, apt):
        for label, lo, hi in get_sinbanpo2_area_rules():
            if label == group:
                return lo <= m2 < hi
        return False
    if is_gaepo_woosung_apartment(dong, apt):
        for label, lo, hi in get_gaepo_woosung_area_rules():
            if label == group:
                return lo <= m2 <= hi
        return False
    if is_sinhyundai_apartment(dong, apt):
        for label, lo, hi in get_sinhyundai_area_rules():
            if label == group:
                return lo <= m2 <= hi
        return False
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


def _safe_assign_pyeong_from_m2_row(row: pd.Series) -> str | None:
    try:
        apt = resolve_apt_for_pyeong_rules(row)
        display = area_m2_to_pyeong_string(
            _parse_area_m2(row.get("전용면적(㎡)")),
            dong=_safe_str(row.get("법정동", "")),
            apt=apt,
        )
        return pyeong_string_to_group(display) if display else None
    except Exception as exc:
        _log_row_parse_error("add_pyeong", exc)
        return None


def _safe_is_allowed_area_row(row: pd.Series) -> bool:
    try:
        return is_allowed_area_m2(
            _parse_area_m2(row.get("전용면적(㎡)")),
            _safe_str(row.get("평형그룹", "")),
            dong=_safe_str(row.get("법정동", "")),
            apt=resolve_apt_for_pyeong_rules(row),
        )
    except Exception as exc:
        _log_row_parse_error("area_allowed", exc)
        return False


def add_pyeong_columns(df: pd.DataFrame) -> pd.DataFrame:
    """초정밀 ㎡ 구간으로 24·34평형만 남기고 나머지는 전부 삭제합니다."""
    if df.empty:
        return df
    out = df.copy()
    if "전용면적(㎡)" not in out.columns:
        return out.iloc[0:0].copy()

    out["전용면적(㎡)"] = pd.to_numeric(out["전용면적(㎡)"], errors="coerce")
    out["평형그룹"] = out.apply(
        lambda r: _safe_assign_pyeong_from_m2_row(r),
        axis=1,
    )

    out["평형"] = out.apply(
        lambda r: area_m2_to_pyeong_string(
            r.get("전용면적(㎡)"),
            dong=_safe_str(r.get("법정동", "")),
            apt=resolve_apt_for_pyeong_rules(r),
        ),
        axis=1,
    )

    # 32평(70~74㎡)·90㎡+ 등 비허용 면적 즉시 삭제 — 44평·31평은 유지
    out = out[
        out["평형그룹"].isin(ALLOWED_PYEONG_GROUPS)
        & out["평형"].isin(ALLOWED_DISPLAY_PYEONG)
    ].copy()
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
    out = out.dropna(subset=["평형그룹", "평형"])

    if "전용면적(㎡)" in out.columns:
        out["전용면적(㎡)"] = pd.to_numeric(out["전용면적(㎡)"], errors="coerce")
        out = out[
            out.apply(_safe_is_allowed_area_row, axis=1)
        ].copy()

    display = out["타겟명"] if "타겟명" in out.columns else out["아파트"]
    out["차트라벨"] = [
        format_chart_label(str(n).strip(), str(p))
        for n, p in zip(display, out["평형그룹"])
    ]
    # 면적 기반 평형 문자열 우선 (127㎡→44평, 107㎡→34평)
    out["평형"] = out.apply(
        lambda r: area_m2_to_pyeong_string(
            r.get("전용면적(㎡)"),
            dong=_safe_str(r.get("법정동", "")),
            apt=str(r.get("타겟명", r.get("아파트", ""))).strip(),
        ),
        axis=1,
    )
    out = out[out["평형"].isin(ALLOWED_DISPLAY_PYEONG)].copy()

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
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    cleaned = re.sub(r"[^\d.]", "", str(value).strip())
    if not cleaned:
        return None
    try:
        result = float(cleaned)
        if pd.isna(result):
            return None
        return result
    except (TypeError, ValueError):
        return None


def row_matches_crawl_target(row: dict[str, str] | pd.Series) -> bool:
    """국토부 API aptNm이 수집 대상(CRAWL_APARTMENT_API_NAMES·TARGET)인지."""
    try:
        dong = _safe_str(row.get("법정동", ""))  # type: ignore[union-attr]
        apt = _safe_str(row.get("아파트", ""))  # type: ignore[union-attr]
        if not apt:
            return False
        if (
            is_gaepo_woosung_apartment(dong, apt)
            or is_sinhyundai_apartment(dong, apt)
            or is_sambu_apartment(dong, apt)
            or is_jamsil_jugong5_apartment(dong, apt)
            or is_sinbanpo2_apartment(dong, apt)
        ):
            return True
        apt_norm = re.sub(r"\s+", "", apt)
        for name in getattr(config, "CRAWL_APARTMENT_API_NAMES", []):
            n = re.sub(r"\s+", "", _safe_str(name))
            if not n:
                continue
            if apt_norm == n or n in apt_norm or apt == _safe_str(name):
                return True
        targets = parse_targets(getattr(config, "TARGET_APARTMENTS", []))
        if not targets:
            return False
        mini = pd.DataFrame(
            [{"법정동": dong, "아파트": apt, "전용면적(㎡)": row.get("전용면적(㎡)", "")}]  # type: ignore[union-attr]
        )
        return not filter_by_targets(mini, targets).empty
    except Exception as exc:
        _log_row_parse_error("crawl_target_match", exc)
        return False


def get_data_cache_fingerprint(*, app_cache_version: str = "") -> str:
    """캐시 CSV 수정 시각·크기 해시 — Streamlit st.cache_data 무효화용."""
    import hashlib

    from rent_service import RENT_CACHE_CSV, LEGACY_RENT_CACHE_CSV

    parts: list[str] = [
        str(getattr(config, "CRAWL_DATA_VERSION", "")),
        str(app_cache_version),
    ]
    for path in (SALE_CACHE_CSV, LEGACY_SALE_CACHE_CSV, RENT_CACHE_CSV, LEGACY_RENT_CACHE_CSV):
        if path.exists():
            stat = path.stat()
            parts.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
        else:
            parts.append(f"{path.name}:missing")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _read_crawl_version_stamp() -> str:
    if not CRAWL_VERSION_FILE.exists():
        return ""
    return CRAWL_VERSION_FILE.read_text(encoding="utf-8").strip()


def _write_crawl_version_stamp() -> None:
    version = str(getattr(config, "CRAWL_DATA_VERSION", ""))
    CRAWL_VERSION_FILE.write_text(version, encoding="utf-8")


def crawl_version_changed() -> bool:
    expected = str(getattr(config, "CRAWL_DATA_VERSION", ""))
    return _read_crawl_version_stamp() != expected


def reprocess_sale_cache() -> pd.DataFrame:
    """기존 매매 캐시 CSV — 평형·타겟 규칙 재적용 후 저장."""
    cached = load_cached_data()
    if cached.empty:
        return cached
    out = enforce_strict_pyeong_on_dataframe(cached)
    if not out.empty:
        out = out.drop_duplicates(
            subset=[
                "조회지역코드",
                "조회계약년월",
                "아파트",
                "계약일자",
                "거래금액(만원)",
                "전용면적(㎡)",
                "층",
            ],
            keep="last",
        )
        save_cached_data(out)
    return out


def import_supplemental_rent_csv(csv_path: Path | None = None) -> int:
    """
    전월세 전용 보충 CSV → rent_data.csv 병합.
    data.csv(매매 거래금액만 있는 파일) 등은 절대 병합하지 않음.
    """
    from rent_service import (
        compute_converted_jeonse_deposit,
        enforce_strict_pyeong_on_rent_dataframe,
        load_cached_rent_data,
        save_cached_rent_data,
    )

    rel = str(getattr(config, "SUPPLEMENTAL_RENT_CSV", "") or "").strip()
    if not rel:
        return 0
    path = csv_path or (BASE_DIR / rel)
    if not path.exists():
        return 0

    raw = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    if raw.empty or "아파트" not in raw.columns:
        return 0

    area_col = "전용면적(㎡)" if "전용면적(㎡)" in raw.columns else "전용면적"
    rows: list[dict[str, object]] = []
    for _, r in raw.iterrows():
        try:
            tx_type = _safe_str(r.get("거래유형", ""))
            if tx_type == TRANSACTION_TYPE_SALE:
                continue

            deposit = _parse_amount(str(r.get("보증금(만원)", "")))
            monthly = _parse_amount(str(r.get("월세(만원)", "")))
            if deposit is None and monthly is None:
                # 거래금액(만원)만 있는 행 = 매매 보충 데이터 → 전월세 캐시 병합 금지
                continue
            if tx_type and tx_type != TRANSACTION_TYPE_RENT:
                continue

            row_dict = {
                "법정동": _safe_str(r.get("법정동", "")),
                "아파트": _safe_str(r.get("아파트", "")),
                "전용면적(㎡)": r.get(area_col, r.get("전용면적(㎡)")),
            }
            if not row_matches_crawl_target(row_dict):
                continue
            m2 = _parse_area_m2(row_dict.get("전용면적(㎡)"))
            if m2 is None:
                continue
            if not is_area_in_collection_whitelist(
                m2,
                dong=row_dict["법정동"],
                apt=row_dict["아파트"],
            ):
                continue
            group = assign_pyeong_group_for_cache(
                m2,
                dong=row_dict["법정동"],
                apt=row_dict["아파트"],
            )
            if group is None:
                continue

            converted = compute_converted_jeonse_deposit(deposit, monthly)
            if converted is None:
                continue
            contract_date = _safe_str(r.get("계약일자", ""))
            deal_ym = _safe_str(
                r.get("조회계약년월", contract_date[:6] if len(contract_date) >= 6 else "")
            )
            rows.append(
                {
                    "아파트": row_dict["아파트"],
                    "건축년도": r.get("건축년도", ""),
                    "계약기간": r.get("계약기간", ""),
                    "계약구분": r.get("계약구분", ""),
                    "계약일": _safe_str(r.get("계약일", "")),
                    "계약월": _safe_str(r.get("계약월", "")),
                    "계약년": _safe_str(r.get("계약년", "")),
                    "보증금(만원)": deposit or 0.0,
                    "전용면적(㎡)": m2,
                    "층": r.get("층", ""),
                    "지번": r.get("지번", ""),
                    "월세(만원)": monthly or 0.0,
                    "종전계약보증금": r.get("종전계약보증금", ""),
                    "종전계약월세": r.get("종전계약월세", ""),
                    "지역코드": r.get("지역코드", r.get("조회지역코드", "11680")),
                    "법정동": row_dict["법정동"],
                    "갱신요구권사용": r.get("갱신요구권사용", ""),
                    "평형그룹": group,
                    "환산보증금(만원)": converted,
                    "거래금액(만원)": converted,
                    "거래유형": TRANSACTION_TYPE_RENT,
                    "조회지역코드": _safe_str(r.get("조회지역코드", "11680")),
                    "조회계약년월": deal_ym,
                    "계약일자": contract_date,
                }
            )
        except Exception as exc:
            _log_row_parse_error("supplemental_csv", exc)
            continue

    if not rows:
        return 0

    supplement = enforce_strict_pyeong_on_rent_dataframe(pd.DataFrame(rows))
    if supplement.empty:
        return 0

    cached = load_cached_rent_data()
    combined = (
        pd.concat([cached, supplement], ignore_index=True)
        if not cached.empty
        else supplement
    )
    combined = combined.drop_duplicates(
        subset=[
            "조회지역코드",
            "조회계약년월",
            "아파트",
            "계약일자",
            "거래금액(만원)",
            "전용면적(㎡)",
            "층",
        ],
        keep="last",
    )
    save_cached_rent_data(combined)
    return len(supplement)


def reprocess_rent_cache(*, import_supplemental: bool = False) -> pd.DataFrame:
    """기존 전월세 캐시 — 전월세 전용 평형 재적용 후 저장 (매매 파이프라인과 분리)."""
    from rent_service import (
        enforce_strict_pyeong_on_rent_dataframe,
        load_cached_rent_data,
        save_cached_rent_data,
    )

    cached = load_cached_rent_data()
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
    if import_supplemental:
        import_supplemental_rent_csv()
    return load_cached_rent_data()


def refresh_local_cache_files(*, import_supplemental: bool = False) -> dict[str, int]:
    """API 없이 로컬 CSV 재처리 (매매·전월세 각각 독립)."""
    sale = reprocess_sale_cache()
    rent = reprocess_rent_cache(import_supplemental=import_supplemental)
    _write_crawl_version_stamp()
    return {"sale_rows": len(sale), "rent_rows": len(rent)}


def classify_row_at_ingest(row: dict[str, str]) -> dict[str, str] | None:
    """
    API 행 1건 — CRAWL_APARTMENT_API_NAMES·TARGET_APARTMENTS 매칭 후 평형 부여.
    비허용 면적·비타겟 단지는 None(폐기).
    """
    try:
        if not row_matches_crawl_target(row):
            return None
        m2 = _parse_area_m2(row.get("전용면적(㎡)"))
        if m2 is None:
            return None
        dong = _safe_str(row.get("법정동", ""))
        apt = _safe_str(row.get("아파트", ""))
        if not is_area_in_collection_whitelist(m2, dong=dong, apt=apt):
            return None
        targets = parse_targets(getattr(config, "TARGET_APARTMENTS", []))
        group = assign_pyeong_group_for_cache(
            m2,
            dong=dong,
            apt=apt,
            targets=targets,
        )
        if group is None:
            return None
        row = dict(row)
        row["전용면적(㎡)"] = str(m2)
        row["평형그룹"] = group
        row["거래유형"] = TRANSACTION_TYPE_SALE
        return row
    except Exception as exc:
        _log_row_parse_error("classify_ingest", exc)
        return None


# 캐시에 남아 있을 수 있는 파생 컬럼 — 항상 전용면적(㎡)에서 재계산
_DERIVED_DASHBOARD_COLUMNS = (
    "평형그룹",
    "평형",
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
    ctx = f"trade {lawd_cd}/{deal_ymd}"

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
            response = _requests_get_with_retries(API_URL, params, context=page_ctx)
            if response is None:
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
                    classified = classify_row_at_ingest(row)
                    if classified:
                        all_rows.append(classified)
                except Exception as exc:
                    _log_row_parse_error("fetch_trade", exc)
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
    if "법정동" in out.columns:
        out["법정동"] = out["법정동"].fillna("").astype(str)
    if "아파트" in out.columns:
        out["아파트"] = out["아파트"].fillna("").astype(str)
    return out


def enforce_strict_pyeong_on_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """전용면적(㎡) 기준으로 평형그룹을 재부여·검증하고 비허용 행을 삭제합니다."""
    if df.empty:
        return df
    try:
        out = normalize_raw_dataframe(df)
    except Exception as exc:
        _log_row_parse_error("normalize_raw", exc)
        return df.iloc[0:0].copy()
    targets = parse_targets(getattr(config, "TARGET_APARTMENTS", []))
    out["평형그룹"] = out.apply(
        lambda r: _safe_assign_pyeong_group_for_row(r, targets=targets),
        axis=1,
    )
    return out[out["평형그룹"].notna()].copy()


def _target_row_mask(df: pd.DataFrame, target: TargetDict) -> pd.Series:
    """타겟 단지 행 매칭 (exact_name·번들 매칭·regex 유연화)."""
    if not {"법정동", "아파트"}.issubset(df.columns):
        return pd.Series(False, index=df.index)
    dong_col = df["법정동"].fillna("").astype(str)
    apt_series = df["아파트"].fillna("").astype(str).str.strip()
    if target.get("match_all_gaepo_woosung"):
        return pd.Series(
            [
                is_gaepo_woosung_apartment(d, a)
                for d, a in zip(dong_col, apt_series, strict=False)
            ],
            index=df.index,
        )
    if target.get("match_all_sinhyundai"):
        return pd.Series(
            [
                is_sinhyundai_apartment(d, a)
                for d, a in zip(dong_col, apt_series, strict=False)
            ],
            index=df.index,
        )
    dong = target["dong"]
    name = target["name"]
    dong_mask = dong_col.str.contains(dong, case=False, na=False)
    label = str(target.get("label") or "").strip()
    if target.get("exact_name") is True:
        apt_mask = apt_series == name
        if label and label != name:
            apt_mask = apt_mask | (apt_series == label)
    else:
        apt_mask = apt_series.str.contains(name, case=False, na=False)
        if label and label != name:
            apt_mask = apt_mask | apt_series.str.contains(label, case=False, na=False)
    return dong_mask & apt_mask


def filter_by_targets(df: pd.DataFrame, targets: list[TargetDict]) -> pd.DataFrame:
    if df.empty or not {"법정동", "아파트"}.issubset(df.columns):
        return df.iloc[0:0].copy()

    pieces: list[pd.DataFrame] = []
    for target in targets:
        dong, name = target["dong"], target["name"]
        display_name = target.get("label") or name
        label = target_label(target)
        mask = _target_row_mask(df, target)
        chunk = df.loc[mask].copy()
        if chunk.empty:
            continue
        chunk["타겟라벨"] = label
        chunk["타겟동"] = dong
        chunk["타겟명"] = display_name
        if display_name == str(getattr(config, "GAEPO_WOOSUNG_LABEL", "개포우성 1,2차")):
            chunk["아파트"] = display_name
        if display_name == str(getattr(config, "SINHYUNDAI_LABEL", "신현대")):
            chunk["아파트"] = display_name
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


def filter_sale_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """매매 캐시 — 전월세 행 제거 + 거래유형 고정."""
    if df.empty:
        return df
    out = df.copy()
    if "거래유형" in out.columns:
        mask = out["거래유형"].astype(str).str.strip().isin(("", TRANSACTION_TYPE_SALE, "nan"))
        out = out[mask | out["거래유형"].isna()].copy()
    out["거래유형"] = TRANSACTION_TYPE_SALE
    return out


def _resolve_sale_cache_path() -> Path:
    if SALE_CACHE_CSV.exists():
        return SALE_CACHE_CSV
    if LEGACY_SALE_CACHE_CSV.exists():
        return LEGACY_SALE_CACHE_CSV
    return SALE_CACHE_CSV


def load_cached_data() -> pd.DataFrame:
    path = _resolve_sale_cache_path()
    if not path.exists():
        return pd.DataFrame()
    return filter_sale_transactions(
        pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    )


def save_cached_data(df: pd.DataFrame) -> None:
    out = filter_sale_transactions(df)
    out.to_csv(SALE_CACHE_CSV, index=False, encoding="utf-8-sig")


def clear_cache_file() -> None:
    for path in (SALE_CACHE_CSV, LEGACY_SALE_CACHE_CSV):
        if path.exists():
            path.unlink()


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

    if crawl_version_changed() and not force_rebuild:
        force_rebuild = True

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

    def _flush_sale_frames(*, final: bool = False) -> None:
        """force_rebuild 중에도 월별 수집분을 디스크에 누적 저장."""
        nonlocal cached, new_frames
        if not new_frames:
            return
        try:
            new_df = enforce_strict_pyeong_on_dataframe(
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
                        "거래금액(만원)",
                        "전용면적(㎡)",
                        "층",
                    ],
                    keep="last",
                )
                save_cached_data(cached)
            new_frames = []
        except Exception as exc:
            _log_row_parse_error("flush_sale_frames" if not final else "merge_all_frames", exc)

    for idx, (lawd_cd, deal_ymd) in enumerate(tasks, start=1):
        region = _region_label(lawd_cd, lawd_codes.index(lawd_cd))
        msg = f"{region} {deal_ymd[:4]}.{deal_ymd[4:]}월 수집 중..."
        if progress:
            progress(idx / max(total_tasks, 1), msg)

        try:
            chunk = fetch_apt_trade_data(service_key, lawd_cd, deal_ymd)
            if chunk is not None and not chunk.empty:
                chunk = chunk.copy()
                chunk["조회지역코드"] = lawd_cd
                chunk["조회계약년월"] = deal_ymd
                new_frames.append(chunk)
        except Exception as exc:
            _log_api_fetch_error(f"{region} {deal_ymd}", exc)
            if progress:
                progress(
                    idx / max(total_tasks, 1),
                    f"오류({deal_ymd}): {exc} — 다음 월로 진행",
                )
        finally:
            time.sleep(API_SLEEP_SEC)

        if force_rebuild and idx % 5 == 0:
            _flush_sale_frames()

    if new_frames:
        _flush_sale_frames(final=True)

    if not cached.empty:
        try:
            cached = enforce_strict_pyeong_on_dataframe(cached)
            cached = cached.drop_duplicates(
                subset=["조회지역코드", "조회계약년월", "아파트", "계약일자", "거래금액(만원)", "전용면적(㎡)", "층"],
                keep="last",
            )
            save_cached_data(cached)
        except Exception as exc:
            _log_row_parse_error("save_sale_cache", exc)

    # 매매 캐시만 재처리 — 전월세 캐시는 rent_service.update_rent_cache에서만 갱신
    reprocess_sale_cache()
    cached = load_cached_data()
    _write_crawl_version_stamp()

    if progress and total_tasks == 0:
        progress(1.0, "캐시가 최신 상태입니다. (로컬 재처리·보충 반영 완료)")
    elif progress:
        progress(1.0, f"완료 - 총 {len(cached):,}건")

    return cached


def prepare_dashboard_data(
    raw_df: pd.DataFrame,
    targets: list[TargetDict],
) -> pd.DataFrame:
    """대시보드용: 타겟 필터 → 단지별 평형 분류 → 차트용 컬럼 선계산."""
    if raw_df.empty:
        return raw_df
    base = normalize_raw_dataframe(raw_df)
    drop_cols = [c for c in _DERIVED_DASHBOARD_COLUMNS if c in base.columns]
    if drop_cols:
        base = base.drop(columns=drop_cols)
    # 타겟 단지를 먼저 고른 뒤, 삼부·잠실주공5 등 단지별 ㎡ 규칙을 적용
    filtered = filter_by_targets(base, targets)
    if filtered.empty:
        return filtered
    filtered = add_pyeong_columns(filtered)
    return enrich_chart_columns(filtered)


def sort_chart_labels(labels: list[str], targets: list[TargetDict]) -> list[str]:
    """DASHBOARD_ALLOWED_COMPLEX_LABELS 순 → 평형 순으로 범례/선택 목록 정렬."""
    order = getattr(config, "DASHBOARD_ALLOWED_COMPLEX_LABELS", [])
    pyeong_rank = {name: i for i, name in enumerate(all_pyeong_labels())}

    def sort_key(label: str) -> tuple[int, int, str]:
        apt_part = label.rsplit(" (", 1)[0] if " (" in label else label
        pyeong_part = label.rsplit(" (", 1)[-1].rstrip(")") if " (" in label else ""
        try:
            apt_rank = order.index(apt_part)
        except ValueError:
            apt_rank = len(order)
        return (apt_rank, pyeong_rank.get(pyeong_part, 999), label)

    return sorted(labels, key=sort_key)


def get_apartment_select_column(df: pd.DataFrame) -> str:
    """UI 선택용 아파트 컬럼 (표시명 우선)."""
    if "타겟명" in df.columns and df["타겟명"].notna().any():
        return "타겟명"
    return "아파트"


def default_chart_selection(
    all_labels: list[str],
    target_pyeong: list[str] | None,
    *,
    default_pyeong_groups: list[str] | None = None,
) -> list[str]:
    """
    차트 기본 선택 라벨.
    default_pyeong_groups: 초기 접속용 평형(예: ['24평형']만). None이면 target_pyeong 사용.
    """
    if not all_labels:
        return []
    groups = default_pyeong_groups if default_pyeong_groups is not None else target_pyeong
    if not groups:
        return all_labels
    selected = [lb for lb in all_labels if any(f"({p})" in lb for p in groups)]
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
