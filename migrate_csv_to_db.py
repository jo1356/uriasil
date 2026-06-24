"""
1회성: 로컬 매매·전월세 CSV → Supabase apt_sales / apt_rents 테이블 업로드.

실행: python migrate_csv_to_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from database import (
    RENT_DEDUP_COLUMNS,
    RENTS_TABLE,
    SALE_DEDUP_COLUMNS,
    SALES_TABLE,
    get_database_url,
    get_engine,
    write_table,
)

BASE_DIR = Path(__file__).resolve().parent
SALE_CSV_CANDIDATES = [
    BASE_DIR / "sales_data.csv",
    BASE_DIR / "all_combined_data.csv",
]
RENT_CSV_CANDIDATES = [
    BASE_DIR / "rent_data.csv",
    BASE_DIR / "all_combined_rent_data.csv",
]


def _resolve_csv(candidates: list[Path]) -> Path | None:
    for path in candidates:
        if path.exists() and path.stat().st_size > 0:
            return path
    return None


def _load_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", low_memory=False)


def main() -> int:
    if not get_database_url():
        print("ERROR: DATABASE_URL not configured in .streamlit/secrets.toml", file=sys.stderr)
        return 1

    sale_path = _resolve_csv(SALE_CSV_CANDIDATES)
    rent_path = _resolve_csv(RENT_CSV_CANDIDATES)
    if sale_path is None and rent_path is None:
        print("ERROR: No sale or rent CSV files found to migrate.", file=sys.stderr)
        return 1

    print(f"Connecting: {get_database_url().split('@')[-1]}")
    try:
        with get_engine().connect() as conn:
            conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        print("Connection OK")
    except Exception as exc:
        print(f"ERROR: DB connection failed: {exc}", file=sys.stderr)
        return 1

    if sale_path is not None:
        print(f"Loading {sale_path.name} ...")
        sale_df = _load_csv(sale_path)
        print(f"Uploading apt_sales ({len(sale_df):,} rows) ...")
        n = write_table(sale_df, SALES_TABLE, dedup_columns=SALE_DEDUP_COLUMNS)
        print(f"OK apt_sales <- {sale_path.name}: {n:,} rows")
    else:
        print("WARN: No sale CSV found — skipping apt_sales")

    if rent_path is not None:
        print(f"Loading {rent_path.name} ...")
        rent_df = _load_csv(rent_path)
        print(f"Uploading apt_rents ({len(rent_df):,} rows) ...")
        n = write_table(rent_df, RENTS_TABLE, dedup_columns=RENT_DEDUP_COLUMNS)
        print(f"OK apt_rents <- {rent_path.name}: {n:,} rows")
    else:
        print("WARN: No rent CSV found — skipping apt_rents")

    print("Migration complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
