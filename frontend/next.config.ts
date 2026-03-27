import type { NextConfig } from "next";

const BACKEND = process.env.BACKEND_URL ?? "http://localhost:8003";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      { source: "/socket.io/:path*", destination: `${BACKEND}/socket.io/:path*` },
      { source: "/v1/:path*",        destination: `${BACKEND}/v1/:path*` },
      { source: "/health",           destination: `${BACKEND}/health` },
    ];
  },
};

export default nextConfig;
