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
        return {"last": px, "pre_close": px} if px else None

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
               test_groups_independent, test_db_constraints_and_crud):
        fn()
    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
