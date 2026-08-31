// 后端 API 封装 — 统一走 Next.js rewrite /api/backend/* -> FastAPI

const BASE = "/api/backend";

async function fetchJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(BASE + path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
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
    fetchJSON("/backtest", { method: "POST", body: JSON.stringify(body) }),
};
