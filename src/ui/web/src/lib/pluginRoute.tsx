// 内置插件页装载器（《空间能力系统》槽位四）：路由元素 = 装载
// /static/dist/plugins/<名>.js 的插件 bundle，调其 mount(el, ctx)。
// 页面本体在主 bundle 外（独立构建，vite.plugins.config.ts），运行时经
// window.__COARA_BASELINE__ 共享壳的 React/antd/store 单例。
import { useEffect, useRef, useState } from "react";
import { getAuthToken, tokenQueryFragment } from "./auth";

type PluginMount = (el: HTMLElement, ctx: Record<string, unknown>) => void | (() => void);

interface BuiltinPluginPageProps {
  /** 插件名：映射 /static/dist/plugins/<name>.js */
  name: string;
}

export function BuiltinPluginPage({ name }: BuiltinPluginPageProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return undefined;
    let disposed = false;
    let cleanup: void | (() => void);
    setError(null);
    host.replaceChildren();

    const token = tokenQueryFragment();
    import(/* @vite-ignore */ `/static/dist/plugins/${name}.js`)
      .then((mod) => {
        if (disposed) return;
        const mount = mod?.default?.mount ?? mod?.mount;
        if (typeof mount !== "function") {
          setError(`内置插件 ${name} 缺少 mount 导出（需重新构建：npm run build）`);
          return;
        }
        try {
          cleanup = (mount as PluginMount)(host, {
            workspace: { name: "", path: "" },
            token: getAuthToken() ?? "",
            apiFetch: (path: string, init?: RequestInit) =>
              fetch(`${path}${path.includes("?") ? "&" : "?"}${token}`, init),
            baseline: window.__COARA_BASELINE__ ?? {},
          });
        } catch (exc) {
          setError(`内置插件 ${name} 挂载失败: ${String(exc)}`);
        }
      })
      .catch((exc) => {
        if (!disposed) setError(`内置插件 ${name} 加载失败: ${String(exc)}`);
      });

    return () => {
      disposed = true;
      try {
        if (typeof cleanup === "function") cleanup();
      } catch {
        // 插件清理失败不阻塞壳
      }
      host.replaceChildren();
    };
  }, [name]);

  return (
    <div
      style={{
        flex: 1,
        height: "100%",
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
      }}
    >
      {error ? (
        <div
          style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "var(--coara-text-muted)",
            fontSize: 13,
            padding: 24,
          }}
        >
          {error}
        </div>
      ) : (
        <div
          ref={hostRef}
          style={{ flex: 1, height: "100%", minHeight: 0, display: "flex", flexDirection: "column" }}
        />
      )}
    </div>
  );
}

/** 内置插件的具名组件装载（如 /workflow/editor/:draftId 取 workflow 插件的
 *  WorkflowEditorView 命名导出）：组件经 createElement 直渲，不走 mount。 */
export function BuiltinPluginComponent({ name, exportName }: { name: string; exportName: string }) {
  const [error, setError] = useState<string | null>(null);
  const [Component, setComponent] = useState<React.ComponentType | null>(null);

  useEffect(() => {
    let disposed = false;
    setError(null);
    setComponent(null);
    import(/* @vite-ignore */ `/static/dist/plugins/${name}.js`)
      .then((mod) => {
        if (disposed) return;
        const C = mod?.[exportName];
        if (typeof C !== "function") {
          setError(`内置插件 ${name} 缺少导出 ${exportName}（需重新构建：npm run build）`);
          return;
        }
        setComponent(() => C);
      })
      .catch((exc) => {
        if (!disposed) setError(`内置插件 ${name} 加载失败: ${String(exc)}`);
      });
    return () => {
      disposed = true;
    };
  }, [name, exportName]);

  if (error) {
    return (
      <div
        style={{
          flex: 1,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "var(--coara-text-muted)",
          fontSize: 13,
          padding: 24,
        }}
      >
        {error}
      </div>
    );
  }
  if (!Component) return null;
  return <Component />;
}
