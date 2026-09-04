"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import SymbolPicker from "@/components/SymbolPicker";

// ---- 类型 (对应 GET /api/conditions 返回) ----
type CondOrder = {
  id: string;
  symbol: string;
  trigger_gap_pct: number;
  buy_qty: number;
  sell_rally_pct: number;
  open_window_min: number;
  state: "WATCH" | "ARMED" | "DONE";
  day: string;
  buy_locked: boolean;
  buy_price: number;
  buy_ts: string;
  sell_price: number;
  sell_ts: string;
  last_gap_pct: number;
  last_price: number;
  error: string;
};

type CondStatus = {
  running: boolean;
  live: boolean | null;
  poll_seconds: number | null;
  orders: CondOrder[];
  portfolio: {
    cash: number;
    initial_cash: number;
    positions: Record<string, { qty: number; avg_price: number; target: number }>;
    open_orders: number;
  };
  logs: string[];
};

const STATE_STYLE: Record<CondOrder["state"], { label: string; cls: string }> = {
  WATCH: { label: "👁 盯盘", cls: "bg-[#0d2a3a] text-[#4fc3f7] border-[#1b5a7a]" },
  ARMED: { label: "🎯 持仓待卖", cls: "bg-[#332207] text-[#ffb74d] border-[#8a5a12]" },
  DONE: { label: "✓ 完成", cls: "bg-[#0d2a1a] text-[#66bb6a] border-[#1b6a3a]" },
};

const fmtPct = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;

export default function ConditionsPage() {
  const [st, setSt] = useState<CondStatus | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [formErr, setFormErr] = useState("");

  // 引擎启动参数
  const [cash, setCash] = useState(100000);
  const [poll, setPoll] = useState(5);
  const [live, setLive] = useState(false);

  // 新增条件单表单
  const [symbol, setSymbol] = useState("");
  const [trigger, setTrigger] = useState(-4);
  const [qty, setQty] = useState(1000);
  const [rally, setRally] = useState(1);
  const [windowMin, setWindowMin] = useState(3);

  const logRef = useRef<HTMLPreElement>(null);

  const refresh = useCallback(async () => {
    try {
      const d = await api.conditions();
      setSt(d);
      setErr("");
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, [refresh]);

  // 日志自动滚到底
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [st?.logs]);

  const act = async (fn: () => Promise<any>, done?: () => void) => {
    setBusy(true);
    setFormErr("");
    try {
      await fn();
      await refresh();
      done?.();
    } catch (e: any) {
      setFormErr(e?.message ?? String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleStart = () => {
    if (live && !confirm("⚠️ 实盘模式将真实调用同花顺下单, 不可撤销。确认启动?")) return;
    act(() => api.startConditionEngine({ live, cash, poll_seconds: poll }));
  };
  const handleStop = () => act(() => api.stopConditionEngine());

  const handleAdd = () => {
    const sym = symbol.trim();
    if (!sym) { setFormErr("请输入标的代码"); return; }
    act(
      () => api.addCondition({
        symbol: sym, trigger_gap_pct: trigger, buy_qty: qty,
        sell_rally_pct: rally, open_window_min: windowMin,
      }),
      () => setSymbol(""),
    );
  };
  const handleDelete = (id: string) => {
    if (!confirm(`删除条件单 ${id}? (引擎运行中会同时停止其盯盘协程)`)) return;
    act(() => api.deleteCondition(id));
  };

  if (err && !st) {
    return (
      <div className="p-8 space-y-2">
        <h1 className="text-2xl font-bold">⚡ 条件单</h1>
        <div className="text-sm text-[#f44336] bg-[#2a0808] border border-[#7f1d1d] rounded p-3 whitespace-pre-wrap">
          加载失败：{err}
        </div>
      </div>
    );
  }
  if (!st) return <div className="text-[#666] p-8">加载中...</div>;

  const pf = st.portfolio;
  const running = st.running;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <h1 className="text-2xl font-bold">⚡ 条件单</h1>
        <span className={`text-xs px-2 py-0.5 rounded border ${
          running
            ? st.live
              ? "bg-[#2a0808] text-[#ef5350] border-[#7f1d1d] animate-pulse"
              : "bg-[#0d2a1a] text-[#66bb6a] border-[#1b6a3a]"
            : "bg-[#1a1a1a] text-[#888] border-[#333]"
        }`}>
          {running ? (st.live ? "● 运行中 · 实盘" : "● 运行中 · 模拟") : "○ 已停止"}
        </span>
        {running && st.poll_seconds != null && (
          <span className="text-xs text-[#666]">行情刷新 {st.poll_seconds}s</span>
        )}
      </div>

      {err && (
        <div className="text-sm text-[#f44336] bg-[#2a0808] border border-[#7f1d1d] rounded p-3 whitespace-pre-wrap">
          {err}
        </div>
      )}

      {/* ============== 引擎控制 ============== */}
      <section className="border border-[#2a2a2a] rounded-lg bg-[#141414] p-5 space-y-3">
        <h2 className="text-lg font-semibold">引擎控制</h2>
        {running ? (
          <div className="flex items-center gap-4">
            <button
              onClick={handleStop}
              disabled={busy}
              className="px-5 py-1.5 bg-[#b71c1c] text-white text-sm rounded hover:bg-[#d32f2f] disabled:opacity-50"
            >
              ⏹ 停止引擎
            </button>
            <span className="text-xs text-[#888]">
              停止会取消全部盯盘协程, 订单状态已持久化, 重启后恢复 (ARMED/DONE 不丢)
            </span>
          </div>
        ) : (
          <div className="flex flex-wrap items-end gap-4">
            <label className="text-xs text-[#888] space-y-1">
              <div>初始资金 (已有状态时沿用)</div>
              <input type="number" value={cash} step={10000} min={10000}
                onChange={(e) => setCash(parseFloat(e.target.value) || 0)}
                className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 w-32 font-mono text-sm text-white" />
            </label>
            <label className="text-xs text-[#888] space-y-1">
              <div>行情刷新间隔 (秒)</div>
              <input type="number" value={poll} step={1} min={1}
                onChange={(e) => setPoll(parseFloat(e.target.value) || 5)}
                className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 w-24 font-mono text-sm text-white" />
            </label>
            <label className="flex items-center gap-2 text-sm pb-1.5">
              <input type="checkbox" checked={live}
                onChange={(e) => setLive(e.target.checked)}
                className="accent-[#ff6d00] w-4 h-4" />
              <span className={live ? "text-[#ef5350] font-semibold animate-pulse" : "text-[#888]"}>
                ⚡ 实盘 (真实同花顺下单, 不可撤销)
              </span>
            </label>
            <button
              onClick={handleStart}
              disabled={busy}
              className="px-5 py-1.5 bg-[#ff6d00] text-white text-sm rounded hover:bg-[#e65100] disabled:opacity-50"
            >
              ▶ 启动引擎
            </button>
          </div>
        )}
      </section>

      {/* ============== 新增条件单 ============== */}
      <section className="border border-[#2a2a2a] rounded-lg bg-[#141414] p-5 space-y-3">
        <h2 className="text-lg font-semibold">新增条件单</h2>
        <p className="text-xs text-[#888]">
          状态机: <span className="text-[#4fc3f7]">盯盘</span> — 开盘前 {windowMin} 分钟内最新价相对昨收
          跌破 <code className="text-[#ffd54f]">{trigger}%</code> → 买入{" "}
          <code className="text-[#ffd54f]">{qty}</code> 股 →{" "}
          <span className="text-[#ffb74d]">持仓待卖</span> — 反弹到买入价{" "}
          <code className="text-[#ffd54f]">+{rally}%</code> → 卖出 →{" "}
          <span className="text-[#66bb6a]">完成</span>。同一标的只允许一单。
        </p>
        <div className="flex flex-wrap items-end gap-4">
          <label className="text-xs text-[#888] space-y-1">
            <div>标的代码</div>
            <SymbolPicker value={symbol} onChange={setSymbol}
              placeholder="601899 / sz159915" width="w-36" />
          </label>
          <label className="text-xs text-[#888] space-y-1">
            <div>触发跌幅 % (负=低开买入)</div>
            <input type="number" value={trigger} step={0.5}
              onChange={(e) => setTrigger(parseFloat(e.target.value) || 0)}
              className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 w-28 font-mono text-sm text-white" />
          </label>
          <label className="text-xs text-[#888] space-y-1">
            <div>买入数量 (股)</div>
            <input type="number" value={qty} step={100} min={100}
              onChange={(e) => setQty(parseInt(e.target.value) || 0)}
              className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 w-28 font-mono text-sm text-white" />
          </label>
          <label className="text-xs text-[#888] space-y-1">
            <div>反弹卖出 %</div>
            <input type="number" value={rally} step={0.1} min={0}
              onChange={(e) => setRally(parseFloat(e.target.value) || 0)}
              className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 w-24 font-mono text-sm text-white" />
          </label>
          <label className="text-xs text-[#888] space-y-1">
            <div>开盘判定窗口 (分钟)</div>
            <input type="number" value={windowMin} step={1} min={1}
              onChange={(e) => setWindowMin(parseInt(e.target.value) || 3)}
              className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 w-24 font-mono text-sm text-white" />
          </label>
          <button
            onClick={handleAdd}
            disabled={busy}
            className="px-5 py-1.5 bg-[#2a6a2a] text-white text-sm rounded hover:bg-[#2e7d32] disabled:opacity-50"
          >
            ＋ 添加
          </button>
        </div>
        {formErr && (
          <div className="text-xs text-[#ef5350] bg-[#2a0808] border border-[#7f1d1d] rounded px-3 py-2 whitespace-pre-wrap">
            {formErr}
          </div>
        )}
      </section>

      {/* ============== 订单列表 ============== */}
      <section className="border border-[#2a2a2a] rounded-lg bg-[#141414] p-5 space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">条件单列表 ({st.orders.length})</h2>
          <span className="text-xs text-[#666]">每 3 秒自动刷新</span>
        </div>
        {st.orders.length === 0 ? (
          <div className="text-sm text-[#666] py-4 text-center">
            还没有条件单, 用上方表单添加第一单
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[#888] text-xs border-b border-[#2a2a2a]">
                  <th className="py-2 pr-3">标的</th>
                  <th className="py-2 pr-3">状态</th>
                  <th className="py-2 pr-3">最新价 / 相对昨收</th>
                  <th className="py-2 pr-3">触发跌幅</th>
                  <th className="py-2 pr-3">买入</th>
                  <th className="py-2 pr-3">反弹卖出</th>
                  <th className="py-2 pr-3">买入记录</th>
                  <th className="py-2 pr-3">卖出记录</th>
                  <th className="py-2 pr-3"></th>
                </tr>
              </thead>
              <tbody>
                {st.orders.map((o) => {
                  const s = STATE_STYLE[o.state];
                  return (
                    <tr key={o.id} className="border-b border-[#1f1f1f] hover:bg-[#1a1a1a]">
                      <td className="py-2 pr-3 font-mono text-white">{o.symbol}</td>
                      <td className="py-2 pr-3">
                        <span className={`text-xs px-2 py-0.5 rounded border whitespace-nowrap ${s.cls}`}>
                          {s.label}
                        </span>
                        {o.buy_locked && o.state === "WATCH" && (
                          <div className="text-[10px] text-[#888] mt-0.5">今日窗口已过</div>
                        )}
                      </td>
                      <td className="py-2 pr-3 font-mono">
                        {o.last_price > 0 ? (
                          <>
                            {o.last_price.toFixed(3)}
                            <span className={o.last_gap_pct >= 0 ? "text-[#ef5350] ml-2" : "text-[#26a69a] ml-2"}>
                              {fmtPct(o.last_gap_pct)}
                            </span>
                          </>
                        ) : "—"}
                      </td>
                      <td className="py-2 pr-3 font-mono text-[#4fc3f7]">{o.trigger_gap_pct}%</td>
                      <td className="py-2 pr-3 font-mono">{o.buy_qty} 股</td>
                      <td className="py-2 pr-3 font-mono">+{o.sell_rally_pct}%</td>
                      <td className="py-2 pr-3 font-mono text-xs text-[#ccc]">
                        {o.buy_price > 0 ? `${o.buy_price.toFixed(3)} · ${o.buy_ts.slice(5, 19)}` : "—"}
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs text-[#ccc]">
                        {o.sell_price > 0 ? `${o.sell_price.toFixed(3)} · ${o.sell_ts.slice(5, 19)}` : "—"}
                      </td>
                      <td className="py-2 pr-3">
                        <button onClick={() => handleDelete(o.id)}
                          className="text-xs text-[#ef5350] hover:text-white bg-transparent border border-[#5a2a2a] rounded px-2 py-0.5 hover:bg-[#3a1515]">
                          删除
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {st.orders.some((o) => o.error) && (
          <div className="text-xs text-[#ffb74d] bg-[#2a1a08] border border-[#7a5a12] rounded px-3 py-2">
            {st.orders.filter((o) => o.error).map((o) => (
              <div key={o.id}>⚠ {o.id}: {o.error}</div>
            ))}
          </div>
        )}
      </section>

      {/* ============== 模拟账户 + 引擎日志 ============== */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <section className="border border-[#2a2a2a] rounded-lg bg-[#141414] p-5 space-y-2">
          <h2 className="text-lg font-semibold">账户 (Portfolio)</h2>
          <div className="text-sm font-mono space-y-1">
            <div className="flex justify-between border-b border-[#1f1f1f] pb-1">
              <span className="text-[#888]">现金</span>
              <span className="text-[#ffd54f]">{pf.cash.toFixed(2)}</span>
            </div>
            <div className="flex justify-between border-b border-[#1f1f1f] pb-1">
              <span className="text-[#888]">初始资金</span>
              <span>{pf.initial_cash.toFixed(2)}</span>
            </div>
            <div className="flex justify-between border-b border-[#1f1f1f] pb-1">
              <span className="text-[#888]">未成交订单</span>
              <span>{pf.open_orders}</span>
            </div>
          </div>
          {Object.keys(pf.positions).length === 0 ? (
            <div className="text-xs text-[#666] pt-1">当前无持仓</div>
          ) : (
            <table className="w-full text-xs font-mono">
              <thead>
                <tr className="text-left text-[#888] border-b border-[#2a2a2a]">
                  <th className="py-1 pr-2">标的</th>
                  <th className="py-1 pr-2">持仓</th>
                  <th className="py-1 pr-2">成本价</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(pf.positions).map(([sym, p]) => (
                  <tr key={sym} className="border-b border-[#1f1f1f]">
                    <td className="py-1 pr-2 text-white">{sym}</td>
                    <td className="py-1 pr-2">{p.qty}</td>
                    <td className="py-1 pr-2">{p.avg_price.toFixed(3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="border border-[#2a2a2a] rounded-lg bg-[#141414] p-5 space-y-2">
          <h2 className="text-lg font-semibold">引擎日志</h2>
          <pre ref={logRef}
            className="text-[11px] leading-4 font-mono bg-[#0a0a0a] border border-[#1f1f1f] rounded p-3 h-64 overflow-y-auto whitespace-pre-wrap text-[#9ccc65]">
{st.logs.length ? st.logs.join("\n") : "引擎未运行或暂无日志"}
          </pre>
        </section>
      </div>
    </div>
  );
}
