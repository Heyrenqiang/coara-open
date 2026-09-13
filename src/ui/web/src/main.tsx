import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { initAuthToken } from "./lib/auth";
import { applyTokens } from "./theme/tokens";
import "./index.css";

// 旧版 Chromium（如 Edge 92 及更早）缺少 ES2022 的 Object.hasOwn，
// micromark 解析 Markdown 时调用它会抛 TypeError，React 整树卸载成白屏。
// 缺则用最简实现补齐（仅当环境真的没有时才打补丁）。
if (typeof (Object as { hasOwn?: unknown }).hasOwn !== "function") {
  (Object as unknown as { hasOwn: (obj: object, key: PropertyKey) => boolean }).hasOwn = (
    obj: object,
    key: PropertyKey,
  ) => Object.prototype.hasOwnProperty.call(obj, key);
}

// 在 React 渲染前初始化 token：把 URL 中的 ?token=xxx 持久化到
// sessionStorage，并从 URL 中清除。后续所有 API/WS 调用从 sessionStorage
// 读取，避免路由切换或刷新时丢 token 导致 401。
initAuthToken();

// 设计令牌单一真源 → 注入 :root CSS 变量。
// 须在 React 渲染前执行，避免首帧拿到未定义的 var()。
applyTokens();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
