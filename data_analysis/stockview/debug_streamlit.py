import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from data_analysis.stockview.fund_flow import _load_rank_snapshot, _pick_default_top_names

snapshot = _load_rank_snapshot("industry", "today")
print(f"Total rows: {len(snapshot.rows)}")

top_n = 12
default_names = _pick_default_top_names(snapshot.rows, top_n)
print(f"Default names length: {len(default_names)}")
print("Default names:")
for name in default_names:
    print(" - " + name)
