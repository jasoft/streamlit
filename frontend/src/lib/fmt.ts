// 全局数字/中英文标题格式化: 最多保留 2 位小数 (百分比也如此)
// 图表 KLineChart 也 export 了同名函数, 这里做一层统一来源供 page 之间共享.

export function fmtMoney(v: number | null | undefined): string {
  if (v == null || !isFinite(Number(v))) return "-";
  const n = Number(v);
  const rounded = Math.round(n * 100) / 100;
  return rounded.toLocaleString("zh-CN", { maximumFractionDigits: 2, minimumFractionDigits: 0 });
}

export function fmtPrice(v: number | null | undefined): string {
  if (v == null || !isFinite(Number(v))) return "-";
  const n = Number(v);
  const abs = Math.abs(n);
  const fixed = abs < 10 ? n.toFixed(3) : n.toFixed(2);
  return fixed.replace(/\.?0+$/, "");
}

/**
 * 百分比格式化:
 * - 如果 isRatio=true (默认), 入参是小数比率 (0.05 => 5%).
 * - 如果 isRatio=false, 入参已经是百分数 (5 => 5%).
 */
export function fmtPct(v: number | null | undefined, isRatio = true): string {
  if (v == null || !isFinite(Number(v))) return "-";
  const n = Number(v) * (isRatio ? 100 : 1);
  const rounded = Math.round(n * 100) / 100;
  return rounded.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) + "%";
}

export function fmtNum(v: number | null | undefined): string {
  if (v == null || !isFinite(Number(v))) return "-";
  const n = Math.round(Number(v) * 100) / 100;
  return n.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

// ==================== 参数字段英文 → 中文标题 (回测/图表统一) ====================
const PARAM_CN: Record<string, string> = {
  window: "均线窗口",
  fast: "快线窗口",
  slow: "慢线窗口",
  avg_days: "平均天数",
  vol_expand: "放量阈值",
  vol_shrink: "缩量阈值",
  min_amount_yi: "最低成交额(亿)",
  rsi_fast: "RSI快线",
  rsi_slow: "RSI慢线",
  oversold: "超卖阈值",
  vol_burst: "放量倍数",
  vwap_band: "VWAP带宽",
  surge: "涨幅阈值",
  stale_min: "停滞分钟",
  gate: "开始时间",
  exit_time: "平仓时间",
  switch_every: "切换间隔",
  stop_loss_pct: "止损比例",
  take_profit_pct: "止盈比例",
  max_pos: "最大仓位",
  trailing_pct: "跟踪回撤",
  leverage: "杠杆倍数",
  threshold: "阈值",
  period: "周期",
  symbol: "标的",
  cash: "初始资金",
  qfq: "前复权",
  interval: "轮询间隔",
  execute_time: "执行时间",
  execute_every_poll: "每次轮询执行",
  poll_seconds: "轮询秒数",
};

const STAT_CN: Record<string, string> = {
  // ===== vectorbt 原生字段 (带空格和 [%]) =====
  start: "开始日期",
  end: "结束日期",
  period: "回测周期",
  start_value: "初始资金",
  end_value: "最终权益",
  total_return: "总收益",
  benchmark_return: "基准收益",
  max_drawdown: "最大回撤",
  max_drawdown_duration: "最大回撤期",
  ann_return: "年化收益",
  annual_return: "年化收益",
  ann_volatility: "年化波动率",
  volatility: "波动率",
  sharpe_ratio: "夏普比率",
  sharpe: "夏普比率",
  calmar_ratio: "卡玛比率",
  calmar: "卡玛比率",
  sortino_ratio: "索提诺比率",
  sortino: "索提诺比率",
  skew: "偏度",
  kurtosis: "峰度",
  tail_ratio: "尾部比率",
  value_at_risk: "在险价值 (VaR)",
  expected_shortfall: "条件在险价值 (CVaR)",
  cvar: "条件在险价值",
  trades: "交易笔数",
  win_rate: "胜率",
  best_trade: "最佳单笔",
  worst_trade: "最差单笔",
  avg_winning_trade: "平均盈利",
  avg_losing_trade: "平均亏损",
  avg_winning_trade_duration: "平均持仓(盈)",
  avg_losing_trade_duration: "平均持仓(亏)",
  profit_factor: "盈亏比",
  expectancy: "期望收益",
  // ===== 自定义字段 =====
  total_pnl: "累计盈亏",
  equity: "最终权益",
  cost: "投入成本",
  bars: "K线数量",
  signals: "信号数",
  turnover: "换手率",
  fee: "手续费",
  slippage: "滑点",
  initial_cash: "初始资金",
  final_equity: "最终权益",
  returns: "收益率",
  days: "交易天数",
  exposure: "敞口比例",
  wins: "盈利笔数",
  losses: "亏损笔数",
  avg_win: "平均盈利",
  avg_loss: "平均亏损",
  exec_pricing: "成交价格",
  strategy: "策略",
};

/** 规范化 vbt 原始 stat key:
 *  "Total Return [%]" → "total_return"
 *  "Avg Winning Trade [%]" → "avg_winning_trade"
 *  "Max Drawdown Duration" → "max_drawdown_duration"
 *  返回 { norm: 规范化key, hadPctSuffix: 原key 是否带 [%] (值已是百分数非比率) }
 */
function _normStatKey(key: string): { norm: string; hadPctSuffix: boolean } {
  let s = String(key ?? "");
  const hadPctSuffix = /\[.*%.*\]/.test(s);
  s = s.replace(/\[.*?\]/g, "");        // 去掉 [%] 等括号内容
  s = s.trim().toLowerCase();
  s = s.replace(/[^a-z0-9]+/g, "_");     // 空格/标点 → 下划线
  s = s.replace(/^_+|_+$/g, "");         // 去首尾下划线
  // 去掉尾部重复的 "_pct" / "_return" 叠加修饰
  return { norm: s, hadPctSuffix };
}

export function paramCn(key: string): string {
  if (!key) return "";
  if (PARAM_CN[key]) return PARAM_CN[key];
  // 后缀匹配: *_pct → 百分之x; *_days → x天数
  if (key.endsWith("_pct")) return paramCn(key.slice(0, -4)) + "(%)";
  if (key.endsWith("_days")) return paramCn(key.slice(0, -5)) + "(天)";
  if (key.endsWith("_min")) return paramCn(key.slice(0, -4)) + "(分钟)";
  if (key.endsWith("_window")) return paramCn(key.slice(0, -7)) + "窗口";
  return key;
}

export function statCn(key: string): string {
  if (!key) return "";
  if (STAT_CN[key]) return STAT_CN[key];                // 精确命中 (如自定义字段 strategy/exec_pricing)
  const { norm } = _normStatKey(key);
  if (STAT_CN[norm]) return STAT_CN[norm];              // 规范化后命中 (vbt 字段)
  // 未命中: 用原 key 的可读格式 (下划线→空格)
  const readable = key.replace(/_/g, " ").replace(/\[.*?\]/g, "").trim();
  return readable || key;
}

/** 根据 stat key 自动猜格式化类型 (pct / money / num).
 *  支持 vbt 原始字段: 带 "[%]" 的 key 表示值已是百分数 (如 202.3 表示 202.3%), 不再乘 100.
 */
export function formatStat(key: string, v: any): string {
  if (v == null) return "-";
  const { norm, hadPctSuffix } = _normStatKey(key);
  // 判断百分比类 key (按 norm 判断, 更鲁棒)
  const isPctKey =
    /(_pct|_return|drawdown|_rate|_ratio|turnover|exposure|volatility|win_rate|best_trade|worst_trade|avg_winning_trade|avg_losing_trade|expectancy)$/.test(norm) ||
    /^(total_return|benchmark_return|ann_return|annual_return|max_drawdown)$/.test(norm);
  // 判断金额类 key
  const isMoneyKey =
    /(_pnl|_equity|_cost|_cash|_fee|_profit|_loss|_amount|_value|at_risk|cvar|start_value|end_value|initial_cash|final_equity|total_pnl)$/.test(norm) &&
    !/^(wins|losses|trades|bars|signals|days)$/.test(norm);
  // 整数型字段: 交易笔数、天数等 (直接取整, 不加小数)
  const isIntKey = /^(trades|wins|losses|bars|signals|days|period)$/.test(norm);

  const n = Number(v);
  if (!isFinite(n)) return String(v);
  if (isIntKey) return Math.round(n).toLocaleString("zh-CN");
  if (isPctKey) {
    // hadPctSuffix: 原值已是百分数 (5.23 = 5.23%), isRatio=false
    // 否则: 数字 < 10 当比率 (0.05 → 5%), 否则当百分数直接加 %
    const isRatio = hadPctSuffix ? false : Math.abs(n) < 10;
    return fmtPct(n, isRatio);
  }
  if (isMoneyKey) return fmtMoney(n);
  return fmtNum(n);
}
