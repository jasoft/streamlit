#!/usr/bin/env python3
"""选股自动交易引擎单测 (不依赖 fdata/盘中时段/同花顺).

覆盖:
- 买入成交 -> 计入策略 ID 挂钩的买入组 (SQLite), 指令流水留痕
- 已持有去重 + max_positions 限仓
- 卖出条件命中 -> 卖出指令 -> 平仓记录 (sold) + 盈亏
- T+1 保护: 当日买入当日不卖 (t1_protect 开/关)
- 多策略组彼此独立 (同码不同组, 参数独立, 组合独立)
- picker_db CRUD + 同组同码活动持仓唯一约束

用法: uv run python test_stock_picker.py   (亦可 pytest test_stock_picker.py)
"""
from __future__ import annotations

import asyncio
import os
import tempfile

os.environ["PICKER_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="picker_test_"), "test.db")

import backend.picker_db as picker_db  # noqa: E402  (须在设置环境变量后导入)
import trading.stock_picker as sp  # noqa: E402
from trading.picker_strategies.base import PickCandidate, PickStrategy  # noqa: E402


# ------------------------------------------------------------- 测试替身 ----
class FakePicker(PickStrategy):
    """可控选股插件: 候选/行情由测试注入."""
    ID = "fake"
    TITLE = "测试选股"
    PARAMS = {"sell_at": {"default": 10.0, "desc": "现价达到该值触发卖出"}}

    def __init__(self):
        self.candidates: list[PickCandidate] = []

    async def select(self, universe: list[str], params: dict) -> list[PickCandidate]:
        return [c for c in self.candidates if c.code in universe]

    def sell_reason(self, pos: dict, quote: dict | None, bars: list[dict],
                    params: dict) -> str:
        last = float((quote or {}).get("last") or 0)
        sell_at = float(params.get("sell_at") or 10.0)
        if last >= sell_at:
            return f"现价 {last:.3f} >= 卖线 {sell_at:g}"
        return ""


class PickerEnv:
    """一台引擎 + 假行情 + 无落盘 Portfolio (进程内隔离)."""

    def __init__(self, groups_cfg: list[dict], prices: dict[str, float],
                 cash: float = 100_000.0):
        self.prices = dict(prices)
        self.plugin = FakePicker()
        # Portfolio.save 改 no-op: 不写 strategy/state/*.json, 进程内验证
        sp.Portfolio.save = lambda self: None
        sp.Portfolio.load = classmethod(
            lambda cls, name, cash=100_000.0: cls(name, cash))
        self.eng = sp.StockPickerEngine(groups_cfg, live=False, cash=cash,
                                        poll_seconds=1.0,
                                        pickers={"fake": self.plugin})

    def set_price(self, code: str, px: float) -> None:
        self.prices[code] = px

    async def _quote(self, code: str):
        px = self.prices.get(code)
        # fdata quote 平铺结构: last/pre_close/name (名称来自 quote, K线无 name)
        return ({"last": px, "pre_close": px, "name": f"名称{code[-3:]}"}
                if px else None)

    async def _bars(self, code: str, limit: int):
        return []

    async def run_once(self, gid: str) -> dict:
        self.eng._fetch_quote = self._quote    # type: ignore[method-assign]
        self.eng._fetch_bars = self._bars      # type: ignore[method-assign]
        return await self.eng.run_once(gid)


def group_cfg(gid: str, universe: list[str], *, max_positions: int = 2,
              t1_protect: bool = True, params: dict | None = None) -> dict:
    return {
        "strategy_id": gid, "picker": "fake", "title": gid,
        "universe": universe, "params": params or {"sell_at": 10.0},
        "per_qty": 1000, "cash_per_symbol": 10000.0,
        "max_positions": max_positions, "buy_scan_every": 1,
        "t1_protect": t1_protect, "enabled": True,
    }


def holding_codes(gid: str) -> set[str]:
    return {p["code"] for p in picker_db.list_positions(strategy_id=gid,
                                                        status="holding")}


# ----------------------------------------------------------------- 用例 ----
def test_buy_records_into_buy_group():
    """买入成交 -> 计入该策略 ID 的买入组, 流水留痕."""
    picker_db.upsert_group(group_cfg("g1", ["601899", "600519"]))
    env = PickerEnv([group_cfg("g1", ["601899", "600519"])],
                    {"601899": 9.0, "600519": 8.0})
    env.plugin.candidates = [
        PickCandidate(code="601899", name="紫金矿业", price=9.0, reason="RSI 超卖"),
        PickCandidate(code="600519", name="贵州茅台", price=8.0, reason="RSI 超卖"),
    ]
    snap = asyncio.run(env.run_once("g1"))

    rows = picker_db.list_positions(strategy_id="g1", status="holding")
    assert {(r["code"], r["qty"]) for r in rows} == {("601899", 1000),
                                                     ("600519", 1000)}, rows
    assert all(r["strategy_id"] == "g1" for r in rows)
    assert all(r["buy_reason"] == "RSI 超卖" for r in rows)
    # 名称来自实时快照, 不得与代码相同 (选股名称 bug 回归)
    assert all(r["name"] and r["name"] != r["code"] for r in rows), rows
    assert all(r["buy_price"] > 0 for r in rows)
    buys = [e for e in snap["events"] if e["side"] == "buy" and e["status"] == "filled"]
    assert len(buys) == 2, buys
    pf = snap["portfolios"]["g1"]
    assert pf["cash"] < 100_000.0  # 买入扣款
    print("  ✓ 买入成交按 strategy_id 入库 (买入组)")


def test_dedup_and_max_positions():
    """已持有不重复买; max_positions=1 时只买第一只."""
    cfg = group_cfg("g2", ["601899", "600519"], max_positions=1)
    picker_db.upsert_group(cfg)
    env = PickerEnv([cfg], {"601899": 9.0, "600519": 8.0})
    env.plugin.candidates = [
        PickCandidate(code="601899", price=9.0, score=2.0),
        PickCandidate(code="600519", price=8.0, score=1.0),
    ]
    asyncio.run(env.run_once("g2"))
    assert holding_codes("g2") == {"601899"}          # score 高者优先, 限仓 1 只
    # 第二轮: 相同候选 -> 去重 + 已满, 不新增成交
    snap = asyncio.run(env.run_once("g2"))
    assert holding_codes("g2") == {"601899"}
    g2_fills = [e for e in snap["events"] if e["strategy_id"] == "g2"
                and e["side"] == "buy" and e["status"] == "filled"]
    assert len(g2_fills) == 1, g2_fills
    print("  ✓ 已持有去重 + max_positions 限仓")


def test_sell_closes_position():
    """卖出条件命中 -> 发卖出指令 -> 平仓记录 (sold) + 盈亏留痕."""
    cfg = group_cfg("g3", ["601899"], t1_protect=False, params={"sell_at": 10.0})
    picker_db.upsert_group(cfg)
    env = PickerEnv([cfg], {"601899": 9.0})
    env.plugin.candidates = [PickCandidate(code="601899", price=9.0)]
    asyncio.run(env.run_once("g3"))
    assert holding_codes("g3") == {"601899"}
    # 涨到卖线 -> 卖出 (清空候选: 聚焦卖出链路, 否则同轮选股会再买回)
    env.set_price("601899", 10.5)
    env.plugin.candidates = []
    snap = asyncio.run(env.run_once("g3"))
    sold = picker_db.list_positions(strategy_id="g3", status="sold")
    assert len(sold) == 1 and sold[0]["sell_price"] == 10.5, sold
    assert "卖线" in (sold[0]["sell_reason"] or "")
    sells = [e for e in snap["events"] if e["side"] == "sell"
             and e["status"] == "filled"]
    assert len(sells) == 1
    assert snap["portfolios"]["g3"]["cash"] > 100_000.0  # 9.0 买 10.5 卖
    print("  ✓ 卖出指令 + 平仓记录 (含原因/盈亏)")


def test_t1_protect():
    """T+1 保护: 当日买入当日不卖; 关闭后可卖."""
    cfg = group_cfg("g4", ["601899"], t1_protect=True, params={"sell_at": 10.0})
    picker_db.upsert_group(cfg)
    env = PickerEnv([cfg], {"601899": 9.0})
    env.plugin.candidates = [PickCandidate(code="601899", price=9.0)]
    asyncio.run(env.run_once("g4"))
    env.set_price("601899", 11.0)                     # 立即满足卖出条件
    env.plugin.candidates = []                        # 聚焦卖出链路, 不再补买
    snap = asyncio.run(env.run_once("g4"))
    assert holding_codes("g4") == {"601899"}, "T+1 保护生效, 当日不应卖出"
    assert not [e for e in snap["events"] if e["strategy_id"] == "g4"
                and e["side"] == "sell"]
    env.eng.update_group("g4", {"t1_protect": False})
    snap = asyncio.run(env.run_once("g4"))
    assert holding_codes("g4") == set()
    assert [e for e in snap["events"] if e["strategy_id"] == "g4"
            and e["side"] == "sell" and e["status"] == "filled"]
    print("  ✓ T+1 保护开关")


def test_groups_independent():
    """不同策略组彼此独立: 同码不同组, 卖出参数/组合互不影响."""
    cfg_a = group_cfg("ga", ["601899"], t1_protect=False, params={"sell_at": 10.0})
    cfg_b = group_cfg("gb", ["601899"], t1_protect=False, params={"sell_at": 99.0})
    for c in (cfg_a, cfg_b):
        picker_db.upsert_group(c)
    env = PickerEnv([cfg_a, cfg_b], {"601899": 9.0})
    env.plugin.candidates = [PickCandidate(code="601899", price=9.0)]
    asyncio.run(env.run_once("ga"))
    asyncio.run(env.run_once("gb"))
    assert holding_codes("ga") == {"601899"} and holding_codes("gb") == {"601899"}
    env.set_price("601899", 10.5)                     # 只到 ga 的卖线
    env.plugin.candidates = []                        # 卖出后不再补买, 聚焦独立性
    snap = asyncio.run(env.run_once("ga"))
    assert holding_codes("ga") == set(), "ga 应卖出"
    assert holding_codes("gb") == {"601899"}, "gb 卖线 99, 应继续持有"
    # 组合独立: ga 现金增加不影响 gb
    assert snap["portfolios"]["gb"]["cash"] < 100_000.0
    print("  ✓ 多策略组彼此独立 (参数/持仓/资金)")


def test_buy_respects_group_cash():
    """现金约束: 固定股数超组内资金时下调到整手可负担量, 现金不为负."""
    cfg = group_cfg("g6", ["601899"])
    cfg["per_qty"] = 100000                          # 远超 10 万组资金
    picker_db.upsert_group(cfg)
    env = PickerEnv([cfg], {"601899": 9.0})
    env.plugin.candidates = [PickCandidate(code="601899", price=9.0)]
    snap = asyncio.run(env.run_once("g6"))
    rows = picker_db.list_positions(strategy_id="g6", status="holding")
    assert len(rows) == 1
    expect = 100_000 // 9 // 100 * 100               # 可负担的最大整手数 = 11100
    assert rows[0]["qty"] == expect, rows[0]
    assert snap["portfolios"]["g6"]["cash"] >= 0
    print("  ✓ 现金约束 (数量下调, 现金不为负)")


def test_rule_engine():
    """条件原语求值: 买入全命中才选入, 卖出任一命中即卖."""
    from trading import picker_rules
    # 连跌 20 天: RSI6 = 0
    bars = [{"date": f"2026-01-{i+1:02d}", "close": round(10 - i * 0.1, 2),
             "volume": 1000} for i in range(20)]
    ok = picker_rules.eval_buy(bars, [{"type": "rsi_below", "n": 6, "threshold": 25}])
    assert "RSI" in ok
    assert picker_rules.eval_buy(bars, [{"type": "rsi_below", "n": 6, "threshold": -1}]) == ""
    # 全命中语义: 两个条件, 一个不满足 -> 空串
    assert picker_rules.eval_buy(bars, [
        {"type": "rsi_below", "n": 6, "threshold": 25},
        {"type": "vol_ratio_above", "days": 5, "ratio": 99}]) == ""
    # 卖出: 止盈 / 持仓天数
    pos = {"buy_price": 8.0, "buy_ts": "2026-01-01T10:00:00"}
    assert "止盈" in picker_rules.eval_sell(
        pos, bars, [{"type": "take_profit", "pct": 10}], px=9.0)
    assert "持仓" in picker_rules.eval_sell(
        pos, bars, [{"type": "hold_days", "days": 5}], today="2026-01-10")
    assert picker_rules.eval_sell(pos, bars, [{"type": "stop_loss", "pct": -50}],
                                  px=9.0) == ""
    # 校验器: 未知类型/非数字参数报错, 参数平铺清洗
    clean, err = picker_rules.validate_rules("buy", [{"type": "rsi_below", "n": 6,
                                                      "threshold": 30}])
    assert not err and clean[0]["threshold"] == 30.0
    _, err = picker_rules.validate_rules("buy", [{"type": "no_such"}])
    assert err
    _, err = picker_rules.validate_rules("sell", [{"type": "take_profit", "pct": "abc"}])
    assert err
    # 空槽清洗: 盘前占位 bar (量额 0) 不应让标的被判停牌 (e2e 抓到的回归)
    dirty = bars + [{"date": "2026-01-25", "close": 8.0, "volume": 0}]
    assert picker_rules.eval_buy(dirty, [{"type": "rsi_below", "n": 6,
                                          "threshold": 25}]) == ""
    cleaned = picker_rules.clean_day_bars(dirty)
    assert len(cleaned) == len(bars)
    assert picker_rules.eval_buy(cleaned, [{"type": "rsi_below", "n": 6,
                                            "threshold": 25}]) != ""
    print("  ✓ 规则引擎求值 + 校验器 + 空槽清洗")


def test_backtest_core():
    """回测核心 (合成数据): 先卖后买/T+1/整手/指标完整性."""
    import datetime as dt
    from trading.picker_backtest import _run_core
    base = dt.date(2026, 1, 5)
    px_a, px_b = 10.0, 20.0
    bars_a, bars_b = [], []
    for i in range(60):
        px_a = round(px_a * (1.01 if i < 30 else 0.99), 4)   # 涨30天跌30天
        px_b = round(px_b * (0.995 if i < 30 else 1.005), 4)  # 反向
        d = (base + dt.timedelta(days=i)).isoformat()
        bars_a.append({"date": d, "close": px_a, "volume": 1000})
        bars_b.append({"date": d, "close": px_b, "volume": 1000})
    res = _run_core(
        [{"type": "pct_change_above", "pct": -100}],          # 买入必中
        [{"type": "hold_days", "days": 3}],                   # 持仓3天必卖
        {"AAA": bars_a, "BBB": bars_b},
        days=60, cash=100_000.0, max_positions=2, t1_protect=True)
    m = res["metrics"]
    assert m["trades"] > 0, m
    assert len(res["equity"]) == 60
    assert res["equity"][-1]["value"] > 0
    assert 0 <= m["win_rate_pct"] <= 100
    assert all(t["qty"] % 100 == 0 for t in res["trades"])
    assert all(t["sell_date"] > t["buy_date"] for t in res["trades"])  # T+1
    print(f"  ✓ 回测核心 (合成数据, {m['trades']} 笔交易, 收益 {m['total_return_pct']}%)")


def test_strategy_library():
    """策略库: 预置种子 / CRUD / 被策略组引用时拒绝删除."""
    from backend import stockpicker as sp_web
    presets = picker_db.list_strategies()
    assert any(s["id"] == "preset_rsi_rebound" for s in presets), "策略库应自动播种"
    picker_db.upsert_strategy({
        "id": "st_test", "title": "测试策略",
        "buy_rules": [{"type": "rsi_below", "n": 6, "threshold": 30}],
        "sell_rules": [{"type": "take_profit", "pct": 8}]})
    assert picker_db.get_strategy("st_test")["title"] == "测试策略"
    # 被引用: 删除被拒
    picker_db.upsert_group({**group_cfg("gref", ["601899"]), "picker": "st_test"})
    try:
        sp_web.delete_strategy("st_test")
        raise AssertionError("被策略组引用的策略应拒绝删除")
    except ValueError:
        pass
    picker_db.delete_group("gref")
    sp_web.delete_strategy("st_test")
    assert picker_db.get_strategy("st_test") is None
    print("  ✓ 策略库 CRUD + 引用保护 + 预置种子")


def test_db_constraints_and_crud():
    """同组同码活动持仓唯一; 组 CRUD."""
    picker_db.upsert_group(group_cfg("g5", ["601899"]))
    picker_db.insert_position("g5", "601899", "紫金", 1000, 9.0, "2026-09-04T10:00:00")
    try:
        picker_db.insert_position("g5", "601899", "紫金", 1000, 9.0, "x")
        raise AssertionError("同组同码重复买入应被唯一约束拦截")
    except ValueError:
        pass
    assert picker_db.get_group("g5")["universe"] == ["601899"]
    picker_db.upsert_group({**picker_db.get_group("g5"), "max_positions": 5})
    assert picker_db.get_group("g5")["max_positions"] == 5
    assert picker_db.delete_group("g5") and picker_db.get_group("g5") is None
    print("  ✓ SQLite 唯一约束 + 组 CRUD")


def main() -> None:
    for fn in (test_buy_records_into_buy_group, test_dedup_and_max_positions,
               test_sell_closes_position, test_t1_protect,
               test_groups_independent, test_buy_respects_group_cash,
               test_rule_engine, test_backtest_core, test_strategy_library,
               test_db_constraints_and_crud):
        fn()
    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
