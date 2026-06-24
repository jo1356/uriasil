"""
Supabase(PostgreSQL) 연결 및 apt_sales / apt_rents 테이블 CRUD.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse, urlunparse

import pandas as pd
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

BASE_DIR = Path(__file__).resolve().parent
SECRETS_FILE = BASE_DIR / ".streamlit" / "secrets.toml"

SALES_TABLE = "apt_sales"
RENTS_TABLE = "apt_rents"

SALE_DEDUP_COLUMNS = [
    "조회지역코드",
    "조회계약년월",
    "아파트",
    "계약일자",
    "거래금액(만원)",
    "전용면적(㎡)",
    "층",
]

RENT_DEDUP_COLUMNS = [
    "조회지역코드",
    "조회계약년월",
    "아파트",
    "계약일자",
    "보증금(만원)",
    "월세(만원)",
    "전용면적(㎡)",
    "층",
]


def normalize_database_url(url: str) -> str:
    """postgresql:// → postgresql+psycopg2:// (SQLAlchemy + psycopg2 호환)."""
    u = str(url or "").strip().strip('"').strip("'")
    if u.startswith("postgresql://") and "+psycopg2" not in u:
        u = u.replace("postgresql://", "postgresql+psycopg2://", 1)
    return _resolve_unreachable_hostname(u)


def _resolve_unreachable_hostname(url: str) -> str:
    """Windows 등에서 AAAA-only 호스트 DNS 실패 시 IPv6 리터럴로 대체."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host or host.startswith("["):
        return url
    try:
        socket.getaddrinfo(host, parsed.port or 5432)
        return url
    except OSError:
        pass

    ipv6 = _lookup_ipv6_via_nslookup(host)
    if not ipv6:
        return url

    userinfo = ""
    if parsed.username:
        userinfo = quote_plus(parsed.username)
        if parsed.password:
            userinfo += f":{quote_plus(parsed.password)}"
        userinfo += "@"

    port = f":{parsed.port}" if parsed.port else ""
    new_netloc = f"{userinfo}[{ipv6}]{port}"
    return urlunparse(parsed._replace(netloc=new_netloc))


def _lookup_ipv6_via_nslookup(host: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["nslookup", host],
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=15,
        )
    except Exception:
        return None
    for line in out.splitlines():
        if "Address" not in line or host in line:
            continue
        candidate = line.split("Address:")[-1].strip()
        if ":" in candidate and candidate.count(".") < 3:
            return candidate
    return None


def _read_database_url_from_secrets_file() -> str:
    if not SECRETS_FILE.exists():
        return ""
    try:
        import tomllib

        data = tomllib.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        return str(data.get("DATABASE_URL", "") or "").strip()
    except Exception:
        text_body = SECRETS_FILE.read_text(encoding="utf-8")
        match = re.search(
            r'^\s*DATABASE_URL\s*=\s*["\']?(.+?)["\']?\s*$',
            text_body,
            re.MULTILINE,
        )
        return match.group(1).strip() if match else ""


def get_database_url() -> str:
    """환경변수 → Streamlit secrets → secrets.toml 순으로 DATABASE_URL 조회."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return normalize_database_url(url)
    try:
        import streamlit as st

        secret = str(st.secrets.get("DATABASE_URL", "") or "").strip()
        if secret:
            return normalize_database_url(secret)
    except Exception:
        pass
    file_url = _read_database_url_from_secrets_file()
    if file_url:
        return normalize_database_url(file_url)
    return ""


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = get_database_url()
    if not url:
        raise RuntimeError(
            "DATABASE_URL이 설정되지 않았습니다. "
            ".streamlit/secrets.toml 또는 환경변수 DATABASE_URL을 확인하세요."
        )
    connect_args: dict[str, Any] = {"connect_timeout": 30}
    if "supabase.co" in url and "sslmode=" not in url:
        connect_args["sslmode"] = "require"
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


def table_exists(table_name: str) -> bool:
    try:
        return inspect(get_engine()).has_table(table_name)
    except Exception:
        return False


def get_table_row_count(table_name: str) -> int:
    """테이블 행 수 — SELECT * 없이 COUNT만."""
    if not table_exists(table_name):
        return 0
    try:
        return int(
            pd.read_sql(
                f'SELECT COUNT(*) AS cnt FROM "{table_name}"',
                get_engine(),
            ).iloc[0]["cnt"]
        )
    except Exception:
        return 0


def get_distinct_slot_pairs(table_name: str) -> set[tuple[str, str]]:
    """(조회지역코드, 조회계약년월) DISTINCT — 전체 테이블 로드 없이 슬롯 집계."""
    if not table_exists(table_name):
        return set()
    try:
        df = pd.read_sql(
            f'''
            SELECT DISTINCT "조회지역코드", "조회계약년월"
            FROM "{table_name}"
            WHERE "조회지역코드" IS NOT NULL AND "조회계약년월" IS NOT NULL
            ''',
            get_engine(),
        )
        if df.empty:
            return set()
        return {
            (str(r["조회지역코드"]), str(r["조회계약년월"]))
            for _, r in df.iterrows()
        }
    except Exception:
        return set()


def read_table(table_name: str) -> pd.DataFrame:
    """테이블 전체 조회 — 없으면 빈 DataFrame."""
    if not table_exists(table_name):
        return pd.DataFrame()
    return pd.read_sql(f'SELECT * FROM "{table_name}"', get_engine())


def _dedupe(df: pd.DataFrame, subset: list[str]) -> pd.DataFrame:
    if df.empty:
        return df
    cols = [c for c in subset if c in df.columns]
    if not cols:
        return df
    return df.drop_duplicates(subset=cols, keep="last")


def write_table(
    df: pd.DataFrame,
    table_name: str,
    *,
    dedup_columns: list[str] | None = None,
) -> int:
    """DataFrame을 테이블에 저장 (replace). 저장 전 dedup 적용."""
    if df is None or df.empty:
        if table_exists(table_name):
            with get_engine().begin() as conn:
                conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
        return 0
    out = df.copy()
    if dedup_columns:
        out = _dedupe(out, dedup_columns)
    out.to_sql(
        table_name,
        get_engine(),
        if_exists="replace",
        index=False,
        method="multi",
        chunksize=500,
    )
    return len(out)


def clear_table(table_name: str) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))


def get_table_fingerprint(table_name: str) -> str:
    """행 수·최신 계약일자 기반 해시 — Streamlit cache 무효화용."""
    import hashlib

    if not table_exists(table_name):
        return f"{table_name}:missing"
    try:
        row = pd.read_sql(
            f'''
            SELECT COUNT(*) AS cnt,
                   MAX("계약일자") AS max_dt
            FROM "{table_name}"
            ''',
            get_engine(),
        ).iloc[0]
        cnt = int(row.get("cnt", 0) or 0)
        max_dt = str(row.get("max_dt") or "")
        return f"{table_name}:{cnt}:{max_dt}"
    except Exception:
        return f"{table_name}:error"


def cache_storage_status(table_name: str) -> dict[str, Any]:
    rows = get_table_row_count(table_name)
    return {
        "exists": rows > 0,
        "rows": rows,
        "path": f"supabase:{table_name}",
    }
