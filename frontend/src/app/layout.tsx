import type { Metadata } from "next";
import "./globals.css";
import Link from "next/link";
// 组件自带 "use client" (独立的 Client Component), 可被 Server Component 直接静态导入
import ServerStatusBadge from "@/components/ServerStatusBadge";

export const metadata: Metadata = {
  title: "量化交易控制台",
  description: "个人量化 — 回测 + 实盘",
};

const nav = [
  { href: "/charts", label: "图会话", icon: "📊" },
  { href: "/backtest", label: "回测", icon: "📈" },
  { href: "/conditions", label: "条件单", icon: "⚡" },
  { href: "/grids", label: "网格", icon: "🌐" },
  { href: "/portfolios", label: "组合", icon: "🧺" },
  { href: "/config", label: "配置", icon: "⚙️" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <nav className="flex items-center gap-6 px-6 h-14 border-b border-[#2a2a2a] bg-[#141414] sticky top-0 z-50">
          <span className="text-[#ff6d00] font-bold text-lg mr-4">QuantUI</span>
          {nav.map((n) => (
            <Link
              key={n.href}
              href={n.href}
              className="text-[#aaa] hover:text-white transition-colors text-sm"
            >
              {n.icon} {n.label}
            </Link>
          ))}
          <ServerStatusBadge />
        </nav>
        <main className="p-6 max-w-[1600px] mx-auto">{children}</main>
      </body>
    </html>
  );
}
