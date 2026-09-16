import { Suspense, lazy, useEffect } from "react";
import { BrowserRouter, Routes, Route, Navigate, useNavigate } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { getWS } from "./lib/ws";
import { startTabPresence } from "./lib/tabPresence";
import { useStore } from "./lib/store";
import { fetchAccountStatus } from "./lib/account";
import { coaraAntdTheme } from "./theme/tokens";
import { AppLayout } from "./components/AppLayout";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { SingleTabGuard } from "./components/SingleTabGuard";
import { InteractionDialog } from "./features/interaction/InteractionDialog";
import { ChatView } from "./views/ChatView";

// Lazy-load heavy route views so the initial chunk only pays for the chat page.
const FileView = lazy(() =>
  import("./views/FileView").then((m) => ({ default: m.FileView }))
);
const RecordsView = lazy(() =>
  import("./views/RecordsView").then((m) => ({ default: m.RecordsView }))
);
const ReviewView = lazy(() =>
  import("./views/ReviewView").then((m) => ({ default: m.ReviewView }))
);
const LoginView = lazy(() =>
  import("./views/LoginView").then((m) => ({ default: m.LoginView }))
);
const PersonalView = lazy(() =>
  import("./views/PersonalView").then((m) => ({ default: m.PersonalView }))
);
const WorkflowView = lazy(() =>
  import("./views/WorkflowView").then((m) => ({ default: m.WorkflowView }))
);
const WorkflowEditorView = lazy(() => import("./views/WorkflowEditorView"));
const UsageView = lazy(() =>
  import("./views/UsageView").then((m) => ({ default: m.UsageView }))
);
const ConfigView = lazy(() =>
  import("./views/ConfigView").then((m) => ({ default: m.ConfigView }))
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
      handleServerMessage(msg);
    });
    const unsubConn = ws.onConnection((c, err) => {
      setConnected(c);
      setConnError(err ?? null);
      if (c) {
        void hydrateToolActivity();
      }
    });

    return () => {
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
                <Routes>
                  <Route path="/chat" element={<ChatView />} />
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
                  <Route path="/" element={<Navigate to="/chat" replace />} />
                </Routes>
              </Suspense>
            </ErrorBoundary>
          </AppLayout>
          <InteractionDialog />
        </BrowserRouter>
      </SingleTabGuard>
    </ConfigProvider>
  );
}
