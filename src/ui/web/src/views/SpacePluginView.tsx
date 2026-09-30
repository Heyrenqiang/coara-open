import { useEffect, useRef, useState } from "react";
import { getAuthToken, tokenQueryFragment } from "../lib/auth";
import type { WorkspaceInfo } from "../lib/chatTypes";

/** 插件页壳上下文：一期最小面 + 冻结基线引用。 */
export interface PluginShellContext {
  workspace: { name: string; path: string };
  token: string;
  apiFetch: (path: string, init?: RequestInit) => Promise<Response>;
  /** 冻结共享基线（window.__COARA_BASELINE__）：React/antd/store/tokens/壳组件。
   *  插件从这里取共享实例，不自带 React——见 lib/baseline.ts。 */
  baseline: Record<string, unknown>;
}

/** 插件 bundle 契约：default export 是 mount 函数。
 *  返回 void 或清理函数（unmount 时调用）。
 *  插件技术栈自由（原生 JS / 任意框架打的 ESM），壳只给 DOM 挂载点。 */
type PluginMount = (el: HTMLElement, ctx: PluginShellContext) => void | (() => void);

const PLUGIN_PREFIX = "plugin:";

/** home_view 取值是否为插件声明（`plugin:bundle.js`）。 */
export function isPluginHomeView(homeView: string | undefined | null): boolean {
  return typeof homeView === "string" && homeView.trim().startsWith(PLUGIN_PREFIX);
}

function pluginBundleName(homeView: string): string {
  return homeView.trim().slice(PLUGIN_PREFIX.length).trim();
}

interface SpacePluginViewProps {
  workspace: WorkspaceInfo;
  homeView: string;
}

/** 空间插件页槽位：整页渲染空间自声明的 UI bundle。
 *
 *  数据流：home_view = "plugin:bundle.js" → 拉
 *  `/api/space-ui/{space}/{relpath}`（token 鉴权）→ dynamic import
 *  → default(el, ctx) 挂载。加载失败显错误页而非静默空白。
 */
export function SpacePluginView({ workspace, homeView }: SpacePluginViewProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const bundleName = pluginBundleName(homeView);
  // 插件口按空间名/条目 id 寻址（后端 resolve_name_or_id），与目录路径无关
  const spaceKey = workspace.name;

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !bundleName) return undefined;
    let disposed = false;
    let cleanup: void | (() => void);

    setError(null);
    host.replaceChildren();

    const token = tokenQueryFragment();
    const url = `/api/space-ui/${encodeURIComponent(spaceKey)}/${encodeURIComponent(bundleName)}${token ? `?${token}` : ""}`;
    import(/* @vite-ignore */ url)
      .then((mod) => {
        if (disposed) return;
        const mount = mod?.default as PluginMount | undefined;
        if (typeof mount !== "function") {
          setError("插件 bundle 缺少 default mount 函数导出");
          return;
        }
        const ctx: PluginShellContext = {
          workspace: { name: workspace.name, path: workspace.path },
          token: getAuthToken() ?? "",
          apiFetch: (path, init) =>
            fetch(`${path}${path.includes("?") ? "&" : "?"}${token}`, init),
          baseline: window.__COARA_BASELINE__ ?? {},
        };
        try {
          cleanup = mount(host, ctx);
        } catch (exc) {
          setError(`插件挂载失败: ${String(exc)}`);
        }
      })
      .catch((exc) => {
        if (!disposed) setError(`插件加载失败: ${String(exc)}`);
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
  }, [spaceKey, bundleName, workspace.name, workspace.path]);

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
      {error ? (
        <div
          style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "var(--coara-text-muted)",
            fontSize: 13,
            whiteSpace: "pre-wrap",
            padding: 24,
          }}
        >
          {error}
        </div>
      ) : (
        <div ref={hostRef} style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }} />
      )}
    </div>
  );
}
