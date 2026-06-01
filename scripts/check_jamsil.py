"""Quick check for Jamsil Jugong 5 data."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from data_service import (
    filter_by_targets,
    is_jamsil_jugong5_apartment,
    load_cached_data,
    parse_targets,
    prepare_dashboard_data,
)
from rent_service import load_cached_rent_data

targets = parse_targets(config.TARGET_APARTMENTS)
sale_raw = load_cached_data()
rent_raw = load_cached_rent_data()

apt_col = "아파트"
for label, df in [("sale_raw", sale_raw), ("rent_raw", rent_raw)]:
    m = df[apt_col].astype(str).str.replace(" ", "", regex=False)
    j = m.str.contains("잠실주공5", na=False) | m.str.contains("주공아파트5", na=False)
    print(f"{label} jugong-like rows: {j.sum()}")
    if j.any():
        print("  names:", df.loc[j, apt_col].unique()[:5])
    if "법정동" in df.columns:
        sub = df[df[apt_col].astype(str).str.contains("주공", na=False)]
        print(f"  주공 rows: {len(sub)}, dong sample: {sub['법정동'].head(3).tolist()}")

print("\nfilter_by_targets:")
sf = filter_by_targets(sale_raw, targets)
rf = filter_by_targets(rent_raw, targets)
sj = sf[sf["타겟명"] == "잠실주공5단지"] if "타겟명" in sf.columns else sf
rj = rf[rf["타겟명"] == "잠실주공5단지"] if "타겟명" in rf.columns else rf
print(f"  sale matched: {len(sj)}")
print(f"  rent matched: {len(rj)}")

print("\nprepare_dashboard_data:")
from data_service import filter_by_targets, add_pyeong_columns
filtered = filter_by_targets(sale_raw, targets)
fj = filtered[filtered["타겟명"] == "잠실주공5단지"]
print(f"  after filter: {len(fj)}, 평형그룹 in cache: {fj['평형그룹'].unique() if '평형그룹' in fj.columns else 'n/a'}")
after = add_pyeong_columns(fj)
print(f"  after add_pyeong_columns: {len(after)}")
if len(after) == 0 and len(fj) > 0:
    from data_service import resolve_apt_for_pyeong_rules, area_m2_to_pyeong_string, _parse_area_m2
    r = fj.iloc[0]
    apt = resolve_apt_for_pyeong_rules(r)
    m2 = _parse_area_m2(r.get("전용면적(㎡)"))
    disp = area_m2_to_pyeong_string(m2, dong=str(r.get("법정동")), apt=apt)
    disp2 = area_m2_to_pyeong_string(m2, dong=str(r.get("법정동")), apt=str(r.get("타겟명")))
    print(f"  sample m2={m2} apt_resolve={apt!r} pyeong={disp!r} pyeong_tgt={disp2!r}")

sd = prepare_dashboard_data(sale_raw, targets)
rd = prepare_dashboard_data(rent_raw, targets)
print(f"  sale dashboard: {len(sd[sd['타겟명']=='잠실주공5단지']) if not sd.empty else 0}")
print(f"  rent dashboard: {len(rd[rd['타겟명']=='잠실주공5단지']) if not rd.empty else 0}")

print("\nis_jamsil_jugong5_apartment samples:")
for apt in ["주공아파트 5단지", "잠실주공5단지", "잠실주공 5단지"]:
    print(f"  {apt!r}: {is_jamsil_jugong5_apartment('잠실동', apt)}")
