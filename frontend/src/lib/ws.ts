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
