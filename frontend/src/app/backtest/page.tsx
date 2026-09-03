"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import * as echarts from "echarts";
import type { EChartsOption } from "echarts";
import { useEffect, useRef } from "react";
import { paramCn, statCn, formatStat, fmtMoney, fmtPct, fmtNum } from "@/lib/fmt";

type BacktestResult = {
  stats: Record<string, any>;
  equity: { time: string; value: number }[];
  buyhold?: { time: string; value: number }[];
  markers: { date: string; price: number; target: number }[];
  close: { time: string; value: number }[];
};

export default function BacktestPage() {
  const [strategies, setStrategies] = useState<any[]>([]);
  const [selected, setSelected] = useState<string>("ma20_trend");
  const [symbols, setSymbols] = useState<string>("sz159915");
  const [params, setParams] = useState<Record<string, any>>({ window: 20 });
  const [tf, setTf] = useState<string>("strategy");
  const [customTf, setCustomTf] = useState<string>("45");
  const [limit, setLimit] = useState<string>("");
  const [result, setResult] = useState<Record<string, BacktestResult> | null>(null);
  const [loading, setLoading] = useState(false);

  // schema 来自 /api/strategies/{name}/schema，含 params schema + timeframe
  const [schema, setSchema] = useState<any>(null);

  const [tab, setTab] = useState<"single" | "optimize">("single");

  const timeframes = [
    { value: "strategy", label: "策略默认" },
    { value: "1m", label: "1分钟" },
    { value: "2m", label: "2分钟" },
    { value: "5m", label: "5分钟" },
    { value: "15m", label: "15分钟" },
    { value: "30m", label: "30分钟" },
    { value: "60m", label: "60分钟" },
    { value: "240m", label: "4小时" },
    { value: "day", label: "日线" },
    { value: "week", label: "周线" },
    { value: "custom", label: "自定义" },
  ];
  // 可用于优化搜索的周期（不含"策略默认/日线/周线"等，避免 intraday_t 在日线没意义）
  const OPT_TFS = ["1m", "2m", "5m", "15m", "30m", "60m"];
  const METRICS = [
    { value: "calmar", label: "卡玛 Calmar (风险/收益比)" },
    { value: "calmar_alpha", label: "卡玛超额 (+跑赢买持)" },
    { value: "buyhold_alpha", label: "跑赢买入持有" },
    { value: "total_return", label: "总收益率" },
    { value: "sharpe", label: "夏普 Sharpe" },
    { value: "win_rate", label: "胜率" },
  ];

  const resolvedTf = tf === "custom" ? `${customTf}m` : tf === "strategy" ? "" : tf;

  // 加载策略列表
  useEffect(() => {
    api.strategies().then((rows) => {
      setStrategies(rows);
      if (rows.length > 0) {
        const first = rows[0];
        setSelected(first.name);
        setParams(first.params ?? {});
        // 立即获取 schema（含 timeframe + params schema）
        api.strategySchema(first.name).then((sc) => setSchema(sc)).catch(() => {});
      }
    });
  }, []);

  // 策略切换时获取 schema
  const switchStrategy = (name: string) => {
    setSelected(name);
    const s = strategies.find((x) => x.name === name);
    setParams(s?.params ?? {});
    setSchema(null);
    api.strategySchema(name).then((sc) => setSchema(sc)).catch(() => {});
  };

  const handleRun = async () => {
    setLoading(true);
    try {
      const out = await api.backtest({
        strategy: selected,
        symbols: symbols.split(",").map((s) => s.trim()),
        params,
        tf: resolvedTf,
        qfq: false,
        cash: 100000,
        limit: Number(limit) || 0,
      });
      setResult(out as Record<string, BacktestResult>);
    } catch (e: any) {
      const msg = e?.message ?? String(e);
      alert("回测失败:\n" + msg);
    } finally {
      setLoading(false);
    }
  };

  const strat = strategies.find((s) => s.name === selected);
  // paramSchema 来自 /api/strategies/{name}/schema 的 params 字段: {k: {type,default,min,max}}
  const paramSchema = schema?.params ?? {};
  const strategyTf = schema?.timeframe ?? "";
  const hasTfSearch = !!schema && strategyTf !== "day" && strategyTf !== "week" && strategyTf !== "";

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">📈 回测</h1>

      {/* 顶部统一控制: 策略 / 标的 */}
      <div className="flex flex-wrap items-end gap-4 p-4 bg-[#141414] border border-[#2a2a2a] rounded-lg">
        <div>
          <label className="block text-xs text-[#666] mb-1">策略</label>
          <select
            value={selected}
            onChange={(e) => switchStrategy(e.target.value)}
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
      </div>

      {/* Tab 切换 */}
      <div className="flex gap-2 border-b border-[#2a2a2a]">
        {[
          { k: "single", label: "🔍 单次回测" },
          { k: "optimize", label: "🎯 参数优化" },
        ].map((t) => (
          <button
            key={t.k}
            onClick={() => setTab(t.k as any)}
            className={`px-4 py-2 text-sm -mb-px transition ${
              tab === t.k
                ? "border-b-2 border-[#ff6d00] text-[#ff6d00] font-semibold"
                : "text-[#888] hover:text-white"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "single" && (
        <>
          <SingleBacktestPanel
            params={params} setParams={setParams}
            paramSchema={paramSchema}
            tf={tf} setTf={setTf}
            customTf={customTf} setCustomTf={setCustomTf}
            limit={limit} setLimit={setLimit}
            timeframes={timeframes}
            loading={loading} onRun={handleRun}
          />
          {result && Object.entries(result).map(([sym, r]) => (
            <ResultCard key={sym} sym={sym} r={r} />
          ))}
        </>
      )}

      {tab === "optimize" && (
        <ParamOptimizePanel
          strategyName={selected}
          strategyTf={strategyTf}
          symbol={symbols.split(",")[0]?.trim() ?? ""}
          paramSchema={paramSchema}
          defaultParams={params}
          hasTfSearch={hasTfSearch}
          OPT_TFS={OPT_TFS}
          METRICS={METRICS}
          onApplyBest={(bestTf, bestParams) => {
            // 把最佳参数应用到顶部参数表单
            setParams({ ...params, ...bestParams });
            if (bestTf) {
              const t = timeframes.find((x) => x.value === bestTf);
              if (t) setTf(bestTf);
            }
            setTab("single");
          }}
        />
      )}
    </div>
  );
}

/* ======================================================= 单次回测面板 ====== */
function SingleBacktestPanel(props: {
  params: Record<string, any>; setParams: (p: Record<string, any>) => void;
  paramSchema: Record<string, any>;
  tf: string; setTf: (v: string) => void;
  customTf: string; setCustomTf: (v: string) => void;
  limit: string; setLimit: (v: string) => void;
  timeframes: { value: string; label: string }[];
  loading: boolean; onRun: () => void;
}) {
  const { params, setParams, paramSchema, tf, setTf, customTf, setCustomTf,
          limit, setLimit, timeframes, loading, onRun } = props;
  return (
    <div className="flex flex-wrap items-end gap-4 p-4 bg-[#141414] border border-[#2a2a2a] rounded-lg">
      <div>
        <label className="block text-xs text-[#666] mb-1">时间周期</label>
        <select
          value={tf}
          onChange={(e) => setTf(e.target.value)}
          className="bg-[#1a1a1a] border border-[#333] rounded px-3 py-1.5 text-sm"
        >
          {timeframes.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
      </div>
      {tf === "custom" && (
        <div>
          <label className="block text-xs text-[#666] mb-1">自定义周期 (分钟)</label>
          <input type="number" min="1" value={customTf}
            onChange={(e) => setCustomTf(e.target.value)}
            className="bg-[#1a1a1a] border border-[#333] rounded px-3 py-1.5 text-sm w-24" />
        </div>
      )}
      <div>
        <label className="block text-xs text-[#666] mb-1">K线条数 (0=全量)</label>
        <input type="number" min="0" placeholder="0 全量" value={limit}
          onChange={(e) => setLimit(e.target.value)}
          className="bg-[#1a1a1a] border border-[#333] rounded px-3 py-1.5 text-sm w-24" />
      </div>
      {Object.keys(paramSchema || {}).length > 0 && (
        <div className="flex gap-3 flex-wrap">
          {Object.entries(paramSchema).map(([k, spec]: any) => (
            <div key={k}>
              <label className="block text-xs text-[#666] mb-1">{paramCn(k)}</label>
              <input
                type="number" step="any"
                value={params[k] ?? spec.default}
                onChange={(e) => setParams({ ...params, [k]: Number(e.target.value) })}
                className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-sm w-20"
              />
            </div>
          ))}
        </div>
      )}
      <button
        onClick={onRun}
        disabled={loading}
        className="px-5 py-1.5 bg-[#ff6d00] text-white rounded hover:bg-[#e65100] disabled:opacity-50"
      >
        {loading ? "回测中..." : "▶ 跑回测"}
      </button>
    </div>
  );
}

/* ======================================================= 单次回测结果 ====== */
function ResultCard({ sym, r }: { sym: string; r: BacktestResult }) {
  // 把 trade_costs 对象从 stats 里摘出来单独展示 (因为 formatStat 处理不了对象)
  const tradeCosts = (r.stats as any)?.trade_costs as any;
  const otherStats = Object.entries(r.stats).filter(([k]) => k !== "trade_costs");
  const catOfSymbol = (s: string): string => {
    const x = s.trim().toLowerCase();
    if (x.startsWith("sh") || x.startsWith("sz") || x.startsWith("bj")) return "股票/ETF/指数";
    if (x.endsWith(".c") || x.endsWith(".p")) return "期权";
    const a = /[a-z]/.test(x), d = /\d/.test(x);
    if (a && d) return "期货合约";
    return "股票";
  };

  return (
    <div className="space-y-4">
      {/* 标的 + 生效成本 摘要条 */}
      {tradeCosts && (
        <div className="flex flex-wrap items-center gap-4 bg-[#141414] border border-[#2a2a2a] rounded p-3">
          <div>
            <div className="text-xs text-[#666]">标的 / 识别大类</div>
            <div className="font-mono text-sm">
              <span className="text-[#ffd54f]">{sym}</span>
              <span className="text-[#666] mx-2">·</span>
              <span>{catOfSymbol(sym)}</span>
            </div>
          </div>
          <CostBadge label="买入手续费" value={tradeCosts.buy_fee} />
          <CostBadge label="卖出手续费" value={tradeCosts.sell_fee} />
          <CostBadge label="卖出印花税" value={tradeCosts.sell_stamp_duty} />
          <CostBadge label="滑点 (双向)" value={tradeCosts.slippage} />
          <div className="ml-auto text-right">
            <div className="text-xs text-[#666]">单边 vbt 费率</div>
            <div className="font-mono text-xs text-[#90caf9]">
              {(tradeCosts.effective_fees_per_trade * 100).toFixed(4)}%
            </div>
          </div>
        </div>
      )}
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-3">
        {otherStats.map(([k, v]) => (
          <div key={k} className="bg-[#141414] border border-[#2a2a2a] rounded p-3">
            <div className="text-xs text-[#666]">{statCn(k)}</div>
            <div className="text-sm font-mono mt-1">{formatStat(k, v)}</div>
          </div>
        ))}
      </div>
      <Chart
        title={`${sym} · 资金曲线`}
        series={[
          { name: "策略回测", data: r.equity.map((e) => ({ time: e.time, value: e.value })),
            color: "#4fc3f7", area: true },
          { name: "买入持有",
            data: (r.buyhold ?? []).map((e) => ({ time: e.time, value: e.value })),
            color: "#81c784", area: false },
        ]}
      />
    </div>
  );
}

/* 成本小标签：小数 -> 百分比 + 万比 */
function CostBadge({ label, value }: { label: string; value: number }) {
  const pct = (value * 100).toFixed(4) + "%";
  const wan = "万" + (value * 10000).toFixed(2);
  return (
    <div>
      <div className="text-xs text-[#666]">{label}</div>
      <div className="font-mono text-xs">
        <span className="text-[#a5d6a7]">{pct}</span>
        <span className="text-[#666] mx-1">/</span>
        <span className="text-[#ffd54f]">{wan}</span>
      </div>
    </div>
  );
}

/* ============================================== 参数优化 Tab 面板 ========== */
type ParamSearchMode = "fixed" | "discrete" | "range";

function ParamOptimizePanel(props: {
  strategyName: string;
  strategyTf: string;
  symbol: string;
  paramSchema: Record<string, any>;
  defaultParams: Record<string, any>;
  hasTfSearch: boolean;
  OPT_TFS: string[];
  METRICS: { value: string; label: string }[];
  onApplyBest: (bestTf: string, bestParams: Record<string, any>) => void;
}) {
  const { strategyName, symbol, paramSchema, defaultParams, hasTfSearch,
          OPT_TFS, METRICS } = props;

  // --- 配置状态 ---
  const [mode, setMode] = useState<"grid" | "bayesian">("grid");
  const [metric, setMetric] = useState<string>("calmar");
  const [optTfs, setOptTfs] = useState<string[]>(
    hasTfSearch ? (props.strategyTf && OPT_TFS.includes(props.strategyTf)
                    ? [props.strategyTf, "1m", "5m", "15m"].filter((x, i, a) => a.indexOf(x) === i && OPT_TFS.includes(x))
                    : ["5m"]) : []) ;
  const [optLimit, setOptLimit] = useState<string>("2000");
  const [nCalls, setNCalls] = useState<number>(40);
  const [nInit, setNInit] = useState<number>(10);
  const [topN, setTopN] = useState<number>(8);

  // 每个参数的搜索模式 + 配置
  type PConf = { mode: ParamSearchMode; discrete: string; rangeLo: string; rangeHi: string; };
  const [pconfs, setPconfs] = useState<Record<string, PConf>>(() => {
    const out: Record<string, PConf> = {};
    Object.entries(paramSchema || {}).forEach(([k, spec]: any) => {
      const def = defaultParams[k] ?? spec.default;
      out[k] = {
        mode: "fixed",
        discrete: `${def}`,
        rangeLo: `${spec.min ?? def}`,
        rangeHi: `${spec.max ?? def}`,
      };
    });
    return out;
  });

  // 模式切换时清空 pconfs 对不上的键 / 补上缺的键
  useEffect(() => {
    setPconfs((prev) => {
      const out: Record<string, PConf> = { ...prev };
      Object.entries(paramSchema || {}).forEach(([k, spec]: any) => {
        if (!out[k]) {
          const def = defaultParams[k] ?? spec.default;
          out[k] = {
            mode: "fixed",
            discrete: `${def}`,
            rangeLo: `${spec.min ?? def}`,
            rangeHi: `${spec.max ?? def}`,
          };
        }
      });
      Object.keys(out).forEach((k) => {
        if (!(paramSchema && k in paramSchema)) delete out[k];
      });
      return out;
    });
  }, [strategyName, paramSchema, defaultParams]);

  // --- 运行状态 ---
  const [jobId, setJobId] = useState<string | null>(null);
  const [status, setStatus] = useState<"idle" | "running" | "done" | "failed">("idle");
  const [progress, setProgress] = useState<any>(null);
  const [result, setResult] = useState<any>(null);
  const [errorMsg, setErrorMsg] = useState<string>("");
  const stopRef = useRef<boolean>(false);

  const buildRequestBody = (): any => {
    const body: any = {
      strategy: strategyName, symbol, mode, metric,
      timeframes: optTfs.slice(),
      qfq: false, cash: 100_000, fees: 0.0001,
      limit: Number(optLimit) || 0, top_n: topN,
    };
    if (mode === "bayesian") {
      body.n_calls = nCalls;
      body.n_initial_points = nInit;
      body.base_estimator = "GP";
      body.overrides = {};
      Object.entries(pconfs).forEach(([k, c]) => {
        if (c.mode === "discrete") {
          const arr = c.discrete.split(",").map((s) => s.trim()).filter(Boolean).map(parseBestEffort);
          if (arr.length >= 2) body.overrides[k] = arr;
        } else if (c.mode === "range") {
          const spec: any = paramSchema[k];
          const isInt = spec?.type === "int";
          body.overrides[k] = isInt
            ? { lo: parseInt(c.rangeLo), hi: parseInt(c.rangeHi) }
            : { lo: parseFloat(c.rangeLo), hi: parseFloat(c.rangeHi) };
        }
        // fixed 不进 overrides，让 objective 用默认值
      });
    } else {
      // grid: 给 param_grid
      body.param_grid = {};
      Object.entries(pconfs).forEach(([k, c]) => {
        if (c.mode === "discrete") {
          const arr = c.discrete.split(",").map((s) => s.trim()).filter(Boolean).map(parseBestEffort);
          if (arr.length >= 1) body.param_grid[k] = arr;
        } else if (c.mode === "range") {
          // grid 不能直接吃范围，给 5 个均匀散点
          const spec: any = paramSchema[k];
          const isInt = spec?.type === "int";
          const lo = isInt ? parseInt(c.rangeLo) : parseFloat(c.rangeLo);
          const hi = isInt ? parseInt(c.rangeHi) : parseFloat(c.rangeHi);
          const pts: any[] = [];
          const PTS_COUNT: number = 5;
          for (let i = 0; i < PTS_COUNT; i++) {
            const t = PTS_COUNT <= 1 ? 0 : i / (PTS_COUNT - 1);
            const raw = lo + (hi - lo) * t;
            const v = isInt ? Math.round(raw) : +raw.toFixed(4);
            pts.push(v);
          }
          body.param_grid[k] = Array.from(new Set(pts));
        }
      });
    }
    return body;
  };

  const startOpt = async () => {
    if (mode === "grid") {
      const n_tf = Math.max(1, optTfs.length);
      const pcount = Object.values(pconfs).reduce((acc, c) =>
        acc * (c.mode === "discrete"
          ? Math.max(1, c.discrete.split(",").filter(Boolean).length)
          : c.mode === "range" ? 5 : 1), 1);
      const total = n_tf * pcount;
      if (total > 500) {
        if (!confirm(`⚠️ 网格组合数 ${total} 过多，建议用贝叶斯模式。仍继续？`)) return;
      }
    }
    setStatus("running");
    setErrorMsg("");
    setResult(null);
    setProgress(null);
    stopRef.current = false;
    try {
      const body = buildRequestBody();
      const { job_id } = await api.paramOptimizeStart(body);
      setJobId(job_id);
      // 轮询
      const tick = async () => {
        while (!stopRef.current) {
          try {
            const r = await api.paramOptimizePoll(job_id);
            setProgress(r.progress ?? null);
            if (r.status === "done") {
              setResult(r.result);
              setStatus("done");
              return;
            } else if (r.status === "failed") {
              setErrorMsg(r.error || "未知错误");
              setStatus("failed");
              return;
            }
          } catch (e: any) {
            setErrorMsg("轮询失败: " + (e?.message ?? e));
            setStatus("failed");
            return;
          }
          await new Promise((res) => setTimeout(res, 1500));
        }
      };
      tick();
    } catch (e: any) {
      setErrorMsg("提交失败: " + (e?.message ?? e));
      setStatus("failed");
    }
  };

  const stop = () => { stopRef.current = true; setStatus("idle"); };

  const total = progress?.total ?? 0;
  const current = progress?.current ?? 0;
  const pct = total ? Math.min(100, Math.round((current / total) * 100)) : 0;
  const elapsedSec = Math.round((progress?.elapsed_ms ?? 0) / 1000);

  return (
    <div className="space-y-4">
      {/* 控制区 */}
      <div className="p-4 bg-[#141414] border border-[#2a2a2a] rounded-lg space-y-4">
        <div className="flex flex-wrap gap-4 items-end">
          <div>
            <label className="block text-xs text-[#666] mb-1">优化模式</label>
            <select value={mode} onChange={(e) => setMode(e.target.value as any)}
              className="bg-[#1a1a1a] border border-[#333] rounded px-3 py-1.5 text-sm">
              <option value="grid">网格搜索（组合少时用）</option>
              <option value="bayesian">贝叶斯优化（组合多时用）</option>
            </select>
          </div>
          <div>
            <label className="block text-xs text-[#666] mb-1">评分指标</label>
            <select value={metric} onChange={(e) => setMetric(e.target.value)}
              className="bg-[#1a1a1a] border border-[#333] rounded px-3 py-1.5 text-sm w-60">
              {METRICS.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </div>
          {mode === "bayesian" && (
            <>
              <div>
                <label className="block text-xs text-[#666] mb-1">评估次数 n_calls</label>
                <input type="number" min={10} max={500} value={nCalls}
                  onChange={(e) => setNCalls(Number(e.target.value))}
                  className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-sm w-20" />
              </div>
              <div>
                <label className="block text-xs text-[#666] mb-1">初始点 n_init</label>
                <input type="number" min={3} max={100} value={nInit}
                  onChange={(e) => setNInit(Number(e.target.value))}
                  className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-sm w-20" />
              </div>
            </>
          )}
          <div>
            <label className="block text-xs text-[#666] mb-1">Top N</label>
            <input type="number" min={3} max={50} value={topN}
              onChange={(e) => setTopN(Number(e.target.value))}
              className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-sm w-16" />
          </div>
          <div>
            <label className="block text-xs text-[#666] mb-1">K线条数 (0=全量)</label>
            <input type="number" min={0} placeholder="2000" value={optLimit}
              onChange={(e) => setOptLimit(e.target.value)}
              className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-sm w-24" />
          </div>
        </div>

        {/* 周期多选 */}
        {hasTfSearch && (
          <div>
            <div className="text-xs text-[#666] mb-1">K线周期（多选，一起搜索对比）</div>
            <div className="flex flex-wrap gap-2">
              {OPT_TFS.map((t) => (
                <label key={t} className="flex items-center gap-1 text-sm cursor-pointer px-2 py-1 border border-[#333] rounded hover:bg-[#1a1a1a]">
                  <input
                    type="checkbox"
                    checked={optTfs.includes(t)}
                    onChange={(e) => {
                      if (e.target.checked) setOptTfs([...optTfs, t]);
                      else setOptTfs(optTfs.filter((x) => x !== t));
                    }}
                  />
                  {t}
                </label>
              ))}
            </div>
          </div>
        )}

        {/* 参数搜索配置表 */}
        <div>
          <div className="text-xs text-[#666] mb-2">
            每个参数可选: <b>固定</b>（用当前值）/ <b>离散</b>（逗号分隔候选）/ <b>区间</b>（网格会均匀散 5 点，贝叶斯在区间内连续搜）
          </div>
          {Object.keys(paramSchema || {}).length === 0
            ? <div className="text-sm text-[#666] italic">（当前策略无可调参数）</div>
            : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm border-collapse">
                  <thead>
                    <tr className="text-[#666] text-xs border-b border-[#2a2a2a]">
                      <th className="text-left py-2 px-2">参数</th>
                      <th className="text-left py-2 px-2">类型</th>
                      <th className="text-left py-2 px-2">范围</th>
                      <th className="text-left py-2 px-2 w-32">搜索模式</th>
                      <th className="text-left py-2 px-2">离散候选（逗号分隔）</th>
                      <th className="text-left py-2 px-2">区间 Lo</th>
                      <th className="text-left py-2 px-2">区间 Hi</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(paramSchema).map(([k, spec]: any) => {
                      const c = pconfs[k];
                      if (!c) return null;
                      const def = defaultParams[k] ?? spec.default;
                      return (
                        <tr key={k} className="border-b border-[#1f1f1f]">
                          <td className="py-1.5 px-2">
                            <div className="font-mono text-[#ffd54f]">{k}</div>
                            <div className="text-xs text-[#888]">{paramCn(k)}</div>
                          </td>
                          <td className="py-1.5 px-2 text-[#888]">
                            {spec.type === "int" ? "整数" : spec.type === "float" ? "浮点" : String(spec.type || "?")}
                          </td>
                          <td className="py-1.5 px-2 text-[#888] font-mono text-xs">
                            {spec.min} ~ {spec.max}（默认 {def}）
                          </td>
                          <td className="py-1.5 px-2">
                            <select value={c.mode}
                              onChange={(e) => setPconfs({
                                ...pconfs,
                                [k]: { ...c, mode: e.target.value as ParamSearchMode },
                              })}
                              className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-xs w-28">
                              <option value="fixed">固定</option>
                              <option value="discrete">离散列表</option>
                              <option value="range">区间</option>
                            </select>
                          </td>
                          <td className="py-1.5 px-2">
                            <input
                              value={c.discrete}
                              disabled={c.mode !== "discrete"}
                              onChange={(e) => setPconfs({
                                ...pconfs, [k]: { ...c, discrete: e.target.value },
                              })}
                              placeholder="如 4,6,8"
                              className={`w-56 bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-xs font-mono ${c.mode !== "discrete" ? "opacity-40" : ""}`}
                            />
                          </td>
                          <td className="py-1.5 px-2">
                            <input
                              value={c.rangeLo}
                              disabled={c.mode !== "range"}
                              onChange={(e) => setPconfs({
                                ...pconfs, [k]: { ...c, rangeLo: e.target.value },
                              })}
                              className={`w-20 bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-xs font-mono ${c.mode !== "range" ? "opacity-40" : ""}`}
                            />
                          </td>
                          <td className="py-1.5 px-2">
                            <input
                              value={c.rangeHi}
                              disabled={c.mode !== "range"}
                              onChange={(e) => setPconfs({
                                ...pconfs, [k]: { ...c, rangeHi: e.target.value },
                              })}
                              className={`w-20 bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 text-xs font-mono ${c.mode !== "range" ? "opacity-40" : ""}`}
                            />
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
        </div>

        <div className="flex items-center gap-3 pt-2">
          <button
            onClick={startOpt}
            disabled={status === "running" || !symbol}
            className="px-5 py-1.5 bg-[#ff6d00] text-white rounded hover:bg-[#e65100] disabled:opacity-50"
          >
            {status === "running" ? "优化中..." : "🎯 开始参数优化"}
          </button>
          {status === "running" && (
            <button onClick={stop}
              className="px-4 py-1.5 bg-[#7f1d1d] text-white rounded hover:bg-[#991b1b]">
              停止
            </button>
          )}
          {jobId && (
            <span className="text-xs text-[#666]">job: {jobId}</span>
          )}
        </div>

        {/* 进度条 */}
        {status === "running" && (
          <div className="space-y-1">
            <div className="h-2 bg-[#1f1f1f] rounded overflow-hidden">
              <div className="h-full bg-[#ff6d00] transition-all" style={{ width: `${pct}%` }} />
            </div>
            <div className="flex justify-between text-xs text-[#888]">
              <span>进度 {current}/{total || "?"} ({pct}%) · 用时 {elapsedSec}s</span>
              <span>
                最新分数: <b className="text-[#ffd54f] font-mono">{progress?.latest_score?.toFixed?.(4) ?? "-"}</b>
                {progress?.latest_info?.tf ? ` · tf=${progress.latest_info.tf}` : ""}
              </span>
            </div>
            {/* 最近一轮参数 */}
            {progress?.latest_info?.params && (
              <div className="text-[11px] text-[#666] font-mono bg-black/40 p-2 rounded overflow-x-auto">
                {Object.entries(progress.latest_info.params).map(([k, v]: any) =>
                  <span key={k} className="mr-3 inline-block">{k}={v}</span>
                )}
                {progress.latest_info.stats ? (
                  <span className="ml-2 text-[#888]">
                    ret={fmtPct(progress.latest_info.stats.total_return)}
                    {" · mdd="}{fmtPct(progress.latest_info.stats.max_drawdown)}
                    {" · trades="}{progress.latest_info.n_trades ?? "?"}
                  </span>
                ) : null}
              </div>
            )}
          </div>
        )}

        {errorMsg && (
          <div className="text-xs text-[#f44336] bg-[#2a0808] border border-[#7f1d1d] rounded p-3 whitespace-pre-wrap">
            {errorMsg}
          </div>
        )}
      </div>

      {/* 结果区 */}
      {result && (
        <OptimResultDisplay
          result={result}
          onApply={(tf, p) => props.onApplyBest(tf, p)}
        />
      )}
    </div>
  );
}

/* ===================================================== 优化结果展示 ====== */
function OptimResultDisplay({ result, onApply }: {
  result: any;
  onApply: (bestTf: string, bestParams: Record<string, any>) => void;
}) {
  const [selectedTopIdx, setSelectedTopIdx] = useState<number>(0);
  const top = result.top ?? [];
  const bestIdx = 0;
  const cur = top[selectedTopIdx] ?? result;

  const _top = (x: any) => ({
    ...x,
    params: { ...x.params },
    stats: { ...(x.stats || {}) },
  });
  const curTf = cur?.tf ?? result.best_tf ?? "";
  const curParams = cur?.params ?? result.best_params ?? {};
  const curStats = cur?.stats ?? result.best_stats ?? {};

  // 收敛曲线：用 history 的 score 序列
  const history: any[] = result.history ?? [];
  const series: any[] = [];
  if (history.length) {
    let bestSoFar = -Infinity;
    const runningMax: { time: string; value: number | null }[] = [];
    const scores: { time: string; value: number | null }[] = [];
    history.forEach((h, i) => {
      const t = `${i + 1}`;
      const sc = Number(h.score);
      const scValid: number | null = Number.isFinite(sc) ? sc : null;
      scores.push({ time: t, value: scValid });
      if (Number.isFinite(sc)) bestSoFar = Math.max(bestSoFar, sc);
      const bestValid: number | null = Number.isFinite(bestSoFar) ? bestSoFar : null;
      runningMax.push({ time: t, value: bestValid });
    });
    series.push(
      { name: "当前分数", data: scores, color: "#90caf9", area: false },
      { name: "历史最佳", data: runningMax, color: "#ffd54f", area: false },
    );
  }

  return (
    <div className="space-y-4">
      {/* 摘要卡 */}
      <div className="p-4 bg-[#141414] border border-[#2a2a2a] rounded-lg space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="px-2 py-0.5 rounded bg-[#1b5e20] text-[#a5d6a7] text-xs font-semibold">
              ✓ 完成 · {result.n_valid}/{result.n_combos} 有效
            </span>
            <span className="text-xs text-[#888]">
              指标 <b>{result.metric}</b>
              {" · 模式 "}{result.mode}
              {" · 用时 "}{(result.elapsed_sec ?? 0).toFixed(1)}s
            </span>
          </div>
          <button
            onClick={() => onApply(curTf, curParams)}
            className="px-4 py-1.5 bg-[#2e7d32] text-white rounded text-sm hover:bg-[#1b5e20]"
          >
            ✨ 应用这组参数到「单次回测」
          </button>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
          <Stat label="最佳分数" value={fmtNum(result.best_score)} highlight />
          <Stat label="最佳周期 tf" value={result.best_tf || "-"} />
          <Stat label="总收益" value={fmtPct(curStats.total_return ?? curStats["Total Return [%]"])} />
          <Stat label="买入持有" value={fmtPct(curStats.buyhold_return)} />
          <Stat label="最大回撤" value={fmtPct(curStats.max_drawdown ?? curStats["Max Drawdown [%]"])} />
          <Stat label="交易次数 / 胜率"
            value={`${curStats["Total Trades"] ?? cur.n_trades ?? "-"} / ${curStats["Trade Win Rate"] ?? "-"}`} />
        </div>
        {/* 最佳参数 */}
        <div>
          <div className="text-xs text-[#666] mb-1">
            选中组合的参数（点 Top N 行切换）
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge k="tf" v={curTf || "默认"} />
            {Object.entries(curParams).map(([k, v]) => (
              <Badge key={k} k={k} v={String(v)} />
            ))}
          </div>
        </div>
      </div>

      {/* 收敛曲线 + Top N 表 */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        <div className="lg:col-span-3">
          {series.length > 0 && (
            <Chart title="🔻 收敛曲线（分数 vs 评估轮次）" series={series} />
          )}
        </div>
        <div className="lg:col-span-2 p-3 bg-[#141414] border border-[#2a2a2a] rounded-lg overflow-auto max-h-[360px]">
          <div className="text-xs text-[#666] mb-2">🏆 Top {top.length} 参数组合（点击行切换左侧展示）</div>
          <table className="w-full text-xs border-collapse">
            <thead className="text-[#888] sticky top-0 bg-[#141414]">
              <tr className="border-b border-[#2a2a2a]">
                <th className="text-left py-1 px-1">#</th>
                <th className="text-left py-1 px-1">分数</th>
                <th className="text-left py-1 px-1">tf</th>
                <th className="text-left py-1 px-1">收益</th>
                <th className="text-left py-1 px-1">回撤</th>
                <th className="text-left py-1 px-1">参数摘要</th>
              </tr>
            </thead>
            <tbody>
              {top.map((t: any, i: number) => {
                const sel = selectedTopIdx === i;
                const st = t.stats || {};
                const kvs = Object.entries(t.params || {})
                  .filter(([, v]: any) => String(v).length <= 8)
                  .slice(0, 3)
                  .map(([k, v]: any) => `${k}=${v}`).join(" ");
                return (
                  <tr key={i}
                    onClick={() => setSelectedTopIdx(i)}
                    className={`cursor-pointer border-b border-[#1f1f1f] ${sel ? "bg-[#3e2723]" : "hover:bg-[#1a1a1a]"} ${i === bestIdx ? "font-semibold" : ""}`}>
                    <td className="py-1 px-1">
                      {i === bestIdx ? "🥇" : i + 1}
                    </td>
                    <td className="py-1 px-1 font-mono text-[#ffd54f]">
                      {typeof t.score === "number" ? t.score.toFixed(3) : t.score}
                    </td>
                    <td className="py-1 px-1">{t.tf || "-"}</td>
                    <td className="py-1 px-1 text-[#a5d6a7] font-mono">
                      {fmtPct(st.total_return ?? st["Total Return [%]"])}
                    </td>
                    <td className="py-1 px-1 text-[#ef9a9a] font-mono">
                      {fmtPct(st.max_drawdown ?? st["Max Drawdown [%]"])}
                    </td>
                    <td className="py-1 px-1 text-[#b0bec5] font-mono truncate max-w-[160px]">
                      {kvs}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, highlight }: { label: string; value: any; highlight?: boolean }) {
  return (
    <div className={`rounded p-3 border ${highlight ? "bg-[#3e2723] border-[#5d4037]" : "bg-[#0f0f0f] border-[#2a2a2a]"}`}>
      <div className="text-xs text-[#888]">{label}</div>
      <div className={`mt-1 font-mono ${highlight ? "text-[#ffd54f] text-lg" : "text-sm"}`}>
        {value ?? "-"}
      </div>
    </div>
  );
}

function Badge({ k, v }: { k: string; v: string }) {
  return (
    <span className="text-xs bg-[#1a1a1a] border border-[#333] rounded px-2 py-0.5 font-mono">
      <span className="text-[#666]">{k}=</span>
      <span className="text-[#ffd54f]">{v}</span>
    </span>
  );
}

/* ================================================== 共用：图表 / 工具 ====== */
function Chart({
  title,
  series,
}: {
  title: string;
  series: { name: string; data: { time: string; value: number | null }[]; color: string; area?: boolean }[];
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, "dark");
    chartRef.current = chart;
    const xData = series[0]?.data.map((d) => d.time) ?? [];
    const opt: EChartsOption = {
      backgroundColor: "#141414",
      title: { text: title, textStyle: { color: "#e0e0e0", fontSize: 14 }, left: 12, top: 8 },
      legend: {
        data: series.map((s) => s.name),
        textStyle: { color: "#888" },
        right: 16, top: 8, itemWidth: 14, itemHeight: 2,
      },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#1a1a1a",
        borderColor: "#333",
        textStyle: { color: "#e0e0e0" },
      },
      grid: { left: 60, right: 20, top: 40, bottom: 20 },
      xAxis: { type: "category", data: xData, axisLine: { lineStyle: { color: "#333" } }, axisLabel: { color: "#888", fontSize: 10 } },
      yAxis: {
        type: "value", scale: true,
        splitLine: { lineStyle: { color: "#1f1f1f" } },
        axisLabel: {
          color: "#888", fontSize: 10,
          formatter: (v: number) => fmtMoney(v),
        },
      },
      series: series.map((s) => ({
        type: "line" as const,
        name: s.name, smooth: false, symbol: "none",
        lineStyle: { color: s.color, width: 1.5 },
        data: s.data.map((d) => d.value),
        ...(s.area
          ? { areaStyle: {
              color: {
                type: "linear" as const, x: 0, y: 0, x2: 0, y2: 1,
                colorStops: [
                  { offset: 0, color: s.color + "40" },
                  { offset: 1, color: s.color + "00" },
                ],
              },
            } }
          : {}),
      })),
    };
    chart.setOption(opt);
    const resize = () => chart.resize();
    window.addEventListener("resize", resize);
    return () => { window.removeEventListener("resize", resize); chart.dispose(); chartRef.current = null; };
  }, [title, series]);

  return <div ref={ref} className="w-full h-[320px] border border-[#2a2a2a] rounded-lg" />;
}

function parseBestEffort(s: string): any {
  if (s === "") return NaN;
  if (/^-?\d+$/.test(s)) return parseInt(s, 10);
  const f = parseFloat(s);
  return Number.isFinite(f) ? f : s;
}
