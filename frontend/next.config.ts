import type { NextConfig } from "next";

// 多 worktree 并行开发: BACKEND_ORIGIN 指向各自的 FastAPI (默认主栈 :8000)
const BACKEND = process.env.BACKEND_ORIGIN || "http://localhost:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/backend/:path*",
        destination: `${BACKEND}/api/:path*`,
      },
      {
        source: "/ws/market",
        destination: `${BACKEND}/ws/market`,
      },
      {
        source: "/ws/mock_stream",
        destination: `${BACKEND}/ws/mock_stream`,
      },
    ];
  },
};

export default nextConfig;
