import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// Rewind timeline UI — Vite config.
//
// The built artifact lands in `../web/dist` (sibling of `src/rewind`) and is
// served by FastAPI at `/ui`. Dev mode (`vite` on :5173) proxies `/api` and
// `/v1` to the FastAPI receiver on :8484 so the SPA can run against the
// real Python backend without CORS configuration.
//
// Base path is `/ui/` so Vite emits absolute asset URLs that work inside the
// FastAPI-mounted sub-app — without this, Vite assumes root-relative URLs
// (`/assets/...`) which would collide with the OTLP receiver's routes.
export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8484",
      "/v1": "http://127.0.0.1:8484",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    // Tinier artifact: no inline asset base64, names hashed for caching.
    assetsDir: "assets",
    chunkSizeWarningLimit: 1_500_000,
  },
});
