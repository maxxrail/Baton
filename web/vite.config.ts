/// <reference types="vitest" />
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The Baton API. scripts/dev.py sets BATON_API_URL when it runs the API on another port.
const api = process.env.BATON_API_URL ?? "http://localhost:8000";

// Everything the browser asks the API for goes through Vite's proxy, so the widget, the
// console and the API share one origin (cookies and WebSockets just work, no CORS).
const proxied = ["/conversations", "/aops", "/ops", "/channels", "/healthz"];

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      ...Object.fromEntries(proxied.map((p) => [p, { target: api, changeOrigin: false }])),
      "/ws": { target: api.replace(/^http/, "ws"), ws: true },
    },
  },
  build: {
    // Mermaid (AOP graphs in the console) is large but loaded on demand, in its own chunks.
    chunkSizeWarningLimit: 800,
    rollupOptions: {
      input: {
        app: fileURLToPath(new URL("index.html", import.meta.url)),
        demo: fileURLToPath(new URL("demo-store.html", import.meta.url)),
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
