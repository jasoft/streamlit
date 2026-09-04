"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

// ---- 类型 (对应 /api/portfolios 系列) ----
type PfItem = {
  code: string;
  name: string;
  weight: number;
  price: number;
  position: number | null;   // null = 同花顺持仓不可得
  available: number | null;
  cost: number | null;
  market_value: number;
  actual_weight: number;
  drift: number;             // 实际权重 - 目标权重 (%)
};

type HistoryEntry = {
  ts: string;
  action: string;
  dry_run: boolean;
  amount?: number;
  ok_count?: number;
  fail_count?: number;
  summary?: string;
  orders: Record<string, any>[];
};

type Portfolio = {
  id: string;
  name: string;
  note: string;
  items: PfItem[];
  market_value: number;
  history: HistoryEntry[];
  created_at: string;
  updated_at: string;
};

type Overview = {
  portfolios: Portfolio[];
  ths_ok: boolean | null;
  ths_msg: string;
  ts: string;
};

type PlanRow = {
  code: string;
  name: string;
  weight: number;
  price: number;
  qty: number;
  amount?: number;
  note?: string;
  position?: number;
  target_value?: number;
  cur_value?: number;
  delta_value?: number;
  side?: string;
};

type PreviewData = {
  action: "buy" | "sell" | "sync";
  amount: number;
  plan: { rows: PlanRow[]; used?: number; leftover?: number;
          total_value?: number; external?: Record<string, any>[] };
  ts: string;
};

type ExecResult = {
  ok: boolean;
  dry_run: boolean;
  action: string;
  orders: Record<string, any>[];
  summary?: string;
  msg?: string;
};

type Action = "buy" | "sell" | "sync" | "adjust";

const ACTION_LABEL: Record<Action, string> = {
  buy: "💰 买入", sell: "💸 卖出", sync: "🔄 同步仓位", adjust: "✏️ 调整",
};

export default function PortfoliosPage() {
  const [st, setSt] = useState<Overview | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);

  // ---- 创建组合表单 ----
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [mode, setMode] = useState<"weight" | "amount" | "qty">("weight");
  const [rows, setRows] = useState(
    [{ code: "", name: "", price: 0, value: "" }] as
    { code: string; name: string; price: number; value: string }[]);
  const [createErr, setCreateErr] = useState("");

  // ---- 展开的操作面板 + 每面板输入/预览/执行结果 ----
  const [panel, setPanel] = useState<{ pid: string; action: Action } | null>(null);
  const [amount, setAmount] = useState(0);
  const [padPct, setPadPct] = useState(0.3);
  const [minOrder, setMinOrder] = useState(1000);
  const [previews, setPreviews] = useState<Record<string, PreviewData>>({});
  const [results, setResults] = useState<Record<string, ExecResult | undefined>>({});
  const [adjustRows, setAdjustRows] = useState<Record<string,
    { code: string; name: string; weight: string }[]>>({});

  const refresh = useCallback(async () => {
    try {
      const d = await api.portfolios(true);
      setSt(d);
      setErr("");
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    if (!autoRefresh) return;
    const t = setInterval(refresh, 6000);
    return () => clearInterval(t);
  }, [refresh, autoRefresh]);

  const act = async (fn: () => Promise<any>) => {
    setBusy(true);
    setErr("");
    try {
      await fn();
      await refresh();
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    } finally {
      setBusy(false);
    }
  };

  // ---- 行情辅助: 代码失焦自动带出名称/现价 ----
  const fillQuote = async (idx: number, forAdjust = false, rowIdx = -1) => {
    const code = forAdjust
      ? adjustRows[panel?.pid ?? ""]?.[rowIdx]?.code?.trim()
      : rows[idx]?.code?.trim();
    if (!code || code.length !== 6) return;
    try {
      const q: any = await api.quote(code);
      const last = Number(q?.last) || 0;
      const nm = String(q?.name || "");
      if (forAdjust && panel) {
        setAdjustRows((prev) => ({
          ...prev,
          [panel.pid]: (prev[panel.pid] ?? []).map((r, i) =>
            i === rowIdx ? { ...r, name: nm || r.name } : r),
        }));
      } else {
        setRows((prev) => prev.map((r, i) =>
          i === idx ? { ...r, name: nm || r.name, price: last || r.price } : r));
      }
    } catch { /* 行情失败静默, 手填也行 */ }
  };

  // ---- 创建: 按 mode 折算权重提交 ----
  const valueOf = (r: { value: string; price: number }) =>
    mode === "qty" ? (parseFloat(r.value) || 0) * r.price : (parseFloat(r.value) || 0);

  const handleCreate = () => {
    setCreateErr("");
    const valid = rows.filter((r) => r.code.trim());
    if (!name.trim()) { setCreateErr("请填写组合名称"); return; }
    if (!valid.length) { setCreateErr("至少添加 1 个标的"); return; }
    const vals = valid.map(valueOf);
    const sum = vals.reduce((a, b) => a + b, 0);
    if (mode !== "weight" && sum <= 0) {
      setCreateErr(mode === "amount" ? "金额合计必须 > 0" : "股数×现价合计必须 > 0 (代码失焦自动取现价)");
      return;
    }
    if (mode === "qty" && valid.some((r, i) => vals[i] > 0 && r.price <= 0)) {
      setCreateErr("股数模式需要现价 (点击代码输入框失焦自动获取)");
      return;
    }
    const items = valid.map((r, i) => ({
      code: r.code.trim(), name: r.name,
      weight: mode === "weight" ? vals[i] : (vals[i] / sum) * 100,
    }));
    if (items.some((it) => !it.weight || it.weight <= 0)) {
      setCreateErr("每只标的的权重/金额/股数必须 > 0");
      return;
    }
    act(async () => {
      await api.createPortfolio({ name: name.trim(), note, items });
      setName(""); setNote("");
      setRows([{ code: "", name: "", price: 0, value: "" }]);
    });
  };

  // ---- 面板开关 ----
  const openPanel = (p: Portfolio, action: Action) => {
    if (panel?.pid === p.id && panel.action === action) { setPanel(null); return; }
    setPanel({ pid: p.id, action });
    setAmount(action === "buy" || action === "sell"
      ? Math.round(p.market_value || 0) : 0);
    setResults((prev) => ({ ...prev, [`${p.id}:${action}`]: undefined }));
    if (action === "adjust") {
      setAdjustRows((prev) => ({
        ...prev,
        [p.id]: (prev[p.id] ?? p.items.map((it) => ({
          code: it.code, name: it.name, weight: String(it.weight),
        }))).map((r) => ({ ...r })),
      }));
    }
  };

  const pkey = panel ? `${panel.pid}:${panel.action}` : "";
  const preview = previews[pkey];
  const result = results[pkey];

  const doPreview = async () => {
    if (!panel) return;
    await act(async () => {
      const d = await api.portfolioPreview(
        panel.pid, panel.action as "buy" | "sell" | "sync",
        amount || 0, minOrder);
      setPreviews((prev) => ({ ...prev, [pkey]: d }));
    });
  };

  const doExecute = () => {
    if (!panel || panel.action === "adjust") return;
    const n = preview?.plan?.rows?.filter((r) => r.qty > 0).length ?? 0;
    if (n === 0) { setErr("计划为空 (无待委托订单), 先预览"); return; }
    if (!confirm(`⚠️ 将按计划真实委托 ${n} 笔 (同花顺自动下单, 不可撤销)。确认执行?`)) return;
    act(async () => {
      const body = panel.action === "sync"
        ? { dry_run: false, pad_pct: padPct, min_order_value: minOrder }
        : { total_amount: amount, dry_run: false, pad_pct: padPct };
      const fn = panel.action === "buy" ? api.portfolioBuy
        : panel.action === "sell" ? api.portfolioSell : api.portfolioSync;
      const d = await fn(panel.pid, body);
      setResults((prev) => ({ ...prev, [pkey]: d }));
    });
  };

  const saveAdjust = () => {
    if (!panel) return;
    const items = (adjustRows[panel.pid] ?? [])
      .filter((r) => r.code.trim())
      .map((r) => ({ code: r.code.trim(), name: r.name,
                     weight: parseFloat(r.weight) || 0 }));
    if (!items.length) { setErr("至少保留 1 个标的"); return; }
    if (items.some((it) => it.weight <= 0)) { setErr("权重必须 > 0"); return; }
    act(async () => {
      await api.updatePortfolio(panel.pid, { items });
      setPanel(null);
    });
  };

  const handleDelete = (p: Portfolio) => {
    if (!confirm(`删除组合「${p.name}」? (只删配比记录, 不影响真实持仓)`)) return;
    act(() => api.deletePortfolio(p.id));
  };

  const inputCls = "bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 font-mono text-sm text-white placeholder-[#555]";

  if (err && !st) {
    return (
      <div className="p-8 space-y-2">
        <h1 className="text-2xl font-bold">🧺 组合交易</h1>
        <div className="text-sm text-[#f44336] bg-[#2a0808] border border-[#7f1d1d] rounded p-3 whitespace-pre-wrap">
          加载失败：{err}
        </div>
      </div>
    );
  }
  if (!st) return <div className="text-[#666] p-8">加载中...</div>;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <h1 className="text-2xl font-bold">🧺 组合交易 <span className="text-sm text-[#888] font-normal">人工ETF · 按权重买卖篮子 · 同步真实仓位</span></h1>
        <span className={`text-xs px-2 py-0.5 rounded border ${
          st.ths_ok === true
            ? "bg-[#0d2a1a] text-[#66bb6a] border-[#1b6a3a]"
            : st.ths_ok === false
              ? "bg-[#332207] text-[#ffb74d] border-[#8a5a12]"
              : "bg-[#1a1a1a] text-[#888] border-[#333]"
        }`}>
          {st.ths_ok === true ? "● 同花顺持仓已连接"
            : st.ths_ok === false ? `⚠ 同花顺不可达: ${st.ths_msg || "未运行"}` : "○ 持仓未请求"}
        </span>
        <label className="flex items-center gap-1.5 text-xs text-[#888] ml-auto">
          <input type="checkbox" checked={autoRefresh}
            onChange={(e) => setAutoRefresh(e.target.checked)}
            className="accent-[#ff6d00] w-3.5 h-3.5" />
          自动刷新 (6s)
        </label>
      </div>

      {err && (
        <div className="text-sm text-[#f44336] bg-[#2a0808] border border-[#7f1d1d] rounded p-3 whitespace-pre-wrap">
          {err}
        </div>
      )}

      {/* ============== 创建组合 ============== */}
      <section className="border border-[#2a2a2a] rounded-lg bg-[#141414] p-5 space-y-3">
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-semibold">创建组合</h2>
          <select value={mode} onChange={(e) => setMode(e.target.value as any)}
            className={`${inputCls} text-xs`}>
            <option value="weight">按比例 (%)</option>
            <option value="amount">按金额 (元)</option>
            <option value="qty">按股数 (自动折算权重)</option>
          </select>
          <span className="text-xs text-[#666]">
            {mode === "weight"
              ? "权重会自动归一到 100; 买入时按总金额 × 权重分配"
              : mode === "amount"
                ? "按每只金额占总金额的比例折算成权重"
                : "按每只股数 × 现价的市值占比折算成权重"}
          </span>
        </div>
        <div className="space-y-2">
          {rows.map((r, i) => {
            const v = valueOf(r);
            const sum = rows.reduce((a, x) => a + valueOf(x), 0);
            const w = mode === "weight" ? v : sum > 0 ? (v / sum) * 100 : 0;
            return (
              <div key={i} className="flex flex-wrap items-center gap-2">
                <input value={r.code} placeholder="代码 601899"
                  onChange={(e) => setRows((p) => p.map((x, j) =>
                    j === i ? { ...x, code: e.target.value } : x))}
                  onBlur={() => fillQuote(i)}
                  className={`${inputCls} w-28`} />
                <span className="text-xs text-[#aaa] w-28 truncate"
                  title={r.name}>{r.name || "—"}</span>
                <span className="text-xs font-mono text-[#4fc3f7] w-16">
                  {r.price > 0 ? r.price.toFixed(3) : "—"}
                </span>
                <input value={r.value} type="number"
                  placeholder={mode === "weight" ? "权重 %" : mode === "amount" ? "金额 (元)" : "股数"}
                  onChange={(e) => setRows((p) => p.map((x, j) =>
                    j === i ? { ...x, value: e.target.value } : x))}
                  className={`${inputCls} w-32`} />
                <span className="text-xs font-mono text-[#888] w-20">
                  ≈ {w.toFixed(1)}%
                </span>
                <button onClick={() => setRows((p) => p.filter((_, j) => j !== i))}
                  disabled={rows.length <= 1}
                  className="text-xs text-[#ef5350] border border-[#5a2a2a] rounded px-2 py-0.5 hover:bg-[#3a1515] disabled:opacity-30">
                  ×
                </button>
              </div>
            );
          })}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <input value={name} placeholder="组合名称, 如 红利低波"
            onChange={(e) => setName(e.target.value)} className={`${inputCls} w-48`} />
          <input value={note} placeholder="备注 (可选)"
            onChange={(e) => setNote(e.target.value)} className={`${inputCls} w-56`} />
          <button onClick={() => setRows((p) => [...p, { code: "", name: "", price: 0, value: "" }])}
            className="text-xs px-3 py-1 rounded border border-[#2a5a7a] text-[#4fc3f7] hover:bg-[#0d2a3a]">
            ＋ 添加标的
          </button>
          <button onClick={handleCreate} disabled={busy}
            className="px-5 py-1.5 bg-[#2a6a2a] text-white text-sm rounded hover:bg-[#2e7d32] disabled:opacity-50">
            ＋ 创建组合
          </button>
        </div>
        {createErr && (
          <div className="text-xs text-[#ef5350] bg-[#2a0808] border border-[#7f1d1d] rounded px-3 py-2">
            {createErr}
          </div>
        )}
      </section>

      {/* ============== 组合列表 ============== */}
      <section className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">组合列表 ({st.portfolios.length})</h2>
          <span className="text-xs text-[#666]">持仓/市值来自同花顺真实账户 · 组合外持仓在「同步仓位」里只展示不动</span>
        </div>
        {st.portfolios.length === 0 && (
          <div className="text-sm text-[#666] py-6 text-center border border-dashed border-[#333] rounded">
            还没有组合, 用上方表单创建第一个 (类似自建一只人工 ETF)
          </div>
        )}
        {st.portfolios.map((p) => {
          const isOpen = panel?.pid === p.id;
          return (
            <div key={p.id} className="border border-[#2a2a2a] rounded-lg bg-[#141414] p-4 space-y-3">
              {/* 标题行 */}
              <div className="flex flex-wrap items-center gap-3">
                <span className="font-semibold text-white">{p.name}</span>
                {p.note && <span className="text-xs text-[#666]">{p.note}</span>}
                <span className="text-xs font-mono text-[#888]">{p.items.length} 只标的</span>
                <span className="ml-auto text-sm font-mono text-[#ffd54f]">
                  市值 {p.market_value.toFixed(0)} 元
                </span>
                <span className="text-[10px] text-[#555]">更新 {p.updated_at.replace("T", " ")}</span>
              </div>

              {/* 标的表 */}
              <table className="w-full text-xs font-mono">
                <thead>
                  <tr className="text-left text-[#888] border-b border-[#2a2a2a]">
                    <th className="py-1 pr-2">代码</th>
                    <th className="py-1 pr-2">名称</th>
                    <th className="py-1 pr-2 text-right">目标权重</th>
                    <th className="py-1 pr-2 text-right">现价</th>
                    <th className="py-1 pr-2 text-right">持仓</th>
                    <th className="py-1 pr-2 text-right">可卖</th>
                    <th className="py-1 pr-2 text-right">市值</th>
                    <th className="py-1 pr-2 text-right">实际权重</th>
                    <th className="py-1 pr-2 text-right">偏差</th>
                  </tr>
                </thead>
                <tbody>
                  {p.items.map((it) => (
                    <tr key={it.code} className="border-b border-[#1f1f1f]">
                      <td className="py-1 pr-2 text-white">{it.code}</td>
                      <td className="py-1 pr-2 text-[#aaa]">{it.name || "—"}</td>
                      <td className="py-1 pr-2 text-right">{it.weight.toFixed(1)}%</td>
                      <td className="py-1 pr-2 text-right text-[#4fc3f7]">
                        {it.price > 0 ? it.price.toFixed(3) : "—"}
                      </td>
                      <td className="py-1 pr-2 text-right">
                        {it.position ?? <span className="text-[#555]">?</span>}
                      </td>
                      <td className="py-1 pr-2 text-right text-[#888]">
                        {it.available ?? "—"}
                      </td>
                      <td className="py-1 pr-2 text-right">{it.market_value.toFixed(0)}</td>
                      <td className="py-1 pr-2 text-right">{it.actual_weight.toFixed(1)}%</td>
                      <td className={`py-1 pr-2 text-right ${
                        Math.abs(it.drift) < 1 ? "text-[#666]"
                          : it.drift > 0 ? "text-[#ef5350]" : "text-[#26a69a]"}`}>
                        {it.drift > 0 ? "+" : ""}{it.drift.toFixed(1)}pp
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {/* 操作栏 */}
              <div className="flex flex-wrap items-center gap-2 pt-1">
                {(["buy", "sell", "sync", "adjust"] as Action[]).map((a) => (
                  <button key={a} onClick={() => openPanel(p, a)} disabled={busy}
                    className={`text-xs rounded px-3 py-1 border ${
                      isOpen && panel?.action === a
                        ? "bg-[#333] text-white border-[#555]"
                        : a === "sync"
                          ? "text-[#ffb74d] border-[#7a5a12] hover:bg-[#2a1a08]"
                          : "text-[#4fc3f7] border-[#2a5a7a] hover:bg-[#0d2a3a]"
                    }`}>
                    {ACTION_LABEL[a]}
                  </button>
                ))}
                <button onClick={() => handleDelete(p)} disabled={busy}
                  className="text-xs text-[#ef5350] border border-[#5a2a2a] rounded px-3 py-1 hover:bg-[#3a1515]">
                  🗑 删除
                </button>
                {st.ths_ok === false && (
                  <span className="text-[10px] text-[#ffb74d]">
                    同花顺不可达: 买入/卖出/同步需真实持仓, 请先启动同花顺客户端
                  </span>
                )}
              </div>

              {/* ============ 操作面板 ============ */}
              {isOpen && panel?.action !== "adjust" && (
                <div className="border border-[#2a5a7a] rounded bg-[#0d1520] p-4 space-y-3">
                  <div className="flex flex-wrap items-end gap-3">
                    {panel.action !== "sync" ? (
                      <label className="text-xs text-[#888] space-y-1">
                        <div>{panel.action === "buy" ? "买入总金额 (元)" : "卖出总金额 (元)"}</div>
                        <input type="number" value={amount || ""} step={1000} min={0}
                          onChange={(e) => setAmount(parseFloat(e.target.value) || 0)}
                          className={`${inputCls} w-40`} />
                      </label>
                    ) : (
                      <label className="text-xs text-[#888] space-y-1">
                        <div>差额门槛 (元, 零股除外)</div>
                        <input type="number" value={minOrder} step={500} min={0}
                          onChange={(e) => setMinOrder(parseFloat(e.target.value) || 0)}
                          className={`${inputCls} w-32`} />
                      </label>
                    )}
                    {panel.action === "sell" && p.market_value > 0 && (
                      <div className="flex items-center gap-1 pb-0.5">
                        {[0.25, 0.5, 1].map((f) => (
                          <button key={f} onClick={() => setAmount(Math.round(p.market_value * f))}
                            className="text-[10px] px-2 py-1 rounded border border-[#2a5a7a] text-[#4fc3f7] hover:bg-[#0d2a3a]">
                            {f === 1 ? "全部" : `${f * 100}%`}
                          </button>
                        ))}
                      </div>
                    )}
                    <label className="text-xs text-[#888] space-y-1">
                      <div>限价浮动 % (买加价/卖降价)</div>
                      <input type="number" value={padPct} step={0.1} min={0}
                        onChange={(e) => setPadPct(parseFloat(e.target.value) || 0)}
                        className={`${inputCls} w-24`} />
                    </label>
                    <button onClick={doPreview} disabled={busy}
                      className="px-4 py-1.5 bg-[#0d3a5a] text-white text-sm rounded hover:bg-[#0d4a7a] disabled:opacity-50">
                      {panel.action === "sync" ? "🔄 计算调仓计划" : "📐 预览分配"}
                    </button>
                    {preview && (
                      <button onClick={doExecute} disabled={busy}
                        className="px-4 py-1.5 bg-[#b71c1c] text-white text-sm rounded hover:bg-[#d32f2f] disabled:opacity-50">
                        ⚡ 执行{ACTION_LABEL[panel.action].slice(2)} (真实委托)
                      </button>
                    )}
                  </div>

                  {/* 预览表 */}
                  {preview && (
                    <div className="space-y-2">
                      {panel.action === "sync" && (
                        <div className="text-xs text-[#888]">
                          组合总市值 <span className="text-[#ffd54f] font-mono">
                            {preview.plan.total_value?.toFixed(0)}</span> 元 ·
                          目标 = 总市值 × 权重, 差额生成先卖后买订单
                        </div>
                      )}
                      <table className="w-full text-xs font-mono">
                        <thead>
                          <tr className="text-left text-[#888] border-b border-[#2a2a2a]">
                            <th className="py-1 pr-2">代码</th>
                            <th className="py-1 pr-2">名称</th>
                            {panel.action === "sync" ? (
                              <>
                                <th className="py-1 pr-2 text-right">目标市值</th>
                                <th className="py-1 pr-2 text-right">当前市值</th>
                                <th className="py-1 pr-2 text-right">差额</th>
                              </>
                            ) : (
                              <>
                                <th className="py-1 pr-2 text-right">权重</th>
                                <th className="py-1 pr-2 text-right">现价</th>
                                <th className="py-1 pr-2 text-right">委托股数</th>
                                <th className="py-1 pr-2 text-right">金额</th>
                              </>
                            )}
                            <th className="py-1 pr-2">方向</th>
                            <th className="py-1 pr-2">说明</th>
                          </tr>
                        </thead>
                        <tbody>
                          {preview.plan.rows.map((r) => (
                            <tr key={r.code} className="border-b border-[#1f1f1f]">
                              <td className="py-1 pr-2 text-white">{r.code}</td>
                              <td className="py-1 pr-2 text-[#aaa]">{r.name || "—"}</td>
                              {panel.action === "sync" ? (
                                <>
                                  <td className="py-1 pr-2 text-right">{(r.target_value ?? 0).toFixed(0)}</td>
                                  <td className="py-1 pr-2 text-right">{(r.cur_value ?? 0).toFixed(0)}</td>
                                  <td className={`py-1 pr-2 text-right ${
                                    (r.delta_value ?? 0) >= 0 ? "text-[#26a69a]" : "text-[#ef5350]"}`}>
                                    {(r.delta_value ?? 0) >= 0 ? "+" : ""}{(r.delta_value ?? 0).toFixed(0)}
                                  </td>
                                </>
                              ) : (
                                <>
                                  <td className="py-1 pr-2 text-right">{r.weight.toFixed(1)}%</td>
                                  <td className="py-1 pr-2 text-right text-[#4fc3f7]">
                                    {r.price > 0 ? r.price.toFixed(3) : "—"}
                                  </td>
                                  <td className="py-1 pr-2 text-right">{r.qty}</td>
                                  <td className="py-1 pr-2 text-right">{(r.amount ?? 0).toFixed(0)}</td>
                                </>
                              )}
                              <td className={`py-1 pr-2 ${
                                r.side === "buy" ? "text-[#26a69a]"
                                  : r.side === "sell" ? "text-[#ef5350]" : "text-[#555]"}`}>
                                {r.side === "buy" ? "买入" : r.side === "sell" ? "卖出" : "—"}
                              </td>
                              <td className="py-1 pr-2 text-[#777]">{r.note || ""}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      <div className="text-xs text-[#888] space-y-0.5">
                        {panel.action === "buy" && (
                          <div>预计占用 <span className="text-[#ffd54f]">{preview.plan.used?.toFixed(0)}</span> 元 ·
                            预留 {preview.plan.leftover?.toFixed(0)} 元 (整手取整/补一手后)</div>
                        )}
                        {panel.action === "sell" && (
                          <div>预计卖出 <span className="text-[#ffd54f]">{preview.plan.used?.toFixed(0)}</span> 元</div>
                        )}
                        {(preview.plan.external?.length ?? 0) > 0 && (
                          <div className="text-[#777]">
                            组合外持仓 (不动): {preview.plan.external!.map((e) =>
                              `${e.code}×${e.position}`).join("、")}
                          </div>
                        )}
                      </div>
                    </div>
                  )}

                  {/* 执行结果 */}
                  {result && (
                    <div className={`rounded p-3 text-xs space-y-1 ${
                      result.ok ? "bg-[#0d2a1a] border border-[#1b6a3a]"
                                : "bg-[#2a0808] border border-[#7f1d1d]"}`}>
                      <div className="font-semibold">
                        {result.ok ? `✅ ${result.summary || result.msg || "已执行"}`
                                   : `❌ ${result.msg || "执行失败"}`}
                      </div>
                      {(result.orders ?? []).length > 0 && (
                        <table className="w-full font-mono">
                          <tbody>
                            {result.orders.map((o, i) => (
                              <tr key={i} className="border-t border-[#1f3a2a]">
                                <td className="py-0.5 pr-3">{o.code} {o.name}</td>
                                <td className={`py-0.5 pr-3 ${
                                  o.side === "buy" ? "text-[#26a69a]" : "text-[#ef5350]"}`}>
                                  {o.side} {o.qty}股
                                </td>
                                <td className="py-0.5 pr-3">限价 {o.limit_price}</td>
                                <td className={`py-0.5 pr-3 ${
                                  o.ok === true ? "text-[#66bb6a]"
                                    : o.ok === false ? "text-[#ef5350]" : "text-[#888]"}`}>
                                  {o.status === "planned" ? "试算" : o.ok ? "已受理" : "失败"}
                                </td>
                                <td className="py-0.5 text-[#ffb74d]">{o.result_text || ""}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* ============ 调整面板 ============ */}
              {isOpen && panel?.action === "adjust" && (
                <div className="border border-[#2a5a7a] rounded bg-[#0d1520] p-4 space-y-3">
                  <div className="text-xs text-[#888]">
                    增减个股 / 改权重, 保存后只更新配比; 已移出的持仓在下一次「同步仓位」时按
                    ETF 剔除成分口径整笔清仓。
                  </div>
                  {(adjustRows[p.id] ?? []).map((r, i) => (
                    <div key={i} className="flex flex-wrap items-center gap-2">
                      <input value={r.code} placeholder="代码"
                        onChange={(e) => setAdjustRows((prev) => ({
                          ...prev, [p.id]: prev[p.id].map((x, j) =>
                            j === i ? { ...x, code: e.target.value } : x),
                        }))}
                        onBlur={() => fillQuote(i, true, i)}
                        className={`${inputCls} w-28`} />
                      <span className="text-xs text-[#aaa] w-28 truncate">{r.name || "—"}</span>
                      <input value={r.weight} type="number" placeholder="权重 %"
                        onChange={(e) => setAdjustRows((prev) => ({
                          ...prev, [p.id]: prev[p.id].map((x, j) =>
                            j === i ? { ...x, weight: e.target.value } : x),
                        }))}
                        className={`${inputCls} w-28`} />
                      <span className="text-xs font-mono text-[#888]">
                        合计 {(adjustRows[p.id] ?? []).reduce(
                          (a, x) => a + (parseFloat(x.weight) || 0), 0).toFixed(1)}%
                        <span className="text-[#555]"> (自动归一)</span>
                      </span>
                      <button onClick={() => setAdjustRows((prev) => ({
                        ...prev, [p.id]: prev[p.id].filter((_, j) => j !== i),
                      }))}
                        disabled={(adjustRows[p.id] ?? []).length <= 1}
                        className="text-xs text-[#ef5350] border border-[#5a2a2a] rounded px-2 py-0.5 hover:bg-[#3a1515] disabled:opacity-30">
                        ×
                      </button>
                    </div>
                  ))}
                  <div className="flex items-center gap-3">
                    <button onClick={() => setAdjustRows((prev) => ({
                      ...prev, [p.id]: [...(prev[p.id] ?? []),
                        { code: "", name: "", weight: "" }],
                    }))}
                      className="text-xs px-3 py-1 rounded border border-[#2a5a7a] text-[#4fc3f7] hover:bg-[#0d2a3a]">
                      ＋ 添加标的
                    </button>
                    <button onClick={saveAdjust} disabled={busy}
                      className="px-4 py-1.5 bg-[#2a6a2a] text-white text-sm rounded hover:bg-[#2e7d32] disabled:opacity-50">
                      ✔ 保存调整
                    </button>
                    <button onClick={() => setPanel(null)}
                      className="text-xs text-[#888] hover:text-white">取消</button>
                  </div>
                </div>
              )}

              {/* 历史 */}
              {p.history.length > 0 && (
                <details className="text-xs">
                  <summary className="cursor-pointer text-[#888] hover:text-white select-none">
                    📜 执行历史 ({p.history.length})
                  </summary>
                  <div className="mt-2 space-y-1 font-mono">
                    {p.history.slice().reverse().map((h, i) => (
                      <div key={i} className="flex flex-wrap gap-x-3 border-b border-[#1f1f1f] pb-1">
                        <span className="text-[#555]">{h.ts.replace("T", " ")}</span>
                        <span className="text-[#4fc3f7]">{h.action}</span>
                        {h.dry_run && <span className="text-[#777]">试算</span>}
                        {h.amount != null && h.amount > 0 &&
                          <span className="text-[#ffd54f]">{h.amount.toFixed(0)} 元</span>}
                        <span className={h.fail_count ? "text-[#ffb74d]" : "text-[#66bb6a]"}>
                          {h.summary}
                        </span>
                        {h.orders?.length > 0 && (
                          <span className="text-[#555]">
                            {h.orders.map((o: any) =>
                              `${o.side === "buy" ? "买" : "卖"}${o.code}×${o.qty}${o.result_text ? `(⚠${o.result_text})` : ""}`).join(" ")}
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </div>
          );
        })}
      </section>

      <div className="text-[10px] text-[#555] leading-4 space-y-0.5">
        <div>· 买入: 总金额 × 权重 → 整手取整 → 剩余预算按权重补一手; 卖出: 总金额 × 权重分摊, 受可卖数量封顶 (T+1)。</div>
        <div>· 同步仓位: 真实持仓市值按新配比重新分配 (先卖后买); 移出的成分整笔清仓; 差额小于门槛的跳过。</div>
        <div>· 执行通过同花顺 GUI 自动下单 (限价 = 现价×(1±浮动%) 按品种 tick 取整), 提交后请留意同花顺委托回报。</div>
      </div>
    </div>
  );
}
