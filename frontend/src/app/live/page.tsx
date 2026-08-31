"use client";

import { useEffect, useState, useRef, useCallback } from "react";
import { api } from "@/lib/api";
import { marketWs, type TickMsg } from "@/lib/ws";
import IntradayChart from "@/components/IntradayChart";

type Strategy = {
  name: string;
  title: string;
  enabled: boolean;
  running: boolean;
  pid: number | null;
  symbols: string[];
  params: Record<string, any>;
  live: Record<string, any>;
  status: string;
  next_run: string;
  last_run: string;
};

type EvalRow = {
  ts: string;
  symbol: string;
  price: number;
  msg: string;
  target: number;
  [key: string]: any;
};

export default function LivePage() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [evals, setEvals] = useState<Record<string, EvalRow[]>>({});
  const [expanded, setExpanded] = useState<string | null>(null);
  const [tick, setTick] = useState<TickMsg | null>(null);
  const wsSubscribedRef = useRef<Set<string>>(new Set());

  // 加载策略列表 + 初始化 WS
  useEffect(() => {
    api.strategies().then(setStrategies).catch(console.error);

    // 订阅 WS (如果 marketWs 可用)
    if (marketWs!) {
      const off = marketWs!.onMessage((msg) => {
        if (msg.type === "tick") setTick(msg);
      });
      // 先订阅最常用的 symbol
      marketWs!.subscribe("sz159915");
      marketWs!.subscribe("sh510300");
      wsSubscribedRef.current.add("sz159915");
      wsSubscribedRef.current.add("sh510300");
      return () => {
        off();
        marketWs!.close();
      };
    }
  }, []);

  // 每 5s 刷新策略状态 + 评估记录
  useEffect(() => {
    const t = setInterval(async () => {
      try {
        const rows = await api.strategies();
        setStrategies(rows);
        // 刷新展开的策略的 evals
        if (expanded) {
          const e = await api.evals(expanded, 60);
          setEvals((prev) => ({ ...prev, [expanded]: e }));
        }
      } catch {}
    }, 5000);
    return () => clearInterval(t);
  }, [expanded]);

  // 展开策略时, 订阅其 symbol 的 WS + 加载 evals
  const toggleExpand = useCallback(async (name: string) => {
    const next = expanded === name ? null : name;
    setExpanded(next);
    if (next) {
      try {
        const e = await api.evals(next, 60);
        setEvals((prev) => ({ ...prev, [next]: e }));
      } catch {}
      // 订阅策略 symbols
      const strat = strategies.find((s) => s.name === next);
      if (strat && marketWs) {
        strat.symbols.forEach((sym) => {
          if (!wsSubscribedRef.current.has(sym)) {
            marketWs!.subscribe(sym);
            wsSubscribedRef.current.add(sym);
          }
        });
      }
    }
  }, [expanded, strategies]);

  const handleStart = async (name: string) => {
    await api.start(name);
    const rows = await api.strategies();
    setStrategies(rows);
  };
  const handleStop = async (name: string) => {
    await api.stop(name);
    const rows = await api.strategies();
    setStrategies(rows);
  };
  const handleRunOnce = async (name: string) => {
    await api.runOnce(name);
    const rows = await api.strategies();
    setStrategies(rows);
    const e = await api.evals(name, 60);
    setEvals((prev) => ({ ...prev, [name]: e }));
  };

  if (strategies.length === 0) {
    return <div className="text-[#666] p-8">加载中...</div>;
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold mb-4">🚦 实盘策略管理</h1>

      {strategies.map((s) => (
        <StrategyCard
          key={s.name}
          strat={s}
          expanded={expanded === s.name}
          evals={evals[s.name] ?? []}
          tick={tick}
          onToggle={() => toggleExpand(s.name)}
          onStart={() => handleStart(s.name)}
          onStop={() => handleStop(s.name)}
          onRunOnce={() => handleRunOnce(s.name)}
        />
      ))}
    </div>
  );
}

function StrategyCard({
  strat,
  expanded,
  evals,
  tick,
  onToggle,
  onStart,
  onStop,
  onRunOnce,
}: {
  strat: Strategy;
  expanded: boolean;
  evals: EvalRow[];
  tick: TickMsg | null;
  onToggle: () => void;
  onStart: () => void;
  onStop: () => void;
  onRunOnce: () => void;
}) {
  const [intradayData, setIntradayData] = useState<Record<string, any>>({});

  // 展开时加载分时图初始数据
  useEffect(() => {
    if (!expanded) return;
    strat.symbols.forEach((sym) => {
      if (!intradayData[sym]) {
        api.intraday(sym).then((d) =>
          setIntradayData((prev) => ({ ...prev, [sym]: d }))
        ).catch(() => {});
      }
    });
  }, [expanded, strat.symbols, intradayData]);

  const running = strat.running;
  const chg = tick?.snapshot?.change_pct;
  const tickSymbol = tick?.symbol;

  return (
    <div className="border border-[#2a2a2a] rounded-lg bg-[#141414] overflow-hidden">
      {/* 头部 */}
      <div
        className="flex items-center gap-4 px-4 py-3 cursor-pointer hover:bg-[#1a1a1a] transition-colors"
        onClick={onToggle}
      >
        <div className={`w-2 h-2 rounded-full ${running ? "bg-[#26a69a] animate-pulse" : "bg-[#555]"}`} />
        <div className="font-semibold">{strat.title}</div>
        <div className="text-[#666] text-xs">{strat.name}</div>
        <div className="text-[#888] text-xs ml-2">
          {running ? `运行中 pid ${strat.pid}` : "未运行"}
        </div>
        <div className="text-[#666] text-xs ml-auto">
          {strat.symbols.join(", ")}
        </div>
        <div className="text-[#666] text-xs">
          {expanded ? "▲ 收起" : "▼ 展开"}
        </div>
      </div>

      {/* 展开内容 */}
      {expanded && (
        <div className="border-t border-[#2a2a2a] p-4 space-y-4">
          {/* 控制按钮 */}
          <div className="flex items-center gap-2">
            {running ? (
              <button
                onClick={(e) => { e.stopPropagation(); onStop(); }}
                className="px-4 py-1.5 bg-[#ef5350] text-white text-sm rounded hover:bg-[#d32f2f]"
              >
                停止
              </button>
            ) : (
              <button
                onClick={(e) => { e.stopPropagation(); onStart(); }}
                className="px-4 py-1.5 bg-[#26a69a] text-white text-sm rounded hover:bg-[#1e8e6f]"
              >
                启动
              </button>
            )}
            <button
              onClick={(e) => { e.stopPropagation(); onRunOnce(); }}
              className="px-4 py-1.5 bg-[#333] text-[#e0e0e0] text-sm rounded hover:bg-[#444]"
            >
              立即跑一轮 (dry-run)
            </button>
            <div className="ml-auto text-xs text-[#666]">
              下次执行: <span className="text-[#aaa]">{strat.next_run}</span>
            </div>
          </div>

          {/* 分时图 — 每个 symbol 一张 */}
          {strat.symbols.map((sym) => {
            const data = intradayData[sym];
            const symbolTick = tick?.symbol === sym ? tick : null;
            if (!data) return <div key={sym} className="text-[#666] text-sm">加载 {sym} 分时数据...</div>;
            return (
              <div key={sym} className="border border-[#2a2a2a] rounded-lg overflow-hidden">
                <IntradayChart
                  symbol={sym}
                  preClose={data.pre_close}
                  bars={data.bars}
                  tick={symbolTick}
                  titleExtra={running ? `● 运行中 (WS 每秒刷新)` : ""}
                />
              </div>
            );
          })}

          {/* 最新评估 metrics */}
          {evals.length > 0 && (
            <div>
              <div className="text-sm text-[#888] mb-2">最新评估</div>
              <div className="flex gap-3 flex-wrap">
                {Object.values(
                  evals.reduce((acc, e) => {
                    acc[e.symbol] = e;
                    return acc;
                  }, {} as Record<string, EvalRow>)
                ).map((e) => (
                  <div key={e.symbol} className="bg-[#1a1a1a] border border-[#2a2a2a] rounded px-3 py-2">
                    <div className="text-xs text-[#666]">{e.symbol}</div>
                    <div className="text-lg font-mono">{e.price}</div>
                    <div className="text-xs text-[#888]">{e.msg}</div>
                    <div className="text-xs mt-1">
                      目标仓位: <span className={e.target === 1 ? "text-[#26a69a]" : "text-[#ef5350]"}>
                        {e.target === 1 ? "持仓" : "空仓"}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 评估流水 */}
          {evals.length > 0 && (
            <div>
              <div className="text-sm text-[#888] mb-2">评估流水 (最近 30 条)</div>
              <div className="overflow-auto max-h-64 text-xs">
                <table className="w-full text-[#aaa]">
                  <thead className="text-[#666] border-b border-[#2a2a2a] sticky top-0 bg-[#141414]">
                    <tr>
                      <th className="text-left px-2 py-1">时间</th>
                      <th className="text-left px-2 py-1">标的</th>
                      <th className="text-right px-2 py-1">价格</th>
                      <th className="text-left px-2 py-1">信号</th>
                      <th className="text-center px-2 py-1">目标</th>
                    </tr>
                  </thead>
                  <tbody>
                    {evals.slice(-30).reverse().map((e, i) => (
                      <tr key={i} className="border-b border-[#1a1a1a] hover:bg-[#1a1a1a]">
                        <td className="px-2 py-1 font-mono text-[#666]">{e.ts}</td>
                        <td className="px-2 py-1">{e.symbol}</td>
                        <td className="px-2 py-1 text-right font-mono">{e.price}</td>
                        <td className="px-2 py-1">{e.msg}</td>
                        <td className="px-2 py-1 text-center">
                          <span className={`px-1.5 py-0.5 rounded text-[10px] ${e.target === 1 ? "bg-[#1e8e6f]/30 text-[#26a69a]" : "bg-[#d32f2f]/30 text-[#ef5350]"}`}>
                            {e.target === 1 ? "1" : "0"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
