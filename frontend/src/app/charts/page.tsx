"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { api } from "@/lib/api";
import { MockStreamWs, marketWs } from "@/lib/ws";
import KLineChart, {
  type KBar, type KMarker,
  fmtMoney, fmtPrice, fmtPct, fmtNum,
} from "@/components/KLineChart";
import ParamForm, { type ParamSchema } from "@/components/ParamForm";
import SymbolPicker from "@/components/SymbolPicker";

type StratInfo = { name: string; title: string; [k: string]: any };

type ChartOrder = {
  side: "buy" | "sell";
  qty?: number;
  price?: number;
  avg_fill_price?: number;
  status?: string;
  pnl?: number;
  ts?: string;
  order_id?: string;
  symbol?: string;
  filled_qty?: number;
  error?: string;
};

type ChartSession = {
  id: string;
  symbol: string;
  tf: string;
  strategy: string;
  params: Record<string, number>;
  schema: ParamSchema;
  liveMode: boolean;        // ✅ true=真实同花顺下单, false=纸面模拟(SimulatedBroker). 替代旧 mode/dryRun 双字段
  bars: KBar[];
  markers: KMarker[];
  orders: ChartOrder[];
  snapshot: any;
  running: boolean;
  auto: boolean;
  logs: string[];
  streaming: boolean;       // 流式测试模式 (mock 数据逐根推进)
  speed: string;            // 流式速度: 1x/2x/5x/10x/20x
  preClose?: number;        // 分时图用: 昨收价
};

const TFS = ["分时", "day", "5m", "15m", "30m", "1h", "1w"];
const INTRADAY_TF = "分时";  // 分时 (intraday) 周期标识
const COMMON_SYMBOLS = ["sz159915", "sh510300", "sh510050", "sz159919"];

// 元数据不存 bars/markers/orders/snapshot... 持久化到后端 SQLite (跨浏览器/设备)
// 注意: 新版只存 liveMode 一个布尔, 不再存 mode/dryRun. 读取时做向前兼容迁移.
type ChartMeta = Omit<ChartSession, "bars" | "markers" | "orders" | "snapshot" | "running" | "auto" | "logs">;

function toMeta(c: ChartSession): ChartMeta {
  return {
    id: c.id, symbol: c.symbol, tf: c.tf, strategy: c.strategy,
    params: c.params, schema: c.schema, liveMode: c.liveMode,
    streaming: c.streaming, speed: c.speed,
  };
}

// 旧版本 ChartMeta (mode+dryRun) → 新版本 liveMode 的迁移规则
// 语义: mode==="live" && !dryRun → liveMode=true, 其它情况一律 paper (false)
function migrateMeta(m: any): ChartMeta {
  if (typeof m.liveMode === "boolean") return m as ChartMeta;
  const live = (m.mode === "live") && (m.dryRun === false);
  return { ...m, liveMode: live } as ChartMeta;
}

// 运行日志工具: 给指定 session 追加一条日志, 格式 HH:MM:SS 内容
// 保留最近 50 条 (防无限增长)
const MAX_LOGS = 50;
function appendLog(prev: string[], msg: string): string[] {
  const now = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  const line = `${now}  ${msg}`;
  const next = [...prev, line];
  return next.length > MAX_LOGS ? next.slice(-MAX_LOGS) : next;
}

// markers -> orders: 回测的 vbt markers 转成最近订单格式 (orders 面板渲染)
function markersToOrders(markers: KMarker[]): ChartOrder[] {
  return markers.map(m => ({
    side: (m.action === "买入" ? "buy" : "sell") as "buy" | "sell",
    qty: m.qty ?? 0,
    avg_fill_price: m.price,
    price: m.price,
    status: "filled",
    pnl: m.pnl ?? undefined,
    ts: m.date?.replace("T", " ").slice(0, 16),
  }));
}

async function loadChartsFromServer(): Promise<ChartSession[]> {
  let { sessions } = await api.chartSessions();
  // 迁移旧 localStorage 数据: 后端为空时, 若本机仍留有旧的 localStorage 图则导入一次
  if (!sessions.length) {
    try {
      const raw = localStorage.getItem("quantui_charts_v1");
      if (raw) {
        const legacy: any[] = JSON.parse(raw);
        if (legacy.length) {
          const migrated = legacy.map(migrateMeta);
          api.saveChartSessions(migrated).catch(() => {});
          sessions = migrated;
        }
      }
    } catch { /* 无旧数据或解析失败 */ }
  }
  const out: ChartSession[] = [];
  for (const raw of sessions) {
    try {
      const m = migrateMeta(raw);
      if (m.tf === INTRADAY_TF) {
        const r = await api.intraday(m.symbol);
        const bars = (r.bars || []).map((b: any) => ({
          date: b.time, open: b.open, high: b.high, low: b.low,
          close: b.close, volume: b.volume ?? b.volume_lots ?? 0,
        }));
        out.push({
          ...m, bars, markers: [], orders: [],
          snapshot: null, running: false, auto: false,
          logs: ["已恢复 (分时)"],
          streaming: false, preClose: r.pre_close,
        });
      } else {
        const kdata = await api.kline(m.symbol, m.tf);
        out.push({
          ...m, bars: kdata.bars || [], markers: [], orders: [],
          snapshot: null, running: false, auto: false,
          logs: [`已恢复 (${m.tf})`],
          streaming: false,
        });
      }
    } catch { /* 跳过拉取失败的图 */ }
  }
  return out;
}

export default function ChartsPage() {
  const [strategies, setStrategies] = useState<StratInfo[]>([]);
  const [charts, setCharts] = useState<ChartSession[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);

  // 新建图表单 — 只需股票代码和周期, 策略在图创建后加载
  const [showNew, setShowNew] = useState(true);
  const [fSymbol, setFSymbol] = useState("sz159915");
  const [fTf, setFTf] = useState("day");
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // 标的代码格式校验: 支持A股(sh/sz+6位 / 纯6位)、期货(主连 au0/IF0 + 合约 au2612/sr609)、
  // 期权(8位数字 100*/900*)、基金(*.of)、外盘(@xxx / xxx.xx)
  const isValidSymbol = (s: string): boolean => {
    const v = s.trim();
    if (!v) return false;
    return /^(sh|sz|SH|SZ)?\d{6}$/.test(v)        // A股/ETF/指数
      || /^[a-zA-Z]{1,2}(?:0|\d{3,4})$/.test(v)     // 期货 主连 au0/IF0 + 合约 au2612/sr609
      || /^\d{8}$/.test(v)                         // 期权 1000xxxx / 900xxxx
      || /^@[\w.]+$/.test(v)                       // 外盘 @xxx
      || /^\w+\.\w{2,3}$/.test(v);                 // 基金 xxx.of / 外盘 xxx.hk
  };

  useEffect(() => {
    api.strategies().then(setStrategies).catch(console.error);
    // 从后端 SQLite 恢复图列表 (跨浏览器/设备)
    loadChartsFromServer().then(restored => {
      if (restored.length) {
        setCharts(restored);
        setActiveId(restored[restored.length - 1].id);
        setShowNew(false);
      }
    });
  }, []);

  // charts 元数据变化时防抖持久化到后端 SQLite (跨浏览器/设备共享)
  useEffect(() => {
    const metas: ChartMeta[] = charts.map(toMeta);
    const h = setTimeout(() => {
      api.saveChartSessions(metas).catch((e) =>
        console.error("保存图表失败:", e?.message ?? e));
    }, 800);
    return () => clearTimeout(h);
  }, [charts]);

  // 在活跃图上切换策略时加载参数 schema
  const loadStrategy = useCallback(async (chartId: string, strategyName: string) => {
    if (!strategyName) {
      setCharts((p) => p.map((c) => c.id === chartId ? { ...c, strategy: "", schema: {}, params: {}, markers: [], logs: appendLog(c.logs, "未挂载策略") } : c));
      return;
    }
    try {
      const s = await api.strategySchema(strategyName);
      const init: Record<string, number> = {};
      Object.entries(s.params || {}).forEach(([k, spec]: any) => {
        if (spec.type !== "const") init[k] = spec.default ?? 0;
      });
      setCharts((p) => p.map((c) => c.id === chartId ? {
        ...c, strategy: strategyName, schema: s.params || {}, params: init,
        markers: [], logs: appendLog(c.logs, `已加载策略: ${strategyName}`),
      } : c));
    } catch (e: any) {
      setCharts((p) => p.map((c) => c.id === chartId ? { ...c, logs: appendLog(c.logs, "加载策略失败: " + (e?.message || String(e))) } : c));
    }
  }, []);

  const handleCreate = async () => {
    const sym = fSymbol.trim();
    if (!sym) {
      setFormError("请输入标的代码，例如 sz159915 或 sh510300");
      return;
    }
    if (!isValidSymbol(sym)) {
      setFormError(`「${sym}」不像有效的标的代码，请检查后重试。支持：sh/sz+6位数字、6位数字、期货(rb2510)、期权(8位数字)等`);
      return;
    }
    setFormError(null);
    setCreating(true);
    try {
      const kdata = await api.kline(sym, fTf);
      if (!kdata.bars || kdata.bars.length === 0) {
        setFormError(`未查到「${sym}」的K线数据，请确认代码是否正确`);
        return;
      }
      const id = `chart_${Date.now()}`;
      const initLogs = appendLog([], `新建图: ${sym} @ ${fTf}（请选择策略）`);
      const session: ChartSession = {
        id, symbol: sym, tf: fTf, strategy: "",
        params: {}, schema: {}, liveMode: false,
        bars: kdata.bars || [], markers: [], orders: [], snapshot: null,
        running: false, auto: false, logs: initLogs,
        streaming: false, speed: "5x",
      };
      setCharts((p) => [...p, session]);
      setActiveId(id);
      setShowNew(false);
    } catch (e: any) {
      // 友善提示: 截断后端 traceback, 只展示首段关键信息
      const raw = e?.message || String(e);
      const firstLine = raw.split("\n")[0].slice(0, 120);
      setFormError(`查询「${sym}」失败：${firstLine}`);
    } finally {
      setCreating(false);
    }
  };

  // 周期切换: 分时 -> intraday API (1m bars + pre_close + 预占全日轴), 其他 -> kline API
  const switchTf = async (id: string, tf: string) => {
    const s = charts.find(c => c.id === id);
    if (!s || s.tf === tf) return;
    setCharts(p => p.map(c => c.id === id ? { ...c, tf, bars: [], markers: [], preClose: undefined, logs: appendLog(c.logs, `切换 ${tf}...`) } : c));
    try {
      if (tf === INTRADAY_TF) {
        const r = await api.intraday(s.symbol);
        const bars = (r.bars || []).map((b: any) => ({
          date: b.time, open: b.open, high: b.high, low: b.low,
          close: b.close, volume: b.volume ?? b.volume_lots ?? 0,
        }));
        setCharts(p => p.map(c => c.id === id ? {
          ...c, bars, preClose: r.pre_close, logs: appendLog(c.logs, `分时已加载 ${bars.length} 分钟`)
        } : c));
      } else {
        const kdata = await api.kline(s.symbol, tf);
        setCharts(p => p.map(c => c.id === id ? {
          ...c, bars: kdata.bars || [], logs: appendLog(c.logs, `已切换 ${tf}`)
        } : c));
      }
    } catch (e: any) {
      setCharts(p => p.map(c => c.id === id ? { ...c, logs: appendLog(c.logs, "切换失败: " + (e?.message || String(e))) } : c));
    }
  };

  // 流式测试 WS 实例池 (per chart id)
  const streamWsRef = useRef<Record<string, MockStreamWs>>({});
  // 流式启动前"回测/挂载后"的原始视图快照, 停止流式时还原
  const preStreamStateRef = useRef<Record<string, Pick<ChartSession,
    "bars"|"markers"|"orders"|"snapshot"|"preClose"|"logs">>>({});

  // 同花顺真实账户: 持仓 + 资产信息 (进页时拉, 30s 周期刷新; 出错只记 error 不中断)
  const [thsPositions, setThsPositions] = useState<{
    ok: boolean; columns: string[]; rows: any[][]; account?: string;
    error?: string; loading?: boolean; ts?: string;
  }>({ ok: false, columns: [], rows: [] });

  const refreshThsPositions = useCallback(async () => {
    setThsPositions(p => ({ ...p, loading: true }));
    try {
      const r = await api.positions() as any;
      if (!r || !r.ok) {
        setThsPositions({
          ok: false, columns: [], rows: [],
          error: (r?.error || r?.stderr || "查询失败") as string,
          ts: new Date().toLocaleTimeString("zh-CN"),
        });
      } else {
        setThsPositions({
          ok: true,
          columns: (r.columns || []) as string[],
          rows: (r.rows || []) as any[][],
          account: (r.account || undefined) as string | undefined,
          ts: new Date().toLocaleTimeString("zh-CN"),
        });
      }
    } catch (e: any) {
      setThsPositions({
        ok: false, columns: [], rows: [],
        error: e?.message || String(e),
        ts: new Date().toLocaleTimeString("zh-CN"),
      });
    }
  }, []);

  // 进入页面 + 每 30s 刷新 THS 持仓 (慢路径, 不阻塞主渲染)
  useEffect(() => {
    refreshThsPositions();
    const h = setInterval(refreshThsPositions, 30_000);
    return () => clearInterval(h);
  }, [refreshThsPositions]);
  const toggleStream = (id: string) => {
    const s = charts.find(c => c.id === id);
    if (!s) return;
    if (s.streaming) {
      // 停止: 还原启动流式前的 bars/markers/orders/snapshot → 返回上一个视图
      streamWsRef.current[id]?.stop();
      delete streamWsRef.current[id];
      const pre = preStreamStateRef.current[id];
      if (pre) {
        setCharts(p => p.map(c => c.id === id ? {
          ...c, streaming: false,
          bars: pre.bars, markers: pre.markers, orders: pre.orders,
          snapshot: pre.snapshot, preClose: pre.preClose,
          logs: appendLog(c.logs, "流式已结束, 已恢复回测视图"),
        } : c));
        delete preStreamStateRef.current[id];
      } else {
        setCharts(p => p.map(c => c.id === id ? { ...c, streaming: false, logs: appendLog(c.logs, "流式已停止") } : c));
      }
    } else {
      // 启动: 保存当前视图快照, 然后清空 bars/markers 开始流式
      preStreamStateRef.current[id] = {
        bars: s.bars, markers: s.markers, orders: s.orders,
        snapshot: s.snapshot, preClose: s.preClose, logs: s.logs,
      };
      setCharts(p => p.map(c => c.id === id ? {
        ...c, streaming: true, bars: [], markers: [], orders: [],
        snapshot: null, logs: appendLog(c.logs, `流式测试中 (${s.speed})...`),
      } : c));
      const ws = new MockStreamWs(
        (msg) => {
          if (msg.type === "bar" && msg.bar) {
            setCharts(prev => prev.map(c => {
              if (c.id !== id) return c;
              const newBars = [...c.bars, msg.bar!];
              const newMarkers = msg.markers
                ? [...c.markers, ...msg.markers.map(m => ({
                    date: m.date, price: m.price, action: m.action,
                    qty: m.qty, pnl: m.pnl ?? undefined,
                  }))]
                : c.markers;
              // 合并 orders: 如果推送的 orders 带 pnl/ts 也带上
              const nextOrders = msg.orders?.length
                ? [...c.orders, ...msg.orders.map(o => ({
                    ...o,
                    ts: o.ts ?? msg.bar!.date,
                  }))]
                : c.orders;
              const logMsg = msg.error
                ? `信号错误: ${msg.error}`
                : (msg.msg || `bar ${msg.bar!.date.slice(11, 16)} C=${msg.bar!.close} target=${msg.target ?? "-"}`);
              return {
                ...c, bars: newBars, markers: newMarkers,
                orders: nextOrders,
                snapshot: msg.snapshot || c.snapshot,
                logs: appendLog(c.logs, logMsg),
              };
            }));
          } else if (msg.type === "info") {
            // info 可能带 pre_close (流式启动时的 init msg), 也可能是跨日提示
            setCharts(prev => prev.map(c => {
              if (c.id !== id) return c;
              return {
                ...c,
                preClose: msg.pre_close != null ? msg.pre_close : c.preClose,
                logs: appendLog(c.logs, msg.msg || "流式消息"),
              };
            }));
          } else if (msg.type === "error") {
            setCharts(p => p.map(c => c.id === id ? { ...c, streaming: false, logs: appendLog(c.logs, `错误: ${msg.msg}`) } : c));
          }
        },
        () => {
          setCharts(p => p.map(c => c.id === id ? { ...c, streaming: false, logs: appendLog(c.logs, "流式连接断开") } : c));
        }
      );
      streamWsRef.current[id] = ws;
      ws.start(s.symbol, s.strategy, s.tf, s.speed, s.params);
    }
  };

  const changeSpeed = (id: string, speed: string) => {
    const s = charts.find(c => c.id === id);
    if (!s) return;
    setCharts(p => p.map(c => c.id === id ? { ...c, speed } : c));
    // 如果正在流式, 重启以应用新速度
    if (s.streaming) {
      streamWsRef.current[id]?.stop();
      setTimeout(() => {
        setCharts(prev => prev.map(c => c.id === id ? { ...c, speed } : c));
        const updated = charts.find(c => c.id === id);
        if (updated) {
          // 重启需重新取最新 state (speed 已更新)
          const ws = new MockStreamWs(
            (msg) => {
              if (msg.type === "bar" && msg.bar) {
                setCharts(prev => prev.map(c => {
                  if (c.id !== id) return c;
                  return {
                    ...c, bars: [...c.bars, msg.bar!],
                    markers: msg.markers ? [...c.markers, ...msg.markers] : c.markers,
                    orders: msg.orders ? [...c.orders, ...msg.orders] : c.orders,
                    snapshot: msg.snapshot || c.snapshot,
                    logs: appendLog(c.logs, msg.msg || `bar ${msg.bar!.date.slice(11, 16)} C=${msg.bar!.close}`),
                  };
                }));
              }
            },
            () => setCharts(p => p.map(c => c.id === id ? { ...c, streaming: false } : c))
          );
          streamWsRef.current[id] = ws;
          ws.start(updated.symbol, updated.strategy, updated.tf, speed, updated.params);
        }
      }, 100);
    }
  };

  // 回测 = vectorbt 全历史回测 -> 在K线上画所有买卖点 + 盈亏标注
  const handleMount = async (id: string) => {
    const session = charts.find(c => c.id === id);
    if (!session) return;
    if (!session.strategy) {
      setCharts(p => p.map(c => c.id === id ? { ...c, logs: appendLog(c.logs, "请先选择策略再回测") } : c));
      return;
    }
    setCharts(p => p.map(c => c.id === id ? { ...c, running: true, logs: appendLog(c.logs, "回测中...") } : c));
    try {
      const res = await api.backtest({
        strategy: session.strategy, symbol: session.symbol,
        params: session.params, tf: session.tf, cash: 100000,
      });
      const r = res[session.symbol];
      if (!r) throw new Error("回测无结果");
      const newOrders = markersToOrders(r.markers || []);
      setCharts(p => p.map(c => c.id === id ? {
        ...c, running: false,
        markers: r.markers || [],
        orders: newOrders,
        snapshot: { stats: r.stats, equity_tail: (r.equity || []).slice(-5) },
        logs: appendLog(c.logs, `回测完成: ${(r.markers||[]).length}信号, 总收益 ${fmtPct(r.stats?.total_return ?? 0)}`),
      } : c));
    } catch (e: any) {
      setCharts(p => p.map(c => c.id === id ? { ...c, running: false, logs: appendLog(c.logs, "回测失败: " + (e?.message || String(e))) } : c));
    }
  };

  const handleRunOnce = useCallback(async (id: string) => {
    const session = charts.find((c) => c.id === id);
    if (!session || session.running) return;
    if (!session.strategy) {
      setCharts((p) => p.map((c) => c.id === id ? { ...c, logs: appendLog(c.logs, "请先选择策略") } : c));
      return;
    }
    // ⚡ 语义按大王要求: 实盘开关 ON = 真实同花顺下单 (mode=live + dry_run=false)
    //              OFF = 纸面模拟, 只用 SimulatedBroker (mode=paper)
    const mode = session.liveMode ? "live" : "paper";
    const dry_run = session.liveMode ? false : false;   // paper 模式 dry_run 无意义, 一律 false
    setCharts((p) => p.map((c) => (c.id === id ? { ...c, running: true } : c)));
    try {
      const res = await api.chartRun({
        strategy: session.strategy,
        symbol: session.symbol,
        params: session.params,
        mode,
        dry_run,
        cash_per_symbol: 10000,
      });
      const barDate = res.bar_date || "";
      const newMarkers: KMarker[] = (res.orders || [])
        .filter((o: any) => o.status === "filled")
        .map((o: any) => ({
          date: barDate,
          price: o.avg_fill_price,
          action: o.side === "buy" ? "买入" : "卖出",
        }));
      setCharts((p) =>
        p.map((c) =>
          c.id === id
            ? {
                ...c,
                running: false,
                orders: res.orders || [],
                snapshot: res.snapshot,
                // markers 去重 (按 date+price+action)
                markers: dedupMarkers([...c.markers, ...newMarkers]),
                logs: appendLog(c.logs, `${session.liveMode ? "⚡[实盘]" : "🧻[纸面]"} ` + (res.msg || res.error || "完成")),
              }
            : c
        )
      );
    } catch (e: any) {
      setCharts((p) =>
        p.map((c) =>
          c.id === id ? { ...c, running: false, logs: appendLog(c.logs, "错误: " + (e?.message || String(e))) } : c
        )
      );
    }
  }, [charts]);

  // 自动运行: 每个 auto session 定时跑一轮
  const autoRef = useRef<any>(null);
  useEffect(() => {
    const hasAuto = charts.some((c) => c.auto && !c.running);
    if (hasAuto && !autoRef.current) {
      autoRef.current = setInterval(() => {
        charts.filter((c) => c.auto && !c.running).forEach((c) => handleRunOnce(c.id));
      }, 6000);
    } else if (!hasAuto && autoRef.current) {
      clearInterval(autoRef.current);
      autoRef.current = null;
    }
    return () => {};
  }, [charts, handleRunOnce]);

  const toggleAuto = (id: string) => {
    setCharts((p) => p.map((c) => (c.id === id ? { ...c, auto: !c.auto } : c)));
  };

  const closeChart = (id: string) => {
    streamWsRef.current[id]?.stop();
    delete streamWsRef.current[id];
    setCharts((p) => p.filter((c) => c.id !== id));
    if (activeId === id) {
      const rest = charts.filter((c) => c.id !== id);
      setActiveId(rest.length ? rest[rest.length - 1].id : null);
    }
  };

  const active = charts.find((c) => c.id === activeId);

  // 分时模式 WebSocket tick 实时更新: 后端每秒推 tick, 更新最后一根 bar close + 追加新分钟 bar
  useEffect(() => {
    if (!active || active.tf !== INTRADAY_TF || active.streaming || !marketWs) return;
    const id = active.id;
    const sym = active.symbol;
    const ws = marketWs;
    ws.subscribe(sym);
    const unsub = ws.onMessage((msg) => {
      if (msg.type !== "tick" || msg.symbol !== sym) return;
      const tickBars = msg.bars || [];
      const lastTick = tickBars[tickBars.length - 1];
      if (!lastTick) return;
      setCharts(prev => prev.map(c => {
        if (c.id !== id) return c;
        const snap = msg.snapshot;
        let newBars = [...c.bars];
        if (newBars.length > 0) {
          const lastIdx = newBars.length - 1;
          const last = newBars[lastIdx];
          const lastMin = last.date.slice(11, 16);   // HH:MM
          const tickMin = lastTick.time.slice(11, 16);
          if (lastMin === tickMin) {
            // 同分钟: 用新对象替换 (React 需要新引用)
            newBars[lastIdx] = {
              ...last,
              close: snap.last,
              high: Math.max(last.high, snap.last),
              low: Math.min(last.low, snap.last),
            };
          } else {
            // 新分钟: 追加新 bar
            newBars.push({
              date: lastTick.time,
              open: snap.last, high: snap.last, low: snap.last,
              close: snap.last, volume: 0,
            });
          }
        }
        return {
          ...c,
          bars: newBars,
          preClose: snap.pre_close ?? c.preClose,
          // ⚠️ 注意: 每秒 WebSocket tick 不要写运行日志!
          // 用户操作的回测/下单结果会被每秒的 tick 日志淹没 (例如刚显示"回测完成"
          // 就立刻被 "15:00 3.335 -2.26%" 覆盖). 只在真正事件发生时 appendLog.
        };
      }));
    });
    return () => { unsub(); ws.unsubscribe(sym); };
  }, [active?.id, active?.tf, active?.streaming]);

  return (
    <div className="flex flex-col h-[calc(100vh-72px)] gap-2">
      {/* 顶部紧凑工具栏：标题 + 新建按钮 + 图 tabs（单行） */}
      <div className="flex items-center gap-2 flex-wrap shrink-0">
        <h1 className="text-lg font-bold">📈 图会话</h1>
        <button
          onClick={() => setShowNew((v) => !v)}
          className="px-2.5 py-1 bg-[#26a69a] text-white text-xs rounded hover:bg-[#1e8e6f]"
        >
          ➕ 新建
        </button>
        {charts.map((c) => (
          <button
            key={c.id}
            onClick={() => setActiveId(c.id)}
            className={`px-2.5 py-1 text-xs rounded border ${
              activeId === c.id
                ? "bg-[#1e8e6f]/30 border-[#26a69a] text-[#26a69a]"
                : "bg-[#1a1a1a] border-[#2a2a2a] text-[#888] hover:text-[#e0e0e0]"
            }`}
          >
            {c.symbol}·{c.tf} {c.auto && "🔄"}
            <span
              onClick={(e) => { e.stopPropagation(); closeChart(c.id); }}
              className="ml-1.5 text-[#666] hover:text-[#ef5350]"
            >×</span>
          </button>
        ))}
      </div>

      {/* 新建表单：单行紧凑，不占常驻垂直空间 */}
      {showNew && (
        <div className="border border-[#2a2a2a] rounded-lg bg-[#141414] p-2 shrink-0">
          <div className="flex items-end gap-2">
            <div className="flex flex-col gap-0.5">
              <label className="text-[10px] text-[#666]">标的</label>
              <SymbolPicker
                value={fSymbol}
                onChange={(v) => { setFSymbol(v); setFormError(null); }}
                onKeyDown={(e) => { if (e.key === "Enter") handleCreate(); }}
                placeholder="如 sz159915"
                width="w-32"
                invalid={!!formError}
                extraSymbols={COMMON_SYMBOLS}
                inputClassName={`bg-[#1a1a1a] border rounded px-2 py-1 text-xs text-[#e0e0e0] outline-none w-32 focus:border-[#4fc3f7] ${
                  formError ? "border-[#ef5350]" : "border-[#2a2a2a]"
                }`}
              />
            </div>
            <div className="flex flex-col gap-0.5">
              <label className="text-[10px] text-[#666]">周期</label>
              <select
                value={fTf} onChange={(e) => setFTf(e.target.value)}
                className="bg-[#1a1a1a] border border-[#2a2a2a] rounded px-2 py-1 text-xs text-[#e0e0e0] focus:border-[#4fc3f7] outline-none"
              >
                {TFS.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <button
              onClick={handleCreate}
              disabled={!fSymbol || creating}
              className="px-3 py-1 bg-[#26a69a] text-white text-xs rounded hover:bg-[#1e8e6f] disabled:opacity-50"
            >
              {creating ? "创建中..." : "创建"}
            </button>
            <span className="text-[10px] text-[#555] pb-1">创建后加载策略</span>
          </div>
          {formError && (
            <div className="mt-1.5 text-[11px] text-[#ef5350] leading-tight whitespace-pre-wrap">
              ⚠️ {formError}
            </div>
          )}
        </div>
      )}

      {/* 活跃图：flex-1 占满剩余空间 */}
      {active ? (
        <div className="flex flex-col flex-1 min-h-0 border border-[#2a2a2a] rounded-lg bg-[#141414]">
          {/* 操作工具栏：标的+策略+周期切换+操作按钮（单行） */}
          <div className="flex items-center gap-2 flex-wrap shrink-0 px-3 py-2 border-b border-[#2a2a2a]">
            <span className="font-semibold text-sm text-[#e0e0e0]">
              {active.symbol}
            </span>
            <div className="h-5 w-px bg-[#333]" />
            {/* ============== 置顶 ⚡ 实盘 开关 ============== */}
            <label
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded border text-xs transition ${
                active.liveMode
                  ? "bg-[#7f1d1d]/40 border-[#ef5350] text-[#ef5350] font-semibold animate-pulse"
                  : "bg-[#1a1a1a] border-[#333] text-[#888]"
              }`}
              title={active.liveMode
                ? "已开启实盘 → 点「扫描」或「自动」时会直接通过同花顺下真实单 (非 dry-run)！"
                : "纸面模拟 → 只记账，不操作同花顺真实账户"}
            >
              <input
                type="checkbox"
                className="accent-[#ef5350] w-3.5 h-3.5"
                checked={active.liveMode}
                onChange={(e) => {
                  const v = e.target.checked;
                  const warnMsg = "⚠️ 开启实盘：后续「扫描」「自动」将直接通过同花顺下真实订单，没有 dry-run 兜底。\n确认开启？";
                  if (v && !confirm(warnMsg)) return;
                  setCharts((p) => p.map((c) => c.id === active.id
                    ? {
                        ...c,
                        liveMode: v,
                        logs: appendLog(c.logs, v
                          ? "⚠️ 已开启【实盘】模式，扫描/自动将真实下单"
                          : "🧻 已切回纸面模拟"),
                      }
                    : c));
                }}
              />
              ⚡ 实盘
              <span className="ml-1 text-[9px] opacity-80">
                {active.liveMode ? "真实交易 · 不可撤销" : "纸面模拟"}
              </span>
            </label>
            <div className="h-5 w-px bg-[#333]" />
            <div className="flex items-center gap-1">
              <label className="text-[10px] text-[#666]">策略</label>
              <select
                value={active.strategy}
                onChange={(e) => loadStrategy(active.id, e.target.value)}
                className="bg-[#1a1a1a] border border-[#2a2a2a] rounded px-1.5 py-0.5 text-xs text-[#e0e0e0] focus:border-[#4fc3f7] outline-none min-w-[120px]"
              >
                <option value="">未选择</option>
                {strategies.map((s) => <option key={s.name} value={s.name}>{s.title}</option>)}
              </select>
            </div>
            <div className="h-5 w-px bg-[#333]" />
            {/* 周期切换：紧凑按钮组 */}
            <div className="flex gap-0.5">
              {TFS.map(t => (
                <button
                  key={t}
                  onClick={() => switchTf(active.id, t)}
                  className={`px-2 py-0.5 text-[11px] rounded transition-colors ${
                    active.tf === t
                      ? "bg-[#26a69a] text-white"
                      : "bg-[#2a2a2a] text-[#888] hover:text-[#e0e0e0] hover:bg-[#333]"
                  }`}
                >
                  {t}
                </button>
              ))}
            </div>
            <div className="h-5 w-px bg-[#333]" />
            {/* 操作按钮：回测 + 扫描（扫描颜色随实盘变红） + 自动 + 流式 */}
            <button
              onClick={() => handleMount(active.id)}
              disabled={active.running || !active.strategy}
              className="px-2.5 py-1 bg-[#ff9800] text-white text-xs rounded hover:bg-[#f57c00] disabled:opacity-50"
              title="全历史回测"
            >
              {active.running ? "回测中..." : "📊 回测"}
            </button>
            <button
              onClick={() => handleRunOnce(active.id)}
              disabled={active.running || !active.strategy}
              className={`px-2.5 py-1 text-white text-xs rounded disabled:opacity-50 ${
                active.liveMode
                  ? "bg-[#e53935] hover:bg-[#c62828] ring-1 ring-[#ef5350]"
                  : "bg-[#26a69a] hover:bg-[#1e8e6f]"
              }`}
              title={active.liveMode
                ? "⚡ 实盘扫描：触发信号将直接通过同花顺下真实订单"
                : "扫描信号（纸面模拟，不下真实单）"}
            >
              {active.running
                ? "运行中..."
                : active.liveMode ? "⚡ 实盘扫描" : "▶ 扫描"}
            </button>
            <button
              onClick={() => toggleAuto(active.id)}
              disabled={!active.strategy}
              className={`px-2.5 py-1 text-xs rounded border ${
                active.auto
                  ? active.liveMode
                    ? "bg-[#7f1d1d]/30 border-[#ef5350] text-[#ef5350]"
                    : "bg-[#1e8e6f]/30 border-[#26a69a] text-[#26a69a]"
                  : "bg-[#333] border-[#444] text-[#e0e0e0] disabled:opacity-30"
              }`}
              title={active.liveMode
                ? (active.auto ? "停止：关闭自动真实下单循环" : "🔄 自动：每 6s 扫描一次，命中即真实下单")
                : "自动运行（纸面模拟）"}
            >
              {active.auto ? "⏸ 停止" : "🔄 自动"}
            </button>
            <button
              onClick={() => toggleStream(active.id)}
              disabled={!active.strategy}
              className={`px-2.5 py-1 text-xs rounded border ${
                active.streaming
                  ? "bg-[#d32f2f]/30 border-[#ef5350] text-[#ef5350]"
                  : "bg-[#333] border-[#444] text-[#e0e0e0] hover:border-[#ff9800] hover:text-[#ff9800] disabled:opacity-30"
              }`}
              title="流式测试"
            >
              {active.streaming ? "⏹ 停流式" : "▶ 流式"}
            </button>
            <select
              value={active.speed}
              onChange={(e) => changeSpeed(active.id, e.target.value)}
              disabled={!active.streaming}
              className="bg-[#1a1a1a] border border-[#2a2a2a] rounded px-1 py-0.5 text-[11px] text-[#e0e0e0] disabled:opacity-50 w-14"
            >
              {["1x","2x","5x","10x","20x"].map(s => <option key={s} value={s}>{s}</option>)}
            </select>
            {/* 策略参数入口：右侧可折叠 */}
            {active.strategy && Object.keys(active.schema).length > 0 && (
              <details className="ml-auto">
                <summary className="text-xs text-[#888] cursor-pointer hover:text-[#e0e0e0] list-none">
                  ⚙ 参数
                </summary>
                <div className="absolute mt-1 right-0 z-10 bg-[#1a1a1a] border border-[#2a2a2a] rounded p-3 shadow-xl min-w-[280px]">
                  <ParamForm
                    schema={active.schema}
                    params={active.params}
                    onChange={(p) => setCharts((prev) => prev.map((c) => c.id === active.id ? { ...c, params: p } : c))}
                  />
                </div>
              </details>
            )}
          </div>

          {/* 图表区域：flex-1 占满主空间 */}
          <div className="flex-1 min-h-0 p-2">
            <KLineChart
              symbol={active.symbol}
              tf={active.tf}
              bars={active.bars}
              markers={active.markers}
              maWindow={active.params.window ?? 0}
              mode={active.tf === INTRADAY_TF ? "intraday" : "kline"}
              fullDayAxis={active.tf === INTRADAY_TF}
              preClose={active.preClose}
            />
          </div>

          {/* 底部状态栏：3列紧凑，高度受限可滚动 */}
          <div className="grid grid-cols-3 gap-2 shrink-0 border-t border-[#2a2a2a] p-2 max-h-44 overflow-hidden">
            {/* 同花顺真实持仓 + 模拟快照 */}
            <div className="bg-[#1a1a1a] border border-[#2a2a2a] rounded p-1.5 flex flex-col overflow-hidden">
              <div className="flex items-center justify-between mb-1">
                <div className="text-[10px] text-[#666]">
                  真实持仓
                  {thsPositions.account && <span className="ml-1 text-[#888]">· {thsPositions.account}</span>}
                  {thsPositions.ts && <span className="ml-1 text-[#555]">@{thsPositions.ts}</span>}
                </div>
                <button
                  onClick={refreshThsPositions}
                  disabled={thsPositions.loading}
                  className="text-[10px] text-[#888] hover:text-white border border-[#333] rounded px-1 py-0 disabled:opacity-40"
                >
                  {thsPositions.loading ? "…" : "⟳"}
                </button>
              </div>

              {!thsPositions.ok ? (
                <div className="text-[10px] text-[#888] leading-tight">
                  {thsPositions.loading && !thsPositions.error ? "加载中…" : (thsPositions.error || "未获取到持仓")}
                </div>
              ) : thsPositions.rows.length === 0 ? (
                <div className="text-[10px] text-[#666]">空仓</div>
              ) : (
                <div className="overflow-auto text-[10px] font-mono max-h-16">
                  <table className="w-full border-collapse">
                    <thead className="text-[#666]">
                      <tr>
                        {thsPositions.columns.map((c, i) => (
                          <th key={i} className="text-left font-normal pr-1.5 whitespace-nowrap">{c}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {thsPositions.rows.map((row, i) => {
                        let values: any[];
                        if (Array.isArray(row)) {
                          values = row;
                        } else if (row && typeof row === "object") {
                          values = thsPositions.columns.map(c => (row as any)[c]);
                        } else {
                          values = [row];
                        }
                        return (
                          <tr key={i} className="text-[#aaa]">
                            {values.map((v, j) => (
                              <td key={j} className="pr-1.5 whitespace-nowrap">{String(v ?? "")}</td>
                            ))}
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}

              {/* 模拟权益快照 */}
              <div className="mt-1 pt-1 border-t border-[#2a2a2a]">
                <div className="text-[10px] text-[#666] mb-0.5">模拟权益</div>
                {active.snapshot ? (
                  <div className="grid grid-cols-2 gap-x-1.5 gap-y-0 text-[10px] font-mono text-[#aaa]">
                    {active.snapshot.equity != null && (
                      <>
                        <span className="text-[#666]">权益</span>
                        <span className={`${active.snapshot.cost != null && active.snapshot.equity < active.snapshot.cost ? "text-[#ef5350]" : "text-[#26a69a]"}`}>
                          {fmtMoney(active.snapshot.equity)}
                        </span>
                      </>
                    )}
                    {active.snapshot.cost != null && (
                      <>
                        <span className="text-[#666]">成本</span>
                        <span>{fmtMoney(active.snapshot.cost)}</span>
                      </>
                    )}
                    {active.snapshot.cash != null && (
                      <>
                        <span className="text-[#666]">现金</span>
                        <span>{fmtMoney(active.snapshot.cash)}</span>
                      </>
                    )}
                    <span className="text-[#666]">持仓</span>
                    <span>{fmtNum(active.snapshot.position)}</span>
                    {active.snapshot.avg_cost != null && (
                      <>
                        <span className="text-[#666]">均价</span>
                        <span>{fmtPrice(active.snapshot.avg_cost)}</span>
                      </>
                    )}
                    {active.snapshot.total_pnl != null && (
                      <>
                        <span className="text-[#666]">累计盈亏</span>
                        <span className={Number(active.snapshot.total_pnl) >= 0 ? "text-[#26a69a]" : "text-[#ef5350]"}>
                          {Number(active.snapshot.total_pnl) >= 0 ? "+" : ""}{fmtMoney(active.snapshot.total_pnl)}
                        </span>
                      </>
                    )}
                    {active.snapshot.total_pnl != null && active.snapshot.cost ? (
                      <>
                        <span className="text-[#666]">收益率</span>
                        <span className={Number(active.snapshot.total_pnl) >= 0 ? "text-[#26a69a]" : "text-[#ef5350]"}>
                          {Number(active.snapshot.total_pnl) >= 0 ? "+" : ""}{fmtPct(Number(active.snapshot.total_pnl) / Number(active.snapshot.cost))}
                        </span>
                      </>
                    ) : null}
                  </div>
                ) : (
                  <div className="text-[10px] text-[#666]">未运行</div>
                )}
              </div>
            </div>
            {/* 最近订单 */}
            <div className="bg-[#1a1a1a] border border-[#2a2a2a] rounded p-1.5 overflow-hidden">
              <div className="text-[10px] text-[#666] mb-1">最近订单 ({active.orders.length})</div>
              <div className="overflow-auto max-h-28 text-[10px]">
                {active.orders.slice(-5).reverse().map((o, i) => (
                  <div key={i} className="font-mono text-[#aaa] leading-tight py-0.5">
                    <span className={o.side === "buy" ? "text-[#26a69a]" : (o.pnl != null && o.pnl < 0 ? "text-[#ef5350]" : "text-[#ff9800]")}>
                      {o.side === "buy" ? "买" : "卖"}
                    </span>
                    {" "}{o.qty ?? 0}股@{fmtPrice(o.avg_fill_price ?? o.price)}
                    <span className="text-[#666] ml-1">{o.status}</span>
                    {o.pnl != null && (
                      <span className={`ml-1 font-bold ${o.pnl >= 0 ? "text-[#26a69a]" : "text-[#ef5350]"}`}>
                        {o.pnl >= 0 ? "+" : ""}{fmtMoney(o.pnl)}
                      </span>
                    )}
                    {o.ts && (
                      <div className="text-[#666] text-[9px]">{String(o.ts).replace("T"," ").slice(5,16)}</div>
                    )}
                  </div>
                ))}
                {active.orders.length === 0 && <span className="text-[#666]">无</span>}
              </div>
            </div>
            {/* 运行日志 */}
            <div className="bg-[#1a1a1a] border border-[#2a2a2a] rounded p-1.5 overflow-hidden">
              <div className="text-[10px] text-[#666] mb-1">运行日志</div>
              <div className="text-[10px] text-[#aaa] overflow-auto max-h-28 font-mono whitespace-pre-wrap leading-tight">
                {(active.logs || []).slice().reverse().map((ln, i) => (
                  <div key={i} className="border-b border-[#222] py-0.5">{ln}</div>
                ))}
              </div>
            </div>
          </div>
        </div>
      ) : (
        !showNew && (
          <div className="text-[#666] p-8 text-center">
            还没有图，点「➕ 新建」开始
          </div>
        )
      )}
    </div>
  );
}

function dedupMarkers(markers: KMarker[]): KMarker[] {
  const seen = new Set<string>();
  const out: KMarker[] = [];
  for (const m of markers) {
    const k = `${m.date}|${m.price}|${m.action}`;
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(m);
  }
  return out;
}
