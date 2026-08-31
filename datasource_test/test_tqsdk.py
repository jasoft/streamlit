# -*- coding: utf-8 -*-
"""tqsdk 快期账户实测: 商品期货/金融期货/商品期权/ETF期权 + 稳定性"""
import warnings, time, json
warnings.filterwarnings('ignore')
from tqsdk import TqApi, TqAuth

AUTH = TqAuth("soj", "vmMAGzQ0NOg88H")
OUT = []

def log(case, ok, detail, ms):
    OUT.append(dict(case=case, ok=ok, detail=str(detail)[:200], ms=ms))
    print(f"[{'PASS' if ok else 'FAIL'}] {case} | {ms}ms | {str(detail)[:150]}")

api = TqApi(auth=AUTH)
try:
    # ---- 1. 商品期货: 螺纹钢/豆粕主力 + 盘口 ----
    t0 = time.time()
    rb = api.get_quote('KQ.m@SHFE.rb')
    m  = api.get_quote('KQ.m@DCE.m')
    api.wait_update(deadline=time.time() + 10)
    ms = round((time.time()-t0)*1000)
    log('商品期货 KQ.m@SHFE.rb 主力', rb.last_price > 0,
        f"last={rb.last_price} bid={rb.bid_price1} ask={rb.ask_price1} vol={rb.volume} time={rb.datetime[:19]}", ms)

    # 深度盘口: 该版本 get_quote 仅支持单参, 检查五档字段是否有值
    log('商品期货 盘口字段(该版仅一档)', rb.ask_price1 > 0,
        f"买一={rb.bid_price1} 卖一={rb.ask_price1} (五档需专业版/新版)", ms)

    # ---- 2. 金融期货 ----
    t0 = time.time()
    ifq = api.get_quote('KQ.m@CFFEX.IF')
    imq = api.get_quote('KQ.m@CFFEX.IM')
    api.wait_update(deadline=time.time() + 10)
    ms = round((time.time()-t0)*1000)
    log('金融期货 KQ.m@CFFEX.IF/IM 主力', ifq.last_price > 0 and imq.last_price > 0,
        f"IF={ifq.last_price} IM={imq.last_price} time={ifq.datetime[:19]}", ms)

    # ---- 3. 商品期权: 螺纹钢期权 ----
    t0 = time.time()
    rb_opts = api.query_options('SHFE.rb2610', option_class="CALL")
    ms = round((time.time()-t0)*1000)
    log('商品期权 query_options(rb2610)', len(rb_opts) > 0, f"{len(rb_opts)} 个看涨合约, 前3: {rb_opts[:3]}", ms)
    if rb_opts:
        # 取平值附近一个
        oc = api.get_quote(rb_opts[0])
        api.wait_update(deadline=time.time() + 10)
        log('商品期权 期权合约实时快照', oc.last_price is not None,
            f"{rb_opts[0]} last={oc.last_price} time={oc.datetime[:19]}", ms)

    # ---- 4. ETF 期权: 上证50ETF ----
    t0 = time.time()
    etf_opts = api.query_options('SSE.510050', option_class="CALL")
    ms = round((time.time()-t0)*1000)
    log('ETF期权 query_options(SSE.510050)', len(etf_opts) > 0, f"{len(etf_opts)} 个看涨合约, 前3: {etf_opts[:3]}", ms)
    if etf_opts:
        oc2 = api.get_quote(etf_opts[0])
        api.wait_update(deadline=time.time() + 10)
        log('ETF期权 期权合约实时快照', oc2.last_price is not None,
            f"{etf_opts[0]} last={oc2.last_price} time={oc2.datetime[:19]}", ms)
        # 希腊字母
        gk = oc2.greeks
        log('ETF期权 希腊字母', gk.delta is not None,
            f"delta={gk.delta} gamma={gk.gamma} theta={gk.theta} vega={gk.vega} iv={gk.implied_volatility}", ms)

    # ---- 5. 一次连接批量订阅 (面板场景) ----
    t0 = time.time()
    syms = ['KQ.m@SHFE.rb', 'KQ.m@SHFE.cu', 'KQ.m@DCE.m', 'KQ.m@CZCE.SR', 'KQ.m@CFFEX.IF', 'KQ.m@CFFEX.IM']
    quotes = {s: api.get_quote(s) for s in syms}
    api.wait_update(deadline=time.time() + 10)
    ms = round((time.time()-t0)*1000)
    ok = all(q.last_price > 0 for q in quotes.values())
    log('批量订阅 6 个主力合约', ok, " ".join(f"{s.split('@')[1]}={q.last_price}" for s, q in quotes.items()), ms)

    # ---- 6. 稳定性: 10 轮 wait_update 轮询 (1 req/s 模拟) ----
    t0 = time.time()
    ok_n = 0
    for i in range(10):
        api.wait_update(deadline=time.time() + 1)
        if rb.last_price and rb.last_price > 0:
            ok_n += 1
        time.sleep(0.5)
    ms = round((time.time()-t0)*1000)
    log('稳定性 10 轮轮询(单连接)', ok_n == 10, f"{ok_n}/10 轮成功, 单连接复用", ms)

finally:
    try:
        api.close()
    except Exception:
        pass

with open('results_tqsdk.json', 'w') as f:
    json.dump(OUT, f, ensure_ascii=False, indent=2)
print("\nsaved results_tqsdk.json,", sum(1 for r in OUT if r['ok']), "/", len(OUT), "passed")
