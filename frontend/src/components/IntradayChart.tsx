"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import * as echarts from "echarts";
import type { TickMsg } from "@/lib/ws";

type Bar = {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  amount: number;
  vwap: number;
  dif: number;
  dea: number;
  macd_hist: number;
};

type Props = {
  symbol: string;
  preClose: number;
  bars: Bar[];
  tick: TickMsg | null;
  titleExtra?: string;
};

/**
 * 同花顺风格分时图 — 3 子图 (价+VWAP / 量 / MACD)
 *
 * 关键: ECharts 实例只创建一次 (useRef 持有), 每次 tick 来只 setOption 更新 series.data
 *       ECharts 内部 diff 只重绘变化的点, 不会销毁重渲染 = 无闪烁
 */
export default function IntradayChart({ symbol, preClose, bars, tick, titleExtra }: Props) {
  const chartRef = useRef<echarts.ECharts | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const initKeyRef = useRef<string>("");
  const [err, setErr] = useState<string | null>(null);

  // --- 1. 创建 ECharts 实例 (只一次) ---
  useEffect(() => {
    if (!containerRef.current || chartRef.current) return;
    try {
      const chart = echarts.init(containerRef.current, "dark", { renderer: "canvas" });
      chartRef.current = chart;
      const resize = () => chart.resize();
      window.addEventListener("resize", resize);
      return () => {
        window.removeEventListener("resize", resize);
        chart.dispose();
        chartRef.current = null;
        initKeyRef.current = ""; // StrictMode 下会 mount→unmount→mount, 必须重置 key 让下次全量 setOption 生效
      };
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  // --- 2. 首次/数据变更: 全量 setOption ---
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || bars.length === 0) return;

    const key = symbol + ":" + bars.length;
    if (key === initKeyRef.current) return; // 同一 symbol 同一数据量不重复全量 set
    initKeyRef.current = key;

    const times = bars.map((b) => b.time);
    const prices = bars.map((b) => b.close);
    const vwap = bars.map((b) => b.vwap);
    const volumes = bars.map((b) => b.volume);
    const volColors = bars.map((b) => (b.close >= b.open ? "#ef5350" : "#26a69a"));
    const dif = bars.map((b) => b.dif);
    const dea = bars.map((b) => b.dea);
    const hist = bars.map((b) => b.macd_hist);
    const histColors = hist.map((v) => (v >= 0 ? "#ef5350" : "#26a69a"));

    chart.setOption({
      backgroundColor: "#0f0f0f",
      title: {
        text: `${symbol}${titleExtra ? " · " + titleExtra : ""}`,
        left: 12, top: 8,
        textStyle: { color: "#e0e0e0", fontSize: 14 },
      },
      tooltip: {
        trigger: "axis",
        backgroundColor: "rgba(20,20,20,0.95)",
        borderColor: "#333",
        textStyle: { color: "#e0e0e0" },
        axisPointer: { type: "cross", lineStyle: { color: "#555" } },
      },
      grid: [
        { left: 55, right: 65, top: 42, height: "55%" },
        { left: 55, right: 65, top: "63%", height: "18%" },
        { left: 55, right: 65, top: "84%", height: "14%" },
      ],
      xAxis: [
        { type: "category", data: times, gridIndex: 0, axisLine: { lineStyle: { color: "#333" } }, axisLabel: { color: "#888" }, splitLine: { show: false } },
        { type: "category", gridIndex: 1, axisLine: { lineStyle: { color: "#333" } }, axisLabel: { show: false }, splitLine: { show: false } },
        { type: "category", gridIndex: 2, axisLine: { lineStyle: { color: "#333" } }, axisLabel: { show: false }, splitLine: { show: false } },
      ],
      yAxis: [
        { type: "value", gridIndex: 0, scale: true, splitLine: { lineStyle: { color: "#1f1f1f" } }, axisLabel: { color: "#888" } },
        { type: "value", gridIndex: 1, splitLine: { lineStyle: { color: "#1f1f1f" } }, axisLabel: { color: "#888", formatter: (v: number) => (v / 10000).toFixed(0) + "万" } },
        { type: "value", gridIndex: 2, splitLine: { lineStyle: { color: "#1f1f1f" } }, axisLabel: { color: "#888" } },
      ],
      series: [
        // 0: VWAP
        { name: "均价", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: vwap, symbol: "none", lineStyle: { color: "#ffd700", width: 1.3 }, animation: false },
        // 1: 价格 + 昨收 markLine
        {
          name: "价格", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: prices, symbol: "none",
          lineStyle: { color: "#4fc3f7", width: 1.5 }, animation: false,
          markLine: {
            silent: true, symbol: "none",
            lineStyle: { color: "#888", type: "dotted" },
            data: [{ yAxis: preClose, label: { formatter: `昨收 ${preClose.toFixed(3)}`, color: "#aaa", position: "end" } }],
          },
        },
        // 2: 成交量柱
        { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1,
          data: volumes.map((v, i) => ({ value: v, itemStyle: { color: volColors[i] } })),
          animation: false },
        // 3: MACD 柱
        { name: "MACD", type: "bar", xAxisIndex: 2, yAxisIndex: 2,
          data: hist.map((v, i) => ({ value: v, itemStyle: { color: histColors[i] } })),
          animation: false },
        // 4: DIF
        { name: "DIF", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: dif, symbol: "none", lineStyle: { color: "#e0e0e0", width: 1 }, animation: false },
        // 5: DEA
        { name: "DEA", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: dea, symbol: "none", lineStyle: { color: "#ffd700", width: 1 }, animation: false },
      ],
    }, true);
  }, [symbol, bars, preClose, titleExtra]);

  // --- 3. WS tick 增量: merge 模式 + silent, 只更新 series.data ---
  // ECharts 按 index 匹配已有 series, 只合并 data 属性
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !tick || tick.type !== "tick" || tick.symbol !== symbol) return;
    if (bars.length === 0) return;

    const last = tick.bars[tick.bars.length - 1];
    if (!last) return;

    // 用快照 last 覆写最后一根 bar 的 close (实时跳动)
    const priceLast = tick.snapshot?.last ?? last.close;
    const prices = bars.map((b, i) => (i === bars.length - 1 ? priceLast : b.close));
    const vwap = bars.map((b) => b.vwap);
    const volumes = bars.map((b) => b.volume);
    const volColors = bars.map((b) => (b.close >= b.open ? "#ef5350" : "#26a69a"));
    const difArr = bars.map((b) => b.dif);
    const deaArr = bars.map((b) => b.dea);
    const histArr = bars.map((b) => b.macd_hist);
    const histColors = histArr.map((v) => (v >= 0 ? "#ef5350" : "#26a69a"));

    // merge 模式 (notMerge=false, silent=true)
    // series 按 index 匹配已有 series, 只合并 data / markLine / itemStyle
    chart.setOption({
      series: [
        { data: vwap },
        { data: prices, markLine: { data: [{ yAxis: preClose }] } },
        { data: volumes.map((v, i) => ({ value: v, itemStyle: { color: volColors[i] } })) },
        { data: histArr.map((v, i) => ({ value: v, itemStyle: { color: histColors[i] } })) },
        { data: difArr },
        { data: deaArr },
      ],
    }, false, true);
  }, [tick, symbol, bars, preClose]);

  if (err) {
    return <div className="text-[#ef5350] p-4">ECharts 初始化失败: {err}</div>;
  }

  return (
    <div
      ref={containerRef}
      className="w-full h-[480px] bg-[#0f0f0f] rounded-lg border border-[#222]"
    />
  );
}
