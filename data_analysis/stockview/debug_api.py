import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from data_analysis.stockview.fund_flow import _load_rank_snapshot, _get_session
import requests

snapshot = _load_rank_snapshot("industry", "today")
name_code_map = snapshot.name_code_map

test_name = "电子" # this was one of the empty ones
code = name_code_map[test_name]
print(f"Code for {test_name}: {code}")

url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
params = {
    "secid": f"90.{code}",
    "fields1": "f1,f2,f3,f7",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
    "lmt": "0",
    "klt": "1",
    "ut": "b2884a393a59ad64002292a3e90d46a5",
}
resp = requests.get(url, params=params)
print(resp.json())
