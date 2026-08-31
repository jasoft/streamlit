#!/usr/bin/env python3
"""多数据源实时行情高频压测：测各源在高频轮询下的稳定性与封 IP 风险。

协议（每个源相同）：
  A) 基线   3 次请求
  B) 高频   100 次连发（串行、无 sleep，模拟最激进的实时轮询）
  C) 复测   休息 60 秒后再 10 次，判断是否被限流/封禁及恢复情况
"""
import json
import re
import socket
import statistics
import sys
import time
import traceback

import requests

CODE = "600519"  # 贵州茅台，周末休市价格定格不影响连通性测试
BURST = 100
RECOVER_N = 10
REST_SECONDS = 60

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# ── 各源的取价函数：返回 (price: float|None, detail: str)；抛异常算失败 ──

def fetch_tencent():
    r = sess.get("https://qt.gtimg.cn/q=sh600519", timeout=5)
    r.raise_for_status()
    m = re.search(r'v_sh600519="([^"]+)"', r.content.decode("gbk", "ignore"))
    if not m:
        return None, "no payload"
    f = m.group(1).split("~")
    if len(f) < 4 or not f[3]:
        return None, "empty price"
    return float(f[3]), f[1]


def fetch_sina():
    # 新浪 2022 起必须带 Referer，否则 4xx
    r = sess.get("https://hq.sinajs.cn/list=sh600519", timeout=5,
                 headers={"Referer": "https://finance.sina.com.cn/"})
    r.raise_for_status()
    m = re.search(r'hq_str_sh600519="([^"]*)"', r.content.decode("gbk", "ignore"))
    if not m or not m.group(1):
        return None, "empty payload"
    f = m.group(1).split(",")
    if len(f) < 4 or not f[3]:
        return None, "empty price"
    return float(f[3]), f[0]


def fetch_eastmoney():
    r = sess.get("https://push2.eastmoney.com/api/qt/stock/get", timeout=5, params={
        "secid": "1.600519", "fltt": "2", "invt": "2",
        "fields": "f43,f57,f58",
    })
    r.raise_for_status()
    d = r.json().get("data") or {}
    if d.get("f43") in (None, "-"):
        return None, "empty data"
    return float(d["f43"]), str(d.get("f58", ""))


def fetch_baidu():
    r = sess.get("https://finance.pae.baidu.com/selfselect/getstockquotation", timeout=5, params={
        "all": "1", "isIndex": "false", "isBk": "false", "isBlock": "false",
        "isFutures": "false", "isStock": "true", "newFormat": "1",
        "group": "quotation_kline_ab", "finClientType": "pc", "code": CODE, "ktype": "1",
    }, headers={"Referer": "https://gushitong.baidu.com/"})
    r.raise_for_status()
    d = r.json().get("Result", {}).get("newMarketData", {})
    rows = [x for x in d.get("marketData", "").split(";") if x]
    if not rows:
        return None, "empty rows"
    last = rows[-1].split(",")
    # keys[1]=open? 实测 keys: time,open,close,... 这里只验证有价可取
    return float(last[2]), last[0][:16]


_MOOTDX = {}

def fetch_mootdx():
    if "client" not in _MOOTDX:
        from mootdx import config
        from mootdx.quotes import Quotes
        # mootdx 0.11.x: factory(server=...) 会被 config.set bug 吞掉，实际连
        # config.json 里陈旧的 BESTIP（实测 180.153.18.170 已死）→ 必须 set BESTIP
        for ip in ["110.41.147.114", "124.71.187.122", "116.205.163.254",
                   "123.60.70.228", "122.51.120.217", "115.238.90.165"]:
            try:
                config.set("BESTIP", {"HQ": (ip, 7709)})
                c = Quotes.factory(market="std")
                q = c.quotes(symbol=[CODE])
                if q is not None and not q.empty and q.iloc[0]["price"] > 0:
                    _MOOTDX["client"] = c
                    _MOOTDX["server"] = ip
                    break
            except Exception:
                continue
        else:
            raise RuntimeError("no live tdx server")
    c = _MOOTDX["client"]
    q = c.quotes(symbol=[CODE])
    if q is None or q.empty:
        return None, "empty quote"
    return float(q.iloc[0]["price"]), _MOOTDX.get("server", "tdx")


SOURCES = {
    "tencent": fetch_tencent,
    "sina": fetch_sina,
    "eastmoney_push2": fetch_eastmoney,
    "baidu": fetch_baidu,
    "mootdx_tcp": fetch_mootdx,
}

sess = requests.Session()
sess.headers.update({"User-Agent": UA})


def run_phase(name, fn, n, label):
    lat, ok, fail = [], 0, {}
    sample_price = None
    for i in range(n):
        t0 = time.perf_counter()
        try:
            price, detail = fn()
            dt = (time.perf_counter() - t0) * 1000
            lat.append(dt)
            ok += 1
            if price is not None and sample_price is None:
                sample_price = price
            if price is None:
                fail[f"data:{detail}"] = fail.get(f"data:{detail}", 0) + 1
        except Exception as e:
            dt = (time.perf_counter() - t0) * 1000
            lat.append(dt)
            key = f"{type(e).__name__}:{str(e)[:80]}"
            fail[key] = fail.get(key, 0) + 1
    res = {
        "phase": label, "n": n, "ok": ok, "fail": fail,
        "p50_ms": round(statistics.median(lat), 1) if lat else None,
        "p95_ms": round(sorted(lat)[int(len(lat) * 0.95)] if len(lat) > 1 else lat[0], 1) if lat else None,
        "max_ms": round(max(lat), 1) if lat else None,
        "sample_price": sample_price,
    }
    print(f"  [{name}] {label}: {ok}/{n} ok, p50={res['p50_ms']}ms p95={res['p95_ms']}ms", flush=True)
    if fail:
        print(f"    fails: {fail}", flush=True)
    return res


def main():
    recovery_only = len(sys.argv) > 1 and sys.argv[1] == "recovery_only"
    results = {}
    for name, fn in SOURCES.items():
        print(f"=== {name} ===", flush=True)
        r = {"baseline": None, "burst": None, "recover": None}
        try:
            r["baseline"] = run_phase(name, fn, 3, "baseline x3")
            if not recovery_only or name == "mootdx_tcp":
                r["burst"] = run_phase(name, fn, BURST, f"burst x{BURST}")
        except Exception as e:
            r["fatal"] = f"{type(e).__name__}: {e}"
            print(f"  FATAL: {r['fatal']}", flush=True)
            traceback.print_exc()
        results[name] = r
        if name != list(SOURCES)[-1]:
            print("  rest 5s before next source...", flush=True)
            time.sleep(5)
    print(f"\n--- 休息 {REST_SECONDS}s 后复测（判断封禁/恢复） ---", flush=True)
    time.sleep(REST_SECONDS)
    for name, fn in SOURCES.items():
        try:
            results[name]["recover"] = run_phase(name, fn, RECOVER_N, f"recover x{RECOVER_N}")
        except Exception as e:
            results[name]["recover"] = {"error": str(e)[:120]}
            print(f"  [{name}] recover FATAL: {e}", flush=True)
    out = "/tmp/rate_test_result.json"
    with open(out, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nsaved -> {out}", flush=True)


if __name__ == "__main__":
    main()
