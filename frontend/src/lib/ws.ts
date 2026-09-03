// WebSocket 管理 — 自动重连 + 多 symbol 订阅
// Next.js rewrite: /ws/market -> http://localhost:8000/ws/market

type TickMsg = {
  type: "tick";
  symbol: string;
  ts: string;
  snapshot: {
    last: number;
    pre_close: number;
    change_pct: number;
    high: number;
    low: number;
    amount: number;
  };
  bars: {
    time: string;
    close: number;
    vwap: number | null;
    dif: number;
    dea: number;
    macd_hist: number;
  }[];
};

type WsMsg = TickMsg | { type: "error"; msg: string };

type Listener = (msg: WsMsg) => void;

export class MarketWs {
  private ws: WebSocket | null = null;
  private symbols: Set<string> = new Set();
  private listeners: Set<Listener> = new Set();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private url: string;

  constructor() {
    const proto = typeof window !== "undefined" && window.location.protocol === "https:" ? "wss:" : "ws:";
    this.url = `${proto}//${typeof window !== "undefined" ? window.location.host : "localhost:3000"}/ws/market`;
  }

  subscribe(symbol: string) {
    this.symbols.add(symbol);
    this.reconnect();
  }

  unsubscribe(symbol: string) {
    this.symbols.delete(symbol);
  }

  onMessage(fn: Listener) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private connect() {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return;
    if (this.ws) this.ws.close();

    const url = this.url + "?symbols=" + Array.from(this.symbols).join(",");
    this.ws = new WebSocket(url);

    this.ws.onmessage = (ev) => {
      try {
        const msg: WsMsg = JSON.parse(ev.data);
        this.listeners.forEach((fn) => fn(msg));
      } catch {}
    };

    this.ws.onclose = () => {
      this.scheduleReconnect();
    };

    this.ws.onerror = () => {
      this.ws?.close();
    };
  }

  private scheduleReconnect() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => {
      if (this.symbols.size > 0) this.connect();
    }, 1500);
  }

  private reconnect() {
    this.connect();
  }

  close() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
    this.ws = null;
  }
}

// 单例
export const marketWs = typeof window !== "undefined" ? new MarketWs() : null;
export type { TickMsg };

// ================================================================ 流式测试 WS ===
type MockBar = {
  date: string; open: number; high: number; low: number;
  close: number; volume: number; amount?: number;
};

type MockStreamMsg = {
  type: "bar" | "info" | "error";
  bar?: MockBar;
  orders?: { side: "buy" | "sell"; qty: number; price: number; status: string; pnl?: number; ts?: string }[];
  markers?: { date: string; price: number; action: "买入" | "卖出"; qty?: number; pnl?: number | null }[];
  snapshot?: { cash: number; position: number; avg_cost?: number; equity?: number };
  target?: number;
  msg?: string;
  error?: string;
  pre_close?: number;   // 流式分时: 昨收价 (init info 消息带)
};

/** 一次性流式测试连接: 连上即推, 断开即停, 不重连. */
export class MockStreamWs {
  private ws: WebSocket | null = null;
  private onMsg: (msg: MockStreamMsg) => void;
  private onClose: () => void;

  constructor(onMsg: (msg: MockStreamMsg) => void, onClose: () => void) {
    this.onMsg = onMsg;
    this.onClose = onClose;
  }

  start(symbol: string, strategy: string, tf: string, speed: string, params: Record<string, number>) {
    this.stop();
    const proto = typeof window !== "undefined" && window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = typeof window !== "undefined" ? window.location.host : "localhost:3000";
    const q = `symbol=${encodeURIComponent(symbol)}&strategy=${encodeURIComponent(strategy)}`
            + `&tf=${encodeURIComponent(tf)}&speed=${encodeURIComponent(speed)}`
            + `&params=${encodeURIComponent(JSON.stringify(params || {}))}`;
    this.ws = new WebSocket(`${proto}//${host}/ws/mock_stream?${q}`);
    this.ws.onmessage = (ev) => {
      try { this.onMsg(JSON.parse(ev.data)); } catch {}
    };
    this.ws.onclose = () => this.onClose();
    this.ws.onerror = () => { this.ws?.close(); };
  }

  stop() {
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
  }
}

