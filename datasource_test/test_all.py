# -*- coding: utf-8 -*-
"""金融数据源实时行情统一测试: 商品期货/金融期货/ETF期权/商品期权"""
import warnings, time, json, traceback, concurrent.futures as cf
warnings.filterwarnings('ignore')

RESULTS = []

def run_case(lib, category, api, fn, timeout=30):
    """fn: callable -> (ok: bool, detail: str)"""
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
    print(f"[{'PASS' if ok else 'FAIL'}] {lib:12s} | {category:8s} | {api:42s} | {ms:6d}ms | {detail[:90]}")

# ---------- akshare ----------
def t_akshare():
    import akshare as ak
    CAT_F = '商品期货'; CAT_F2 = '金融期货'
    # 主力合约总表
    def display_main():
        df = ak.futures_display_main_sina()
        has_rb = 'RB' in ''.join(df['symbol'].astype(str).values)
        return (has_rb, f"rows={len(df)} cols={list(df.columns)[:6]} symbols含RB={has_rb}")
    run_case('akshare', CAT_F, 'futures_display_main_sina', display_main)
    # 商品期货 spot
    def spot_rb():
        df = ak.futures_zh_spot(symbol='RB0', market='CF', adjust='0')
        v = df.iloc[0].to_dict()
        price = v.get('last_price') or v.get('最新价')
        return (price is not None and float(price) > 0, f"RB0 last={price} time={v.get('time')}")
    run_case('akshare', CAT_F, 'futures_zh_spot(RB0)', spot_rb)
    def spot_contract():
        df = ak.futures_zh_spot(symbol='RB2610', market='CF', adjust='0')
        v = df.iloc[0].to_dict()
        price = v.get('last_price') or v.get('最新价')
        return (price is not None and float(price) > 0, f"RB2610 last={price}")
    run_case('akshare', CAT_F, 'futures_zh_spot(RB2610)', spot_contract)
    # 品种级实时
    def realtime_rb():
        df = ak.futures_zh_realtime(symbol='螺纹钢')
        return (len(df) > 0, f"rows={len(df)}")
    run_case('akshare', CAT_F, 'futures_zh_realtime(螺纹钢)', realtime_rb)
    # 金融期货
    def spot_if():
        df = ak.futures_zh_spot(symbol='IF0', market='CFF', adjust='0')
        v = df.iloc[0].to_dict()
        price = v.get('last_price') or v.get('最新价')
        return (price is not None and float(price) > 0, f"IF0 last={price}")
    run_case('akshare', CAT_F2, 'futures_zh_spot(IF0,CFF)', spot_if)
    def main_if():
        df = ak.futures_main_sina(symbol='IF0', start_date='20260820', end_date='20260830')
        last = df.iloc[-1].to_dict()
        return (len(df) > 0, f"rows={len(df)} last_close={last.get('close')}")
    run_case('akshare', CAT_F2, 'futures_main_sina(IF0)', main_if)
    # ETF 期权
    CAT_EO = 'ETF期权'
    def und():
        df = ak.option_sse_underlying_spot_price_sina('sh510050')
        return (len(df) > 0, f"rows={len(df)} head={df.iloc[0].to_dict() if len(df) else {}}"[:120])
    run_case('akshare', CAT_EO, 'option_sse_underlying_spot_price_sina', und)
    def codes():
        df = ak.option_sse_codes_sina(symbol='看涨期权', trade_date='202609', underlying='510050')
        return (len(df) > 0, f"rows={len(df)} first={df.iloc[0].to_dict() if len(df) else {}}"[:120])
    run_case('akshare', CAT_EO, 'option_sse_codes_sina(2609)', codes)
    def opt_spot():
        codes_df = ak.option_sse_codes_sina(symbol='看涨期权', trade_date='202609', underlying='510050')
        code = str(codes_df.iloc[0, 0])
        df = ak.option_sse_spot_price_sina(code)
        return (len(df) > 0, f"code={code} rows={len(df)}"[:120])
    run_case('akshare', CAT_EO, 'option_sse_spot_price_sina', opt_spot)
    def em_opt():
        df = ak.option_current_em()
        return (len(df) > 0, f"rows={len(df)}")
    run_case('akshare', CAT_EO, 'option_current_em(东财)', em_opt)
    def szse_day():
        df = ak.option_current_day_szse()
        return (len(df) > 0, f"rows={len(df)}")
    run_case('akshare', CAT_EO, 'option_current_day_szse', szse_day)
    # 商品期权
    CAT_CO = '商品期权'
    def co_contract():
        df = ak.option_commodity_contract_sina(symbol='螺纹钢期权')
        return (len(df) > 0, f"rows={len(df)} cols={list(df.columns)[:5]}")
    run_case('akshare', CAT_CO, 'option_commodity_contract_sina', co_contract)
    def co_sina_direct():
        # 新浪商品期权快照: 尝试常见代码格式
        import requests
        codes_df = ak.option_commodity_contract_sina(symbol='螺纹钢期权')
        code = str(codes_df.iloc[0, 0]) if len(codes_df) else ''
        url = f"https://hq.sinajs.cn/list=OPT_{code}"
        r = requests.get(url, headers={'Referer': 'https://finance.sina.com.cn'}, timeout=8)
        empty = '""' in r.text
        return (not empty, f"code={code} resp={r.text[:80]!r}")
    run_case('akshare', CAT_CO, 'sina OPT_ 直连(实验)', co_sina_direct)

# ---------- efinance ----------
def t_efinance():
    import efinance as ef
    def fut_rt():
        df = ef.futures.get_realtime_quotes()
        return (len(df) > 0, f"rows={len(df)} cols={list(df.columns)[:6]}")
    run_case('efinance', '商品期货', 'futures.get_realtime_quotes', fut_rt)
    def fut_rt_specific():
        df = ef.futures.get_realtime_quotes()
        cff = df[df['市场'].astype(str).str.contains('中金', na=False)] if '市场' in df.columns else df.head(0)
        return (len(cff) > 0, f"中金所 rows={len(cff)}")
    run_case('efinance', '金融期货', 'get_realtime_quotes→中金所', fut_rt_specific)

# ---------- qstock ----------
def t_qstock():
    import qstock as qs
    def fut_info():
        df = qs.future_info()
        return (len(df) > 0, f"rows={len(df)} cols={list(df.columns)[:6]}")
    run_case('qstock', '商品期货', 'future_info()', fut_info)
    def fut_code():
        df = qs.future_info()
        cff = df[df.iloc[:, 1].astype(str).str.contains('IF|IM|IH|IC', na=False, regex=True)]
        return (len(cff) > 0, f"疑似中金所 rows={len(cff)}")
    run_case('qstock', '金融期货', 'future_info()→IF/IM', fut_code)
    def fut_realtime():
        df = qs.realtime_data('期货') if 'realtime_data' in dir(qs) else None
        if df is None:
            return False, '无 realtime_data(期货) 接口'
        return (len(df) > 0, f"rows={len(df)}")
    run_case('qstock', '商品期货', 'realtime_data(期货)', fut_realtime)

# ---------- pytdx / mootdx ----------
def t_pytdx():
    from pytdx.exhq import TdxExHq_API
    def ext_connect():
        hosts = ['115.238.90.165','218.75.126.9','124.160.88.183','60.12.136.250',
                 '61.152.107.141','124.71.187.122','106.14.201.131','111.229.247.189']
        for ip in hosts:
            api = TdxExHq_API()
            try:
                if api.connect(ip, 7727, time_out=4):
                    return True, f"connected {ip}"
            except Exception:
                pass
        return False, f"全部 {len(hosts)} 台扩展行情服务器 7727 端口拒连"
    run_case('pytdx', '商品期货', 'TdxExHq_API(扩展行情)', ext_connect)

def t_mootdx():
    def ext():
        try:
            from mootdx.quotes import Quotes
            q = Quotes.factory(market='ext')
            df = q.quote('RB2610')
            return (df is not None and len(df) > 0, str(df)[:80])
        except Exception as e:
            return False, str(e)[:160]
    run_case('mootdx', '商品期货', 'Quotes(ext).quote', ext)

# ---------- tqsdk ----------
def t_tqsdk():
    def anon():
        from tqsdk import TqApi
        api = TqApi()
        try:
            q = api.get_quote('KQ.m@SHFE.rb')
            api.wait_update(deadline=time.time()+8)
            return (q.last_price > 0, f"rb last={q.last_price}")
        finally:
            try: api.close()
            except Exception: pass
    run_case('tqsdk', '商品期货', 'TqApi(匿名)+KQ.m@SHFE.rb', anon, timeout=40)
    def with_auth():
        from tqsdk import TqApi, TqAuth
        api = TqApi(auth=TqAuth("test_user", "test_pwd"))
        try:
            q = api.get_quote('KQ.m@SHFE.rb')
            api.wait_update(deadline=time.time()+8)
            return (q.last_price > 0, f"rb last={q.last_price}")
        finally:
            try: api.close()
            except Exception: pass
    run_case('tqsdk', '商品期货', 'TqApi(假账号验证需注册)', with_auth, timeout=40)

# ---------- eltdx ----------
def t_eltdx():
    from eltdx import TdxClient
    def snapshot():
        c = TdxClient(timeout=5)
        rows = c.quotes.get_snapshots([('sh', '510050')])
        return (len(rows) > 0, f"510050 rows={len(rows)}")
    run_case('eltdx', 'ETF期权(仅标的,无期权)', 'quotes.get_snapshots', snapshot)

if __name__ == '__main__':
    for t in [t_akshare, t_efinance, t_qstock, t_pytdx, t_mootdx, t_tqsdk, t_eltdx]:
        try:
            t()
        except Exception as e:
            print(f"!!! suite {t.__name__} crashed: {e}")
        time.sleep(1)
    with open('results.json', 'w') as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)
    print("\nsaved results.json,", len(RESULTS), "cases")
