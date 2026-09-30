import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// base must match the aiohttp static route prefix so built assets resolve
export default defineConfig({
  plugins: [react()],
  base: "/static/dist/",
  resolve: {
    alias: {
      // 壳 API 门面（双构建契约）：主构建指向真模块；插件构建由
      // plugin-kit/vite.config.mjs 指到全局垫片。源码统一 import "coara:shell"。
      "coara:shell": fileURLToPath(new URL("./src/lib/shellApi.ts", import.meta.url)),
    },
  },
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
      // 冻结共享基线（《空间能力系统》槽位四地基）：三个 baseline 条目稳定
      // 命名，import map 把裸标识符映射过去，插件 externals 对着标识符、
      // 运行时共享壳的 React 生态（不自带）。插件构建见 plugins/README.md。
      input: {
        main: "index.html",
      },
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
