"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

// ---- 类型 (对应 GET /api/picker 返回) ----
type Position = {
  id: number;
  strategy_id: string;
  code: string;
  name: string;
  qty: number;
  buy_price: number;
  buy_ts: string;
  buy_order_id: string;
  buy_reason: string;
  status: "holding" | "selling" | "sold";
  sell_price: number | null;
  sell_ts: string | null;
  sell_order_id: string | null;
  sell_reason: string | null;
  last_price?: number;
  pnl_pct?: number | null;
};

type Rule = { type: string; [k: string]: any };
type RuleTypeMeta = { label: string; params: Record<string, number>; desc?: string };
type RuleTypes = { buy: Record<string, RuleTypeMeta>; sell: Record<string, RuleTypeMeta> };

type StrategyDef = {
  id: string;
  title: string;
  desc: string;
  source: "preset" | "user" | "code";
  buy_rules?: Rule[];
  sell_rules?: Rule[];
  params?: Record<string, any>;
};

type Group = {
  strategy_id: string;
  title: string;
  picker: string;
  picker_title?: string;
  universe: string[];
  params: Record<string, any>;
  per_qty: number;
  cash_per_symbol: number;
  max_positions: number;
  buy_scan_every: number;
  t1_protect: boolean | number;
  enabled: boolean | number;
  created_at: string;
  updated_at: string;
  running?: boolean;
  rounds?: number;
  last_buy_scan?: string;
  last_sell_scan?: string;
  last_error?: string;
  holdings: Position[];
  selling: Position[];
  pending_buys: any[];
};

type PickerEvent = {
  id: number;
  ts: string;
  strategy_id: string;
  code: string;
  side: string;
  qty: number;
  price: number;
  order_id: string;
  dry_run: number;
  status: string;
  detail: string;
};

type PickerStatus = {
  running: boolean;
  live: boolean | null;
  poll_seconds: number | null;
  groups: Group[];
  events: PickerEvent[];
  logs: string[];
  portfolios?: Record<string, { cash: number; positions: Record<string, any> }>;
};

type BacktestResult = {
  strategy_id: string;
  days: number;
  metrics: {
    initial_cash: number;
    final_value: number;
    total_return_pct: number;
    max_drawdown_pct: number;
    trades: number;
    win_rate_pct: number | null;
    avg_pnl_pct: number | null;
  };
  equity: { date: string; value: number }[];
  trades: {
    code: string; qty: number; buy_date: string; buy_price: number;
    sell_date: string; sell_price: number; pnl: number; pnl_pct: number;
    sell_reason: string;
  }[];
  open_positions: { code: string; qty: number; buy_date: string; buy_price: number; reason: string }[];
};

const SOURCE_TAG: Record<StrategyDef["source"], { label: string; cls: string }> = {
  preset: { label: "预置", cls: "bg-[#26313a] text-[#4fc3f7] border-[#1b5a7a]" },
  user: { label: "自定义", cls: "bg-[#0d2a1a] text-[#66bb6a] border-[#1b6a3a]" },
  code: { label: "代码插件", cls: "bg-[#2a2a2a] text-[#999] border-[#3a3a3a]" },
};

const fmtPct = (v: number | null | undefined) =>
  v === null || v === undefined ? "-" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
const pnlCls = (v: number | null | undefined) =>
  v === null || v === undefined
    ? "text-[#888]"
    : v >= 0
      ? "text-[#ef5350]"
      : "text-[#26a69a]";

const SIDE_STYLE: Record<string, string> = {
  buy: "bg-[#332207] text-[#ef5350] border-[#8a5a12]",
  sell: "bg-[#0d2a3a] text-[#26a69a] border-[#1b5a7a]",
};

/** 规则编辑器: 条件类型下拉 + 数值参数 (全部 text-sm, 修复弹层大字) */
function RuleBuilder({ rules, onChange, types, kind }: {
  rules: Rule[];
  onChange: (r: Rule[]) => void;
  types: Record<string, RuleTypeMeta>;
  kind: "buy" | "sell";
}) {
  const meta = (t: string) => types[t];
  const setAt = (i: number, r: Rule) => onChange(rules.map((x, j) => (j === i ? r : x)));
  const changeType = (i: number, t: string) => {
    const m = meta(t);
    setAt(i, { type: t, ...(m?.params ?? {}) });
  };
  const add = () => {
    const t = Object.keys(types)[kind === "buy" ? 0 : 0];
    onChange([...rules, { type: t, ...(meta(t)?.params ?? {}) }]);
  };
  return (
    <div className="space-y-1.5">
      {rules.map((r, i) => {
        const m = meta(r.type);
        return (
          <div key={i} className="flex items-center gap-1.5">
            <select
              value={r.type}
              onChange={(e) => changeType(i, e.target.value)}
              title={m?.desc}
              className="bg-[#111] border border-[#333] rounded px-1.5 py-1 text-sm flex-1 min-w-0"
            >
              {Object.entries(types).map(([t, mt]) => (
                <option key={t} value={t} className="text-sm" title={mt.desc}>
                  {mt.label}
                </option>
              ))}
            </select>
            {Object.entries(m?.params ?? {}).map(([p, def]) => (
              <label key={p} className="flex items-center gap-1 text-xs text-[#888] shrink-0">
                {p}
                <input
                  type="number"
                  step="any"
                  value={r[p] ?? def}
                  onChange={(e) => setAt(i, { ...r, [p]: Number(e.target.value) })}
                  className="bg-[#111] border border-[#333] rounded px-1 py-1 text-sm w-16"
                />
              </label>
            ))}
            <button
              onClick={() => onChange(rules.filter((_, j) => j !== i))}
              className="px-1.5 py-0.5 rounded bg-[#3a1515] text-[#ef5350] text-xs shrink-0"
            >
              ✕
            </button>
          </div>
        );
      })}
      <button
        onClick={add}
        className="px-2 py-0.5 rounded bg-[#26313a] hover:bg-[#33424e] text-[#4fc3f7] text-xs"
      >
        + 添加{kind === "buy" ? "买入" : "卖出"}条件
      </button>
    </div>
  );
}

/** 净值曲线 (轻量 SVG 折线) */
function EquityCurve({ equity }: { equity: { date: string; value: number }[] }) {
  if (equity.length < 2) return null;
  const vs = equity.map((e) => e.value);
  const min = Math.min(...vs);
  const max = Math.max(...vs);
  const span = max - min || 1;
  const w = 560;
  const h = 80;
  const pts = vs
    .map((v, i) => `${((i / (vs.length - 1)) * w).toFixed(1)},${(h - ((v - min) / span) * (h - 6) - 3).toFixed(1)}`)
    .join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-20" preserveAspectRatio="none">
      <polyline points={pts} fill="none" stroke="#4fc3f7" strokeWidth="1.5" />
    </svg>
  );
}

export default function PickerPage() {
  const [st, setSt] = useState<PickerStatus | null>(null);
  const [strategies, setStrategies] = useState<StrategyDef[]>([]);
  const [ruleTypes, setRuleTypes] = useState<RuleTypes>({ buy: {}, sell: {} });
  const [err, setErr] = useState("");
  const [formErr, setFormErr] = useState("");
  const [busy, setBusy] = useState(false);

  // 引擎启动参数
  const [cash, setCash] = useState(100000);
  const [poll, setPoll] = useState(5);
  const [live, setLive] = useState(false);

  // 策略库: 编辑器 + 回测面板
  const [editSt, setEditSt] = useState<{
    id: string; title: string; desc: string;
    buy_rules: Rule[]; sell_rules: Rule[];
  } | null>(null);
  const [btFor, setBtFor] = useState<StrategyDef | null>(null);
  const [btForm, setBtForm] = useState({ universe: "", days: 250, cash: 100000, maxPos: 3 });
  const [btResult, setBtResult] = useState<BacktestResult | null>(null);
  const [btBusy, setBtBusy] = useState(false);

  // 新建策略组表单
  const [gid, setGid] = useState("");
  const [title, setTitle] = useState("");
  const [pickerId, setPickerId] = useState("");
  const [universe, setUniverse] = useState("");
  const [paramsText, setParamsText] = useState("{}");
  const [perQty, setPerQty] = useState(0);
  const [cashPer, setCashPer] = useState(10000);
  const [maxPos, setMaxPos] = useState(3);
  const [scanEvery, setScanEvery] = useState(60);
  const [t1, setT1] = useState(true);

  const logRef = useRef<HTMLPreElement>(null);

  const refresh = useCallback(async () => {
    try {
      const d = await api.pickerStatus();
      setSt(d);
      setErr("");
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    }
  }, []);

  const loadStrategies = useCallback(async () => {
    try {
      const [list, rt] = await Promise.all([
        api.pickerStrategies(),
        api.pickerRuleTypes().catch(() => ({ buy: {}, sell: {} })),
      ]);
      setStrategies(list);
      setRuleTypes(rt);
      setPickerId((prev) => prev || list.find((s: StrategyDef) => s.source !== "code")?.id || "");
    } catch { /* 引擎未启动时静默 */ }
  }, []);

  useEffect(() => {
    refresh();
    loadStrategies();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
    act(() => api.startPickerEngine({ live, cash, poll_seconds: poll }));
  };
  const handleStop = () => act(() => api.stopPickerEngine());

  // ---- 策略库 ----
  const handlePickerChange = (id: string) => {
    setPickerId(id);
    const s = strategies.find((x) => x.id === id);
    setParamsText(s && s.source === "code"
      ? JSON.stringify(Object.fromEntries(Object.entries(s.params ?? {}).map(
        ([k, v]: [string, any]) => [k, v?.default ?? v])), null, 2)
      : "{}");
  };

  const openNewStrategy = () => setEditSt({
    id: "", title: "", desc: "",
    buy_rules: [{ type: Object.keys(ruleTypes.buy)[0] ?? "rsi_below", ...(ruleTypes.buy[Object.keys(ruleTypes.buy)[0]]?.params ?? {}) }],
    sell_rules: [{ type: Object.keys(ruleTypes.sell)[0] ?? "take_profit", ...(ruleTypes.sell[Object.keys(ruleTypes.sell)[0]]?.params ?? {}) }],
  });

  const handleSaveStrategy = () => {
    if (!editSt) return;
    const body = { id: editSt.id, title: editSt.title, desc: editSt.desc,
      buy_rules: editSt.buy_rules, sell_rules: editSt.sell_rules };
    act(
      () => (editSt.id ? api.updatePickerStrategy(editSt.id, body)
        : api.addPickerStrategy(body)),
      () => { setEditSt(null); loadStrategies(); },
    );
  };

  const handleDeleteStrategy = (s: StrategyDef) => {
    if (!confirm(`删除策略「${s.title}」? 被策略组引用时会被拒绝。`)) return;
    act(() => api.deletePickerStrategy(s.id), loadStrategies);
  };

  const openBacktest = (s: StrategyDef) => {
    setBtFor(s);
    setBtResult(null);
    setBtForm({ universe: "601899 600519 000858 601318 300750 002594 600036",
      days: 250, cash: 100000, maxPos: 3 });
  };

  const runBacktest = () => {
    if (!btFor) return;
    const uni = btForm.universe.trim().split(/[\s,，]+/).filter(Boolean);
    if (!uni.length) { setFormErr("回测股票池不能为空"); return; }
    setBtBusy(true);
    setFormErr("");
    api.backtestPickerStrategy(btFor.id, {
      universe: uni, days: btForm.days, cash: btForm.cash,
      max_positions: btForm.maxPos, t1_protect: true,
    }).then((r: any) => setBtResult(r))
      .catch((e: any) => setFormErr(e?.message ?? String(e)))
      .finally(() => setBtBusy(false));
  };

  // ---- 新建策略组 ----
  const handleAdd = () => {
    let params: any = {};
    try {
      params = JSON.parse(paramsText || "{}");
    } catch {
      setFormErr("参数不是合法 JSON");
      return;
    }
    const uni = universe.trim().split(/[\s,，]+/).filter(Boolean);
    if (!uni.length) {
      setFormErr("请填写股票池 (空格/逗号分隔, 如 601899 600519 000858)");
      return;
    }
    act(
      () =>
        api.addPickerGroup({
          strategy_id: gid.trim(),
          title: title.trim(),
          picker: pickerId,
          universe: uni,
          params,
          per_qty: perQty,
          cash_per_symbol: cashPer,
          max_positions: maxPos,
          buy_scan_every: scanEvery,
          t1_protect: t1,
          enabled: true,
        }),
      () => {
        setGid("");
        setTitle("");
        setUniverse("");
      },
    );
  };

  const handleDelete = (id: string) => {
    if (!confirm(`删除策略组 ${id}? 买入组持仓与流水会保留 (审计用)。`)) return;
    act(() => api.deletePickerGroup(id));
  };

  const handleToggle = (g: Group) =>
    act(() => api.updatePickerGroup(g.strategy_id, { enabled: !g.enabled }));
  const handleRunOnce = (id: string) => act(() => api.runPickerOnce(id));

  const running = st?.running ?? false;
  const isLive = st?.live === true;
  const libStrategies = strategies.filter((s) => s.source !== "code");
  const codeStrategies = strategies.filter((s) => s.source === "code");
  const rulesSummary = (rules?: Rule[], kind: "buy" | "sell" = "buy") =>
    (rules ?? []).map((r) => ruleTypes[kind]?.[r.type]?.label ?? r.type).join(" + ") || "-";

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">🎯 选股自动交易</h1>
        <span
          className={`text-sm px-3 py-1 rounded-full border ${
            running
              ? isLive
                ? "bg-[#3a0d0d] text-[#ef5350] border-[#8a2020] animate-pulse"
                : "bg-[#0d2a1a] text-[#66bb6a] border-[#1b6a3a]"
              : "bg-[#222] text-[#888] border-[#3a3a3a]"
          }`}
        >
          {running ? (isLive ? "● 实盘扫描中" : "● 模拟扫描中") : "○ 引擎已停止"}
        </span>
      </div>

      {err && (
        <div className="text-[#ef5350] text-sm bg-[#3a0d0d] border border-[#8a2020] rounded p-3">
          {err}
        </div>
      )}

      {/* ---- 引擎控制 ---- */}
      <div className="bg-[#1a1a1a] border border-[#2a2a2a] rounded-lg p-4 space-y-3">
        <div className="text-sm text-[#aaa]">引擎控制 (启动后按组并行扫描)</div>
        <div className="flex flex-wrap items-center gap-4 text-sm">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
            实盘 (同花顺真实下单)
          </label>
          <label className="flex items-center gap-2">
            每组资金
            <input
              type="number"
              value={cash}
              onChange={(e) => setCash(Number(e.target.value))}
              className="bg-[#111] border border-[#333] rounded px-2 py-1 w-28"
            />
          </label>
          <label className="flex items-center gap-2">
            扫描间隔(秒)
            <input
              type="number"
              value={poll}
              onChange={(e) => setPoll(Number(e.target.value))}
              className="bg-[#111] border border-[#333] rounded px-2 py-1 w-16"
            />
          </label>
          {running ? (
            <button
              onClick={handleStop}
              disabled={busy}
              className="px-4 py-1.5 rounded bg-[#8a2020] hover:bg-[#a02525] text-white disabled:opacity-50"
            >
              停止引擎
            </button>
          ) : (
            <button
              onClick={handleStart}
              disabled={busy}
              className="px-4 py-1.5 rounded bg-[#1b6a3a] hover:bg-[#22804a] text-white disabled:opacity-50"
            >
              启动引擎
            </button>
          )}
        </div>
      </div>

      {/* ---- 策略库 ---- */}
      <div className="bg-[#1a1a1a] border border-[#2a2a2a] rounded-lg p-4 space-y-3">
        <div className="flex items-center justify-between">
          <div className="text-sm text-[#aaa]">
            策略库 — 每个策略都是一套可回测的选股/卖出规则, 供下方策略组引用
          </div>
          {!editSt && (
            <button
              onClick={openNewStrategy}
              disabled={!Object.keys(ruleTypes.buy).length}
              className="px-3 py-1 rounded bg-[#1b5a7a] hover:bg-[#227090] text-white text-sm disabled:opacity-50"
            >
              + 新建策略
            </button>
          )}
        </div>

        {editSt && (
          <div className="border border-[#333] rounded-lg p-3 space-y-3 bg-[#151515]">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
              <label className="flex flex-col gap-1">
                策略名称
                <input
                  value={editSt.title}
                  onChange={(e) => setEditSt({ ...editSt, title: e.target.value })}
                  placeholder="超跌反弹"
                  className="bg-[#111] border border-[#333] rounded px-2 py-1"
                />
              </label>
              <label className="flex flex-col gap-1">
                说明
                <input
                  value={editSt.desc}
                  onChange={(e) => setEditSt({ ...editSt, desc: e.target.value })}
                  placeholder="RSI 超卖 + 放量承接买入, 反弹兑现卖出"
                  className="bg-[#111] border border-[#333] rounded px-2 py-1"
                />
              </label>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
              <div>
                <div className="text-[#aaa] mb-1.5">买入条件 (全部命中才选入)</div>
                <RuleBuilder
                  rules={editSt.buy_rules}
                  onChange={(r) => setEditSt({ ...editSt, buy_rules: r })}
                  types={ruleTypes.buy}
                  kind="buy"
                />
              </div>
              <div>
                <div className="text-[#aaa] mb-1.5">卖出条件 (任一命中即卖出)</div>
                <RuleBuilder
                  rules={editSt.sell_rules}
                  onChange={(r) => setEditSt({ ...editSt, sell_rules: r })}
                  types={ruleTypes.sell}
                  kind="sell"
                />
              </div>
            </div>
            <div className="flex gap-2">
              <button
                onClick={handleSaveStrategy}
                disabled={busy}
                className="px-4 py-1.5 rounded bg-[#1b6a3a] hover:bg-[#22804a] text-white text-sm disabled:opacity-50"
              >
                保存策略
              </button>
              <button
                onClick={() => setEditSt(null)}
                className="px-4 py-1.5 rounded bg-[#26313a] hover:bg-[#33424e] text-[#aaa] text-sm"
              >
                取消
              </button>
            </div>
          </div>
        )}

        <div className="space-y-2">
          {libStrategies.map((s) => (
            <div
              key={s.id}
              className="border border-[#2a2a2a] rounded-lg p-3 flex flex-wrap items-center gap-3 text-sm bg-[#151515]"
            >
              <span className="font-bold">{s.title}</span>
              <span className={`text-xs px-2 py-0.5 rounded border ${SOURCE_TAG[s.source].cls}`}>
                {SOURCE_TAG[s.source].label}
              </span>
              <span className="text-xs text-[#666] font-mono">{s.id}</span>
              <span className="text-xs text-[#888] truncate max-w-[380px]">{s.desc}</span>
              <div className="ml-auto flex gap-2">
                {s.source !== "code" && (
                  <>
                    <button
                      onClick={() => openBacktest(s)}
                      className="px-3 py-1 rounded bg-[#26313a] hover:bg-[#33424e] text-[#4fc3f7]"
                    >
                      📈 回测
                    </button>
                    <button
                      onClick={() => setEditSt({
                        id: s.id, title: s.title, desc: s.desc,
                        buy_rules: s.buy_rules ?? [],
                        sell_rules: s.sell_rules ?? [],
                      })}
                      className="px-3 py-1 rounded bg-[#26313a] hover:bg-[#33424e] text-[#aaa]"
                    >
                      编辑
                    </button>
                  </>
                )}
                <button
                  onClick={() => handleDeleteStrategy(s)}
                  disabled={busy}
                  className="px-3 py-1 rounded bg-[#3a1515] hover:bg-[#4d1c1c] text-[#ef5350] disabled:opacity-50"
                >
                  删除
                </button>
              </div>
              <div className="w-full text-xs text-[#666] grid grid-cols-1 md:grid-cols-2 gap-1">
                <span>买入: {rulesSummary(s.buy_rules, "buy")}</span>
                <span>卖出: {rulesSummary(s.sell_rules, "sell")}</span>
              </div>
            </div>
          ))}
          {!libStrategies.length && (
            <div className="text-sm text-[#666]">策略库为空, 点击「+ 新建策略」创建</div>
          )}
        </div>

        {/* 回测面板 */}
        {btFor && (
          <div className="border border-[#1b5a7a] rounded-lg p-3 space-y-3 bg-[#101c24]">
            <div className="flex items-center gap-2">
              <span className="text-sm font-bold text-[#4fc3f7]">
                📈 回测: {btFor.title}
              </span>
              <button
                onClick={() => { setBtFor(null); setBtResult(null); }}
                className="ml-auto text-xs text-[#888] hover:text-white"
              >
                关闭
              </button>
            </div>
            <div className="flex flex-wrap items-end gap-3 text-sm">
              <label className="flex flex-col gap-1 flex-1 min-w-[260px]">
                股票池
                <input
                  value={btForm.universe}
                  onChange={(e) => setBtForm({ ...btForm, universe: e.target.value })}
                  className="bg-[#111] border border-[#333] rounded px-2 py-1"
                />
              </label>
              <label className="flex items-center gap-1">
                天数
                <input
                  type="number"
                  value={btForm.days}
                  onChange={(e) => setBtForm({ ...btForm, days: Number(e.target.value) })}
                  className="bg-[#111] border border-[#333] rounded px-1 py-1 w-20"
                />
              </label>
              <label className="flex items-center gap-1">
                资金
                <input
                  type="number"
                  value={btForm.cash}
                  onChange={(e) => setBtForm({ ...btForm, cash: Number(e.target.value) })}
                  className="bg-[#111] border border-[#333] rounded px-1 py-1 w-24"
                />
              </label>
              <label className="flex items-center gap-1">
                最大持仓
                <input
                  type="number"
                  value={btForm.maxPos}
                  onChange={(e) => setBtForm({ ...btForm, maxPos: Number(e.target.value) })}
                  className="bg-[#111] border border-[#333] rounded px-1 py-1 w-14"
                />
              </label>
              <button
                onClick={runBacktest}
                disabled={btBusy}
                className="px-4 py-1.5 rounded bg-[#1b5a7a] hover:bg-[#227090] text-white disabled:opacity-50"
              >
                {btBusy ? "回测中..." : "运行回测"}
              </button>
            </div>

            {btResult && (
              <div className="space-y-3">
                <div className="flex flex-wrap gap-4 text-sm">
                  <span>总收益
                    <b className={pnlCls(btResult.metrics.total_return_pct)}>
                      {" "}{fmtPct(btResult.metrics.total_return_pct)}
                    </b>
                  </span>
                  <span>最大回撤
                    <b className="text-[#ef5350]"> {btResult.metrics.max_drawdown_pct}%</b>
                  </span>
                  <span>交易 <b>{btResult.metrics.trades}</b> 笔</span>
                  <span>胜率
                    <b> {btResult.metrics.win_rate_pct ?? "-"}{btResult.metrics.win_rate_pct != null ? "%" : ""}</b>
                  </span>
                  <span>期末净值
                    <b> {btResult.metrics.final_value.toLocaleString()}</b>
                  </span>
                </div>
                <EquityCurve equity={btResult.equity} />
                {btResult.trades.length > 0 && (
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-left text-[#888] border-b border-[#2a2a2a]">
                        <th className="py-1">代码</th>
                        <th>买入日</th>
                        <th>买入价</th>
                        <th>卖出日</th>
                        <th>卖出价</th>
                        <th className="text-right">盈亏%</th>
                        <th>卖出原因</th>
                      </tr>
                    </thead>
                    <tbody>
                      {btResult.trades.slice(0, 30).map((t, i) => (
                        <tr key={i} className="border-b border-[#1c1c1c]">
                          <td className="py-1 font-mono">{t.code}</td>
                          <td>{t.buy_date}</td>
                          <td>{t.buy_price.toFixed(2)}</td>
                          <td>{t.sell_date}</td>
                          <td>{t.sell_price.toFixed(2)}</td>
                          <td className={`text-right ${pnlCls(t.pnl_pct)}`}>{fmtPct(t.pnl_pct)}</td>
                          <td className="text-[#888]">{t.sell_reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                {btResult.open_positions.length > 0 && (
                  <div className="text-xs text-[#888]">
                    未平仓: {btResult.open_positions.map(
                      (p) => `${p.code} ${p.qty}@${p.buy_price.toFixed(2)}`).join(", ")}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>

      {/* ---- 新建策略组 ---- */}
      <div className="bg-[#1a1a1a] border border-[#2a2a2a] rounded-lg p-4 space-y-3">
        <div className="text-sm text-[#aaa]">新建策略组 (买入的股票按策略 ID 分组入库)</div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <label className="flex flex-col gap-1">
            策略 ID (留空自动生成)
            <input
              value={gid}
              onChange={(e) => setGid(e.target.value)}
              placeholder="sp_momentum"
              className="bg-[#111] border border-[#333] rounded px-2 py-1"
            />
          </label>
          <label className="flex flex-col gap-1">
            名称
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="超跌反弹组"
              className="bg-[#111] border border-[#333] rounded px-2 py-1"
            />
          </label>
          <label className="flex flex-col gap-1">
            选股策略
            <select
              value={pickerId}
              onChange={(e) => handlePickerChange(e.target.value)}
              className="bg-[#111] border border-[#333] rounded px-2 py-1 text-sm"
            >
              <optgroup label="策略库 (可回测)">
                {libStrategies.map((s) => (
                  <option key={s.id} value={s.id} className="text-sm">
                    {s.title} ({s.id})
                  </option>
                ))}
              </optgroup>
              <optgroup label="代码插件 (开发者)">
                {codeStrategies.map((s) => (
                  <option key={s.id} value={s.id} className="text-sm">
                    {s.title} ({s.id})
                  </option>
                ))}
              </optgroup>
            </select>
          </label>
          <label className="flex flex-col gap-1">
            每只股数 (0=自动整手)
            <input
              type="number"
              value={perQty}
              onChange={(e) => setPerQty(Number(e.target.value))}
              className="bg-[#111] border border-[#333] rounded px-2 py-1"
            />
          </label>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
          <label className="flex flex-col gap-1">
            股票池 (空格/逗号分隔)
            <textarea
              value={universe}
              onChange={(e) => setUniverse(e.target.value)}
              rows={3}
              placeholder="601899 600519 000858 601318"
              className="bg-[#111] border border-[#333] rounded px-2 py-1 font-mono"
            />
          </label>
          <label className="flex flex-col gap-1">
            高级参数 (JSON, 代码插件参数 / kline_limit 等)
            <textarea
              value={paramsText}
              onChange={(e) => setParamsText(e.target.value)}
              rows={3}
              className="bg-[#111] border border-[#333] rounded px-2 py-1 font-mono"
            />
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-4 text-sm">
          <label className="flex items-center gap-2">
            单票预算
            <input
              type="number"
              value={cashPer}
              onChange={(e) => setCashPer(Number(e.target.value))}
              className="bg-[#111] border border-[#333] rounded px-2 py-1 w-24"
            />
          </label>
          <label className="flex items-center gap-2">
            最大持仓
            <input
              type="number"
              value={maxPos}
              onChange={(e) => setMaxPos(Number(e.target.value))}
              className="bg-[#111] border border-[#333] rounded px-2 py-1 w-16"
            />
            只 (0=不限)
          </label>
          <label className="flex items-center gap-2">
            每
            <input
              type="number"
              value={scanEvery}
              onChange={(e) => setScanEvery(Number(e.target.value))}
              className="bg-[#111] border border-[#333] rounded px-2 py-1 w-16"
            />
            轮选股一次
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={t1} onChange={(e) => setT1(e.target.checked)} />
            T+1 保护 (当日买入当日不卖)
          </label>
          <button
            onClick={handleAdd}
            disabled={busy || !pickerId}
            className="px-4 py-1.5 rounded bg-[#1b5a7a] hover:bg-[#227090] text-white disabled:opacity-50"
          >
            + 新建策略组
          </button>
        </div>
        {formErr && <div className="text-[#ef5350] text-sm">{formErr}</div>}
      </div>

      {/* ---- 策略组列表 ---- */}
      {(st?.groups ?? []).map((g) => (
        <div
          key={g.strategy_id}
          className="bg-[#1a1a1a] border border-[#2a2a2a] rounded-lg p-4 space-y-3"
        >
          <div className="flex flex-wrap items-center gap-3">
            <span className="font-bold">{g.title}</span>
            <span className="text-xs font-mono px-2 py-0.5 rounded bg-[#26313a] text-[#4fc3f7]">
              {g.strategy_id}
            </span>
            <span className="text-xs text-[#aaa]">
              策略: {g.picker_title || g.picker}
            </span>
            <span
              className={`text-xs px-2 py-0.5 rounded border ${
                g.enabled
                  ? "bg-[#0d2a1a] text-[#66bb6a] border-[#1b6a3a]"
                  : "bg-[#222] text-[#888] border-[#3a3a3a]"
              }`}
            >
              {g.enabled ? "启用" : "停用"}
            </span>
            {g.running && (
              <span className="text-xs text-[#4fc3f7] animate-pulse">● 扫描中</span>
            )}
            <span className="text-xs text-[#666]">
              池 {g.universe.length} 只 · 持仓 {g.holdings.length}
              {g.max_positions ? `/${g.max_positions}` : ""} · 每轮间隔{" "}
              {g.buy_scan_every} · T+1 {g.t1_protect ? "开" : "关"}
            </span>
            <div className="ml-auto flex gap-2 text-sm">
              <button
                onClick={() => handleRunOnce(g.strategy_id)}
                disabled={busy}
                className="px-3 py-1 rounded bg-[#26313a] hover:bg-[#33424e] text-[#4fc3f7] disabled:opacity-50"
              >
                ▶ 跑一轮
              </button>
              <button
                onClick={() => handleToggle(g)}
                disabled={busy}
                className="px-3 py-1 rounded bg-[#26313a] hover:bg-[#33424e] text-[#aaa] disabled:opacity-50"
              >
                {g.enabled ? "停用" : "启用"}
              </button>
              <button
                onClick={() => handleDelete(g.strategy_id)}
                disabled={busy}
                className="px-3 py-1 rounded bg-[#3a1515] hover:bg-[#4d1c1c] text-[#ef5350] disabled:opacity-50"
              >
                删除
              </button>
            </div>
          </div>

          {g.last_error && (
            <div className="text-[#ef5350] text-xs">⚠ {g.last_error}</div>
          )}
          <div className="text-xs text-[#666]">
            上次选股 {g.last_buy_scan || "-"} · 上次卖出扫描 {g.last_sell_scan || "-"}
          </div>

          {/* 买入组持仓 */}
          {g.holdings.length + g.selling.length > 0 ? (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[#888] border-b border-[#2a2a2a]">
                  <th className="py-1.5">代码</th>
                  <th>名称</th>
                  <th className="text-right">数量</th>
                  <th className="text-right">买入价</th>
                  <th className="text-right">现价</th>
                  <th className="text-right">盈亏%</th>
                  <th>买入时间</th>
                  <th>买入原因 / 卖出状态</th>
                </tr>
              </thead>
              <tbody>
                {g.holdings.map((p) => (
                  <tr key={p.id} className="border-b border-[#222]">
                    <td className="py-1.5 font-mono">{p.code}</td>
                    <td>{p.name || "-"}</td>
                    <td className="text-right">{p.qty}</td>
                    <td className="text-right">{p.buy_price.toFixed(3)}</td>
                    <td className="text-right">
                      {p.last_price ? p.last_price.toFixed(3) : "-"}
                    </td>
                    <td className={`text-right ${pnlCls(p.pnl_pct)}`}>
                      {fmtPct(p.pnl_pct)}
                    </td>
                    <td className="text-xs text-[#888]">{p.buy_ts}</td>
                    <td className="text-xs text-[#4fc3f7]">{p.buy_reason}</td>
                  </tr>
                ))}
                {g.selling.map((p) => (
                  <tr key={p.id} className="border-b border-[#222] opacity-70">
                    <td className="py-1.5 font-mono">{p.code}</td>
                    <td>{p.name || "-"}</td>
                    <td className="text-right">{p.qty}</td>
                    <td className="text-right">{p.buy_price.toFixed(3)}</td>
                    <td className="text-right">-</td>
                    <td className="text-right">-</td>
                    <td className="text-xs text-[#888]">{p.buy_ts}</td>
                    <td className="text-xs text-[#ffb74d]">
                      卖出回报中: {p.sell_reason}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="text-sm text-[#666]">
              买入组为空 (选股命中并成交后会显示在这里)
            </div>
          )}

          {g.pending_buys?.length > 0 && (
            <div className="text-xs text-[#ffb74d]">
              买入回报中:{" "}
              {g.pending_buys
                .map((b: any) => `${b.code} ${b.qty}@${Number(b.price).toFixed(3)}`)
                .join(", ")}
            </div>
          )}
        </div>
      ))}

      {/* ---- 指令流水 + 日志 ---- */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-[#1a1a1a] border border-[#2a2a2a] rounded-lg p-4">
          <div className="text-sm text-[#aaa] mb-2">指令流水 (买卖指令留痕)</div>
          <pre className="text-xs leading-5 max-h-64 overflow-y-auto whitespace-pre-wrap">
            {(st?.events ?? []).slice(0, 40).map((e) => (
              <div key={e.id} className="flex gap-2">
                <span className="text-[#666]">{e.ts.slice(5, 19)}</span>
                <span
                  className={`px-1 rounded border text-[10px] leading-4 ${
                    SIDE_STYLE[e.side] ?? ""
                  }`}
                >
                  {e.side === "buy" ? "买" : "卖"}
                </span>
                <span className="text-[#888] w-20">{e.strategy_id}</span>
                <span className="font-mono">{e.code}</span>
                <span>
                  {e.qty}@{e.price.toFixed(3)}
                </span>
                <span
                  className={
                    e.status === "filled"
                      ? "text-[#66bb6a]"
                      : e.status === "rejected"
                        ? "text-[#ef5350]"
                        : "text-[#ffb74d]"
                  }
                >
                  {e.status}
                </span>
                {e.dry_run === 1 && <span className="text-[#555]">[模拟]</span>}
                <span className="text-[#666] truncate">{e.detail}</span>
              </div>
            ))}
            {!st?.events?.length && (
              <div className="text-[#555]">暂无指令</div>
            )}
          </pre>
        </div>
        <div className="bg-[#1a1a1a] border border-[#2a2a2a] rounded-lg p-4">
          <div className="text-sm text-[#aaa] mb-2">引擎日志</div>
          <pre
            ref={logRef}
            className="text-xs leading-5 max-h-64 overflow-y-auto whitespace-pre-wrap text-[#9e9e9e]"
          >
            {st?.logs?.join("\n") || "暂无日志"}
          </pre>
        </div>
      </div>
    </div>
  );
}
