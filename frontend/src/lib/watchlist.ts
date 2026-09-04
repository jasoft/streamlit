"use client";

// 自选股共享数据源: 多个 SymbolPicker / 自选股页共用一份 30s TTL 缓存,
// 任一处刷新 (增删/同步) 后通过 listeners 广播给所有挂载的组件.
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

export type WatchStock = {
  symbol: string;          // 归一化: sh601899 / sz159915 (期货等非 A 股为原样)
  code: string;            // 6 位代码; 非 A 股为 ""
  market?: string;         // sh / sz / bj / ""
  name: string;
  source: "manual" | "ths";
  added_at: string;
  last_seen_in_positions?: string;
};

export type WatchlistData = {
  stocks: WatchStock[];
  removed_count?: number;
  auto_sync: boolean;
  last_sync?: string | null;
  last_sync_error?: string | null;
};

const TTL = 30_000;

let cache: { data: WatchlistData | null; ts: number } = { data: null, ts: 0 };
let inflight: Promise<WatchlistData> | null = null;
const listeners = new Set<(d: WatchlistData) => void>();

async function load(force: boolean): Promise<WatchlistData> {
  if (!force && cache.data && Date.now() - cache.ts < TTL) return cache.data;
  if (!inflight) {
    inflight = api.watchlist().then((d: WatchlistData) => {
      cache = { data: d, ts: Date.now() };
      listeners.forEach((fn) => fn(d));
      return d;
    }).finally(() => { inflight = null; });
  }
  return inflight;
}

export function useWatchlist() {
  const [data, setData] = useState<WatchlistData | null>(cache.data);
  const [error, setError] = useState("");

  useEffect(() => {
    const fn = (d: WatchlistData) => { setData(d); setError(""); };
    listeners.add(fn);
    load(false).catch((e) => setError(e instanceof Error ? e.message : String(e)));
    return () => { listeners.delete(fn); };
  }, []);

  // 增删/同步后调用: 强制拉最新并广播给所有组件
  const refresh = useCallback(async () => {
    try {
      await load(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  return { data, error, refresh };
}
