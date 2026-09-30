import { memo, useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Badge, Button, Select, Tooltip, Typography, message } from "antd";
import { FolderOutlined } from "@ant-design/icons";
import { PageHeader } from "../components/layout/PageHeader";
import { ChatInput, type Attachment } from "../features/chat/ChatInput";
import { MessageList } from "../features/chat/MessageList";
import { TurnSpinner } from "../features/chat/TurnSpinner";
import { StatusSidebar } from "../components/StatusSidebar";
import { useStore } from "../lib/store";
import { getWS } from "../lib/ws";
import {
  fetchModelChoices,
  startNewSession,
  switchSessionModel,
  uploadRawUrl,
  type ModelChoice,
} from "../lib/api";
import { runSessionHydrate } from "../lib/chatHydrate";
import type { ChatFileAttachment } from "../lib/store";

/** 对话页：主区 + 右侧状态栏（对话专属，不进公共布局）。 */
const STATUS_MIN = 280;
const STATUS_MAX = 640;

const { Text } = Typography;

/** 模型选择器：runtime 心跳（5s 一次）只驱动这个叶子，整页与消息列表不再跟着重渲染。 */
const SessionModelSelect = memo(function SessionModelSelect({
  choices,
  switching,
  onPick,
}: {
  choices: ModelChoice[];
  switching: boolean;
  onPick: (key: string) => void;
}) {
  const runtime = useStore((s) => s.runtime);
  const currentModelKey =
    runtime?.provider && runtime?.model ? `${runtime.provider}/${runtime.model}` : undefined;
  return (
    <Select
      size="small"
      loading={switching}
      disabled={choices.length === 0}
      value={currentModelKey}
      placeholder="选择模型"
      style={{ minWidth: 180, maxWidth: 280 }}
      options={choices.map((c) => ({
        value: c.key,
        label: c.label || c.key.replace("/", "·"),
      }))}
      onChange={(v) => onPick(String(v))}
      title="切换当前工作空间模型"
    />
  );
});

export function ChatView() {
  const turnActive = useStore((s) => s.turnActive);
  const pendingCommand = useStore((s) => s.pendingCommand);
  const connected = useStore((s) => s.connected);
  const connError = useStore((s) => s.connError);
  const activeName = useStore((s) => s.activeName);
  const loadHistory = useStore((s) => s.loadHistory);
  const addUserMessage = useStore((s) => s.addUserMessage);
  const workspaceDir = useStore((s) => s.workspaceDir);
  const resetForNewSession = useStore((s) => s.resetForNewSession);
  const applyLocalModelSwitch = useStore((s) => s.applyLocalModelSwitch);
  const hydrateToolActivity = useStore((s) => s.hydrateToolActivity);
  const needsHydrate = useStore((s) => s.needsHydrate);
  const consumeNeedsHydrate = useStore((s) => s.consumeNeedsHydrate);
  const loadedWorkspace = useRef<string | null>(null);
  const [startingNew, setStartingNew] = useState(false);
  const [modelChoices, setModelChoices] = useState<ModelChoice[]>([]);
  const [switchingModel, setSwitchingModel] = useState(false);
  const navigate = useNavigate();
  const providersRevision = useStore((s) => s.providersRevision);

  // 无可用模型 → 配置页 ?focus=models；providers_changed 后重拉（填完 key 顶栏立刻有新厂商）
  const setupRedirected = useRef(false);
  useEffect(() => {
    let cancelled = false;
    fetchModelChoices()
      .then((data) => {
        if (cancelled) return;
        const catalog = data.catalog ?? [];
        setModelChoices(catalog);
        if (!setupRedirected.current && catalog.length === 0) {
          setupRedirected.current = true;
          navigate("/config?focus=models", { replace: true });
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [navigate, providersRevision]);

  // 状态栏拖宽 [MIN, MAX]
  const [statusWidth, setStatusWidth] = useState(340);
  const statusWidthRef = useRef(statusWidth);
  statusWidthRef.current = statusWidth;

  const startStatusResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = statusWidthRef.current;
    const onMove = (ev: MouseEvent) => {
      const next = startW + (startX - ev.clientX);
      setStatusWidth(Math.min(STATUS_MAX, Math.max(STATUS_MIN, next)));
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  };

  useEffect(() => {
    // 重挂载：剥瞬态标记，防 A→B 闪变
    useStore.getState().sanitizeResidentMessages();

    const runHydrate = (targetDir: string, forceFull = false, sameBoundaryTopUp = false) => {
      runSessionHydrate(targetDir, {
        forceFull,
        sameBoundaryTopUp,
        onError: (err) =>
          message.error(
            `加载聊天记录失败: ${err instanceof Error ? err.message : String(err)}`,
          ),
      });
    };

    if (workspaceDir && workspaceDir !== loadedWorkspace.current) {
      const st = useStore.getState();
      const skipHydrate =
        st.workspaceDir === workspaceDir &&
        st.messages.length > 0 &&
        st.hydratedSeq > 0;
      loadedWorkspace.current = workspaceDir;
      consumeNeedsHydrate();
      if (skipHydrate) {
        return;
      }
      runHydrate(workspaceDir);
      return;
    }
    // 同边界 reset 丢了在飞 hydrate：补一次
    if (workspaceDir && needsHydrate) {
      consumeNeedsHydrate();
      runHydrate(workspaceDir, false, true);
    }
  }, [workspaceDir, loadHistory, needsHydrate, consumeNeedsHydrate]);

  const sendChat = useCallback(
    (text: string, imageRefs?: string[], fileRefs?: string[], attachments?: Attachment[]) => {
      const chatAttachments: ChatFileAttachment[] | undefined = attachments?.map((a) => ({
        file_id: a.ref,
        url: uploadRawUrl(a.ref),
        filename: a.filename,
        mime: "",
        size: a.size,
        is_image: a.kind === "image",
      }));
      // client_msg_id：权威帧按它认领（不靠乐观标记/文本）
      const clientMsgId =
        typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : `c-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      addUserMessage(text, imageRefs, chatAttachments, clientMsgId);
      getWS().send({
        type: "chat",
        text,
        image_refs: imageRefs,
        file_refs: fileRefs,
        client_msg_id: clientMsgId,
        workspace_dir: useStore.getState().workspaceDir ?? undefined,
      });
    },
    [addUserMessage],
  );

  const sendCommand = useCallback((text: string) => {
    useStore.setState({ pendingCommand: text.split(/\s+/)[0] });
    getWS().send({
      type: "command",
      text,
      workspace_dir: useStore.getState().workspaceDir ?? undefined,
    });
  }, []);

  const handleNewSession = async () => {
    if (startingNew) return;
    try {
      setStartingNew(true);
      const result = await startNewSession();
      const newSid = String(result?.session_id || "").trim();
      if (newSid) {
        // /new：本地插分隔线 + 换 session；内容等 hydrate
        resetForNewSession(newSid);
        void hydrateToolActivity();
      }
    } catch (err) {
      console.error("Failed to start new session:", err);
      message.error(`开始新会话失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setStartingNew(false);
    }
  };

  /** 切换中的镜像：让 onPick 引用保持稳定（叶子组件不因开关态变化重渲染）。 */
  const switchingModelRef = useRef(false);

  const handleModelChange = useCallback(async (key: string) => {
    if (!key || switchingModelRef.current) return;
    const rt = useStore.getState().runtime;
    const current =
      rt?.provider && rt?.model ? `${rt.provider}/${rt.model}` : undefined;
    if (key === current) return;
    try {
      switchingModelRef.current = true;
      setSwitchingModel(true);
      const result = await switchSessionModel(key);
      const provider = String(result?.provider || "").trim();
      const model = String(result?.model || "").trim();
      if (provider || model) {
        applyLocalModelSwitch(provider, model, Boolean(result?.deferred));
      }
    } catch (err) {
      console.error("Failed to switch model:", err);
      message.error(`切换模型失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      switchingModelRef.current = false;
      setSwitchingModel(false);
    }
  }, [applyLocalModelSwitch]);

  return (
    <div
      style={{
        display: "flex",
        height: "100%",
        background: "var(--coara-surface)",
        overflow: "hidden",
      }}
    >
      {/* 主区 */}
      <div
        style={{
          flex: 1,
          minWidth: 0,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {/* 顶栏：空间身份 | 连接/模型/新会话 */}
        <PageHeader
          title={
            <Tooltip title="进入空间主页">
              <span
                role="button"
                onClick={() => navigate("/home")}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 8,
                  minWidth: 0,
                  cursor: "pointer",
                }}
              >
                <FolderOutlined style={{ color: "var(--coara-text-muted)" }} />
                {activeName || "对话"}
              </span>
            </Tooltip>
          }
          actions={
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
              <Tooltip title={connected ? "已连接" : connError || "连接断开——点击重连"}>
                <span
                  role={connected ? undefined : "button"}
                  onClick={() => {
                    if (!connected) getWS().reconnectNow();
                  }}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    cursor: connected ? "default" : "pointer",
                  }}
                >
                  <Badge
                    status={connected ? "success" : "error"}
                    text={
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {connected ? "已连接" : "断开"}
                      </Text>
                    }
                  />
                </span>
              </Tooltip>
              <SessionModelSelect
                choices={modelChoices}
                switching={switchingModel}
                onPick={handleModelChange}
              />
              <Button
                size="small"
                loading={startingNew}
                onClick={() => void handleNewSession()}
                title="开始新会话"
              >
                新会话
              </Button>
            </div>
          }
        />
        <div style={{ flex: 1, overflow: "hidden" }}>
          <MessageList />
        </div>
        <TurnSpinner />
        <ChatInput onSend={sendChat} onCommand={sendCommand} disabled={turnActive || pendingCommand !== null} />
      </div>
      {/* 状态栏（可拖宽） */}
      <div
        onMouseDown={startStatusResize}
        style={{
          width: 5,
          flexShrink: 0,
          cursor: "col-resize",
          background: "transparent",
        }}
        title="拖拽调整状态栏宽度"
        aria-label="拖拽调整状态栏宽度"
      />
      <div
        style={{
          width: statusWidth,
          minWidth: STATUS_MIN,
          maxWidth: STATUS_MAX,
          flexShrink: 0,
          overflow: "hidden",
          background: "var(--coara-surface)",
        }}
      >
        <StatusSidebar />
      </div>
    </div>
  );
}
