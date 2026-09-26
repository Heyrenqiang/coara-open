import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base must match the aiohttp static route prefix so built assets resolve
export default defineConfig({
  plugins: [react()],
  base: "/static/dist/",
  server: {
    port: 5173,
    proxy: {
      "/ws": {
        target: "ws://127.0.0.1:8080",
        ws: true,
      },
      "/api": {
        target: "http://127.0.0.1:8080",
      },
      "/static": {
        target: "http://127.0.0.1:8080",
      },
      "/attachments": {
        target: "http://127.0.0.1:8080",
      },
    },
  },
  build: {
    outDir: "../static/dist",
    emptyOutDir: true,
    // Keep them in one chunk; splitting causes circular chunk graphs.
    chunkSizeWarningLimit: 1100,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules")) {
            // React + router + state-management ecosystem.
            // Keep these together to avoid circular chunk warnings.
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
            // avoid circular chunk warnings.
            if (
              id.includes("antd") ||
              id.includes("@ant-design") ||
              id.includes("/rc-")
            ) {
              return "vendor-ui";
            }
            // KaTeX math rendering (large, split from the markdown plugin stack)
            if (id.includes("katex")) {
              return "vendor-katex";
            }
            // Markdown rendering plugin stack (without KaTeX)
            if (
              id.includes("react-markdown") ||
              id.includes("remark-") ||
              id.includes("rehype-") ||
              id.includes("micromark") ||
              id.includes("mdast") ||
              id.includes("unist") ||
              id.includes("hast")
            ) {
              return "vendor-markdown";
            }
          }
        },
      },
    },
  },
});
