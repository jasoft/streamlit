# -*- coding: utf-8 -*-
"""稳定性复测: 对可用接口按用户场景 1 req/s 连续打 10 轮"""
import warnings, time, json
warnings.filterwarnings('ignore')
import akshare as ak, requests

RES = []
def loop(name, fn, n=10, interval=1.0):
    ok_n, lat, errs = 0, [], {}
    for i in range(n):
        t0 = time.time()
        try:
            fn()
            ok_n += 1
            lat.append(time.time()-t0)
        except Exception as e:
            k = type(e).__name__ + ':' + str(e)[:60]
            errs[k] = errs.get(k, 0) + 1
        time.sleep(interval)
    avg = round(sum(lat)/len(lat)*1000) if lat else None
    RES.append(dict(case=name, n=n, ok=ok_n, avg_ms=avg, errors=errs))
    print(f"{name}: {ok_n}/{n} 成功, 平均 {avg}ms, errors={errs if errs else '无'}")

SINAH = {'Referer':'https://stock.finance.sina.com.cn/','User-Agent':'Mozilla/5.0'}

loop('akshare futures_zh_spot(RB0)', lambda: ak.futures_zh_spot(symbol='RB0', market='CF', adjust='0'))
loop('akshare futures_zh_spot(IF0)', lambda: ak.futures_zh_spot(symbol='IF0', market='CFF', adjust='0'))
loop('sina直连 CON_OP_ 批量期权', lambda: requests.get(
    'https://hq.sinajs.cn/list=CON_OP_10011255,CON_OP_10011256,CON_OP_10011257',
    headers=SINAH, timeout=8).raise_for_status())
loop('akshare option_sse_spot_price_sina', lambda: ak.option_sse_spot_price_sina('10011255'), n=5)
loop('akshare option_current_day_sse(官网)', lambda: ak.option_current_day_sse(), n=3, interval=2)
loop('akshare option_current_day_szse(官网)', lambda: ak.option_current_day_szse(), n=3, interval=2)
loop('akshare futures_display_main_sina', lambda: ak.futures_display_main_sina(), n=3, interval=2)
loop('akshare futures_zh_realtime(螺纹钢)', lambda: ak.futures_zh_realtime(symbol='螺纹钢'), n=3, interval=2)
loop('efinance futures.get_realtime_quotes', lambda: __import__('efinance').futures.get_realtime_quotes(), n=5, interval=2)
loop('qstock realtime_data(期货)', lambda: __import__('qstock').realtime_data('期货'), n=3, interval=2)

with open('results_stability.json', 'w') as f:
    json.dump(RES, f, ensure_ascii=False, indent=2)
print('saved results_stability.json')
