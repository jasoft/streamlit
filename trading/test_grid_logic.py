#!/usr/bin/env python3
"""网格引擎逻辑离线测试: 不碰行情/不碰同花顺, 直接喂价格序列驱动 _decide.

覆盖: 等差/等比触发、基准滚动、梯度倍量、回落/反弹确认、T+1、
最大持仓/最小底仓、资金不足、启动底仓、有效期、区间失效(EXHAUSTED)。
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from strategy.runtime.broker import SimulatedBroker
from strategy.runtime.portfolio import Portfolio
from trading.grid_orders import GridEngine, GridOrder

PASS = 0


def ok(cond, name, detail=""):
    global PASS
    status = "✅" if cond else "❌"
    print(f"{status} {name} {detail}")
    assert cond, name
    PASS += 1


def make_engine(cash=100_000.0):
    eng = GridEngine([], live=False, cash=0, poll_seconds=1)
    eng.pf = Portfolio("grid_test_tmp", cash)      # 独立测试账户, 不污染真实 state
    eng.broker = SimulatedBroker(eng.pf)
    return eng


def add(eng, **kw):
    eng.add_grid(kw)
    g = eng.grids[-1]
    eng._recalc_triggers(g)   # 测试里跳过首个 tick, 直接初始化触发价
    return g, eng.ctxs[g.id]


async def main():
    # ---------- 1. 等差触发 + 基准滚动 ----------
    eng = make_engine()
    g, ctx = add(eng, id="t1", symbol="600000", upper=11.0, lower=9.0,
                 grid_unit="price", step=0.5, base_price=10.0,
                 qty_mode="qty", per_qty=1000, t1_protect=False)
    ctx.set_price(10.0)
    ok(g.buy_trigger == 9.5 and g.sell_trigger == 10.5, "初始触发价", f"{g.buy_trigger}/{g.sell_trigger}")
    await eng._decide(g, ctx, 9.5)                      # 触买
    ok(g.pending_order_id != "", "到价触发买入")
    await eng._decide(g, ctx, 9.5)                      # 确认成交 -> 滚动
    ok(g.depth == 1 and g.base_price == 9.5 and ctx.position == 1000,
       "买入成交后基准滚动/depth+1", f"base={g.base_price} depth={g.depth} pos={ctx.position}")
    ok(abs(g.buy_trigger - 9.0) < 1e-9 and abs(g.sell_trigger - 10.0) < 1e-9,
       "触发价重算", f"{g.buy_trigger}/{g.sell_trigger}")
    await eng._decide(g, ctx, 10.0)                     # 触卖 (t1_protect=False)
    await eng._decide(g, ctx, 10.0)
    ok(g.depth == 0 and g.grid_rounds == 1 and ctx.position == 0,
       "卖出成交 depth-1/轮数+1", f"rounds={g.grid_rounds} pos={ctx.position}")
    ok(abs(g.realized_pnl - 500.0) < 0.01, "网格盈亏 (9.5买10.0卖)", f"{g.realized_pnl:+.2f}")

    # ---------- 2. 等比触发 ----------
    eng = make_engine()
    g, ctx = add(eng, id="t2", symbol="600000", upper=11.0, lower=9.0,
                 grid_unit="pct", step=2.0, base_price=10.0,
                 qty_mode="qty", per_qty=1000, t1_protect=False)
    ctx.set_price(10.0)
    ok(abs(g.buy_trigger - 9.8) < 1e-9 and abs(g.sell_trigger - 10.2) < 1e-9,
       "等比触发价", f"{g.buy_trigger:.3f}/{g.sell_trigger:.3f}")

    # ---------- 3. 梯度倍量 ----------
    eng = make_engine()
    g, ctx = add(eng, id="t3", symbol="600000", upper=11.0, lower=8.0,
                 grid_unit="price", step=0.5, base_price=10.0,
                 qty_mode="qty", per_qty=1000, multiplier=2.0, t1_protect=False)
    ctx.set_price(10.0)
    await eng._decide(g, ctx, 9.5); await eng._decide(g, ctx, 9.5)
    ok(g.last_buy_qty == 1000, "第0档买1000", f"{g.last_buy_qty}")
    await eng._decide(g, ctx, 9.0); await eng._decide(g, ctx, 9.0)
    ok(g.depth == 2 and g.last_buy_qty == 2000, "第1档倍量买2000",
       f"depth={g.depth} qty={g.last_buy_qty}")
    await eng._decide(g, ctx, 9.5); await eng._decide(g, ctx, 9.5)
    ok(ctx.position == 3000 - 2000, "卖出最近一批(2000)", f"pos={ctx.position}")

    # ---------- 4. 回落卖出 / 反弹买入确认 ----------
    eng = make_engine()
    g, ctx = add(eng, id="t4", symbol="600000", upper=11.0, lower=9.0,
                 grid_unit="price", step=0.5, base_price=10.0,
                 qty_mode="qty", per_qty=1000, t1_protect=False,
                 sell_retrace_pct=0.5, buy_rebound_pct=0.5)
    ctx.set_price(10.0)
    await ctx.submit_order("buy", 1000, price=10.0)     # 先造底仓 (绕过引擎, 不设 pending)
    await eng._decide(g, ctx, 10.5)                     # 到卖触发 -> 进回落确认
    ok(g.extreme_mode == "wait_retrace", "到卖触发进回落确认")
    await eng._decide(g, ctx, 10.8)                     # 冲高
    await eng._decide(g, ctx, 10.74)                    # 回落 10.8*(1-0.5%)≈10.746, 已跌破
    await eng._decide(g, ctx, 10.74)                    # 成交确认 tick
    ok(g.last_sell_price > 0, "回落后成交卖出", f"sell={g.last_sell_price}")
    await eng._decide(g, ctx, 10.0)                     # 回落到买触发 -> 反弹确认
    ok(g.extreme_mode == "wait_rebound", "到买触发进反弹确认")
    await eng._decide(g, ctx, 9.8)                      # 下探
    await eng._decide(g, ctx, 9.85)                     # 反弹 9.8*(1+0.5%)≈9.849
    await eng._decide(g, ctx, 9.85)                     # 成交确认 tick
    ok(g.last_buy_price > 0 and g.last_buy_price < 9.86, "反弹后成交买入",
       f"buy={g.last_buy_price}")

    # ---------- 5. T+1 保护 ----------
    eng = make_engine()
    g, ctx = add(eng, id="t5", symbol="600000", upper=11.0, lower=9.0,
                 grid_unit="price", step=0.5, base_price=10.0,
                 qty_mode="qty", per_qty=1000, t1_protect=True)
    ctx.set_price(10.0)
    await eng._decide(g, ctx, 9.5); await eng._decide(g, ctx, 9.5)
    await eng._decide(g, ctx, 10.0); await eng._decide(g, ctx, 10.0)
    ok(g.grid_rounds == 0 and g.pending_order_id == "" and ctx.position == 1000,
       "T+1: 当日买入当日不卖", f"rounds={g.grid_rounds}")
    g.t1_protect = False
    await eng._decide(g, ctx, 10.0); await eng._decide(g, ctx, 10.0)
    ok(g.grid_rounds == 1, "关闭 T+1 后可卖")

    # ---------- 6. 最大持仓 / 最小底仓 ----------
    eng = make_engine()
    g, ctx = add(eng, id="t6", symbol="600000", upper=11.0, lower=8.0,
                 grid_unit="price", step=0.5, base_price=10.0,
                 qty_mode="qty", per_qty=1000, max_position=1500, t1_protect=False)
    ctx.set_price(10.0)
    await eng._decide(g, ctx, 9.5); await eng._decide(g, ctx, 9.5)
    await eng._decide(g, ctx, 9.0); await eng._decide(g, ctx, 9.0)
    ok(ctx.position == 1000, "最大持仓拦截: 只买一档", f"pos={ctx.position}")
    eng2 = make_engine()
    g2, ctx2 = add(eng2, id="t6b", symbol="600001", upper=11.0, lower=8.0,
                   grid_unit="price", step=0.5, base_price=10.0,
                   qty_mode="qty", per_qty=1000, min_position=500, t1_protect=False)
    # 先造 500 股底仓
    await ctx2.submit_order("buy", 500, price=10.0)
    ctx2.set_price(10.0)
    await eng2._decide(g2, ctx2, 10.5); await eng2._decide(g2, ctx2, 10.5)
    ok(ctx2.position == 500 and g2.grid_rounds == 0, "最小底仓拦截: 不跌破底仓",
       f"pos={ctx2.position}")

    # ---------- 7. 资金不足 ----------
    eng = make_engine(cash=5000.0)
    g, ctx = add(eng, id="t7", symbol="600002", upper=11.0, lower=9.0,
                 grid_unit="price", step=0.5, base_price=10.0,
                 qty_mode="qty", per_qty=1000, t1_protect=False)
    ctx.set_price(10.0)
    await eng._decide(g, ctx, 9.5); await eng._decide(g, ctx, 9.5)
    ok(g.pending_order_id == "" and ctx.position == 0, "资金不足跳过买入")

    # ---------- 8. 启动底仓 ----------
    eng = make_engine()
    g, ctx = add(eng, id="t8", symbol="600003", upper=11.0, lower=9.0,
                 grid_unit="price", step=0.5, base_price=10.0,
                 qty_mode="qty", per_qty=1000, base_qty=2000, t1_protect=False)
    ctx.set_price(10.0)
    await eng._maybe_bootstrap(g, ctx, 10.0)
    await eng._decide(g, ctx, 10.0)
    ok(g.bootstrapped and ctx.position == 2000 and g.depth == 0,
       "启动底仓买入且不计 depth", f"pos={ctx.position} depth={g.depth}")
    await eng._decide(g, ctx, 10.5); await eng._decide(g, ctx, 10.5)
    ok(g.grid_rounds == 1 and g.realized_pnl > 0, "底仓高抛成交",
       f"pnl={g.realized_pnl:+.2f}")

    # ---------- 9. 有效期 ----------
    eng = make_engine()
    g, ctx = add(eng, id="t9", symbol="600004", upper=11.0, lower=9.0,
                 grid_unit="price", step=0.5, base_price=10.0, expire_date="2026-01-01")
    ctx.set_price(10.0)
    await eng._decide(g, ctx, 9.5)
    ok(g.state == "EXPIRED", "有效期到自动暂停", g.state)

    # ---------- 10. 区间失效 EXHAUSTED ----------
    eng = make_engine()
    g, ctx = add(eng, id="t10", symbol="600005", upper=10.5, lower=10.0,
                 grid_unit="pct", step=3.0, base_price=10.25)
    ctx.set_price(10.25)
    await eng._decide(g, ctx, 10.25)
    ok(g.state == "EXHAUSTED", "双向触发价出界 -> 网格失效", g.state)

    # ---------- 11. 单边上涨卖方向出界停 (不卖飞) ----------
    eng = make_engine()
    g, ctx = add(eng, id="t11", symbol="600006", upper=11.0, lower=9.0,
                 grid_unit="price", step=0.5, base_price=10.0,
                 qty_mode="qty", per_qty=1000, base_qty=2000, t1_protect=False)
    ctx.set_price(10.0)
    await eng._maybe_bootstrap(g, ctx, 10.0); await eng._decide(g, ctx, 10.0)
    for px in (10.5, 11.0, 11.5):                       # 涨破上限
        await eng._decide(g, ctx, px); await eng._decide(g, ctx, px)
    ok(g.state == "RUNNING" and g.sell_trigger > g.upper and g.depth == 0
       and ctx.position == 0,
       "单边上涨: 卖触发出上界停止卖出, 策略不失效",
       f"sell_tr={g.sell_trigger:.2f} pos={ctx.position}")

    print(f"\n全部 {PASS} 项断言通过 ✅")
    eng.pf  # noqa


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
    # 清理测试账户 state
    p = REPO_ROOT / "strategy" / "state" / "grid_test_tmp.state.json"
    p.unlink(missing_ok=True)
    print("已清理测试 state")
