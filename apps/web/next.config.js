/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  agentRules: false,
  async rewrites() {
    return [
      {
        source: "/api/v1/:path*",
        destination: `${process.env.API_PROXY_TARGET || "http://127.0.0.1:8000"}/api/v1/:path*`,
      },
    ];
  },
};

export default nextConfig;
