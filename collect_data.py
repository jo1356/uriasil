"""
터미널에서 캐시 데이터만 수집/업데이트 (대시보드 없이)
실행:
  python collect_data.py           # 누락분만 추가
  python collect_data.py --rebuild # 캐시 삭제 후 전체 재수집
  python collect_data.py --reprocess # 로컬 CSV 재처리·data.csv 보충
"""

from __future__ import annotations

import argparse
import sys

from fetch_data import main as fetch_main


def main() -> None:
    parser = argparse.ArgumentParser(description="아파트 실거래가 캐시 수집")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="매매·전월세 캐시 삭제 후 2014~현재 전체 재수집",
    )
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help="API 없이 로컬 CSV 재처리·data.csv 보충 병합",
    )
    parser.add_argument(
        "--rent-only",
        action="store_true",
        help="전월세 캐시만 수집/업데이트",
    )
    args = parser.parse_args()

    sys.argv = [sys.argv[0]]
    if args.rebuild:
        sys.argv.append("--rebuild")
    if args.reprocess:
        sys.argv.append("--reprocess")
    if args.rent_only:
        sys.argv.append("--rent-only")
    fetch_main()


if __name__ == "__main__":
    main()
