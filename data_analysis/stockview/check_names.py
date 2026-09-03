import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from data_analysis.stockview.fund_flow import _load_rank_snapshot, _pick_default_top_names, _load_selected_sector_minute_klines, _build_trend_frame
import plotly.express as px
import json

snapshot = _load_rank_snapshot("industry", "today")
top_n = 3
default_names = _pick_default_top_names(snapshot.rows, top_n)
klines = _load_selected_sector_minute_klines(snapshot, default_names)
trend_df = _build_trend_frame(klines)

fig = px.line(trend_df, x="时间", y="主力净流入", color="板块")
data = json.loads(fig.to_json())["data"]
for trace in data:
    print("Trace Name:", trace.get("name"))
    print("Trace Hovertemplate:", trace.get("hovertemplate"))
