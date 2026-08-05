import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// Rewind timeline UI — Vite config.
export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    host: "127.0.0.1",
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
