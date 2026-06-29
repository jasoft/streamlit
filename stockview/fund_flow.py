from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
from typing import Any, Dict, Iterable, List

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from stockview.log import logger
from stockview.state import init_slider_state, on_slider_change

_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/",
}

_TRADING_WINDOWS = [
    (pd.Timestamp("09:30:00").time(), pd.Timestamp("11:30:00").time()),
    (pd.Timestamp("13:00:00").time(), pd.Timestamp("15:00:00").time()),
]

_INDICATOR_TO_FIELD = {
    "today": "f62",
    "5day": "f164",
    "10day": "f174",
}

_INDICATOR_TO_STAT = {
    "today": "1",
    "5day": "5",
    "10day": "10",
}

_SECTOR_FS = {
    "industry": "m:90 t:2",
    "concept": "m:90 t:3",
    "region": "m:90 t:1",
}

_TODAY_COLUMNS = [
    "name",
    "change_pct",
    "main_net_inflow",
    "main_net_ratio",
    "super_large_net_inflow",
    "super_large_net_ratio",
    "large_net_inflow",
    "large_net_ratio",
    "medium_net_inflow",
    "medium_net_ratio",
    "small_net_inflow",
    "small_net_ratio",
    "top_stock_name",
    "top_stock_code",
    "update_time",
    "_code",
]

_MULTI_DAY_COLUMNS = [
    "name",
    "change_pct",
    "main_net_inflow",
    "main_net_ratio",
    "super_large_net_inflow",
    "super_large_net_ratio",
    "large_net_inflow",
    "large_net_ratio",
    "medium_net_inflow",
    "medium_net_ratio",
    "small_net_inflow",
    "small_net_ratio",
    "top_stock_name",
    "update_time",
    "_code",
]

_KLINE_COLUMNS = [
    "timestamp",
    "main_net_inflow",
    "small_net_inflow",
    "medium_net_inflow",
    "large_net_inflow",
    "super_large_net_inflow",
]


def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(_EM_HEADERS)
    return session


@st.cache_resource(show_spinner=False)
def _get_session() -> requests.Session:
    return _make_session()


def _now_shanghai() -> pd.Timestamp:
    return pd.Timestamp.now(tz="Asia/Shanghai")


def _is_effective_trading_time(now: pd.Timestamp) -> bool:
    current = now.time()
    return any(start <= current <= end for start, end in _TRADING_WINDOWS)


def _refresh_interval_seconds(now: pd.Timestamp) -> int:
    return 8 if _is_effective_trading_time(now) else 45


@dataclass(frozen=True)
class FundFlowSnapshot:
    trade_date: pd.Timestamp
    rows: List[Dict[str, Any]]
    code_name_map: Dict[str, str]
    name_code_map: Dict[str, str]
    source_type: str
    indicator: str


@dataclass(frozen=True)
class FundFlowTrendPoint:
    time_index: pd.Timestamp
    main_net_inflow: float
    super_large_net_inflow: float
    large_net_inflow: float


def _get_sector_code_name_map_sina(sector_type: str) -> Dict[str, str]:
    fenlei = "0" if sector_type == "industry" else "1"
    url = f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_bk?page=1&num=100&sort=netamount&asc=0&fenlei={fenlei}"
    session = _get_session()
    resp = session.get(url, headers={"Referer": "http://vip.stock.finance.sina.com.cn/"}, timeout=15)
    resp.raise_for_status()
    sectors = resp.json()
    name_code_map = {item['name']: item['category'] for item in sectors}
    return name_code_map


@st.cache_data(ttl=3600, show_spinner=False)
def _get_sector_code_name_map(sector_type: str) -> Dict[str, str]:
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "fid": "f62",
            "po": "1",
            "pz": "500",
            "pn": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "ut": "8dec03ba335b81bf4ebdf7b29ec27d15",
            "fs": _SECTOR_FS[sector_type],
            "fields": "f12,f14,f3,f62,f1,f13",
        }
        session = _get_session()
        resp = session.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()["data"]
        total_page = math.ceil(data["total"] / 500)
        rows: List[Dict[str, Any]] = []
        for page in range(1, total_page + 1):
            params.update({"pn": page})
            if page != 1:
                resp = session.get(url, params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()["data"]
            rows.extend(data["diff"])
        name_code_map = {row["f14"]: row["f12"] for row in rows if "f14" in row and "f12" in row}
        return name_code_map
    except Exception as e:
        logger.warning("Failed to get sector map from Eastmoney: %s. Falling back to Sina.", e)
        try:
            return _get_sector_code_name_map_sina(sector_type)
        except Exception as ex:
            logger.error("Sina map fallback failed: %s", ex)
            return {}


def _load_rank_snapshot_sina(sector_type: str, indicator: str) -> FundFlowSnapshot:
    fenlei = "0" if sector_type == "industry" else "1"
    url = f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_bk?page=1&num=100&sort=netamount&asc=0&fenlei={fenlei}"
    session = _get_session()
    resp = session.get(url, headers={"Referer": "http://vip.stock.finance.sina.com.cn/"}, timeout=15)
    resp.raise_for_status()
    sectors = resp.json()
    
    name_code_map = {item['name']: item['category'] for item in sectors}
    code_name_map = {item['category']: item['name'] for item in sectors}
    
    rows = []
    
    if indicator == "today":
        for item in sectors:
            rows.append({
                "name": item['name'],
                "change_pct": float(item['avg_changeratio']) * 100,
                "main_net_inflow": float(item['netamount']),
                "main_net_ratio": float(item['ratioamount']) * 100,
                "super_large_net_inflow": 0.0,
                "super_large_net_ratio": 0.0,
                "large_net_inflow": 0.0,
                "large_net_ratio": 0.0,
                "medium_net_inflow": 0.0,
                "medium_net_ratio": 0.0,
                "small_net_inflow": 0.0,
                "small_net_ratio": 0.0,
                "top_stock_name": item.get('ts_name', '-'),
                "top_stock_code": item.get('ts_symbol', '-'),
                "update_time": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%H:%M:%S"),
                "_code": item['category']
            })
    else:
        days = 5 if indicator == "5day" else 10
        categories = [item['category'] for item in sectors]
        
        def fetch_history(category):
            h_url = f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_zjlrqs?bankuai={category}&page=1&num={days}&sort=opendate&asc=0"
            try:
                h_resp = session.get(h_url, headers={"Referer": "http://vip.stock.finance.sina.com.cn/"}, timeout=8)
                h_resp.raise_for_status()
                return category, h_resp.json()
            except Exception as e:
                logger.warning("Failed to fetch history for %s from Sina: %s", category, e)
                return category, None
                
        with ThreadPoolExecutor(max_workers=15) as executor:
            histories = dict(executor.map(fetch_history, categories))
            
        for item in sectors:
            cat = item['category']
            hist = histories.get(cat)
            if not hist:
                continue
            
            main_net_inflow = sum(float(day_data['netamount']) for day_data in hist)
            change_pct = sum(float(day_data['avg_changeratio']) for day_data in hist) * 100
            latest_day = hist[0]
            
            rows.append({
                "name": item['name'],
                "change_pct": change_pct,
                "main_net_inflow": main_net_inflow,
                "main_net_ratio": float(latest_day['ratioamount']) * 100,
                "super_large_net_inflow": 0.0,
                "super_large_net_ratio": 0.0,
                "large_net_inflow": 0.0,
                "large_net_ratio": 0.0,
                "medium_net_inflow": 0.0,
                "medium_net_ratio": 0.0,
                "small_net_inflow": 0.0,
                "small_net_ratio": 0.0,
                "top_stock_name": "-",
                "top_stock_code": "-",
                "update_time": latest_day['opendate'],
                "_code": cat
            })
            
    df = pd.DataFrame(rows)
    if not df.empty:
        df.sort_values(by="main_net_inflow", ascending=False, inplace=True)
        df.reset_index(drop=True, inplace=True)
        df.insert(0, "rank", range(1, len(df) + 1))
        
    return FundFlowSnapshot(
        trade_date=pd.Timestamp.now(tz="Asia/Shanghai"),
        rows=df.to_dict(orient="records") if not df.empty else [],
        code_name_map=code_name_map,
        name_code_map=name_code_map,
        source_type="sina",
        indicator=indicator,
    )


def _load_rank_snapshot(sector_type: str, indicator: str) -> FundFlowSnapshot:
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        fields_map = {
            "today": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124",
            "5day": "f12,f14,f2,f109,f164,f165,f166,f167,f168,f169,f170,f171,f172,f173,f257,f258,f124",
            "10day": "f12,f14,f2,f160,f174,f175,f176,f177,f178,f179,f180,f181,f182,f183,f260,f261,f124",
        }
        params = {
            "pn": "1",
            "pz": "500",
            "po": "1",
            "np": "1",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
            "fltt": "2",
            "invt": "2",
            "fid0": _INDICATOR_TO_FIELD[indicator],
            "fs": _SECTOR_FS[sector_type],
            "stat": _INDICATOR_TO_STAT[indicator],
            "fields": fields_map[indicator],
            "rt": "52975239",
            "_": int(time.time() * 1000),
        }
        session = _get_session()
        resp = session.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()["data"]
        total_page = math.ceil(data["total"] / 500) if data.get("total") else 0
        frames: List[pd.DataFrame] = []
        for page in range(1, total_page + 1):
            params.update({"pn": page})
            if page != 1:
                resp = session.get(url, params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()["data"]
            frames.append(pd.DataFrame(data["diff"]))
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if df.empty:
            return FundFlowSnapshot(
                trade_date=_now_shanghai(),
                rows=[],
                code_name_map={},
                name_code_map={},
                source_type="eastmoney",
                indicator=indicator,
            )

        if indicator == "today":
            rename_map = {
                "f12": "_code",
                "f14": "name",
                "f3": "change_pct",
                "f62": "main_net_inflow",
                "f184": "main_net_ratio",
                "f66": "super_large_net_inflow",
                "f69": "super_large_net_ratio",
                "f72": "large_net_inflow",
                "f75": "large_net_ratio",
                "f78": "medium_net_inflow",
                "f81": "medium_net_ratio",
                "f84": "small_net_inflow",
                "f87": "small_net_ratio",
                "f204": "top_stock_name",
                "f205": "top_stock_code",
                "f124": "update_time",
            }
            columns = _TODAY_COLUMNS
        elif indicator == "5day":
            rename_map = {
                "f12": "_code",
                "f14": "name",
                "f109": "change_pct",
                "f164": "main_net_inflow",
                "f165": "main_net_ratio",
                "f166": "super_large_net_inflow",
                "f167": "super_large_net_ratio",
                "f168": "large_net_inflow",
                "f169": "large_net_ratio",
                "f170": "medium_net_inflow",
                "f171": "medium_net_ratio",
                "f172": "small_net_inflow",
                "f173": "small_net_ratio",
                "f257": "top_stock_name",
                "f258": "top_stock_code",
                "f124": "update_time",
            }
            columns = _MULTI_DAY_COLUMNS
        else:
            rename_map = {
                "f12": "_code",
                "f14": "name",
                "f160": "change_pct",
                "f174": "main_net_inflow",
                "f175": "main_net_ratio",
                "f176": "super_large_net_inflow",
                "f177": "super_large_net_ratio",
                "f178": "large_net_inflow",
                "f179": "large_net_ratio",
                "f180": "medium_net_inflow",
                "f181": "medium_net_ratio",
                "f182": "small_net_inflow",
                "f183": "small_net_ratio",
                "f260": "top_stock_name",
                "f261": "top_stock_code",
                "f124": "update_time",
            }
            columns = _MULTI_DAY_COLUMNS

        df = df.rename(columns=rename_map)
        df = df[[col for col in columns if col in df.columns]]
        numeric_cols = [col for col in df.columns if col not in {"name", "top_stock_name", "top_stock_code", "_code"}]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("main_net_inflow", ascending=False).reset_index(drop=True)
        df.insert(0, "rank", range(1, len(df) + 1))

        name_code_map = dict(zip(df["name"], df["_code"]))
        code_name_map = {v: k for k, v in name_code_map.items()}

        latest_ts = _now_shanghai()
        try:
            if not df.empty and "update_time" in df.columns:
                ts_raw = int(df.iloc[0]["update_time"])
                latest_ts = pd.to_datetime(ts_raw, unit="s", utc=True).tz_convert("Asia/Shanghai")
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to parse update_time: %s", e)
            latest_ts = _now_shanghai()

        return FundFlowSnapshot(
            trade_date=latest_ts,
            rows=df.to_dict(orient="records"),
            code_name_map=code_name_map,
            name_code_map=name_code_map,
            source_type="eastmoney",
            indicator=indicator,
        )
    except Exception as e:
        logger.warning("Failed to load rank snapshot from Eastmoney: %s. Falling back to Sina.", e)
        try:
            return _load_rank_snapshot_sina(sector_type, indicator)
        except Exception as ex:
            logger.error("Sina rank snapshot fallback failed: %s", ex)
            return FundFlowSnapshot(
                trade_date=_now_shanghai(),
                rows=[],
                code_name_map={},
                name_code_map={},
                source_type="none",
                indicator=indicator,
            )


def _fetch_sector_minute_kline_sina(session: requests.Session, name_code_map: Dict[str, str], sector_name: str) -> pd.DataFrame:
    if sector_name not in name_code_map:
        return pd.DataFrame(columns=_KLINE_COLUMNS)
    code = name_code_map[sector_name]
    url = f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssx_bkzj_fszs?page=1&num=241&bankuai={code}"
    try:
        resp = session.get(url, headers={"Referer": "http://vip.stock.finance.sina.com.cn/"}, timeout=15)
        resp.raise_for_status()
        raw_data = resp.json()
        ticks = raw_data[1]
    except Exception as e:
        logger.warning("Failed to fetch fszs for %s from Sina: %s", sector_name, e)
        return pd.DataFrame(columns=_KLINE_COLUMNS)

    rows = []
    for item in ticks:
        if item.get('ticktime') in {"14:58:00", "14:59:00", "15:00:00"}:
            continue
        rows.append({
            "timestamp": pd.to_datetime(item['opendate'] + ' ' + item['ticktime']),
            "main_net_inflow": float(item['netamount']),
            "small_net_inflow": 0.0,
            "medium_net_inflow": 0.0,
            "large_net_inflow": 0.0,
            "super_large_net_inflow": 0.0
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df.sort_values(by="timestamp", inplace=True)
        df.reset_index(drop=True, inplace=True)
    else:
        df = pd.DataFrame(columns=_KLINE_COLUMNS)
    return df


def _fetch_sector_minute_kline(session: requests.Session, name_code_map: Dict[str, str], sector_name: str) -> pd.DataFrame:
    if sector_name not in name_code_map:
        return pd.DataFrame(columns=_KLINE_COLUMNS)
    code = name_code_map[sector_name]
    url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
    params = {
        "secid": f"90.{code}",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "lmt": "0",
        "klt": "1",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
    }
    resp = session.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()["data"]["klines"]
    if not data:
        return pd.DataFrame(columns=_KLINE_COLUMNS)
    rows = [line.split(",") for line in data]
    df = pd.DataFrame(rows, columns=_KLINE_COLUMNS[: len(rows[0])])
    df = df[_KLINE_COLUMNS]
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    for col in _KLINE_COLUMNS[1:]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _load_selected_sector_minute_klines(snapshot: FundFlowSnapshot, names: List[str]) -> Dict[str, pd.DataFrame]:
    """加载指定板块的分钟资金流。"""
    session = _get_session()
    result: Dict[str, pd.DataFrame] = {}

    def _load(name: str) -> tuple[str, pd.DataFrame]:
        if snapshot.source_type == "sina":
            return name, _fetch_sector_minute_kline_sina(session, snapshot.name_code_map, name)
        else:
            return name, _fetch_sector_minute_kline(session, snapshot.name_code_map, name)

    with ThreadPoolExecutor(max_workers=min(max(len(names), 1), 12)) as executor:
        futures = [executor.submit(_load, name) for name in set(names)]
        for future in as_completed(futures):
            name, df = future.result()
            result[name] = df

    return result


def _is_trading_minute(ts: pd.Timestamp) -> bool:
    if pd.isna(ts):
        return False
    t = ts.time()
    return any(start <= t <= end for start, end in _TRADING_WINDOWS)


def _build_trend_frame(klines: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """构建趋势 DataFrame，并过滤午休/盘前盘后空点，避免水平直线误导。"""
    frames: List[pd.DataFrame] = []
    for name, df in klines.items():
        if df.empty:
            continue
        temp = df[["timestamp", "main_net_inflow"]].copy()
        temp = temp[temp["timestamp"].apply(_is_trading_minute)]
        temp["时间"] = temp["timestamp"].dt.strftime("%H:%M")
        temp["主力净流入"] = temp["main_net_inflow"] / 1e8
        temp["板块"] = name
        temp["红绿"] = temp["主力净流入"].apply(lambda x: "🔴" if x >= 0 else "🟢")
        frames.append(temp[["板块", "时间", "主力净流入", "红绿"]])
    if not frames:
        return pd.DataFrame(columns=["板块", "时间", "主力净流入", "红绿"])
    return pd.concat(frames, ignore_index=True)


def _pick_default_top_names(rows: List[Dict[str, Any]], top_n: int = 12) -> List[str]:
    """默认选取：流入前 N + 流出后 N，不重复。"""
    names: List[str] = []
    seen: set[str] = set()
    for row in rows[:top_n] + rows[-top_n:]:
        name = row.get("name")
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _filter_rows(rows: List[Dict[str, Any]], names: List[str]) -> List[Dict[str, Any]]:
    if not names:
        return rows
    name_set = set(names)
    return [row for row in rows if row.get("name") in name_set]


def _render_metric_row(snapshot: FundFlowSnapshot, used_kline_rows: int, top_n: int = 12) -> None:
    if not snapshot.rows:
        return
    latest_ts = snapshot.trade_date
    help_text = "新浪财经板块资金流接口返回的最新时间戳；盘后通常为当日收盘时间。" if snapshot.source_type == "sina" else "东方财富板块资金流接口返回的最新时间戳；盘后通常为当日收盘时间。"
    st.metric(
        "数据更新时间",
        value=latest_ts.strftime("%Y-%m-%d %H:%M"),
        help=help_text,
    )
    selected_display = min(len(snapshot.rows), top_n) if top_n else 12
    st.caption(
        f"分时明细已加载 {used_kline_rows} 条（流入前 {selected_display} + 流出后 {selected_display} 个板块并行拉取）。"
    )


def _build_sector_color_map(names: Iterable[str]) -> Dict[str, str]:
    """为板块名称生成稳定的、尽量不重复的颜色映射。"""
    palette = [
        "#E6194B", "#3CB44B", "#4363D8", "#F58231", "#42D4F4",
        "#F032E6", "#FABED4", "#469990", "#DCBEFF", "#9A6324",
        "#800000", "#AAFFC3", "#000075", "#A9A9A9", "#FFE119",
        "#E6BEFF", "#808000", "#FFD8B1", "#000000", "#911EB4",
        "#808080", "#BFEF45", "#E6194B", "#3CB44B", "#4363D8",
    ]
    mapping: Dict[str, str] = {}
    for idx, name in enumerate(names):
        # 先用调色板，保证颜色足够醒目且不重复；超出后才做 hash 补色
        if idx < len(palette):
            mapping[name] = palette[idx]
        else:
            digest = hashlib.md5(name.encode("utf-8")).hexdigest()
            r = int(digest[0:2], 16) % 200 + 30
            g = int(digest[2:4], 16) % 200 + 30
            b = int(digest[4:6], 16) % 200 + 30
            mapping[name] = f"#{r:02X}{g:02X}{b:02X}"
    return mapping


def _plot_sector_trend(df: pd.DataFrame, name_annotation: str = "end") -> go.Figure:
    if df.empty:
        fig = go.Figure()
        fig.update_layout(
            title="暂无当日分时资金流曲线",
            annotations=[{"text": "盘前/数据缺失时显示此提示", "showarrow": False}],
            margin=dict(l=16, r=16, t=40, b=16),
        )
        return fig
    names = list(df["板块"].drop_duplicates())
    color_map = _build_sector_color_map(names)
    fig = px.line(
        df,
        x="时间",
        y="主力净流入",
        color="板块",
        markers=False,
        color_discrete_map=color_map,
        custom_data=["板块", "红绿"],
    )
    fig.update_traces(
        hovertemplate="<b>%{customdata[0]}</b>: %{customdata[1]} %{y:.2f} 亿<extra></extra>"
    )

    # 在每条线末端直接标注板块名称，减少颜色反复对照的困扰。
    if name_annotation == "end":
        for trace in fig.data:
            if not len(trace.x):
                continue
            last_x = trace.x[-1]
            last_y = trace.y[-1]
            fig.add_annotation(
                x=last_x,
                y=last_y,
                text=trace.name,
                showarrow=False,
                xanchor="left",
                yanchor="middle",
                font=dict(size=11),
                xshift=6,
            )

    fig.update_layout(
        title="A股实时板块资金流向（主力净流入）",
        xaxis_title="交易时间",
        xaxis=dict(
            type="category",
            nticks=15,
        ),
        yaxis_title="累计主力净流入（亿元）",
        showlegend=False,
        hovermode="closest",
        margin=dict(l=16, r=16, t=44, b=16 + 120),
        height=800,
    )
    return fig


def _render_rank_table(snapshot: FundFlowSnapshot, selected_names: List[str]) -> None:
    filtered = _filter_rows(snapshot.rows, selected_names) if selected_names else snapshot.rows
    if not filtered:
        st.info("当前未检索到板块资金流向数据。")
        return
    display_df = pd.DataFrame(filtered).copy()
    rename = {
        "rank": "序号",
        "name": "板块",
        "change_pct": "涨跌幅(%)",
        "main_net_inflow": "主力净流入(元)",
        "main_net_ratio": "主力净占比(%)",
        "super_large_net_inflow": "超大单(元)",
        "super_large_net_ratio": "超大单占比(%)",
        "large_net_inflow": "大单(元)",
        "large_net_ratio": "大单占比(%)",
        "medium_net_inflow": "中单(元)",
        "medium_net_ratio": "中单占比(%)",
        "small_net_inflow": "小单(元)",
        "small_net_ratio": "小单占比(%)",
        "top_stock_name": "领涨/领跌股",
    }
    display_df = display_df.rename(columns={k: v for k, v in rename.items() if k in display_df.columns})
    st.dataframe(display_df, use_container_width=True, hide_index=True)


def render_fund_flow_page() -> None:
    st.title("实时板块资金流向")

    with st.sidebar:
        st.subheader("筛选条件")
        sector_type = st.selectbox(
            "板块类型",
            options=["industry", "concept", "region"],
            index=0,
            format_func=lambda x: {"industry": "行业板块", "concept": "概念板块", "region": "地域板块"}[x],
        )
        indicator = st.selectbox(
            "统计周期",
            options=["today", "5day", "10day"],
            index=0,
            format_func=lambda x: {"today": "今日净流入", "5day": "5日累计净流入", "10day": "10日累计净流入"}[x],
        )
        
        snapshot = _load_rank_snapshot(sector_type, indicator)
        
        # 动态计算最大值，作为过滤滑动条的上限（亿元）
        max_val_yuan = max([abs(row.get("main_net_inflow", 0)) for row in snapshot.rows]) if snapshot.rows else 0
        max_val_yi = float(math.ceil(max_val_yuan / 1e8))
        max_slider_val = max(max_val_yi, 1.0)
        
        # 过滤不活跃板块滑块
        min_flow_key = "fund_flow_min_flow"
        init_min_flow = init_slider_state(min_flow_key, default_value=0.0, min_value=0.0, max_value=max_slider_val)
        min_flow = st.slider(
            "过滤不活跃板块 (绝对值 ≥ N 亿元)",
            min_value=0.0,
            max_value=max_slider_val,
            value=init_min_flow,
            step=0.5 if max_slider_val <= 50.0 else 1.0,
            key=min_flow_key,
            on_change=on_slider_change,
            args=(min_flow_key,),
            help="仅展示累计主力净流入/流出绝对值大于或等于该设定值的板块",
        )
        
        # Top N 个板块滑块
        top_n_key = "fund_flow_top_n"
        init_top_n = init_slider_state(top_n_key, default_value=12, min_value=3, max_value=30)
        top_n = st.slider(
            "分时曲线展示前 N 个板块",
            min_value=3,
            max_value=30,
            value=init_top_n,
            step=1,
            key=top_n_key,
            on_change=on_slider_change,
            args=(top_n_key,),
        )
        refresh_seconds = st.number_input("自动刷新间隔（秒）", min_value=5, max_value=120, value=15, step=5)

    source_desc = "新浪财经 (备份源)" if snapshot.source_type == "sina" else "东方财富 (官方源)"
    st.caption(f"数据来源：{source_desc}；分时曲线基于板块分钟级资金流明细，排行表支持今日/5日/10日口径。")

    now = _now_shanghai()
    refresh_interval = max(int(refresh_seconds), _refresh_interval_seconds(now))
    st_autorefresh = __import__("streamlit_autorefresh", fromlist=["st_autorefresh"]).st_autorefresh
    st_autorefresh(interval=refresh_interval * 1000, key="fund_flow_autorefresh")

    # 根据过滤阈值筛选板块
    threshold_yuan = min_flow * 1e8
    filtered_rows = [row for row in snapshot.rows if abs(row.get("main_net_inflow", 0)) >= threshold_yuan]
    
    if not filtered_rows:
        st.warning(f"当前过滤阈值过高，无累计主力净流入/流出绝对值 ≥ {min_flow} 亿元的板块，请调低过滤条件。")
        st.stop()
        
    default_names = _pick_default_top_names(filtered_rows, top_n)
    selected_names = st.multiselect(
        f"选择要对比的板块（默认展示净流入前 {top_n} 个与后 {top_n} 个）",
        options=[row["name"] for row in filtered_rows],
        default=default_names,
    )

    klines = _load_selected_sector_minute_klines(snapshot, selected_names)
    trend_df = _build_trend_frame(klines)
    used_kline_rows = int(trend_df.shape[0])
    _render_metric_row(snapshot, used_kline_rows, top_n=int(top_n))

    fig = _plot_sector_trend(trend_df, name_annotation="end")
    st.plotly_chart(fig, use_container_width=True)

    tab_rank, tab_detail = st.tabs(["板块排行", "说明与口径"])
    with tab_rank:
        _render_rank_table(snapshot, selected_names)
    with tab_detail:
        st.markdown(
            """
**页面逻辑说明**

1. 左侧支持切换行业/概念/地域板块，以及今日、5日、10日三种口径。
2. 分时曲线直接拉取前 N 个板块的**分钟级资金流明细**，用于还原日内净流入变化。
3. 右侧排行表默认展示完整排名，可用于浏览全部板块净流入情况。
4. 盘中自动刷新，非交易时段降频，避免无效请求。

**数据口径**

- 主力净流入 = 超大单 + 大单净流入额（分钟级明细与汇总口径一致）。
- 领涨/领跌股字段来自东方财富板块资金流排名接口原始返回。
- 数据更新时间取自接口返回的最新时间戳，不一定等于页面刷新时间。
"""
        )

    logger.debug(
        "fund_flow_render sector_type=%s indicator=%s top_n=%s rows=%s kline_rows=%s",
        sector_type,
        indicator,
        top_n,
        len(snapshot.rows),
        used_kline_rows,
    )
