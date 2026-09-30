import { Suspense, lazy, useEffect } from "react";
import { BrowserRouter, Routes, Route, Navigate, useNavigate } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { getWS } from "./lib/ws";
import { startTabPresence } from "./lib/tabPresence";
import { topUpSessionTail } from "./lib/chatHydrate";
import { useStore } from "./lib/store";
import { fetchAccountStatus } from "./lib/account";
import { coaraAntdTheme } from "./theme/tokens";
import { AppLayout } from "./components/AppLayout";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { SingleTabGuard } from "./components/SingleTabGuard";
import { InteractionDialog } from "./features/interaction/InteractionDialog";
import { ChatView } from "./views/ChatView";

// Lazy-load heavy route views so the initial chunk only pays for the chat page.
// 内置五页：主构建直连 plugins/*/index 的 default（同壳 React 树，PageShell 高度链完整）。
// 勿改走 /static/dist/plugins/*.js 运行时装载——另根 createRoot + 高度链断裂会整页空白。
// 空间 home_view=plugin:… 仍走 SpacePluginView（槽位四运行时插件）。
const FileView = lazy(() =>
  import("./views/FileView").then((m) => ({ default: m.FileView }))
);
const RecordsView = lazy(() => import("./plugins/records"));
const ReviewView = lazy(() => import("./plugins/review"));
const LoginView = lazy(() =>
  import("./views/LoginView").then((m) => ({ default: m.LoginView }))
);
const PersonalView = lazy(() =>
  import("./views/PersonalView").then((m) => ({ default: m.PersonalView }))
);
const TapeView = lazy(() =>
  import("./views/TapeView").then((m) => ({ default: m.TapeView }))
);
const WorkflowView = lazy(() => import("./plugins/workflow"));
const WorkflowEditorView = lazy(() =>
  import("./plugins/workflow").then((m) => ({ default: m.WorkflowEditorView }))
);
const UsageView = lazy(() => import("./plugins/usage"));
const ConfigView = lazy(() => import("./plugins/config"));
const SpaceHomeView = lazy(() =>
  import("./views/SpaceHomeView").then((m) => ({ default: m.SpaceHomeView }))
);
/**
 * NavigationController — consumes server-pushed navigation requests.
 *
 * Lives inside <BrowserRouter> so it can call useNavigate(). The WS handler
 * in store.ts sets `pendingNav` (e.g. when a command result or error carries
 * a navigate hint); this effect performs the actual navigate() and
 * consumes the value. Keeping navigation side-effect out of the store keeps
 * the store testable and free of react-router coupling.
 */
function NavigationController() {
  const navigate = useNavigate();
  const pendingNav = useStore((s) => s.pendingNav);
  const consumeNav = useStore((s) => s.consumeNav);

  useEffect(() => {
    if (pendingNav) {
      navigate(pendingNav);
      consumeNav();
    }
  }, [pendingNav, navigate, consumeNav]);

  return null;
}

export default function App() {
  // Connection status lives in the store (single source of truth) — the WS
  // connection events below are wired to the store setters.
  const connected = useStore((s) => s.connected);
  const setConnected = useStore((s) => s.setConnected);
  const setConnError = useStore((s) => s.setConnError);
  const setAccount = useStore((s) => s.setAccount);
  const handleServerMessage = useStore((s) => s.handleServerMessage);
  const hydrateToolActivity = useStore((s) => s.hydrateToolActivity);

  // 标签存在性心跳：让内核知道这个标签还在（见 lib/tabPresence.ts）
  useEffect(() => startTabPresence(), []);

  // 账户状态进全局 store：侧边栏个人入口与个人页共用（软闸，不阻塞应用）
  useEffect(() => {
    fetchAccountStatus()
      .then(setAccount)
      .catch(() => setAccount({ logged_in: false }));
  }, [setAccount]);

  useEffect(() => {
    const ws = getWS();
    const unsub = ws.onMessage((msg) => {
      // 托盘/CLI 唤起：若标签还在但 WS 已断，立刻自检重连（勿等用户刷新）
      if (msg.type === "focus_window" && !ws.isConnected()) {
        ws.reconnectNow();
      }
      if (msg.type === "need_topup") {
        topUpSessionTail();
        return;
      }
      handleServerMessage(msg);
    });
    const unsubConn = ws.onConnection((c, err) => {
      setConnected(c);
      setConnError(err ?? null);
      if (c) {
        void hydrateToolActivity();
        // 断线窗口内的实时帧已丢；重连后补拉落带后缀
        topUpSessionTail();
      }
    });

    // 首屏自检：挂载后短暂窗口内仍未连上 → 主动重连（覆盖「打开即断开、刷新才好」）
    const bootTimer = window.setTimeout(() => {
      if (!ws.isConnected() && !ws.isClosed()) {
        ws.reconnectNow();
      }
    }, 600);

    const onWake = () => {
      if (document.visibilityState === "visible" && !ws.isConnected() && !ws.isClosed()) {
        ws.reconnectNow();
      }
    };
    document.addEventListener("visibilitychange", onWake);
    window.addEventListener("pageshow", onWake);

    return () => {
      window.clearTimeout(bootTimer);
      document.removeEventListener("visibilitychange", onWake);
      window.removeEventListener("pageshow", onWake);
      unsub();
      unsubConn();
    };
  }, [handleServerMessage, setConnected, setConnError, hydrateToolActivity]);

  return (
    <ConfigProvider locale={zhCN} theme={coaraAntdTheme}>
      <SingleTabGuard>
        <BrowserRouter>
          <NavigationController />
          <AppLayout connected={connected}>
            <ErrorBoundary>
              <Suspense
                fallback={
                  <div style={{ padding: 40, textAlign: "center", color: "var(--coara-text-muted)" }}>
                    加载中…
                  </div>
                }
              >
                <div style={{ flex: 1, height: "100%", minHeight: 0, display: "flex", flexDirection: "column" }}>
                <Routes>
                  <Route path="/chat" element={<ChatView />} />
                  <Route path="/home" element={<SpaceHomeView />} />
                  <Route path="/review" element={<ReviewView />} />
                  <Route path="/file" element={<FileView />} />
                  <Route path="/records" element={<RecordsView />} />
                  <Route path="/workflow" element={<WorkflowView />} />
                  <Route path="/workflow/editor/:draftId" element={<WorkflowEditorView />} />
                  {/* 旧链接兜底：最近文件已并入记录页「最近」Tab */}
                  <Route path="/recent" element={<Navigate to="/records?tab=recent" replace />} />
                  <Route path="/usage" element={<UsageView />} />
                  <Route path="/config" element={<ConfigView />} />
                  <Route path="/login" element={<LoginView />} />
                  <Route path="/me" element={<PersonalView />} />
                  <Route path="/tape" element={<TapeView />} />
                  <Route path="/" element={<Navigate to="/chat" replace />} />
                </Routes>
                </div>
              </Suspense>
            </ErrorBoundary>
          </AppLayout>
          <InteractionDialog />
        </BrowserRouter>
      </SingleTabGuard>
    </ConfigProvider>
  );
}
