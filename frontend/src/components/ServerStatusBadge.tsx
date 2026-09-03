"use client";
// 本组件使用 useEffect/useState, 必须作为 Client Component 使用.
// layout.tsx (Server Component) 直接静态 import 即可, 不需要 next/dynamic(ssr:false).
import { useEffect, useState } from "react";

/**
 * 右上角后端服务器状态徽章: 每 5s 探活一次 /api/backend/health.
 *
 * - 绿色 ●: FastAPI 后端在线, fdata serve 正常
 * - 黄色 ●: FastAPI 在线, 但 fdata serve 离线 (健康接口返回 fdata=false)
 * - 红色 ●: FastAPI 离线 (接口超时 / 非 2xx)
 * - 旁边显示最后一次成功探活时间
 */
type Status = "online" | "warn" | "offline";

const POLL_MS = 5000;

function fmtTime(d: Date): string {
  const p = (n: number) => n.toString().padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export default function ServerStatusBadge() {
  const [status, setStatus] = useState<Status>("online");
  const [lastOk, setLastOk] = useState<Date | null>(null);

  useEffect(() => {
    let aborted = false;
    let timer: ReturnType<typeof setTimeout>;

    async function probe(): Promise<void> {
      try {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), 3000);
        const r = await fetch("/api/backend/health", { signal: ctrl.signal });
        clearTimeout(t);
        if (aborted) return;
        if (r.ok) {
          try {
            const j = (await r.json()) as { fdata?: boolean; backend?: boolean };
            setStatus(j.fdata === false ? "warn" : "online");
          } catch {
            setStatus("online");
          }
          setLastOk(new Date());
        } else {
          setStatus("offline");
        }
      } catch {
        if (!aborted) setStatus("offline");
      } finally {
        if (!aborted) timer = setTimeout(probe, POLL_MS);
      }
    }

    void probe();
    return () => {
      aborted = true;
      clearTimeout(timer);
    };
  }, []);

  const color =
    status === "online"
      ? "text-[#26a69a]"
      : status === "warn"
      ? "text-[#e0a800]"
      : "text-[#e53935]";
  const label =
    status === "online" ? "Online" : status === "warn" ? "fdata 离线" : "Offline";
  const tip =
    status === "online"
      ? `后端正常 · 最后探活 ${lastOk ? fmtTime(lastOk) : "-"}`
      : status === "warn"
      ? "FastAPI 在线但 fdata serve 离线, 行情不可用 · 尝试重启 dev.sh"
      : "后端 FastAPI 无响应 · 尝试运行 ./dev.sh 重启服务";

  return (
    <div
      className="ml-auto text-xs flex items-center gap-2 cursor-help"
      title={tip}
    >
      <span className={`${color} font-bold text-base leading-none`}>●</span>
      <span className="text-[#888]">
        Backend:{" "}
        <span className={color}>{label}</span>
        {lastOk ? (
          <span className="text-[#555] ml-2">· {fmtTime(lastOk)}</span>
        ) : null}
      </span>
    </div>
  );
}
