import type { NextConfig } from "next";

// The Python stepping server (rewind.ui / start_gemma_stepping.py). The
// browser talks to it through the dev-server rewrites below so we sidestep
// CORS without changing the Python side. Override in .env.local if needed.
const STEPPING_URL = process.env.REWIND_STEPPING_URL ?? "http://127.0.0.1:8484";

const nextConfig: NextConfig = {
  output: "standalone",
  typescript: {
    ignoreBuildErrors: true,
  },
  reactStrictMode: false,
  async rewrites() {
    return [
      // Proxy the stepping-server API to the Python backend so the browser
      // avoids CORS. Matches POST /sessions, GET /sessions[/{id}][/stream],
      // POST /sessions/{id}/decide, POST /sessions/{id}/restart-from,
      // DELETE /sessions/{id}.
      {
        source: "/api/v1/sessions/:path*",
        destination: `${STEPPING_URL}/api/v1/sessions/:path*`,
      },
      {
        source: "/api/v1/sessions",
        destination: `${STEPPING_URL}/api/v1/sessions`,
      },
    ];
  },
};

export default nextConfig;
