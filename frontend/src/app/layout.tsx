import type { Metadata } from "next";
import "./globals.css";
import Link from "next/link";

export const metadata: Metadata = {
  title: "量化交易控制台",
  description: "个人量化 — 回测 + 实盘",
};

const nav = [
  { href: "/live", label: "实盘策略", icon: "🚦" },
  { href: "/backtest", label: "回测", icon: "📈" },
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
          <div className="ml-auto text-xs text-[#666]">
            Backend: <span className="text-[#26a69a]">●</span> FastAPI :8000
          </div>
        </nav>
        <main className="p-6 max-w-[1600px] mx-auto">{children}</main>
      </body>
    </html>
  );
}
