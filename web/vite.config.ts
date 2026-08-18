import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Listen on all interfaces and allow any Host header so the dev server
    // is reachable from a remote browser (LAN IP, SSH tunnel, or
    // forwarded port). The proxy below routes ``/api`` to the FastAPI
    // backend on the same host.
    host: true,
    proxy: {
      "/api": {
        target: "http://localhost:10000",
        changeOrigin: true,
      },
    },
  },
  preview: { port: 10000 },
});
