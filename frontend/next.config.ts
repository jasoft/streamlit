import type { NextConfig } from "next";

// 后端 FastAPI 地址: 主树缺省 8000; 多 worktree 各自随机端口由 dev.sh 注入 BACKEND_ORIGIN
const backendOrigin = process.env.BACKEND_ORIGIN ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/backend/:path*",
        destination: `${backendOrigin}/api/:path*`,
      },
      {
        source: "/ws/market",
        destination: `${backendOrigin}/ws/market`,
      },
      {
        source: "/ws/mock_stream",
        destination: `${backendOrigin}/ws/mock_stream`,
      },
    ];
  },
};

export default nextConfig;
