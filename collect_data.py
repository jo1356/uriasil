"""
터미널에서 캐시 데이터만 수집/업데이트 (대시보드 없이)
실행:
  python collect_data.py           # 누락분만 추가
  python collect_data.py --rebuild # 캐시 삭제 후 전체 재수집
"""

from __future__ import annotations

import argparse

from data_service import cache_status, rebuild_cache_from_scratch, update_cache, validate_service_key
from rent_service import (
    rebuild_rent_cache_from_scratch,
    rent_cache_status,
    update_rent_cache,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="아파트 실거래가 캐시 수집")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="매매·전월세 캐시 삭제 후 2014~현재 전체 재수집",
    )
    parser.add_argument(
        "--rent-only",
        action="store_true",
        help="전월세 캐시만 수집/업데이트",
    )
    args = parser.parse_args()

    validate_service_key()
    sale_status = cache_status()
    rent_status = rent_cache_status()
    print("=" * 60)
    print("  아파트 실거래가 캐시 수집")
    print("=" * 60)
    print(f"  매매: {sale_status['rows']:,}건 ({sale_status['filled_slots']}/{sale_status['total_slots']})")
    print(f"  전월세: {rent_status['rows']:,}건 ({rent_status['filled_slots']}/{rent_status['total_slots']})")
    if args.rebuild:
        print("  모드: 전체 재수집 (캐시 초기화)")
    print("-" * 60)

    def progress(ratio: float, msg: str) -> None:
        print(f"  [{ratio * 100:5.1f}%] {msg}")

    if args.rent_only:
        if args.rebuild:
            df = rebuild_rent_cache_from_scratch(progress)
        else:
            df = update_rent_cache(progress)
        print(f"\n  전월세 완료: {len(df):,}건 -> all_combined_rent_data.csv\n")
    elif args.rebuild:
        sale_df = rebuild_cache_from_scratch(progress)
        rent_df = rebuild_rent_cache_from_scratch(progress)
        print(f"\n  매매: {len(sale_df):,}건 / 전월세: {len(rent_df):,}건\n")
    else:
        sale_df = update_cache(progress)
        rent_df = update_rent_cache(progress)
        print(f"\n  매매: {len(sale_df):,}건 / 전월세: {len(rent_df):,}건\n")


if __name__ == "__main__":
    main()
