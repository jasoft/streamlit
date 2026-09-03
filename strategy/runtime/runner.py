"""Runner: 策略事件驱动运行时. --mode {backtest,paper,live} 切换.

Write Once, Run Anywhere: 同一策略 (signal + on_bar) 在三种模式跑:

- backtest: 遍历历史 df, 每根 bar: flush_pending(bar.open) 成交昨日信号
            -> on_bar(bar) 产生今日信号. BacktestBroker 次日开盘口径.
- paper:    实时拉 bar + on_bar, SimulatedBroker 立即按信号价模拟成交 (不调 ths).
- live:     实时拉 bar + on_bar, LiveBroker 调 ths_trade 下单.

K线结束触发 (TRIGGER_ON_CLOSE=True 默认): 对已收盘的最后一根 bar 调 on_bar,
history 不含当前未完结 bar, 避免未来函数. False 则对当前 bar 调 (每 tick 触发).

数据获取通过注入的 fetch_fn (同步), runner 用 asyncio.to_thread 包装避免阻塞.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Callable, Optional

import pandas as pd

from strategy.base import Strategy
from .broker import BacktestBroker, LiveBroker, SimulatedBroker, Broker
from .ctx import Context
from .portfolio import Portfolio


def _default_lookback(strat: Strategy) -> int:
    return int(getattr(strat, "LOOKBACK", 3000))


class Runner:
    def __init__(self, *, strategy: Strategy, symbol: str, params: dict,
                 mode: str = "backtest", cash: float = 100_000.0,
                 cash_per_symbol: float = 10_000.0, qfq: bool = False,
                 dry_run: Optional[bool] = None,
                 fetch_fn: Optional[Callable] = None,
                 poll_seconds: float = 5.0,
                 state_name: Optional[str] = None):
        self.strat = strategy
        self.symbol = symbol
        self.params = strategy.validate_params(params)
        self.mode = mode
        self.qfq = qfq
        self.fetch_fn = fetch_fn
        self.poll_seconds = poll_seconds
        self.lookback = _default_lookback(strategy)
        self.dry_run = bool(dry_run) if dry_run is not None else (mode == "live" and False)

        # state 名: 多标的隔离用 strategy_symbol, 单标的保持兼容 strategy.NAME
        sname = state_name or (f"{strategy.NAME}_{symbol}" if mode != "backtest"
                                else f"{strategy.NAME}_{symbol}_bt")
        self.pf = Portfolio.load(sname, cash)
        self.broker = self._make_broker(mode, dry_run)
        self.ctx = Context(symbol=symbol, params=self.params, portfolio=self.pf,
                           broker=self.broker, cash_per_symbol=cash_per_symbol,
                           dry_run=self.dry_run)

    def _make_broker(self, mode: str, dry_run: Optional[bool]) -> Broker:
        if mode == "backtest":
            return BacktestBroker(self.pf)
        if mode == "paper":
            return SimulatedBroker(self.pf)
        # live
        return LiveBroker(self.pf)

    # ---------- 回测 ----------
    async def run_backtest(self, df: pd.DataFrame) -> dict:
        """遍历 df, 每根 bar: flush_pending(open) -> on_bar(bar). 返回快照+资金曲线+标记."""
        self.strat.on_init(self.ctx)
        df = df.sort_values("date").reset_index(drop=True)
        equity, markers = [], []
        for i in range(len(df)):
            bar = df.iloc[i]
            self.ctx.set_history(df.iloc[: i + 1])
            self.ctx.set_price(float(bar["close"]))
            # 回测: 先 flush 上一根产生的 pending (用当日 open 成交, 无未来函数)
            if isinstance(self.broker, BacktestBroker):
                for f in self.broker.flush_pending(self.symbol, float(bar["open"])):
                    self.pf.apply_fill(f)
                    markers.append(self._marker(f, bar["date"]))
            await self.strat.on_bar(bar, self.ctx)
            # 记录该 bar 处理完后的资金曲线 (cash + 持仓 * close)
            equity.append({
                "time": str(bar["date"]),
                "value": round(self.pf.cash + self.pf.qty(self.symbol) * float(bar["close"]), 2),
            })
        # 回测末尾 flush 剩余 pending (用最后一根 close 兜底)
        if isinstance(self.broker, BacktestBroker) and self.broker.has_pending(self.symbol):
            last_close = float(df.iloc[-1]["close"]) if len(df) else 0.0
            for f in self.broker.flush_pending(self.symbol, last_close):
                self.pf.apply_fill(f)
                markers.append(self._marker(f, df.iloc[-1]["date"]))
        self.strat.on_stop(self.ctx)
        total = self.pf.cash + self.pf.qty(self.symbol) * (
            float(df.iloc[-1]["close"]) if len(df) else 0.0)
        ret = total / self.pf.initial_cash - 1 if self.pf.initial_cash else 0
        return {
            "stats": {
                "total_return_pct": round(ret * 100, 2),
                "final_value": round(total, 2),
                "initial_cash": round(self.pf.initial_cash, 2),
                "n_orders": len(self.pf.orders),
                "n_filled": sum(1 for o in self.pf.orders.values() if o.status == "filled"),
            },
            "equity": equity,
            "markers": markers,
            "snapshot": self.pf.snapshot(),
        }

    def _marker(self, fill, date) -> dict:
        return {
            "date": str(date),
            "price": round(fill.price, 3),
            "action": "买入" if fill.side == "buy" else "卖出",
        }

    # ---------- 实盘 / 模拟 ----------
    async def run_live(self, once: bool = False) -> dict:
        """定时拉最新 bar -> K线结束触发 -> on_bar. once=True 跑一轮即退出."""
        if self.fetch_fn is None:
            raise RuntimeError("run_live 需要 fetch_fn (trader._fetch)")
        tf = getattr(self.strat, "TIMEFRAME", "day")
        trigger_on_close = bool(getattr(self.strat, "TRIGGER_ON_CLOSE", True))
        self.strat.on_init(self.ctx)
        last_processed = None
        while True:
            try:
                df = await asyncio.to_thread(
                    self.fetch_fn, self.symbol, self.qfq, tf, self.lookback)
            except Exception as e:
                print(f"[{self.strat.NAME}] fetch error: {e!r}", flush=True)
                if once:
                    return {"error": repr(e)}
                await asyncio.sleep(self.poll_seconds)
                continue
            if len(df) < 2:
                if once:
                    return {"error": "数据不足"}
                await asyncio.sleep(self.poll_seconds)
                continue

            # trigger_on_close: 对已收盘的最后一根 (倒数第二根) 调 on_bar
            # 否则对当前未完结 bar (倒数第一根) 调
            bar_idx = -2 if trigger_on_close else -1
            bar = df.iloc[bar_idx]
            bar_time = bar["date"]
            if bar_time == last_processed:
                if once:
                    return {"msg": "no new bar", "snapshot": self.pf.snapshot()}
                await asyncio.sleep(self.poll_seconds)
                continue
            last_processed = bar_time

            hist = df.iloc[:-1] if trigger_on_close else df
            self.ctx.set_history(hist)
            self.ctx.set_price(float(bar["close"]))
            await self.strat.on_bar(bar, self.ctx)
            print(f"[{self.strat.NAME}] {bar_time} close={bar['close']} "
                  f"target={self.ctx.target} pos={self.ctx.position}", flush=True)

            if once:
                return {"msg": "ran", "bar_date": str(bar_time),
                        "snapshot": self.pf.snapshot()}
            await asyncio.sleep(self.poll_seconds)
