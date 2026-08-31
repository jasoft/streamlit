# -*- coding: utf-8 -*-
"""tqsdk 补充测试: 平值商品期权 + ETF期权权限边界"""
import warnings, time, json
warnings.filterwarnings('ignore')
from tqsdk import TqApi, TqAuth

AUTH = TqAuth("soj", "vmMAGzQ0NOg88H")
OUT = []
def log(case, ok, detail, ms):
    OUT.append(dict(case=case, ok=ok, detail=str(detail)[:220], ms=ms))
    print(f"[{'PASS' if ok else 'FAIL'}] {case} | {ms}ms | {str(detail)[:170]}")

api = TqApi(auth=AUTH)
try:
    rb = api.get_quote('KQ.m@SHFE.rb')
    api.wait_update(deadline=time.time() + 10)
    print(f"rb主力={rb.last_price}")

    # 平值/浅虚值看涨
    calls = api.query_options('SHFE.rb2610', option_class="CALL")
    strike = round(rb.last_price / 10) * 10  # 平值近似
    atm = [s for s in calls if s.endswith(f"C{strike}")] or calls
    sym = atm[0]
    t0 = time.time()
    oc = api.get_quote(sym)
    api.wait_update(deadline=time.time() + 10)
    ms = round((time.time()-t0)*1000)
    log(f'商品期权平值快照 {sym}', oc.last_price is not None and oc.last_price == oc.last_price,
        f"last={oc.last_price} bid={oc.bid_price1} ask={oc.ask_price1} vol={oc.volume} OI={oc.open_interest}", ms)

    # 深市 ETF 期权权限
    try:
        sz = api.query_options('SZSE.159919', option_class="CALL")
        if sz:
            q = api.get_quote(sz[0])
            api.wait_update(deadline=time.time() + 8)
            log('深市ETF期权(权限测试)', True, f"{sz[0]} last={q.last_price}", ms)
        else:
            log('深市ETF期权(权限测试)', False, 'query 返回空', ms)
    except Exception as e:
        log('深市ETF期权(权限测试)', False, f"权限不足: {str(e)[:100]}", ms)

    # 商品期权批量: 一次订阅 5 个行权价
    t0 = time.time()
    batch = [s for s in calls if s.endswith(f"C{strike}") or s.endswith(f"C{strike+50}") or s.endswith(f"C{strike-50}")][:5]
    if len(batch) < 5:
        batch = calls[:5]
    qs_ = {s: api.get_quote(s) for s in batch}
    api.wait_update(deadline=time.time() + 10)
    ms = round((time.time()-t0)*1000)
    vals = {s: q.last_price for s, q in qs_.items()}
    log('商品期权批量订阅5合约', len(batch) == 5, str(vals)[:180], ms)

finally:
    try:
        api.close()
    except Exception:
        pass
with open('results_tqsdk2.json', 'w') as f:
    json.dump(OUT, f, ensure_ascii=False, indent=2)
print("\nsaved results_tqsdk2.json")
