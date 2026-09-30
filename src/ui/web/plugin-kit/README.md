# 空间 UI 插件开发

任何工作空间都可以自带一个自定义主页：壳整页渲染你的 ESM bundle，共享壳的 React/antd/store/设计令牌。

## 快速开始

最小形态（原生 JS，零构建）：空间目录写 `space.yaml`：

```yaml
home_view: plugin:plugin.js
```

把 bundle 放进 `<空间>/.coara/ui/plugin.js`：

```js
export default function mount(el, ctx) {
  const { PageShell, React } = ctx.baseline;
  const d = document.createElement("div");
  d.style.padding = "24px";
  d.textContent = `这是 ${ctx.workspace.name} 的自定义主页`;
  el.appendChild(d);
}
```

刷新页面，侧边栏点该空间主页即渲染。

## 用 React 写插件

React 插件不自带 React——从壳的冻结基线取共享实例（不共享=两份 React 运行时，hooks 全炸）。构建用官方套件把裸 import 重定向到基线垫片：

- 插件目录 `package.json` 装好 `vite` 与 `@vitejs/plugin-react`
- 拷 `plugin-kit/vite.config.mjs` 进插件目录（或 `--config` 指过来）
- 入口 `src/plugin.tsx`，正常写 `import { useState } from "react"`、`import antd from "antd"`
- `vite build` 产物 `dist/plugin.js`，拷进 `<空间>/.coara/ui/`

注意：antd 与图标库是无界组件库，垫片只给 namespace default——写 `import antd from "antd"` 再 `const { Button } = antd`，不要写 `import { Button } from "antd"`（构建期会报无此导出）。

## 契约

- bundle 必须 default export 一个 `mount(el, ctx)` 函数；返回一个函数则作为卸载清理（壳 unmount 时调用）
- `ctx.workspace` 当前空间 `{name, path}`；`ctx.token` 鉴权令牌；`ctx.apiFetch(path, init)` 带 token 的内核 API 请求
- `ctx.baseline` 冻结基线表：`React / ReactDOM / ReactDOMClient / ReactRouterDOM / zustand / antd / antdIcons / useStore / tokens / cssVars / coaraAntdTheme / PageShell / PageHeader`。表只增不减

## 边界

- 无 HMR，改完刷新页面（资源口 no-cache）
- 插件与壳同域同 token，能力等同主站——只装你信任的插件
- 手机/CLI 端没有插件槽位，插件空间在那两端退化为纯对话空间
