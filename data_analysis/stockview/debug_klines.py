import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from data_analysis.stockview.fund_flow import _load_rank_snapshot, _pick_default_top_names, _load_selected_sector_minute_klines

snapshot = _load_rank_snapshot("industry", "today")
top_n = 12
default_names = _pick_default_top_names(snapshot.rows, top_n)

klines = _load_selected_sector_minute_klines(snapshot, default_names)
valid = 0
for name in default_names:
    df = klines.get(name)
    if df is not None and not df.empty:
        valid += 1
    else:
        print(f"Empty or None for {name}")

print(f"Valid klines: {valid} out of {len(default_names)}")
