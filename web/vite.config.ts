import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The Python app (uvicorn) in development. Override with BACKEND_URL when it is elsewhere.
const backendTarget = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

// Extra hostnames the dev server will answer to — needed when you expose the dev server
// through a tunnel (ngrok / cloudflared) so you can exercise the real domain-key check.
const allowedHosts = (process.env.ALLOWED_HOSTS ?? "")
  .split(",")
  .map((h) => h.trim())
  .filter(Boolean);

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    // ONE origin for the browser. ChatKit's custom API client is invoked from OUR page
    // (not from inside OpenAI's iframe), so a same-origin `/chatkit` means no CORS and the
    // session cookie rides along for free. The proxy is what makes that true in dev too.
    proxy: {
      "/api": { target: backendTarget, changeOrigin: true },
      // Streaming endpoint. Vite's proxy does not buffer or rewrite Content-Type, which
      // matters more than it looks: a 200 whose content-type is not text/event-stream is
      // indistinguishable from a 5xx to ChatKit and triggers 5 silent retries (~25 s).
      "/chatkit": { target: backendTarget, changeOrigin: true },
    },
    ...(allowedHosts.length > 0 ? { allowedHosts } : {}),
  },
  build: {
    // Served by the Python app (or Caddy) in production. Any unknown path must fall back to
    // index.html — the router is client-side.
    outDir: "dist",
    sourcemap: true,
  },
});
