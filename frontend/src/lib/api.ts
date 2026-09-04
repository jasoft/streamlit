// 后端 API 封装 — 统一走 Next.js rewrite /api/backend/* -> FastAPI

const BASE = "/api/backend";

async function fetchJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(BASE + path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!r.ok) {
    const raw = await r.text();
    let msg = raw;
    // 尝试解析 FastAPI 全局异常 handler 的 JSON 错误格式 {"detail":"异常: 消息 + traceback","error":"类型","message":"消息"}
    try {
      const j = JSON.parse(raw);
      if (typeof j.detail === "string") msg = j.detail;
      else if (j.detail) msg = JSON.stringify(j.detail);
      else if (j.message) msg = `${j.error || ""}: ${j.message}`.trim();
    } catch { /* 非 JSON, 用原始文本 */ }
    // 截断过长 traceback: 保留 1500 字符, 足够覆盖 Type + 消息 + 关键栈帧
    if (msg.length > 1500) {
      msg = msg.slice(0, 1500) + `\n... (共 ${msg.length} 字符已截断)`;
    }
    throw new Error(msg || `HTTP ${r.status}`);
  }
  return r.json();
}

export const api = {
  strategies: () => fetchJSON<any[]>("/strategies"),
  config: () => fetchJSON<any>("/config"),
  saveConfig: (cfg: any) =>
    fetchJSON("/config", { method: "PUT", body: JSON.stringify(cfg) }),
  start: (name: string) => fetchJSON(`/strategies/${encodeURIComponent(name)}/start`, { method: "POST" }),
  stop: (name: string) => fetchJSON(`/strategies/${encodeURIComponent(name)}/stop`, { method: "POST" }),
  runOnce: (name: string) => fetchJSON(`/strategies/${encodeURIComponent(name)}/run-once`, { method: "POST" }),
  evals: (name: string, tail = 60) => fetchJSON<any[]>(`/evals/${encodeURIComponent(name)}?tail=${tail}`),
  positions: () => fetchJSON("/positions"),
  quote: (symbol: string) => fetchJSON(`/quote/${encodeURIComponent(symbol)}`),
  intraday: (symbol: string) => fetchJSON<any>(`/intraday/${encodeURIComponent(symbol)}`),
  backtest: (body: any) =>
    fetchJSON<any>("/backtest", { method: "POST", body: JSON.stringify(body) }),
  // --- 图会话新链路 ---
  kline: (symbol: string, tf = "day", qfq = true, limit = 3000) =>
    fetchJSON<any>(`/kline?symbol=${encodeURIComponent(symbol)}&tf=${tf}&qfq=${qfq}&limit=${limit}`),
  strategySchema: (name: string) =>
    fetchJSON<any>(`/strategies/${encodeURIComponent(name)}/schema`),
  chartRun: (body: any) =>
    fetchJSON<any>("/charts/run", { method: "POST", body: JSON.stringify(body) }),
  optimize: (body: any) =>
    fetchJSON<any>("/optimize", { method: "POST", body: JSON.stringify(body) }),
  // --- 参数优化（异步 start + poll） ---
  paramOptimizeStart: (body: any) =>
    fetchJSON<any>("/param-optimize/start", { method: "POST", body: JSON.stringify(body) }),
  paramOptimizePoll: (jobId: string) =>
    fetchJSON<any>(`/param-optimize/poll/${encodeURIComponent(jobId)}`),
  chartSessions: () => fetchJSON<{ sessions: any[] }>("/charts/sessions"),
  saveChartSessions: (sessions: any[]) =>
    fetchJSON("/charts/sessions", { method: "PUT", body: JSON.stringify(sessions) }),
  // --- 选股自动交易 ---
  pickerStatus: () => fetchJSON<any>("/picker"),
  pickerStrategies: () => fetchJSON<any[]>("/picker/pickers"),
  addPickerGroup: (body: any) =>
    fetchJSON("/picker/groups", { method: "POST", body: JSON.stringify(body) }),
  updatePickerGroup: (id: string, body: any) =>
    fetchJSON(`/picker/groups/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) }),
  deletePickerGroup: (id: string) =>
    fetchJSON(`/picker/groups/${encodeURIComponent(id)}`, { method: "DELETE" }),
  runPickerOnce: (id: string) =>
    fetchJSON(`/picker/groups/${encodeURIComponent(id)}/run-once`, { method: "POST" }),
  startPickerEngine: (body: any) =>
    fetchJSON("/picker/engine/start", { method: "POST", body: JSON.stringify(body) }),
  stopPickerEngine: () =>
    fetchJSON("/picker/engine/stop", { method: "POST" }),
};
