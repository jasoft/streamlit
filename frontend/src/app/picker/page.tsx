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

type PickerPlugin = {
  id: string;
  title: string;
  desc: string;
  params: Record<string, { default: any; desc: string }>;
  defaults: Record<string, any>;
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

export default function PickerPage() {
  const [st, setSt] = useState<PickerStatus | null>(null);
  const [plugins, setPlugins] = useState<PickerPlugin[]>([]);
  const [err, setErr] = useState("");
  const [formErr, setFormErr] = useState("");
  const [busy, setBusy] = useState(false);

  // 引擎启动参数
  const [cash, setCash] = useState(100000);
  const [poll, setPoll] = useState(5);
  const [live, setLive] = useState(false);

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

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, [refresh]);

  useEffect(() => {
    api
      .pickerStrategies()
      .then((list: PickerPlugin[]) => {
        setPlugins(list);
        if (list.length && !pickerId) {
          setPickerId(list[0].id);
          setParamsText(JSON.stringify(list[0].defaults, null, 2));
        }
      })
      .catch(() => {});
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

  const handlePickerChange = (id: string) => {
    setPickerId(id);
    const p = plugins.find((x) => x.id === id);
    if (p) setParamsText(JSON.stringify(p.defaults, null, 2));
  };

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
            选股插件
            <select
              value={pickerId}
              onChange={(e) => handlePickerChange(e.target.value)}
              className="bg-[#111] border border-[#333] rounded px-2 py-1"
            >
              {plugins.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.title} ({p.id})
                </option>
              ))}
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
            插件参数 (JSON, 按组覆盖默认值)
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
              插件: {g.picker_title || g.picker}
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
