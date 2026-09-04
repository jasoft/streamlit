import type { NextConfig } from "next";

// 后端端口与 dev.sh 对齐 (BACKEND_PORT, 默认 8000), 便于多 worktree 并行开发
const BACKEND = `http://localhost:${process.env.BACKEND_PORT ?? 8000}`;

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
