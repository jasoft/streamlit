import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/backend/:path*",
        destination: "http://localhost:8000/api/:path*",
      },
      {
        source: "/ws/market",
        destination: "http://localhost:8000/ws/market",
      },
      {
        source: "/ws/mock_stream",
        destination: "http://localhost:8000/ws/mock_stream",
      },
    ];
  },
};

export default nextConfig;
