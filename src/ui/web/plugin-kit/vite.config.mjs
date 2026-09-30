// 插件构建套件：把插件源码打成「对着壳冻结基线」的单文件 ESM bundle。
// 用法（插件目录下）：vite build --config node_modules/coara-web/plugin-kit/vite.config.mjs
// 或复制本文件进插件目录。入口默认 src/plugin.tsx，产物 dist/plugin.js。
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const kitDir = dirname(fileURLToPath(import.meta.url));
const shim = (name) => resolve(kitDir, "shims", name);

// 裸标识符 → 壳冻结基线垫片（window.__COARA_BASELINE__）。
// 插件源码按正常 import 写法，构建期重定向到垫片，运行时共享壳的单例。
// 正则精确匹配：字符串键会前缀命中（react 吃掉 react/jsx-runtime）。
const BASELINE_ALIASES = [
  { find: /^react$/, replacement: shim("react.js") },
  { find: /^react-dom$/, replacement: shim("react-dom.js") },
  { find: /^react-dom\/client$/, replacement: shim("react-dom-client.js") },
  { find: /^react\/jsx-runtime$/, replacement: shim("react-jsx-runtime.js") },
  { find: /^react-router-dom$/, replacement: shim("react-router-dom.js") },
  { find: /^zustand$/, replacement: shim("zustand.js") },
  { find: /^antd$/, replacement: shim("antd.js") },
  { find: /^@ant-design\/icons$/, replacement: shim("antd-icons.js") },
  { find: /^coara:shell$/, replacement: shim("coara-shell.js") },
];

export default defineConfig({
  plugins: [react()],
  resolve: { alias: BASELINE_ALIASES },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    lib: {
      entry: resolve(process.cwd(), "src/plugin.tsx"),
      formats: ["es"],
      fileName: () => "plugin.js",
    },
    rollupOptions: {
      // 基线标识符不该进产物：alias 已把它们换成垫片文件，垫片里无裸 import
      output: { inlineDynamicImports: true },
    },
  },
});
