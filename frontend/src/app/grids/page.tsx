"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import SymbolPicker from "@/components/SymbolPicker";

// ---- 类型 (对应 GET /api/grids 返回) ----
type GridRow = {
  id: string;
  symbol: string;
  upper: number;
  lower: number;
  grid_unit: "pct" | "price";
  step: number;
  base_price: number;
  qty_mode: "qty" | "cash";
  per_qty: number;
  per_cash: number;
  multiplier: number;
  max_position: number;
  min_position: number;
  sell_retrace_pct: number;
  buy_rebound_pct: number;
  pad_pct: number;
  t1_protect: boolean;
  expire_date: string;
  base_qty: number;
  state: "RUNNING" | "PAUSED" | "EXHAUSTED" | "EXPIRED";
  bootstrapped: boolean;
  buy_trigger: number;
  sell_trigger: number;
  depth: number;
  pending_order_id: string;
  pending_side: string;
  last_buy_price: number;
  last_buy_qty: number;
  last_sell_price: number;
  grid_rounds: number;
  realized_pnl: number;
  trades: number;
  last_price: number;
  error: string;
  created_at: string;
};

type GridStatus = {
  running: boolean;
  live: boolean | null;
  poll_seconds: number | null;
  grids: GridRow[];
  portfolio: {
    cash: number;
    initial_cash: number;
    positions: Record<string, { qty: number; avg_price: number; target: number }>;
    open_orders: number;
  };
  logs: string[];
};

const STATE_STYLE: Record<GridRow["state"], { label: string; cls: string }> = {
  RUNNING: { label: "● 运行中", cls: "bg-[#0d2a1a] text-[#66bb6a] border-[#1b6a3a]" },
  PAUSED: { label: "⏸ 已暂停", cls: "bg-[#332207] text-[#ffb74d] border-[#8a5a12]" },
  EXHAUSTED: { label: "⚠ 区间失效", cls: "bg-[#2a0808] text-[#ef5350] border-[#7f1d1d]" },
  EXPIRED: { label: "⏱ 已到期", cls: "bg-[#1a1a1a] text-[#888] border-[#333]" },
};

// 由下限向上生成网格档位线 (等差: lower + n*step; 等比: lower*(1+step%)^n)
function gridLevels(lower: number, upper: number, unit: string, step: number): number[] {
  const out: number[] = [];
  if (lower <= 0 || upper <= 0 || step <= 0) return out;
  if (unit === "price") {
    for (let p = lower; p <= upper + 1e-9 && out.length < 200; p += step) out.push(p);
  } else {
    const f = 1 + step / 100;
    for (let p = lower; p <= upper * (1 + 1e-9) && out.length < 200; p *= f) out.push(p);
  }
  return out;
}

// 区间可视化: 档位线 + 买/卖触发价 + 当前价指针 (线性刻度 lower..upper)
function RangeBar({ lower, upper, unit, step, buy, sell, last }: {
  lower: number; upper: number; unit: string; step: number;
  buy: number; sell: number; last: number;
}) {
  if (upper <= lower) return null;
  const pct = (p: number) =>
    Math.max(0, Math.min(100, ((p - lower) / (upper - lower)) * 100));
  const levels = gridLevels(lower, upper, unit, step);
  return (
    <div className="relative h-8 rounded bg-[#0a0a0a] border border-[#1f1f1f]">
      {levels.map((p, i) => (
        <div key={i} className="absolute top-1 bottom-1 w-px bg-[#26323a]"
          style={{ left: `${pct(p)}%` }} />
      ))}
      <div className="absolute top-0 bottom-0 w-[2px] bg-[#26a69a]" style={{ left: `${pct(buy)}%` }}
        title={`买触发 ${buy}`} />
      <div className="absolute top-0 bottom-0 w-[2px] bg-[#ef5350]" style={{ left: `${pct(sell)}%` }}
        title={`卖触发 ${sell}`} />
      {last > 0 && (
        <div className="absolute top-0 bottom-0 w-[2px] bg-[#ffd54f] shadow-[0_0_6px_#ffd54f]"
          style={{ left: `${pct(last)}%` }} title={`当前价 ${last}`} />
      )}
      <span className="absolute left-1 top-1/2 -translate-y-1/2 text-[9px] font-mono text-[#555]">{lower}</span>
      <span className="absolute right-1 top-1/2 -translate-y-1/2 text-[9px] font-mono text-[#555]">{upper}</span>
    </div>
  );
}

export default function GridsPage() {
  const [st, setSt] = useState<GridStatus | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [formErr, setFormErr] = useState("");

  // 引擎启动参数
  const [cash, setCash] = useState(100000);
  const [poll, setPoll] = useState(5);
  const [live, setLive] = useState(false);

  // 新建网格表单
  const [symbol, setSymbol] = useState("");
  const [upper, setUpper] = useState(0);
  const [lower, setLower] = useState(0);
  const [unit, setUnit] = useState<"pct" | "price">("pct");
  const [step, setStep] = useState(2);
  const [base, setBase] = useState(0);
  const [qtyMode, setQtyMode] = useState<"qty" | "cash">("cash");
  const [perQty, setPerQty] = useState(1000);
  const [perCash, setPerCash] = useState(5000);
  const [multiplier, setMultiplier] = useState(1);
  const [maxPos, setMaxPos] = useState(0);
  const [minPos, setMinPos] = useState(0);
  const [baseQty, setBaseQty] = useState(0);
  const [sellRetrace, setSellRetrace] = useState(0);
  const [buyRebound, setBuyRebound] = useState(0);
  const [padPct, setPadPct] = useState(0);
  const [t1, setT1] = useState(true);
  const [expire, setExpire] = useState("");

  const logRef = useRef<HTMLPreElement>(null);

  const refresh = useCallback(async () => {
    try {
      const d = await api.grids();
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

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [st?.logs]);

  const act = async (fn: () => Promise<any>) => {
    setBusy(true);
    setFormErr("");
    try {
      await fn();
      await refresh();
    } catch (e: any) {
      setFormErr(e?.message ?? String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleStart = () => {
    if (live && !confirm("⚠️ 实盘模式将真实调用同花顺下单, 不可撤销。确认启动?")) return;
    act(() => api.startGridEngine({ live, cash, poll_seconds: poll }));
  };
  const handleStop = () => act(() => api.stopGridEngine());
  const handleDelete = (id: string) => {
    if (!confirm(`删除网格单 ${id}? (盯盘协程同步取消, 已有持仓保留在账户)`)) return;
    act(() => api.deleteGrid(id));
  };

  // 用实时行情预填基准价
  const fetchQuote = async () => {
    const sym = symbol.trim();
    if (!sym) { setFormErr("请先输入标的代码"); return; }
    act(async () => {
      const q: any = await api.quote(sym);
      const last = Number(q?.last) || 0;
      if (last > 0) {
        setBase(last);
        if (!lower) setLower(Number((last * 0.9).toFixed(3)));
        if (!upper || upper <= lower) setUpper(Number((last * 1.1).toFixed(3)));
      }
    });
  };

  const handleAdd = () => {
    const sym = symbol.trim();
    if (!sym) { setFormErr("请输入标的代码"); return; }
    if (upper <= 0 || lower <= 0 || upper <= lower) { setFormErr("区间非法: 需 0 < 下限 < 上限"); return; }
    if (base <= 0) { setFormErr("请填写基准价 (可用「取当前价」按钮)"); return; }
    act(() => api.addGrid({
      symbol: sym, upper, lower, grid_unit: unit, step, base_price: base,
      qty_mode: qtyMode, per_qty: perQty, per_cash: perCash,
      multiplier, max_position: maxPos, min_position: minPos,
      sell_retrace_pct: sellRetrace, buy_rebound_pct: buyRebound,
      pad_pct: padPct, t1_protect: t1, expire_date: expire, base_qty: baseQty,
    }));
  };

  // ---- 新建表单实时预览 ----
  const previewBase = base > 0 ? base : 0;
  const pBuy = previewBase > 0
    ? (unit === "price" ? previewBase - step : previewBase * (1 - step / 100)) : 0;
  const pSell = previewBase > 0
    ? (unit === "price" ? previewBase + step : previewBase * (1 + step / 100)) : 0;
  // 下行加仓档数与预估最大占用 (含倍量; 上行卖出档数)
  let downSteps = 0, upSteps = 0, estCost = 0;
  if (previewBase > 0 && step > 0 && upper > lower) {
    let p = previewBase;
    const m = Math.max(1, multiplier);
    while (p > lower && downSteps < 200) {
      const px = unit === "price" ? p - step : p * (1 - step / 100);
      if (px < lower) break;
      const qty = qtyMode === "cash"
        ? Math.floor((perCash * m ** downSteps) / px / 100) * 100
        : Math.floor(perQty * m ** downSteps / 100) * 100;
      estCost += qty * px;
      p = px;
      downSteps++;
    }
    while (p < upper && upSteps < 200) {
      const px = unit === "price" ? p + step : p * (1 + step / 100);
      if (px > upper) break;
      p = px;
      upSteps++;
    }
  }
  const estBaseCost = previewBase > 0 && baseQty > 0 ? baseQty * previewBase : 0;

  if (err && !st) {
    return (
      <div className="p-8 space-y-2">
        <h1 className="text-2xl font-bold">🌐 网格交易</h1>
        <div className="text-sm text-[#f44336] bg-[#2a0808] border border-[#7f1d1d] rounded p-3 whitespace-pre-wrap">
          加载失败：{err}
        </div>
      </div>
    );
  }
  if (!st) return <div className="text-[#666] p-8">加载中...</div>;

  const pf = st.portfolio;
  const running = st.running;

  const inputCls = "bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 font-mono text-sm text-white placeholder-[#555]";

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <h1 className="text-2xl font-bold">🌐 网格交易</h1>
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
            <button onClick={handleStop} disabled={busy}
              className="px-5 py-1.5 bg-[#b71c1c] text-white text-sm rounded hover:bg-[#d32f2f] disabled:opacity-50">
              ⏹ 停止引擎
            </button>
            <span className="text-xs text-[#888]">
              停止会取消全部盯盘协程, 网格状态已持久化, 重启后恢复 (depth/触发价/盈亏不丢)
            </span>
          </div>
        ) : (
          <div className="flex flex-wrap items-end gap-4">
            <label className="text-xs text-[#888] space-y-1">
              <div>初始资金 (已有状态时沿用)</div>
              <input type="number" value={cash} step={10000} min={10000}
                onChange={(e) => setCash(parseFloat(e.target.value) || 0)}
                className={`${inputCls} w-32`} />
            </label>
            <label className="text-xs text-[#888] space-y-1">
              <div>行情刷新间隔 (秒)</div>
              <input type="number" value={poll} step={1} min={1}
                onChange={(e) => setPoll(parseFloat(e.target.value) || 5)}
                className={`${inputCls} w-24`} />
            </label>
            <label className="flex items-center gap-2 text-sm pb-1.5">
              <input type="checkbox" checked={live}
                onChange={(e) => setLive(e.target.checked)}
                className="accent-[#ff6d00] w-4 h-4" />
              <span className={live ? "text-[#ef5350] font-semibold animate-pulse" : "text-[#888]"}>
                ⚡ 实盘 (真实同花顺下单, 不可撤销)
              </span>
            </label>
            <button onClick={handleStart} disabled={busy}
              className="px-5 py-1.5 bg-[#ff6d00] text-white text-sm rounded hover:bg-[#e65100] disabled:opacity-50">
              ▶ 启动引擎
            </button>
          </div>
        )}
      </section>

      {/* ============== 新建网格 ============== */}
      <section className="border border-[#2a2a2a] rounded-lg bg-[#141414] p-5 space-y-3">
        <h2 className="text-lg font-semibold">新建网格</h2>
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
          {/* ---- 左: 参数表单 (2列) ---- */}
          <div className="xl:col-span-2 space-y-4">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <label className="text-xs text-[#888] space-y-1 col-span-2 md:col-span-2">
                <div>标的代码</div>
                <div className="flex gap-2">
                  <SymbolPicker value={symbol} onChange={setSymbol}
                    placeholder="601899 / sz159915"
                    inputClassName={`${inputCls} w-full`}
                    wrapClassName="flex-1 min-w-0" />
                  <button type="button" onClick={fetchQuote} disabled={busy}
                    className="whitespace-nowrap text-xs px-2 py-1 rounded border border-[#2a5a7a] text-[#4fc3f7] hover:bg-[#0d2a3a]">
                    取当前价
                  </button>
                </div>
              </label>
              <label className="text-xs text-[#888] space-y-1">
                <div>网格单位</div>
                <select value={unit} onChange={(e) => setUnit(e.target.value as "pct" | "price")}
                  className={`${inputCls} w-full`}>
                  <option value="pct">等比 (%)</option>
                  <option value="price">等差 (元)</option>
                </select>
              </label>
              <label className="text-xs text-[#888] space-y-1">
                <div>网格间距 {unit === "pct" ? "(%)" : "(元)"}</div>
                <input type="number" value={step} step={unit === "pct" ? 0.5 : 0.01} min={0.001}
                  onChange={(e) => setStep(parseFloat(e.target.value) || 0)}
                  className={`${inputCls} w-full`} />
              </label>
              <label className="text-xs text-[#888] space-y-1">
                <div>区间上限</div>
                <input type="number" value={upper || ""} step={0.01}
                  onChange={(e) => setUpper(parseFloat(e.target.value) || 0)}
                  className={`${inputCls} w-full`} />
              </label>
              <label className="text-xs text-[#888] space-y-1">
                <div>区间下限</div>
                <input type="number" value={lower || ""} step={0.01}
                  onChange={(e) => setLower(parseFloat(e.target.value) || 0)}
                  className={`${inputCls} w-full`} />
              </label>
              <label className="text-xs text-[#888] space-y-1 col-span-2">
                <div>基准价 (启动价, 成交后滚动更新)</div>
                <input type="number" value={base || ""} step={0.001}
                  onChange={(e) => setBase(parseFloat(e.target.value) || 0)}
                  className={`${inputCls} w-full`} />
              </label>
              <label className="text-xs text-[#888] space-y-1">
                <div>每格数量模式</div>
                <select value={qtyMode} onChange={(e) => setQtyMode(e.target.value as "qty" | "cash")}
                  className={`${inputCls} w-full`}>
                  <option value="cash">固定金额</option>
                  <option value="qty">固定股数</option>
                </select>
              </label>
              <label className="text-xs text-[#888] space-y-1">
                <div>{qtyMode === "cash" ? "每格金额 (元)" : "每格股数"}</div>
                {qtyMode === "cash" ? (
                  <input type="number" value={perCash} step={1000} min={0}
                    onChange={(e) => setPerCash(parseFloat(e.target.value) || 0)}
                    className={`${inputCls} w-full`} />
                ) : (
                  <input type="number" value={perQty} step={100} min={0}
                    onChange={(e) => setPerQty(parseInt(e.target.value) || 0)}
                    className={`${inputCls} w-full`} />
                )}
              </label>
              <label className="text-xs text-[#888] space-y-1">
                <div>梯度倍量 (1=等量)</div>
                <input type="number" value={multiplier} step={0.1} min={1}
                  onChange={(e) => setMultiplier(parseFloat(e.target.value) || 1)}
                  className={`${inputCls} w-full`} />
              </label>
              <label className="text-xs text-[#888] space-y-1">
                <div>启动底仓 (股, 0=不买)</div>
                <input type="number" value={baseQty} step={100} min={0}
                  onChange={(e) => setBaseQty(parseInt(e.target.value) || 0)}
                  className={`${inputCls} w-full`} />
              </label>
              <label className="text-xs text-[#888] space-y-1">
                <div>最大持仓 (股, 0=不限)</div>
                <input type="number" value={maxPos || ""} step={100} min={0}
                  onChange={(e) => setMaxPos(parseInt(e.target.value) || 0)}
                  className={`${inputCls} w-full`} />
              </label>
              <label className="text-xs text-[#888] space-y-1">
                <div>最小底仓 (股, 0=不限)</div>
                <input type="number" value={minPos || ""} step={100} min={0}
                  onChange={(e) => setMinPos(parseInt(e.target.value) || 0)}
                  className={`${inputCls} w-full`} />
              </label>
              <label className="text-xs text-[#888] space-y-1">
                <div>卖出回落确认 % (0=即卖)</div>
                <input type="number" value={sellRetrace} step={0.1} min={0}
                  onChange={(e) => setSellRetrace(parseFloat(e.target.value) || 0)}
                  className={`${inputCls} w-full`} />
              </label>
              <label className="text-xs text-[#888] space-y-1">
                <div>买入反弹确认 % (0=即买)</div>
                <input type="number" value={buyRebound} step={0.1} min={0}
                  onChange={(e) => setBuyRebound(parseFloat(e.target.value) || 0)}
                  className={`${inputCls} w-full`} />
              </label>
              <label className="text-xs text-[#888] space-y-1">
                <div>下单价格浮动 % (保成交)</div>
                <input type="number" value={padPct} step={0.05} min={0}
                  onChange={(e) => setPadPct(parseFloat(e.target.value) || 0)}
                  className={`${inputCls} w-full`} />
              </label>
              <label className="text-xs text-[#888] space-y-1">
                <div>有效期 (空=长期)</div>
                <input type="date" value={expire}
                  onChange={(e) => setExpire(e.target.value)}
                  className={`${inputCls} w-full`} />
              </label>
              <label className="flex items-center gap-2 text-xs text-[#888] pb-1">
                <input type="checkbox" checked={t1}
                  onChange={(e) => setT1(e.target.checked)}
                  className="accent-[#ff6d00] w-4 h-4" />
                <span>T+1 保护 (当日买入当日不卖)</span>
              </label>
            </div>
            <div className="flex items-center gap-4">
              <button onClick={handleAdd} disabled={busy}
                className="px-5 py-1.5 bg-[#2a6a2a] text-white text-sm rounded hover:bg-[#2e7d32] disabled:opacity-50">
                ＋ 创建网格
              </button>
              <span className="text-xs text-[#666]">
                同一标的一个网格; 创建后引擎运行中立即生效, 无需重启
              </span>
            </div>
            {formErr && (
              <div className="text-xs text-[#ef5350] bg-[#2a0808] border border-[#7f1d1d] rounded px-3 py-2 whitespace-pre-wrap">
                {formErr}
              </div>
            )}
          </div>

          {/* ---- 右: 实时预览 ---- */}
          <div className="space-y-3 text-xs">
            <div className="text-[#888] font-semibold">📐 参数预览</div>
            {previewBase > 0 && pBuy > 0 && pSell > 0 ? (
              <>
                <RangeBar lower={lower || pBuy * 0.9} upper={upper || pSell * 1.1}
                  unit={unit} step={step} buy={pBuy} sell={pSell} last={previewBase} />
                <div className="grid grid-cols-2 gap-x-4 gap-y-1 font-mono bg-[#0a0a0a] border border-[#1f1f1f] rounded p-3">
                  <span className="text-[#888] font-sans">首档买触发</span>
                  <span className="text-[#26a69a] text-right">{pBuy.toFixed(3)}</span>
                  <span className="text-[#888] font-sans">首档卖触发</span>
                  <span className="text-[#ef5350] text-right">{pSell.toFixed(3)}</span>
                  <span className="text-[#888] font-sans">下行可加仓</span>
                  <span className="text-right">{downSteps} 档</span>
                  <span className="text-[#888] font-sans">上行可卖出</span>
                  <span className="text-right">{upSteps} 档</span>
                  <span className="text-[#888] font-sans">预估最大加仓资金</span>
                  <span className="text-[#ffd54f] text-right">≈ {estCost.toFixed(0)}</span>
                  {estBaseCost > 0 && (
                    <>
                      <span className="text-[#888] font-sans">底仓资金</span>
                      <span className="text-[#ffd54f] text-right">≈ {estBaseCost.toFixed(0)}</span>
                      <span className="text-[#888] font-sans">合计预估</span>
                      <span className="text-[#ffd54f] text-right">≈ {(estCost + estBaseCost).toFixed(0)}</span>
                    </>
                  )}
                  {multiplier > 1 && (
                    <span className="col-span-2 text-[10px] text-[#ffb74d] font-sans">
                      已含梯度倍量 ×{multiplier}: 越跌每格买越多 (金字塔)
                    </span>
                  )}
                </div>
                <div className="text-[10px] text-[#666] leading-4">
                  提示: 宽基 ETF 建议 2% 一档, 高波动行业 ETF 3-4%; &lt;1% 手续费会吃掉差价。
                  区间取年内高低点 ±10% 缓冲, 资金按 底仓3成+网格6成+备用1成 分配。
                </div>
              </>
            ) : (
              <div className="text-[#555] py-4 text-center border border-dashed border-[#333] rounded">
                填入标的并「取当前价」后显示预览
              </div>
            )}
          </div>
        </div>
      </section>

      {/* ============== 网格列表 ============== */}
      <section className="border border-[#2a2a2a] rounded-lg bg-[#141414] p-5 space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">网格列表 ({st.grids.length})</h2>
          <span className="text-xs text-[#666]">每 3 秒自动刷新 · 成交驱动: 上一笔未成交不判下一档</span>
        </div>
        {st.grids.length === 0 ? (
          <div className="text-sm text-[#666] py-4 text-center">
            还没有网格单, 用上方表单创建第一个网格
          </div>
        ) : (
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            {st.grids.map((g) => {
              const s = STATE_STYLE[g.state];
              return (
                <div key={g.id} className="border border-[#2a2a2a] rounded-lg bg-[#101010] p-4 space-y-3">
                  {/* 标题行 */}
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-white font-semibold">{g.symbol}</span>
                    <span className={`text-xs px-2 py-0.5 rounded border ${s.cls}`}>{s.label}</span>
                    <span className="text-xs font-mono text-[#888]">
                      {g.grid_unit === "pct" ? `等比 ${g.step}%` : `等差 ${g.step}元`}
                    </span>
                    <span className="text-xs font-mono text-[#888]">
                      {g.qty_mode === "cash" ? `${g.per_cash}元/格` : `${g.per_qty}股/格`}
                      {g.multiplier > 1 ? ` ×${g.multiplier}` : ""}
                    </span>
                    <span className="ml-auto text-sm font-mono font-semibold"
                      style={{ color: g.realized_pnl >= 0 ? "#66bb6a" : "#ef5350" }}>
                      {g.realized_pnl >= 0 ? "+" : ""}{g.realized_pnl.toFixed(2)}
                    </span>
                  </div>

                  {/* 区间可视化 */}
                  <RangeBar lower={g.lower} upper={g.upper} unit={g.grid_unit} step={g.step}
                    buy={g.buy_trigger} sell={g.sell_trigger} last={g.last_price} />

                  {/* 运行数据 */}
                  <div className="grid grid-cols-4 gap-x-3 gap-y-1 text-xs font-mono">
                    <span className="text-[#888]">当前价</span>
                    <span className="text-[#ffd54f]">{g.last_price > 0 ? g.last_price.toFixed(3) : "—"}</span>
                    <span className="text-[#888]">基准价</span>
                    <span>{g.base_price > 0 ? g.base_price.toFixed(3) : "—"}</span>
                    <span className="text-[#888]">买触发</span>
                    <span className="text-[#26a69a]">{g.buy_trigger > 0 ? g.buy_trigger.toFixed(3) : "—"}</span>
                    <span className="text-[#888]">卖触发</span>
                    <span className="text-[#ef5350]">{g.sell_trigger > 0 ? g.sell_trigger.toFixed(3) : "—"}</span>
                    <span className="text-[#888]">加仓深度</span>
                    <span>{g.depth}</span>
                    <span className="text-[#888]">完成轮数</span>
                    <span>{g.grid_rounds}</span>
                    <span className="text-[#888]">最近买入</span>
                    <span className="text-[#26a69a]">
                      {g.last_buy_price > 0 ? `${g.last_buy_qty}@${g.last_buy_price.toFixed(3)}` : "—"}
                    </span>
                    <span className="text-[#888]">最近卖出</span>
                    <span className="text-[#ef5350]">
                      {g.last_sell_price > 0 ? g.last_sell_price.toFixed(3) : "—"}
                    </span>
                  </div>

                  {/* 风控/增强摘要 */}
                  <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-[#777]">
                    <span>区间 {g.lower}~{g.upper}</span>
                    {g.max_position > 0 && <span>最大持仓 {g.max_position}</span>}
                    {g.min_position > 0 && <span>最小底仓 {g.min_position}</span>}
                    {g.base_qty > 0 && <span>启动底仓 {g.base_qty}</span>}
                    {g.sell_retrace_pct > 0 && <span>卖出回落 {g.sell_retrace_pct}%</span>}
                    {g.buy_rebound_pct > 0 && <span>买入反弹 {g.buy_rebound_pct}%</span>}
                    {g.pad_pct > 0 && <span>价格浮动 {g.pad_pct}%</span>}
                    {g.t1_protect && <span>T+1</span>}
                    {g.expire_date && <span>有效期至 {g.expire_date}</span>}
                    {g.pending_order_id && <span className="text-[#4fc3f7]">待成交: {g.pending_side}</span>}
                  </div>

                  {/* 操作 */}
                  <div className="flex items-center gap-2 pt-1">
                    {g.state === "RUNNING" && (
                      <button onClick={() => act(() => api.pauseGrid(g.id))} disabled={busy}
                        className="text-xs text-[#ffb74d] border border-[#7a5a12] rounded px-2 py-0.5 hover:bg-[#2a1a08]">
                        ⏸ 暂停
                      </button>
                    )}
                    {g.state === "PAUSED" && (
                      <button onClick={() => act(() => api.resumeGrid(g.id))} disabled={busy}
                        className="text-xs text-[#66bb6a] border border-[#1b6a3a] rounded px-2 py-0.5 hover:bg-[#0d2a1a]">
                        ▶ 恢复
                      </button>
                    )}
                    <button onClick={() => handleDelete(g.id)} disabled={busy}
                      className="text-xs text-[#ef5350] hover:text-white bg-transparent border border-[#5a2a2a] rounded px-2 py-0.5 hover:bg-[#3a1515]">
                      删除
                    </button>
                    {g.error && <span className="text-[10px] text-[#ffb74d]">⚠ {g.error}</span>}
                  </div>
                </div>
              );
            })}
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
