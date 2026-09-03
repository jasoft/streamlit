"use client";

import { useEffect, useRef, useState } from "react";
import * as echarts from "echarts";
import { api } from "@/lib/api";
// 统一使用 lib/fmt 的格式化函数，并重导出给页面使用
import { fmtMoney, fmtPrice, fmtPct, fmtNum } from "@/lib/fmt";
export { fmtMoney, fmtPrice, fmtPct, fmtNum };

export type KBar = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type KMarker = {
  date: string;   // ISO ts 或 bar date, 内部规范化匹配
  price: number;
  action: "买入" | "卖出";
  qty?: number;     // 成交数量 (股)
  pnl?: number | null;  // 该笔盈亏金额 (仅卖出有)
};

export type ChartMode = "kline" | "intraday";  // K线 / 分时

type Props = {
  symbol: string;
  tf: string;              // 周期名 (显示用, 分时填 "分时")
  bars: KBar[];
  markers?: KMarker[];
  maWindow?: number;       // kline: MA 窗口; intraday 不用
  mode?: ChartMode;        // 图表模式, 默认 kline
  /** intraday 模式: 预占全天交易时段 (09:31-11:30,13:01-15:00 共 240 格),
   *  true: xAxis 显示完整 240 分钟标签 (未到达的时间段 bars[i]=null)
   *  false: 按 bars 实际长度显示 (用于流式测试初阶段, 或无数据场景)
   */
  fullDayAxis?: boolean;
  /** intraday 模式: 基准价 (昨收), 用于画 0% 轴参考线. undefined 时取 bars[0].open */
  preClose?: number;
};

/** 规范化时间字符串到分钟精度 (T→空格, 截前16位), 用于 order.ts 与 bar.date 匹配. */
function normDate(s: string): string {
  return String(s).replace("T", " ").slice(0, 16);
}
/** 只取日期 (前 10 位) 用于日线 marker ↔ 分钟 bar 粗粒度匹配 */
function normDateDay(s: string): string {
  const t = String(s).replace("T", " ");
  return t.slice(0, 10);
}

/** A股交易日 240 个分钟点 (HH:MM), 用于分时图 xAxis 占位 */
const FULL_DAY_LABELS: string[] = (() => {
  const out: string[] = [];
  for (let m = 9 * 60 + 31; m <= 11 * 60 + 30; m++)
    out.push(`${String(Math.floor(m/60)).padStart(2,"0")}:${String(m%60).padStart(2,"0")}`);
  for (let m = 13 * 60 + 1; m <= 15 * 60 + 0; m++)
    out.push(`${String(Math.floor(m/60)).padStart(2,"0")}:${String(m%60).padStart(2,"0")}`);
  return out;
})();

/** 分钟 "HH:MM" -> FULL_DAY_LABELS 的 index; 找不到返回 -1 */
function minuteToIndex(hhmm: string): number {
  return FULL_DAY_LABELS.indexOf(hhmm);
}

/** 把 bar.date (形如 "2026-08-31 10:35:00") 截成 HH:MM */
function toHHMM(s: string): string {
  const t = String(s).replace("T", " ");
  const idx = t.indexOf(" ");
  if (idx < 0) return s.slice(0, 5);
  return t.slice(idx + 1, idx + 6);
}

function computeMA(closes: number[], window: number): (number | null)[] {
  const out: (number | null)[] = [];
  for (let i = 0; i < closes.length; i++) {
    if (i < window - 1) { out.push(null); continue; }
    let s = 0;
    for (let j = i - window + 1; j <= i; j++) s += closes[j];
    out.push(s / window);
  }
  return out;
}

/** 构建 date → xIndex 映射 (双轨: 精确 normDate + 日级兜底)
 * dateExact[normDate(bar.date)] = index
 * dateDay[normDateDay(bar.date)] = index (同天最后一个 bar 的 index, marker 定位更直观)
 */
function buildDateIdx(dates: string[]): {
  exact: Map<string, number>;
  day: Map<string, number>;
} {
  const exact = new Map<string, number>();
  const day = new Map<string, number>();
  dates.forEach((d, i) => {
    exact.set(normDate(d), i);
    day.set(normDateDay(d), i); // 同天多次写入, 保留最后一个位置 (收盘端)
  });
  return { exact, day };
}
/** 用 dateIdx 双轨查询 marker 的 xIndex: 精确命中优先, 否则日级命中. */
function resolveMarkerIdx(
  m: KMarker,
  indexes: { exact: Map<string, number>; day: Map<string, number> }
): number | null {
  if (indexes.exact.has(normDate(m.date))) return indexes.exact.get(normDate(m.date))!;
  if (indexes.day.has(normDateDay(m.date))) return indexes.day.get(normDateDay(m.date))!;
  return null;
}

/** 生成买卖 markPoint 数组. */
function buildMarkPoints(
  markers: KMarker[],
  indexes: { exact: Map<string, number>; day: Map<string, number> },
): any[] {
  return markers
    .map((m) => {
      const xIdx = resolveMarkerIdx(m, indexes);
      if (xIdx == null) return null;
      const lots = m.qty ? Math.round(m.qty / 100) + "手" : "";
      const hasPnl = m.action === "卖出" && m.pnl != null;
      const pnlVal = hasPnl ? (m.pnl! >= 0 ? "+" : "") + fmtMoney(m.pnl!) : "";
      const pnlColor = hasPnl ? (m.pnl! >= 0 ? "#26a69a" : "#ef5350") : "#fff";
      const txt = (m.action === "买入" ? "买" : "卖") + (lots ? " " + lots : "") + (pnlVal ? `\n{pnl|${pnlVal}}` : "");
      return {
        coord: [xIdx, m.price],
        symbol: m.action === "买入" ? "triangle" : "pin",
        symbolSize: 18,
        symbolKeepAspect: true,
        itemStyle: { color: m.action === "买入" ? "#26a69a" : (hasPnl && m.pnl! < 0 ? "#ef5350" : "#ff9800") },
        label: {
          show: true,
          formatter: txt,
          position: m.action === "买入" ? "bottom" : "top",
          distance: 6,
          color: "#fff", fontSize: 10, lineHeight: 14,
          backgroundColor: "rgba(15,15,15,0.85)",
          padding: [2, 4],
          borderRadius: 3,
          rich: {
            pnl: { color: pnlColor, fontSize: 12, fontWeight: "bold" as const },
          },
        },
      };
    })
    .filter((x): x is any => x != null);
}

/** ECharts 图表: kline (含 K/MA/量/标记/缩放拖动) 或 intraday 分时图 (价格线+成交均价黄线+量+标记/预占全天轴).
 * 实例只创建一次 (useRef), 数据变更用 setOption 更新 (无闪烁).
 */
export default function KLineChart({
  symbol, tf, bars, markers = [], maWindow = 0,
  mode = "kline", fullDayAxis = false, preClose,
}: Props) {
  const chartRef = useRef<echarts.ECharts | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const initKeyRef = useRef<string>("");
  const [name, setName] = useState<string>(symbol);

  // 获取股票名称 (后端 /api/quote 返回 name 字段)
  useEffect(() => {
    let cancelled = false;
    api.quote(symbol).then((q: any) => {
      if (!cancelled && q?.name) setName(q.name);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [symbol]);

  useEffect(() => {
    if (!containerRef.current || chartRef.current) return;
    const chart = echarts.init(containerRef.current, "dark", { renderer: "canvas" });
    chartRef.current = chart;
    const resize = () => chart.resize();
    window.addEventListener("resize", resize);
    // 监听容器尺寸变化 (flex 布局下容器尺寸会随父级变化)
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(containerRef.current);
    return () => {
      window.removeEventListener("resize", resize);
      ro.disconnect();
      chart.dispose();
      chartRef.current = null;
      initKeyRef.current = "";
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || bars.length === 0) return;

    const lastBar = bars.length > 0 ? bars[bars.length - 1] : null;
    const key = `${symbol}:${mode}:${tf}:${bars.length}:${markers.length}:${maWindow}:${fullDayAxis}:${preClose}:${lastBar?.close}:${lastBar?.date}:${name}`;
    if (key === initKeyRef.current) return;
    initKeyRef.current = key;

    if (mode === "kline") {
      renderKLine(chart, symbol, name, tf, bars, markers, maWindow);
    } else {
      renderIntraday(chart, symbol, name, tf, bars, markers, fullDayAxis, preClose);
    }
  }, [symbol, name, mode, tf, bars, markers, maWindow, fullDayAxis, preClose]);

  return (
    <div
      ref={containerRef}
      className="w-full h-full min-h-[320px] bg-[#0f0f0f] rounded-lg border border-[#222]"
    />
  );
}

// ============================================================ K线模式 =====
function renderKLine(
  chart: echarts.ECharts,
  symbol: string, name: string, tf: string, bars: KBar[], markers: KMarker[], maWindow: number,
) {
  const dates = bars.map((b) => b.date);
  const ohlc = bars.map((b) => [b.open, b.close, b.low, b.high]);
  const vols = bars.map((b) => ({
    value: b.volume,
    itemStyle: { color: b.close >= b.open ? "#ef5350" : "#26a69a" },
  }));
  const ma = maWindow > 0 ? computeMA(bars.map((b) => b.close), maWindow) : null;
  const indexes = buildDateIdx(dates);
  const markPoints = buildMarkPoints(markers, indexes);

  chart.setOption({
    backgroundColor: "#0f0f0f",
    title: {
      text: `${name}${maWindow ? `  MA(${maWindow})` : ""}${markers.length ? `  · ${markers.length}信号` : ""}`,
      left: 12, top: 8,
      textStyle: { color: "#e0e0e0", fontSize: 14 },
    },
    animation: false,
    tooltip: {
      trigger: "axis",
      backgroundColor: "rgba(20,20,20,0.95)",
      borderColor: "#333",
      textStyle: { color: "#e0e0e0" },
      axisPointer: { type: "cross", lineStyle: { color: "#555" } },
      formatter: (params: any) => {
        if (!Array.isArray(params) || !params.length) return "";
        const dt = params[0].axisValue;
        const idx = params[0].dataIndex ?? 0;
        let out = `<div style="font-size:11px;color:#888">${dt}</div>`;
        params.forEach((p: any) => {
          if (p.seriesName === "K线") {
            const bar = ohlc[idx];
            if (!bar) return;
            const [o, c, l, h] = bar.map((v: number) => v.toFixed(3));
            out += `<div>${p.marker}K线 开${o} 收${c} 低${l} 高${h}</div>`;
          } else if (p.seriesName === "MA") {
            const v = ma && ma[idx] != null ? ma[idx] : null;
            out += `<div>${p.marker}MA ${v == null ? "-" : Number(v).toFixed(3)}</div>`;
          } else if (p.seriesName === "成交量") {
            const v = vols[idx]?.value ?? 0;
            out += `<div>${p.marker}成交量 ${(Number(v) / 10000).toFixed(0)}万</div>`;
          }
        });
        return out;
      },
    },
    legend: {
      data: ma ? ["MA"] : [], top: 8, right: 12,
      textStyle: { color: "#888" },
    },
    grid: [
      { left: 55, right: 30, top: 50, height: "58%" },
      { left: 55, right: 30, top: "72%", height: "16%" },
    ],
    dataZoom: [
      // 内置: 鼠标滚轮缩放 + 拖动平移 (覆盖两个 grid 的 x)
      {
        type: "inside", xAxisIndex: [0, 1],
        start: Math.max(0, 100 - 30000 / Math.max(1, bars.length)),
        end: 100,
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
        moveOnMouseWheel: false,
        preventDefaultMouseMove: true,
      },
      // 底部滑块 (可拖动, 范围选择)
      {
        type: "slider", xAxisIndex: [0, 1],
        start: Math.max(0, 100 - 30000 / Math.max(1, bars.length)),
        end: 100,
        bottom: 4,
        height: 20,
        borderColor: "#333",
        fillerColor: "rgba(38,166,154,0.15)",
        handleStyle: { color: "#26a69a" },
        textStyle: { color: "#888", fontSize: 10 },
        showDataShadow: false,
      },
    ],
    xAxis: [
      { type: "category", data: dates, gridIndex: 0,
        axisLine: { lineStyle: { color: "#333" } },
        axisLabel: { color: "#888", fontSize: 10 },
        splitLine: { show: false },
        axisTick: { show: false },
      },
      { type: "category", gridIndex: 1,
        axisLine: { lineStyle: { color: "#333" } },
        axisLabel: { show: false },
        splitLine: { show: false },
        axisTick: { show: false },
      },
    ],
    yAxis: [
      { type: "value", gridIndex: 0, scale: true,
        splitLine: { lineStyle: { color: "#1f1f1f" } },
        axisLabel: { color: "#888", fontSize: 10, formatter: (v: number) => fmtPrice(v) },
      },
      { type: "value", gridIndex: 1,
        splitLine: { lineStyle: { color: "#1f1f1f" } },
        axisLabel: { color: "#888", fontSize: 10,
          formatter: (v: number) => (v / 10000).toFixed(0) + "万" },
      },
    ],
    series: [
      {
        name: "K线", type: "candlestick", xAxisIndex: 0, yAxisIndex: 0, data: ohlc,
        itemStyle: { color: "#ef5350", color0: "#26a69a",
          borderColor: "#ef5350", borderColor0: "#26a69a" },
        markPoint: { data: markPoints, symbolKeepAspect: true, animation: false },
      },
      { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: vols },
      ...(ma ? [{ name: "MA", type: "line", xAxisIndex: 0, yAxisIndex: 0,
          data: ma, symbol: "none",
          lineStyle: { color: "#ffd700", width: 1 } }] : []),
    ],
  } as any, true);
}

// ============================================================ 分时图模式 =====
function renderIntraday(
  chart: echarts.ECharts,
  symbol: string, name: string, _tf: string, bars: KBar[], markers: KMarker[],
  fullDayAxis: boolean, preClose: number | undefined,
) {
  const close0 = bars[0]?.close ?? 1;
  const base = preClose ?? bars[0]?.open ?? close0;

  // ---- X 轴: fullDayAxis=true 用全天 240 格占位, 否则用实际 bars 的 HH:MM ----
  let xLabels: string[];
  let priceSeries: (number | null)[];   // 白线: 每分钟收盘价
  let vwapSeries: (number | null)[];    // 黄线: 成交均价 = 累计成交额/累计成交量
  let volSeries: any[];                 // 量柱

  if (fullDayAxis) {
    // 以 bar 的 HH:MM 为 key, 把 bars 填入对应 slot, 其他 slot=null
    const priceByMin = new Map<string, number | null>();
    const volByMin = new Map<string, number>();
    const amtByMin = new Map<string, number>();
    FULL_DAY_LABELS.forEach(m => {
      priceByMin.set(m, null);
      volByMin.set(m, 0);
      amtByMin.set(m, 0);
    });
    bars.forEach(b => {
      const m = toHHMM(b.date);
      priceByMin.set(m, b.close);
      volByMin.set(m, (volByMin.get(m) ?? 0) + b.volume);
      const avgPx = (b.open + b.high + b.low + b.close) / 4;
      amtByMin.set(m, (amtByMin.get(m) ?? 0) + avgPx * b.volume);
    });

    // VWAP: 只累计到"实际有成交量"的分钟; 没到达的分钟返回 null (前端不连线/不预画直线)
    let cumAmt = 0, cumVol = 0;
    xLabels = FULL_DAY_LABELS;
    priceSeries = xLabels.map(m => priceByMin.get(m) ?? null);
    vwapSeries = xLabels.map(m => {
      const v = volByMin.get(m) ?? 0;
      const a = amtByMin.get(m) ?? 0;
      // 没成交的分钟 → 均价不填充, 避免被画成"一条预先铺好的直线"
      if (v === 0) return null;
      cumAmt += a; cumVol += v;
      return cumVol > 0 ? cumAmt / cumVol : null;
    });
    volSeries = xLabels.map(m => {
      const v = volByMin.get(m) ?? 0;
      const p = priceByMin.get(m);
      const refPx = p ?? base;
      const prevP = (() => {
        const idx = FULL_DAY_LABELS.indexOf(m);
        for (let k = idx - 1; k >= 0; k--) {
          const pp = priceByMin.get(FULL_DAY_LABELS[k]);
          if (pp != null) return pp;
        }
        return base;
      })();
      const col = refPx >= prevP ? "#ef5350" : "#26a69a";
      return { value: v, itemStyle: { color: v > 0 ? col : "transparent" } };
    });
  } else {
    xLabels = bars.map(b => toHHMM(b.date));
    let cumAmt = 0, cumVol = 0;
    priceSeries = bars.map(b => b.close);
    vwapSeries = bars.map(b => {
      const avgPx = (b.open + b.high + b.low + b.close) / 4;
      cumAmt += avgPx * b.volume;
      cumVol += b.volume;
      return cumVol > 0 ? cumAmt / cumVol : null;
    });
    volSeries = bars.map((b, i) => {
      const prev = i > 0 ? bars[i - 1].close : b.open;
      const col = b.close >= prev ? "#ef5350" : "#26a69a";
      return { value: b.volume, itemStyle: { color: col } };
    });
  }

  // 0 轴 (昨收) 参考线 (涨跌幅%)
  const yMin = Math.min(
    ...priceSeries.filter((v): v is number => v != null),
    ...vwapSeries.filter((v): v is number => v != null),
  );
  const yMax = Math.max(
    ...priceSeries.filter((v): v is number => v != null),
    ...vwapSeries.filter((v): v is number => v != null),
  );
  const yLo = yMin - (yMax - yMin) * 0.15 - 0.01;
  const yHi = yMax + (yMax - yMin) * 0.15 + 0.01;

  // Markers: fullDayAxis (分时) 用 bars 真实时间戳映射槽位, kline 走双轨 (exact/day)
  let markPoints: any[];
  if (fullDayAxis) {
    // 把 marker 日期 (精确/日级) → 全日 240 格分钟槽 index
    const idxExact = new Map<string, number>();
    const idxDay = new Map<string, number>();
    bars.forEach(b => {
      const slotIdx = minuteToIndex(toHHMM(b.date));
      if (slotIdx < 0) return;
      idxExact.set(normDate(b.date), slotIdx);
      idxDay.set(normDateDay(b.date), slotIdx); // 同天保留最后一个
    });
    markPoints = markers
      .map(mk => {
        const x = idxExact.get(normDate(mk.date)) ?? idxDay.get(normDateDay(mk.date));
        if (x == null) return null;
        const lots = mk.qty ? Math.round(mk.qty / 100) + "手" : "";
        const hasPnl = mk.action === "卖出" && mk.pnl != null;
        const pnlVal = hasPnl ? (mk.pnl! >= 0 ? "+" : "") + fmtMoney(mk.pnl!) : "";
        const pnlColor = hasPnl ? (mk.pnl! >= 0 ? "#26a69a" : "#ef5350") : "#fff";
        const txt = (mk.action === "买入" ? "买" : "卖") + (lots ? " " + lots : "") + (pnlVal ? `\n{pnl|${pnlVal}}` : "");
        return {
          coord: [x, mk.price],
          symbol: mk.action === "买入" ? "triangle" : "pin",
          symbolSize: 18,
          symbolKeepAspect: true,
          itemStyle: { color: mk.action === "买入" ? "#26a69a" : (hasPnl && mk.pnl! < 0 ? "#ef5350" : "#ff9800") },
          label: {
            show: true, formatter: txt,
            position: mk.action === "买入" ? "bottom" : "top",
            distance: 6, color: "#fff", fontSize: 10, lineHeight: 14,
            backgroundColor: "rgba(15,15,15,0.85)", padding: [2, 4], borderRadius: 3,
            rich: { pnl: { color: pnlColor, fontSize: 12, fontWeight: "bold" as const } },
          },
        } as any;
      })
      .filter(Boolean) as any[];
  } else {
    const indexes = buildDateIdx(xLabels.map((_l, i) => bars[i]?.date ?? _l));
    markPoints = buildMarkPoints(markers, indexes);
  }

  const pctFmt = (v: number) => fmtPct((v - base) / base);

  chart.setOption({
    backgroundColor: "#0f0f0f",
    title: {
      text: `${name}  分时  VWAP${base ? `  · 昨收 ${fmtPrice(base)}` : ""}${markers.length ? `  · ${markers.length}信号` : ""}`,
      left: 12, top: 8,
      textStyle: { color: "#e0e0e0", fontSize: 14 },
    },
    animation: false,
    tooltip: {
      trigger: "axis",
      backgroundColor: "rgba(20,20,20,0.95)",
      borderColor: "#333",
      textStyle: { color: "#e0e0e0" },
      axisPointer: { type: "cross", lineStyle: { color: "#555" } },
      formatter: (params: any) => {
        if (!Array.isArray(params) || !params.length) return "";
        const x = params[0].axisValue;
        let out = `<div style="font-size:11px;color:#888">${x}</div>`;
        params.forEach((p: any) => {
          if (p.seriesName === "成交量") {
            const v = typeof p.data === "object" ? p.data.value : p.data;
            out += `<div>${p.marker}${p.seriesName}: ${fmtNum(Number(v) / 10000)}万</div>`;
          } else if (p.value != null) {
            const pct = pctFmt(Number(p.value));
            const clr = Number(p.value) >= base ? "#ef5350" : "#26a69a";
            out += `<div>${p.marker}${p.seriesName}: <b>${fmtPrice(Number(p.value))}</b> <span style="color:${clr}">${pct}</span></div>`;
          }
        });
        return out;
      },
    },
    legend: {
      data: ["分时线", "均价"], top: 8, right: 12,
      textStyle: { color: "#888" },
    },
    grid: [
      { left: 55, right: 55, top: 50, height: "58%" },
      { left: 55, right: 55, top: "72%", height: "16%" },
    ],
    dataZoom: [
      {
        type: "inside", xAxisIndex: [0, 1],
        start: 0, end: 100,
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
        moveOnMouseWheel: false,
        preventDefaultMouseMove: true,
      },
      {
        type: "slider", xAxisIndex: [0, 1],
        start: 0, end: 100,
        bottom: 4,
        height: 16,
        borderColor: "#333",
        fillerColor: "rgba(38,166,154,0.15)",
        handleStyle: { color: "#26a69a" },
        textStyle: { color: "#888", fontSize: 10 },
        showDataShadow: false,
      },
    ],
    xAxis: [
      {
        type: "category", data: xLabels, gridIndex: 0,
        boundaryGap: false,
        axisLine: { lineStyle: { color: "#333" } },
        axisLabel: {
          color: "#888", fontSize: 10,
          // 每 30 分钟显示一个标签
          interval: (index: number, v: string) => {
            const mm = v.slice(-2);
            return mm === "00" || mm === "30";
          },
        },
        splitLine: { show: true, lineStyle: { color: "#1a1a1a", type: "dashed" } },
        axisTick: { show: false },
      },
      {
        type: "category", gridIndex: 1,
        axisLine: { lineStyle: { color: "#333" } },
        axisLabel: { show: false },
        splitLine: { show: false },
        axisTick: { show: false },
      },
    ],
    yAxis: [
      {
        type: "value", gridIndex: 0,
        min: yLo, max: yHi,
        splitLine: { lineStyle: { color: "#1f1f1f" } },
        axisLabel: {
          color: "#888", fontSize: 10,
          formatter: (v: number) => fmtPrice(v),
        },
        // 0% (昨收) 轴标记
        splitArea: { show: false },
      },
      {
        // 右轴: 涨跌幅 %
        type: "value", gridIndex: 0, position: "right",
        min: (yLo - base) / base * 100,
        max: (yHi - base) / base * 100,
        splitLine: { show: false },
        axisLabel: {
          color: (v: number) => v > 0 ? "#ef5350" : v < 0 ? "#26a69a" : "#888",
          fontSize: 10,
          // 统一两位小数, 避免 1/0.5/0.75 等宽度不一
          formatter: (v: number) => (v >= 0 ? "+" : "") + v.toFixed(2) + "%",
        },
        axisLine: { show: false },
        axisTick: { show: false },
      },
      {
        type: "value", gridIndex: 1,
        splitLine: { lineStyle: { color: "#1f1f1f" } },
        axisLabel: { color: "#888", fontSize: 10,
          formatter: (v: number) => (v / 10000).toFixed(0) + "万" },
      },
    ],
    series: [
      {
        name: "分时线", type: "line", xAxisIndex: 0, yAxisIndex: 0,
        data: priceSeries,
        smooth: false,
        symbol: "none",
        connectNulls: true, // 午休 11:30-13:01 期间后端无 11:30 bar, 连接前后有效点避免断线
        lineStyle: { color: "#ffffff", width: 1 },
        // 分时线下面渐变色区域
        areaStyle: {
          color: new (echarts.graphic as any).LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: "rgba(255,255,255,0.25)" },
            { offset: 1, color: "rgba(255,255,255,0.02)" },
          ]),
        },
        markLine: {
          symbol: ["none", "none"],
          silent: true,
          lineStyle: { color: "#555", type: "dashed", width: 1 },
          label: { color: "#888", fontSize: 10, formatter: fmtPrice(base), position: "insideEndTop" },
          data: [{ yAxis: base }],
        },
        markPoint: { data: markPoints, symbolKeepAspect: true, animation: false },
      },
      {
        name: "均价", type: "line", xAxisIndex: 0, yAxisIndex: 0,
        data: vwapSeries,
        smooth: false,
        symbol: "none",
        connectNulls: true, // 午休期间均价不变, 连接前后有效点避免断线
        lineStyle: { color: "#ffd700", width: 1 },
      },
      {
        name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 2,
        data: volSeries,
      },
    ],
  } as any, true);
}
