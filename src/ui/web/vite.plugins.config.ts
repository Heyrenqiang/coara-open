// 内置插件构建（《空间能力系统》槽位四）：把 src/plugins/<名>/ 打成
// ../static/dist/plugins/<名>.js 单文件 ESM。与主构建（vite.config.ts）分家，
// 产物不带 hash（壳装载器按稳定名引用）。基线裸 import 经垫片重定向到
// window.__COARA_BASELINE__，与壳共享同一份 React/antd/store 单例。
//
// 新增内置插件：PLUGINS 数组加一行，并在 src/plugins/<名>/index.tsx 提供
// default export（页面组件）与 mount 导出。
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const rootDir = dirname(fileURLToPath(import.meta.url));
const shim = (name: string) => resolve(rootDir, "plugin-kit/shims", name);

const PLUGINS = ["usage", "review", "config", "workflow", "records"];

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      // 正则精确匹配：字符串键会前缀命中（react 吃掉 react/jsx-runtime）
      { find: /^react$/, replacement: shim("react.js") },
      { find: /^react-dom$/, replacement: shim("react-dom.js") },
      { find: /^react-dom\/client$/, replacement: shim("react-dom-client.js") },
      { find: /^react\/jsx-runtime$/, replacement: shim("react-jsx-runtime.js") },
      { find: /^react-router-dom$/, replacement: shim("react-router-dom.js") },
      { find: /^zustand$/, replacement: shim("zustand.js") },
      { find: /^antd$/, replacement: shim("antd.js") },
      { find: /^@ant-design\/icons$/, replacement: shim("antd-icons.js") },
      { find: /^coara:shell$/, replacement: shim("coara-shell.js") },
    ],
  },
  build: {
    outDir: "../static/dist/plugins",
    emptyOutDir: true,
    rollupOptions: {
      input: Object.fromEntries(
        PLUGINS.map((name) => [name, resolve(rootDir, `src/plugins/${name}/index.tsx`)]),
      ),
      // 应用构建默认不保入口导出签名，mount/default 会被 treeshake 摇掉——
      // 插件入口的导出就是它的运行时契约，必须保住（build 级选项）
      preserveEntrySignatures: "exports-only",
      output: {
        format: "es",
        entryFileNames: "[name].js",
        chunkFileNames: "chunks/[name]-[hash].js",
      },
    },
  },
});
