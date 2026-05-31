"""
국토부 API 실거래 데이터 수집 — collect_data.py 래퍼 + 로컬 캐시 재처리.

실행:
  python fetch_data.py                  # 누락 월 API 수집 + 캐시 재처리·보충 병합
  python fetch_data.py --rebuild        # 캐시 삭제 후 2014~현재 전체 재수집
  python fetch_data.py --reprocess      # API 없이 CSV 재처리·data.csv 보충만
"""

from __future__ import annotations

import argparse

import config
from data_service import (
    cache_status,
    rebuild_cache_from_scratch,
    refresh_local_cache_files,
    update_cache,
    validate_service_key,
)
from rent_service import (
    rebuild_rent_cache_from_scratch,
    rent_cache_status,
    update_rent_cache,
)


def _print_crawl_targets() -> None:
    names = getattr(config, "CRAWL_APARTMENT_API_NAMES", [])
    print(f"  수집 대상 API 명칭 ({len(names)}개):")
    for name in names:
        print(f"    - {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="국토부 아파트 실거래·전월세 수집")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="매매·전월세 캐시 삭제 후 2014~현재 전체 재수집",
    )
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help="API 호출 없이 로컬 CSV 재처리 + data.csv 보충 병합만 실행",
    )
    parser.add_argument(
        "--rent-only",
        action="store_true",
        help="전월세만 수집/재처리",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  fetch_data.py - 국토부 API 수집")
    _print_crawl_targets()
    print("=" * 60)

    if args.reprocess:
        stats = refresh_local_cache_files(import_supplemental=False)
        print(f"\n  재처리 완료: 매매 {stats['sale_rows']:,}건 / 전월세 {stats['rent_rows']:,}건\n")
        return

    validate_service_key()
    sale_status = cache_status()
    rent_status = rent_cache_status()
    print(f"  매매: {sale_status['rows']:,}건 ({sale_status['filled_slots']}/{sale_status['total_slots']})")
    print(f"  전월세: {rent_status['rows']:,}건 ({rent_status['filled_slots']}/{rent_status['total_slots']})")
    if args.rebuild:
        print("  모드: 전체 재수집")
    print("-" * 60)

    def progress(ratio: float, msg: str) -> None:
        print(f"  [{ratio * 100:5.1f}%] {msg}")

    if args.rent_only:
        if args.rebuild:
            df = rebuild_rent_cache_from_scratch(progress)
        else:
            df = update_rent_cache(progress)
        print(f"\n  전월세 완료: {len(df):,}건\n")
        return

    if args.rebuild:
        sale_df = rebuild_cache_from_scratch(progress)
        rent_df = rebuild_rent_cache_from_scratch(progress)
        print(f"\n  매매: {len(sale_df):,}건 / 전월세: {len(rent_df):,}건\n")
    else:
        sale_df = update_cache(progress)
        rent_df = update_rent_cache(progress)
        print(f"\n  매매: {len(sale_df):,}건 / 전월세: {len(rent_df):,}건\n")


if __name__ == "__main__":
    main()
