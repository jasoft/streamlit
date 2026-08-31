"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import * as echarts from "echarts";
import type { EChartsOption } from "echarts";
import { useEffect, useRef } from "react";

type BacktestResult = {
  stats: Record<string, any>;
  equity: { time: string; value: number }[];
  markers: { date: string; price: number; target: number }[];
  close: { time: string; value: number }[];
};

export default function BacktestPage() {
  const [strategies, setStrategies] = useState<any[]>([]);
  const [selected, setSelected] = useState<string>("ma20_trend");
  const [symbols, setSymbols] = useState<string>("sz159915");
  const [params, setParams] = useState<Record<string, any>>({ window: 20 });
  const [result, setResult] = useState<Record<string, BacktestResult> | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api.strategies().then((rows) => {
      setStrategies(rows);
      if (rows.length > 0) {
        setSelected(rows[0].name);
        setParams(rows[0].params ?? {});
      }
    });
  }, []);

  const handleRun = async () => {
    setLoading(true);
    try {
      const out = await api.backtest({
        strategy: selected,
        symbols: symbols.split(",").map((s) => s.trim()),
        params,
        qfq: false,
        cash: 100000,
      });
      setResult(out as Record<string, BacktestResult>);
    } catch (e) {
      alert("回测失败: " + e);
    } finally {
      setLoading(false);
    }
  };

  const strat = strategies.find((s) => s.name === selected);

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">📈 回测</h1>

      {/* 控制面板 */}
      <div className="flex flex-wrap items-end gap-4 p-4 bg-[#141414] border border-[#2a2a2a] rounded-lg">
        <div>
          <label className="block text-xs text-[#666] mb-1">策略</label>
          <select
            value={selected}
            onChange={(e) => {
              setSelected(e.target.value);
              const s = strategies.find((x) => x.name === e.target.value);
              setParams(s?.params ?? {});
            }}
            className="bg-[#1a1a1a] border border-[#333] rounded px-3 py-1.5 text-sm"
          >
            {strategies.map((s) => (
              <option key={s.name} value={s.name}>{s.title}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-xs text-[#666] mb-1">标的 (逗号分隔)</label>
          <input
            value={symbols}
            onChange={(e) => setSymbols(e.target.value)}
            className="bg-[#1a1a1a] border border-[#333] rounded px-3 py-1.5 text-sm w-48"
          />
        </div>
        {strat && strat.params && (
          <div className="flex gap-3 flex-wrap">
            {Object.entries(strat.params).map(([k, v]) => (
              <div key={k}>
                <label className="block text-xs text-[#666] mb-1">{k}</label>
                <input
                  type="number"
                  step="any"
                  value={params[k] ?? v}
                  onChange={(e) => setParams({ ...params, [k]: Number(e.target.value) })}
                  className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-sm w-20"
                />
              </div>
            ))}
          </div>
        )}
        <button
          onClick={handleRun}
          disabled={loading}
          className="px-5 py-1.5 bg-[#ff6d00] text-white rounded hover:bg-[#e65100] disabled:opacity-50"
        >
          {loading ? "回测中..." : "▶ 跑回测"}
        </button>
      </div>

      {/* 结果 */}
      {result && Object.entries(result).map(([sym, r]) => (
        <div key={sym} className="space-y-4">
          {/* Stats */}
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-3">
            {Object.entries(r.stats).map(([k, v]) => (
              <div key={k} className="bg-[#141414] border border-[#2a2a2a] rounded p-3">
                <div className="text-xs text-[#666]">{k}</div>
                <div className="text-sm font-mono mt-1">{String(v)}</div>
              </div>
            ))}
          </div>

          {/* 资金曲线 */}
          <Chart title={`${sym} · 资金曲线`} data={r.equity.map((e) => ({ time: e.time, value: e.value }))} color="#4fc3f7" yFormat="¥" />
        </div>
      ))}
    </div>
  );
}

function Chart({ title, data, color, yFormat }: { title: string; data: { time: string; value: number }[]; color: string; yFormat?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, "dark");
    chartRef.current = chart;
    const opt: EChartsOption = {
      backgroundColor: "#141414",
      title: { text: title, textStyle: { color: "#e0e0e0", fontSize: 14 }, left: 12, top: 8 },
      tooltip: { trigger: "axis", backgroundColor: "#1a1a1a", borderColor: "#333", textStyle: { color: "#e0e0e0" } },
      grid: { left: 60, right: 20, top: 40, bottom: 20 },
      xAxis: { type: "category", data: data.map((d) => d.time), axisLine: { lineStyle: { color: "#333" } }, axisLabel: { color: "#888" } },
      yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: "#1f1f1f" } }, axisLabel: { color: "#888", formatter: yFormat ? `{value}` : undefined } },
      series: [{ type: "line", smooth: false, symbol: "none", lineStyle: { color, width: 1.5 }, data: data.map((d) => d.value), areaStyle: { color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: color + "40" }, { offset: 1, color: color + "00" }] } } }],
    };
    chart.setOption(opt);
    const resize = () => chart.resize();
    window.addEventListener("resize", resize);
    return () => { window.removeEventListener("resize", resize); chart.dispose(); chartRef.current = null; };
  }, [title, data, color, yFormat]);

  return <div ref={ref} className="w-full h-[320px] border border-[#2a2a2a] rounded-lg" />;
}
