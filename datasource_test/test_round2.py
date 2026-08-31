# -*- coding: utf-8 -*-
"""第二轮: 修正提取字段 + 补充用例"""
import warnings, time, json, concurrent.futures as cf
warnings.filterwarnings('ignore')
import requests

RESULTS = []
def run_case(lib, category, api, fn, timeout=30):
    def _wrap():
        t0 = time.time()
        try:
            ok, detail = fn()
            return ok, detail, round((time.time()-t0)*1000)
        except Exception as e:
            return False, f"EXC {type(e).__name__}: {str(e)[:160]}", round((time.time()-t0)*1000)
    with cf.ThreadPoolExecutor(1) as ex:
        try:
            ok, detail, ms = ex.submit(_wrap).result(timeout=timeout)
        except cf.TimeoutError:
            ok, detail, ms = False, f"TIMEOUT >{timeout}s", int(timeout*1000)
    RESULTS.append(dict(lib=lib, category=category, api=api, ok=bool(ok), detail=detail, ms=ms))
    print(f"[{'PASS' if ok else 'FAIL'}] {lib:12s} | {category:8s} | {api:44s} | {ms:6d}ms | {detail[:100]}")

import akshare as ak
CAT_F, CAT_F2, CAT_EO, CAT_CO = '商品期货', '金融期货', 'ETF期权', '商品期权'

def spot_rb():
    df = ak.futures_zh_spot(symbol='RB0', market='CF', adjust='0')
    v = df.iloc[0]
    return (v['current_price'] > 0, f"RB0 {v['name'] if 'name' in df.columns else v['symbol']} last={v['current_price']} bid={v['bid_price']} ask={v['ask_price']}")
run_case('akshare', CAT_F, 'futures_zh_spot(RB0)[修正]', spot_rb)

def spot_m():
    df = ak.futures_zh_spot(symbol='M0', market='CF', adjust='0')
    return (df.iloc[0]['current_price'] > 0, f"M0 last={df.iloc[0]['current_price']}")
run_case('akshare', CAT_F, 'futures_zh_spot(M0)', spot_m)

def spot_if():
    df = ak.futures_zh_spot(symbol='IF0', market='CFF', adjust='0')
    v = df.iloc[0]
    return (v['current_price'] > 0, f"IF0 last={v['current_price']}")
run_case('akshare', CAT_F2, 'futures_zh_spot(IF0,CFF)[修正]', spot_if)

def spot_im():
    df = ak.futures_zh_spot(symbol='IM0', market='CFF', adjust='0')
    return (df.iloc[0]['current_price'] > 0, f"IM0 last={df.iloc[0]['current_price']}")
run_case('akshare', CAT_F2, 'futures_zh_spot(IM0,CFF)', spot_im)

# ETF 期权: 用真实期权代码
def opt_spot_fix():
    codes = ak.option_sse_codes_sina(symbol='看涨期权', trade_date='202609', underlying='510050')
    code = str(codes['期权代码'].iloc[0])
    df = ak.option_sse_spot_price_sina(code)
    v = df.iloc[0].to_dict()
    return (len(df) > 0, f"code={code} cols={list(df.columns)[:5]} head={str(v)[:80]}")
run_case('akshare', CAT_EO, 'option_sse_spot_price_sina[修正]', opt_spot_fix)

def sse_official():
    df = ak.option_current_day_sse()
    return (len(df) > 0, f"上交所官网 rows={len(df)} cols={list(df.columns)[:6]}")
run_case('akshare', CAT_EO, 'option_current_day_sse(上交所官网)', sse_official)

def szse_content():
    df = ak.option_current_day_szse()
    cols = list(df.columns)
    has_price = any('价' in c for c in cols)
    return (len(df) > 0 and has_price, f"深交所官网 rows={len(df)} cols={cols[:8]}")
run_case('akshare', CAT_EO, 'option_current_day_szse[内容检查]', szse_content)

# 商品期权: 新浪直连格式探索
def get_rb_opt_codes():
    df = ak.option_commodity_contract_sina(symbol='螺纹钢期权')
    return [str(x) for x in df['合约'].tolist()]

def co_sina_formats():
    codes = get_rb_opt_codes()
    sample = codes[:2] if codes else ['rb2610']
    fmts = []
    for c in sample:
        base = c.upper()
        fmts += [f"OPT_{base}", f"OPT_o_{base}", f"o_{base}", f"nf_OPT_{base}", f"hf_OPT_{base}"]
    r = requests.get("https://hq.sinajs.cn/list=" + ",".join(fmts),
                     headers={'Referer': 'https://finance.sina.com.cn'}, timeout=8)
    nonempty = [l for l in r.text.splitlines() if '=""' not in l and 'var' in l]
    return (len(nonempty) > 0, f"样本={sample} 命中={len(nonempty)} resp[:80]={r.text[:80]!r}")
run_case('akshare', CAT_CO, '新浪直连5种格式(实验)', co_sina_formats)

def co_sina_page():
    # 新浪商品期权行情页数据(全量当日)
    url = "https://stock.finance.sina.com.cn/futures/api/openapi.php/OptionService.getOptionData"
    r = requests.get(url, params={'cate': 'pg_o', 'kind': 1}, timeout=8,
                     headers={'Referer': 'https://finance.sina.com.cn'})
    return (r.status_code == 200 and len(r.text) > 100, f"HTTP {r.status_code} len={len(r.text)} head={r.text[:60]!r}")
run_case('akshare', CAT_CO, '新浪 getOptionData 页面源', co_sina_page)

# efinance 复测(验证 push2 间歇性)
def ef_retry():
    import efinance as ef
    df = ef.futures.get_realtime_quotes()
    return (len(df) > 0, f"rows={len(df)}")
run_case('efinance', CAT_F, 'get_realtime_quotes[复测2]', ef_retry)

# qstock future_info 复测
def qs_fi():
    import qstock as qs
    df = qs.future_info()
    return (len(df) > 0, f"rows={len(df)}")
run_case('qstock', CAT_F, 'future_info()[复测2]', qs_fi)

with open('results2.json', 'w') as f:
    json.dump(RESULTS, f, ensure_ascii=False, indent=2)
print("\nsaved results2.json,", len(RESULTS), "cases")
