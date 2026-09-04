"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useWatchlist } from "@/lib/watchlist";

const fmtPct = (v: number | null) =>
  v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;

export default function WatchlistPage() {
  const { data, error, refresh } = useWatchlist();
  const [busy, setBusy] = useState(false);
  const [formErr, setFormErr] = useState("");
  const [msg, setMsg] = useState("");
  const [symbol, setSymbol] = useState("");

  // 实时快照: 20s 一轮, 单只失败显示 — 不影响其他
  const [quotes, setQuotes] = useState<Record<string, { last: number | null; change_pct: number | null }>>({});
  const stocks = data?.stocks ?? [];
  const stockKey = stocks.map((s) => s.symbol).join(",");

  useEffect(() => {
    let alive = true;
    const fetchAll = async () => {
      const syms = stockKey ? stockKey.split(",") : [];
      if (!syms.length) { if (alive) setQuotes({}); return; }
      const entries = await Promise.all(syms.map(async (sym) => {
        try {
          const q = await api.quote(sym) as Record<string, unknown>;
          return [sym, {
            last: Number(q?.last) || null,
            change_pct: typeof q?.change_pct === "number" ? q.change_pct : null,
          }] as const;
        } catch {
          return [sym, { last: null, change_pct: null }] as const;
        }
      }));
      if (alive) setQuotes(Object.fromEntries(entries));
    };
    fetchAll();
    const t = setInterval(fetchAll, 20000);
    return () => { alive = false; clearInterval(t); };
  }, [stockKey]);

  const act = async (fn: () => Promise<any>, done?: () => void) => {
    setBusy(true);
    setFormErr("");
    setMsg("");
    try {
      const r = await fn();
      await refresh();
      setMsg(r?.msg ?? "");
      done?.();
    } catch (e) {
      setFormErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleAdd = () => {
    const sym = symbol.trim();
    if (!sym) { setFormErr("请输入标的代码, 如 601899 或 sz159915"); return; }
    act(() => api.addWatchStock({ symbol: sym }), () => setSymbol(""));
  };
  const handleDelete = (sym: string, name: string) => {
    if (!confirm(`删除自选股 ${sym}${name ? ` (${name})` : ""}?` +
        "\n删除后同花顺持仓自动同步也不会再回加; 重新手动添加可解除。")) return;
    act(() => api.deleteWatchStock(sym));
  };
  const handleSync = () => act(async () => {
    const r: any = await api.syncWatchlist();
    return {
      msg: r?.ok
        ? `同步完成: 持仓 ${r.positions} 只, 新加入 ${(r.added ?? []).length} 只`
        : `同步失败: ${r?.error ?? "未知错误"}`,
    };
  });
  const toggleAuto = () => act(() =>
    api.watchlistSettings({ auto_sync: !(data?.auto_sync ?? true) }));

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <h1 className="text-2xl font-bold">⭐ 自选股</h1>
        <span className={`text-xs px-2 py-0.5 rounded border ${
          data?.auto_sync
            ? "bg-[#0d2a1a] text-[#66bb6a] border-[#1b6a3a]"
            : "bg-[#1a1a1a] text-[#888] border-[#333]"
        }`}>
          {data?.auto_sync ? "● 持仓自动同步开" : "○ 持仓自动同步关"}
        </span>
        {data?.last_sync && (
          <span className="text-xs text-[#666]">最近同步 {data.last_sync}</span>
        )}
      </div>

      {(error || formErr) && (
        <div className="text-sm text-[#f44336] bg-[#2a0808] border border-[#7f1d1d] rounded p-3 whitespace-pre-wrap">
          {formErr || error}
        </div>
      )}
      {msg && (
        <div className="text-xs text-[#9ccc65] bg-[#0d1a08] border border-[#2a5a12] rounded px-3 py-2">
          {msg}
        </div>
      )}
      {data?.last_sync_error && !formErr && (
        <div className="text-xs text-[#ffb74d] bg-[#2a1a08] border border-[#7a5a12] rounded px-3 py-2">
          最近一次持仓同步失败: {data.last_sync_error}
          <div className="text-[#997a33] mt-0.5">
            请确认同花顺已打开且在交易面板 (未打开时后台会持续静默重试)
          </div>
        </div>
      )}

      {/* ============== 同步控制 + 添加 ============== */}
      <section className="border border-[#2a2a2a] rounded-lg bg-[#141414] p-5 space-y-3">
        <h2 className="text-lg font-semibold">管理</h2>
        <div className="flex flex-wrap items-center gap-4">
          <button onClick={handleSync} disabled={busy}
            className="px-4 py-1.5 bg-[#0d3a5a] text-[#4fc3f7] text-sm rounded border border-[#1b5a7a] hover:bg-[#0d2a3a] disabled:opacity-50">
            🔄 从同花顺持仓同步
          </button>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={data?.auto_sync ?? true}
              onChange={toggleAuto} disabled={busy}
              className="accent-[#ff6d00] w-4 h-4" />
            <span className={data?.auto_sync ? "text-[#ccc]" : "text-[#888]"}>
              自动同步 (后台每 2 分钟把同花顺新持仓加入自选股)
            </span>
          </label>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-[#888] space-y-1">
            <div>添加标的</div>
            <input value={symbol} onChange={(e) => setSymbol(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") handleAdd(); }}
              placeholder="601899 / sz159915 / 510300"
              className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 w-44 font-mono text-sm text-white placeholder-[#555]" />
          </label>
          <button onClick={handleAdd} disabled={busy}
            className="px-5 py-1.5 bg-[#2a6a2a] text-white text-sm rounded hover:bg-[#2e7d32] disabled:opacity-50">
            ＋ 添加
          </button>
          <span className="text-xs text-[#666]">
            名称自动从行情补全; 同一标的重复添加只更新名称
          </span>
        </div>
      </section>

      {/* ============== 自选股列表 ============== */}
      <section className="border border-[#2a2a2a] rounded-lg bg-[#141414] p-5 space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">自选股列表 ({stocks.length})</h2>
          <span className="text-xs text-[#666]">行情每 20 秒刷新</span>
        </div>
        {!data ? (
          <div className="text-sm text-[#666] py-4 text-center">加载中...</div>
        ) : stocks.length === 0 ? (
          <div className="text-sm text-[#666] py-4 text-center">
            还没有自选股: 用上方表单添加, 或点「从同花顺持仓同步」
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[#888] text-xs border-b border-[#2a2a2a]">
                  <th className="py-2 pr-3">代码</th>
                  <th className="py-2 pr-3">名称</th>
                  <th className="py-2 pr-3">来源</th>
                  <th className="py-2 pr-3">现价</th>
                  <th className="py-2 pr-3">涨跌幅</th>
                  <th className="py-2 pr-3">最近在持仓中</th>
                  <th className="py-2 pr-3">添加时间</th>
                  <th className="py-2 pr-3"></th>
                </tr>
              </thead>
              <tbody>
                {stocks.map((s) => {
                  const q = quotes[s.symbol];
                  return (
                    <tr key={s.symbol} className="border-b border-[#1f1f1f] hover:bg-[#1a1a1a]">
                      <td className="py-2 pr-3 font-mono text-white">
                        {s.code || s.symbol}
                      </td>
                      <td className="py-2 pr-3">
                        {s.name || <span className="text-[#555]">—</span>}
                      </td>
                      <td className="py-2 pr-3">
                        <span className={`text-xs px-2 py-0.5 rounded border whitespace-nowrap ${
                          s.source === "ths"
                            ? "bg-[#332207] text-[#ffb74d] border-[#8a5a12]"
                            : "bg-[#0d2a3a] text-[#4fc3f7] border-[#1b5a7a]"
                        }`}>
                          {s.source === "ths" ? "同花顺持仓" : "手动"}
                        </span>
                      </td>
                      <td className="py-2 pr-3 font-mono">
                        {q?.last != null ? q.last.toFixed(3) : "—"}
                      </td>
                      <td className={`py-2 pr-3 font-mono ${
                        q?.change_pct == null ? "" :
                        q.change_pct >= 0 ? "text-[#ef5350]" : "text-[#26a69a]"
                      }`}>
                        {fmtPct(q?.change_pct ?? null)}
                      </td>
                      <td className="py-2 pr-3 text-xs text-[#888]">
                        {s.last_seen_in_positions || "—"}
                      </td>
                      <td className="py-2 pr-3 text-xs text-[#888]">{s.added_at}</td>
                      <td className="py-2 pr-3">
                        <button
                          onClick={() => handleDelete(s.symbol, s.name)}
                          disabled={busy}
                          className="text-xs text-[#ef5350] hover:text-white bg-transparent border border-[#5a2a2a] rounded px-2 py-0.5 hover:bg-[#3a1515] disabled:opacity-50">
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
      </section>
    </div>
  );
}
