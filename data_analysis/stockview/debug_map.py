import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from data_analysis.stockview.fund_flow import _load_rank_snapshot, _get_sector_code_name_map
snapshot = _load_rank_snapshot("industry", "today")
name_code_map = snapshot.name_code_map
df_names = set([row["name"] for row in snapshot.rows])
map_names = set(name_code_map.keys())

print(f"Names in df: {len(df_names)}")
print(f"Names in map: {len(map_names)}")

missing = df_names - map_names
print(f"In df but not in map: {missing}")

# If we get the code from snapshot.rows directly?
for row in snapshot.rows:
    if row["name"] == "电子":
        print(f"Code for 电子 from df: {row.get('_code')}")
