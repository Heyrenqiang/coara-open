import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies /api and /ws to the wdl backend (wdl serve, port 8177).
// Production build outputs to ./dist so `wdl serve` hosts it directly.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8177",
      },
      "/ws": {
        target: "ws://127.0.0.1:8177",
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 1100,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules")) {
            if (
              id.includes("/react/") ||
              id.includes("react-dom") ||
              id.includes("react-router-dom") ||
              id.includes("@remix-run/router") ||
              id.includes("scheduler") ||
              id.includes("use-sync-external-store") ||
              id.includes("zustand")
            ) {
              return "vendor-react";
            }
            if (id.includes("antd") || id.includes("@ant-design") || id.includes("/rc-")) {
              return "vendor-ui";
            }
            if (id.includes("@xyflow")) {
              return "vendor-workflow";
            }
          }
        },
      },
    },
  },
});
