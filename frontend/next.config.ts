import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // API requests to /api/* are proxied to the FastAPI backend during development.
  async rewrites() {
    const apiBase =
      process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
    return [
      {
        source: "/api/:path*",
        destination: `${apiBase}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
