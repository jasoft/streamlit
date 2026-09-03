import sys
import os
import requests
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from data_analysis.stockview.fund_flow import _get_session
session = _get_session()
url = "https://push2.eastmoney.com/api/qt/clist/get"
params = {
    "pn": "1",
    "pz": "500",
    "po": "1",
    "np": "1",
    "ut": "b2884a393a59ad64002292a3e90d46a5",
    "fltt": "2",
    "invt": "2",
    "fid0": "f62",
    "fs": "m:90 t:2",
    "stat": "1",
    "fields": "f12,f14",
    "rt": "52975239",
}
resp = session.get(url, params=params)
data = resp.json()["data"]["diff"]
for i in range(5):
    print(data[i])
print("Total:", len(data))
