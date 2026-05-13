#!/usr/bin/env python3
"""
市场数据获取脚本 - 增强版 v2
移除需要API key的Stooq，使用Yahoo Finance + 新浪财经 + 东方财富
"""

import argparse
import io
import json
import sys
import time
import urllib.parse
import urllib.request
from typing import Optional, Dict, Any, Tuple

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# 重试配置
MAX_RETRIES = 3
BASE_RETRY_DELAY = 2  # 秒

DEFAULT_EXTRAS = ["copper", "bitcoin"]
DEFAULT_TICKERS = ["0700.HK", "9992.HK"]

EXTRA_ALIASES = {
    "copper": "HG=F",
    "铜": "HG=F",
    "btc": "BTC-USD",
    "bitcoin": "BTC-USD",
    "比特币": "BTC-USD",
    "eth": "ETH-USD",
    "ethereum": "ETH-USD",
    "以太坊": "ETH-USD",
    "brent": "BZ=F",
    "布伦特": "BZ=F",
    "gold": "GC=F",
    "silver": "SI=F",
}

# Yahoo Finance ticker映射
BASE_YAHOO_TICKERS = {
    "黄金期货": "GC=F",
    "白银期货": "SI=F",
    "WTI原油": "CL=F",
    "标普500": "^GSPC",
    "纳斯达克100": "^NDX",
    "道琼斯": "^DJI",
}

# 东方财富美股指数映射
EASTMONEY_US_INDEX = {
    "标普500": "1.00.000001",
    "纳斯达克100": "1.00.NDX",  # 可能需要调整
    "道琼斯": "1.00.DJI",  # 可能需要调整
}

# 新浪商品/期货映射
SINA_COMMODITIES = {
    "黄金": "hf_XAU",
    "白银": "hf_XAG", 
    "WTI原油": "hf_CL",
}


def retry_on_fail(func):
    """重试装饰器 - 指数退避避免限流"""
    def wrapper(*args, **kwargs):
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    # 指数退避
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    time.sleep(delay)
        raise last_error
    return wrapper


def fetch_text(url: str, headers=None, timeout: int = 20) -> str:
    """获取URL文本内容"""
    headers = headers or {"User-Agent": UA}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def fetch_json(url: str, headers=None, timeout: int = 20):
    """获取JSON数据"""
    return json.loads(fetch_text(url, headers=headers, timeout=timeout))


def safe_float(value, default=None):
    """安全转换为float"""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def calc_pct(prev_close: Optional[float], close: Optional[float]) -> Optional[float]:
    """计算涨跌幅百分比"""
    if prev_close in (0, None) or close is None:
        return None
    return (close - prev_close) / prev_close * 100.0


def parse_sina_simple(text: str) -> Dict[str, list]:
    """解析新浪财经简版接口数据"""
    out = {}
    for line in text.splitlines():
        if '="' not in line:
            continue
        left, rest = line.split('="', 1)
        code = left.split("hq_str_", 1)[-1]
        raw = rest.rsplit('"', 1)[0]
        out[code] = raw.split(',')
    return out


@retry_on_fail
def get_cn_hk_indexes() -> Dict[str, Any]:
    """获取A股和港股指数 - 使用新浪财经"""
    codes = ["s_sh000001", "s_sz399001", "s_sh000300", "rt_hkHSI", "rt_hkHSTECH"]
    url = "https://hq.sinajs.cn/list=" + ",".join(codes)
    
    try:
        text = fetch_text(url, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn"})
        data = parse_sina_simple(text)
        
        # A股：新浪s_前缀接口格式：名称,当前价,涨跌,涨跌幅,成交量,成交额（共6个字段）
        a_share = {}
        for code, name in [("s_sh000001", "上证指数"), ("s_sz399001", "深证成指"), ("s_sh000300", "沪深300")]:
            parts = data[code]
            if len(parts) < 6:
                raise RuntimeError(f"{name}数据字段不足: {parts}")
            
            a_share[name] = {
                "close": safe_float(parts[1]),      # 当前价
                "change": safe_float(parts[2]),     # 涨跌（直接提供）
                "pct": safe_float(parts[3]),        # 涨跌幅（直接提供）
                "volume": safe_float(parts[4]),     # 成交量
                "amount": safe_float(parts[5]),     # 成交额
                "source": "新浪财经 hq.sinajs.cn",
            }
        
        return {
            "a_share": a_share,
            "hk": {
                "恒生指数": {
                    "open": safe_float(data["rt_hkHSI"][2]),
                    "prev_close": safe_float(data["rt_hkHSI"][3]),
                    "close": safe_float(data["rt_hkHSI"][4]),
                    "high": safe_float(data["rt_hkHSI"][5]),
                    "low": safe_float(data["rt_hkHSI"][6]),
                    "change": safe_float(data["rt_hkHSI"][7]),  # 港股直接提供涨跌
                    "pct": safe_float(data["rt_hkHSI"][8]),     # 港股直接提供涨跌幅
                    "volume": safe_float(data["rt_hkHSI"][11]),
                    "amount": safe_float(data["rt_hkHSI"][12]),
                    "time": f'{data["rt_hkHSI"][17]} {data["rt_hkHSI"][18]}',
                    "source": "新浪财经 hq.sinajs.cn",
                },
                "恒生科技指数": {
                    "open": safe_float(data["rt_hkHSTECH"][2]),
                    "prev_close": safe_float(data["rt_hkHSTECH"][3]),
                    "close": safe_float(data["rt_hkHSTECH"][4]),
                    "high": safe_float(data["rt_hkHSTECH"][5]),
                    "low": safe_float(data["rt_hkHSTECH"][6]),
                    "change": safe_float(data["rt_hkHSTECH"][7]),  # 港股直接提供涨跌
                    "pct": safe_float(data["rt_hkHSTECH"][8]),     # 港股直接提供涨跌幅
                    "volume": safe_float(data["rt_hkHSTECH"][11]),
                    "amount": safe_float(data["rt_hkHSTECH"][12]),
                    "time": f'{data["rt_hkHSTECH"][17]} {data["rt_hkHSTECH"][18]}',
                    "source": "新浪财经 hq.sinajs.cn",
                },
            },
        }
    except Exception as e:
        print(f"⚠️ 新浪财经数据获取失败: {e}", file=sys.stderr)
        raise


@retry_on_fail
def yahoo_quote(symbol: str, label: str = None, timeout: int = 30) -> Dict[str, Any]:
    """从Yahoo Finance获取数据 - 使用yfinance库"""
    try:
        import yfinance as yf
        
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period='5d')
        
        if len(hist) == 0:
            raise RuntimeError(f"yfinance无数据: {symbol}")
        
        last = hist.iloc[-1]
        prev = hist.iloc[-2] if len(hist) >= 2 else None
        
        close = float(last['Close'])
        prev_close = float(prev['Close']) if prev is not None else None
        
        # 获取更多信息
        try:
            info = ticker.info
            long_name = info.get('longName') or info.get('shortName') or label or symbol
            currency = info.get('currency')
            exchange = info.get('exchange')
        except:
            long_name = label or symbol
            currency = None
            exchange = None
        
        pct = None
        if prev_close is not None:
            pct = round((close - prev_close) / prev_close * 100.0, 4)
        
        return {
            "label": long_name,
            "symbol": symbol,
            "date": last.name.strftime("%Y-%m-%d"),
            "close": close,
            "prev_close": prev_close,
            "pct": pct,
            "open": float(last['Open']),
            "high": float(last['High']),
            "low": float(last['Low']),
            "volume": float(last['Volume']) if 'Volume' in last else None,
            "currency": currency,
            "exchange": exchange,
            "source": "Yahoo Finance (yfinance)",
        }
    except Exception as e:
        raise RuntimeError(f"yfinance获取失败: {e}")


@retry_on_fail
def sina_commodity_quote(symbol_key: str, label: str) -> Dict[str, Any]:
    """从新浪财经获取商品数据"""
    sina_code = SINA_COMMODITIES.get(symbol_key)
    if not sina_code:
        raise ValueError(f"未找到新浪商品映射: {symbol_key}")
    
    url = f"https://hq.sinajs.cn/list={sina_code}"
    text = fetch_text(url, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn"})
    
    for line in text.splitlines():
        if '="' not in line:
            continue
        left, rest = line.split('="', 1)
        code = left.split("hq_str_", 1)[-1]
        raw = rest.rsplit('"', 1)[0]
        parts = raw.split(',')
        
        if len(parts) < 8:
            continue
        
        try:
            close = safe_float(parts[3])
            prev_close = safe_float(parts[2])
            
            if close is None:
                raise RuntimeError("最新价无效")
            
            pct = None
            if prev_close is not None and prev_close != 0:
                pct = round((close - prev_close) / prev_close * 100.0, 4)
            
            return {
                "label": label,
                "symbol": sina_code,
                "date": time.strftime("%Y-%m-%d"),
                "close": close,
                "prev_close": prev_close,
                "pct": pct,
                "open": safe_float(parts[1]),
                "high": safe_float(parts[4]),
                "low": safe_float(parts[5]),
                "volume": safe_float(parts[6]),
                "source": "新浪财经 hq.sinajs.cn",
            }
        except (IndexError, ValueError) as e:
            raise RuntimeError(f"解析新浪数据失败: {e}")
    
    raise RuntimeError(f"新浪返回空数据: {symbol_key}")


def get_base_global() -> Dict[str, Any]:
    """获取全球市场数据 - 使用Yahoo Finance + 新浪财经"""
    base = {}
    
    # 商品使用新浪财经（稳定且免费）
    for label in ["黄金", "白银", "WTI原油"]:
        try:
            base[label] = sina_commodity_quote(label, label)
            print(f"✓ {label}: 新浪财经", file=sys.stderr)
        except Exception as e:
            print(f"⚠️ {label} 新浪失败: {e}", file=sys.stderr)
            # Fallback到Yahoo
            yahoo_sym = {
                "黄金": "GC=F",
                "白银": "SI=F",
                "WTI原油": "CL=F",
            }.get(label)
            if yahoo_sym:
                try:
                    time.sleep(1)  # 避免限流
                    base[label] = yahoo_quote(yahoo_sym, label)
                    print(f"✓ {label}: Yahoo Finance (fallback)", file=sys.stderr)
                except Exception as e2:
                    print(f"❌ {label}: 所有数据源失败", file=sys.stderr)
                    base[label] = create_error_item(label, yahoo_sym)
    
    # 美股指数使用Yahoo Finance（新浪不支持）
    # 增加延迟避免429限流
    for i, (label, yahoo_sym) in enumerate([("标普500", "^GSPC"), ("纳斯达克100", "^NDX"), ("道琼斯", "^DJI")]):
        if i > 0:
            time.sleep(5)  # 每个请求间隔5秒
        try:
            base[label] = yahoo_quote(yahoo_sym, label, timeout=30)
            print(f"✓ {label}: Yahoo Finance", file=sys.stderr)
        except Exception as e:
            print(f"❌ {label}: Yahoo Finance失败 - {e}", file=sys.stderr)
            base[label] = create_error_item(label, yahoo_sym)
    
    return base


def create_error_item(label: str, symbol: str) -> Dict[str, Any]:
    """创建错误数据项"""
    return {
        "label": label,
        "symbol": symbol,
        "date": time.strftime("%Y-%m-%d"),
        "close": None,
        "prev_close": None,
        "pct": None,
        "source": "数据获取失败",
        "error": True,
    }


def resolve_extra_token(token: str) -> str:
    """解析额外资产别名"""
    return EXTRA_ALIASES.get(token.lower(), token)


@retry_on_fail
def get_extras(extra_tokens, tickers) -> Dict[str, Any]:
    """获取额外品种和指定股票"""
    out = {}
    
    # 处理额外资产
    for token in extra_tokens:
        symbol = resolve_extra_token(token)
        try:
            time.sleep(0.5)  # 避免限流
            item = yahoo_quote(symbol, token)
            out[token] = item
            print(f"✓ 额外品种 {token}: Yahoo Finance", file=sys.stderr)
        except Exception as e:
            print(f"⚠️ 额外品种 {token} 失败: {e}", file=sys.stderr)
            out[token] = create_error_item(token, symbol)
    
    # 处理指定股票代码
    for symbol in tickers:
        try:
            time.sleep(0.5)  # 避免限流
            item = yahoo_quote(symbol, symbol)
            out[symbol] = item
            print(f"✓ 指定股票 {symbol}: Yahoo Finance", file=sys.stderr)
        except Exception as e:
            print(f"⚠️ 指定股票 {symbol} 失败: {e}", file=sys.stderr)
            out[symbol] = create_error_item(symbol, symbol)
    
    return out


def fmt_pct(v: Optional[float]) -> str:
    """格式化百分比"""
    return "N/A" if v is None else f"{v:+.2f}%"


def fmt_num(v: Optional[float], digits=2) -> str:
    """格式化数字"""
    return "N/A" if v is None else f"{v:,.{digits}f}"


def render_markdown(data: Dict[str, Any]) -> str:
    """渲染Markdown格式输出"""
    lines = ["# 市场简报", ""]
    
    # A股
    lines.append("## A股")
    for name, item in data["cn_hk"]["a_share"].items():
        lines.append(f"- **{name}**：{fmt_num(item['close'], 2)}，{fmt_pct(item['pct'])}，成交量 {fmt_num(item['volume'], 2)}，成交额 {fmt_num(item['amount'], 2)}")
    
    lines.extend(["", "## 港股"])
    for name, item in data["cn_hk"]["hk"].items():
        lines.append(f"- **{name}**：{fmt_num(item['close'], 2)}，{fmt_pct(item['pct'])}，成交量 {fmt_num(item['volume'], 2)}，成交额 {fmt_num(item['amount'], 2)}")
    
    # 贵金属 / 原油 / 美股
    lines.extend(["", "## 贵金属 / 原油 / 美股"])
    for key in ["黄金", "白银", "WTI原油", "标普500", "纳斯达克100", "道琼斯"]:
        item = data["global"].get(key, {})
        if item.get("error"):
            lines.append(f"- **{key}**：数据获取失败 [{item.get('symbol', 'N/A')}]")
        else:
            lines.append(f"- **{key}**：{fmt_num(item['close'], 2)}，{fmt_pct(item['pct'])}（{item.get('date', 'N/A')}）[{item.get('source', 'unknown')}]")
    
    # 额外品种
    if data.get("extras"):
        lines.extend(["", "## 额外品种 / 指定股票"])
        for key, item in data["extras"].items():
            extra = []
            if item.get("symbol") and item.get("symbol") != key:
                extra.append(item["symbol"])
            if item.get("currency"):
                extra.append(item["currency"])
            suffix = f" [{' / '.join(extra)}]" if extra else ""
            
            if item.get("error"):
                lines.append(f"- **{item['label']}**{suffix}：数据获取失败")
            else:
                lines.append(f"- **{item['label']}**{suffix}：{fmt_num(item['close'], 2)}，{fmt_pct(item['pct'])}（{item.get('date', 'N/A')}）[{item.get('source', 'unknown')}]")
    
    # 数据源说明
    lines.extend([
        "",
        "## 数据源",
        "- A股/港股：新浪财经 hq.sinajs.cn（稳定免费）",
        "- 黄金/白银/WTI：新浪财经 hf_XAU/XAG/CL",
        "- 美股指数/个股/加密货币：Yahoo Finance (yfinance库)",
        "- yfinance 内置重试机制，无429限流问题",
    ])
    
    return "\n".join(lines)


def main():
    """主函数"""
    p = argparse.ArgumentParser(description="获取A股、港股、黄金白银油价、美股和自定义股票/资产简报（增强版v3 - yfinance）")
    p.add_argument("--format", choices=["json", "markdown"], default="markdown")
    p.add_argument("--extra", nargs="*", default=[], help="额外资产别名，如 copper bitcoin brent")
    p.add_argument("--ticker", nargs="*", default=[], help="指定 Yahoo Finance 代码，如 AAPL TSLA 0700.HK 9992.HK 600519.SS")
    p.add_argument("--include-default-extras", action="store_true", help="默认附加 copper 和 bitcoin")
    p.add_argument("--include-user-defaults", action="store_true", help="附加用户默认关注：copper bitcoin 0700.HK 9992.HK")
    p.add_argument("--debug", action="store_true", help="显示调试信息")
    args = p.parse_args()
    
    extras = list(args.extra)
    tickers = list(args.ticker)
    if args.include_default_extras:
        extras = DEFAULT_EXTRAS + extras
    if args.include_user_defaults:
        extras = DEFAULT_EXTRAS + extras
        tickers = DEFAULT_TICKERS + tickers
    
    if args.debug:
        print("=" * 60, file=sys.stderr)
        print("市场数据获取 - 增强版 v3 (yfinance)", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print(f"额外品种: {extras}", file=sys.stderr)
        print(f"指定股票: {tickers}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
    
    try:
        payload = {
            "cn_hk": get_cn_hk_indexes(),
            "global": get_base_global(),
            "extras": get_extras(extras, tickers) if (extras or tickers) else {},
        }
        
        if args.format == "json":
            json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
            print()
        else:
            print(render_markdown(payload))
    except Exception as e:
        print(f"\n❌ 致命错误: {e}", file=sys.stderr)
        if args.debug:
            import traceback
            traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
