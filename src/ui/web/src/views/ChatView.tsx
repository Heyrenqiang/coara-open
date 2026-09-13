import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Badge, Button, Select, Tooltip, Typography, message } from "antd";
import { FolderOutlined } from "@ant-design/icons";
import { PageHeader } from "../components/layout/PageHeader";
import { ChatInput, type Attachment } from "../features/chat/ChatInput";
import { MessageList } from "../features/chat/MessageList";
import { TurnSpinner } from "../features/chat/TurnSpinner";
import { VaultPromptCard } from "../components/VaultPromptCard";
import { StatusSidebar } from "../components/StatusSidebar";
import { useStore } from "../lib/store";
import { getWS } from "../lib/ws";
import {
  fetchModelChoices,
  fetchSessionMessages,
  startNewSession,
  switchSessionModel,
  uploadRawUrl,
  type ModelChoice,
} from "../lib/api";
import type { ChatFileAttachment } from "../lib/store";

/** 对话页布局：主区（消息 + 输入）+ 右侧状态栏。
 *  状态栏是对话页的一部分（运行状态/上下文/工具调用只服务于对话），
 *  不提升为公共布局——其它子模块整页独立，不共享此栏。 */
const STATUS_MIN = 280;
const STATUS_MAX = 640;

/** 增量 hydrate 的重叠窗口（帧）：每次多要这么多帧，游标有偏移（他端动过、
 *  落带延迟）也能被同一段帧覆盖校正——同 seq 的行在 store 里原地替换，不产生重影。 */
const HYDRATE_OVERLAP = 50;

const { Text } = Typography;

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
  const runtime = useStore((s) => s.runtime);
  const needsHydrate = useStore((s) => s.needsHydrate);
  const consumeNeedsHydrate = useStore((s) => s.consumeNeedsHydrate);
  const loadedWorkspace = useRef<string | null>(null);
  const [startingNew, setStartingNew] = useState(false);
  const [modelChoices, setModelChoices] = useState<ModelChoice[]>([]);
  const [switchingModel, setSwitchingModel] = useState(false);
  const navigate = useNavigate();

  // 首启引导：没有任何可用模型（未填任何 API Key）时直接导航到配置页的
  // 模型提供者模块（?focus=models 定位），让用户第一眼就能填 Key。
  const setupRedirected = useRef(false);
  useEffect(() => {
    if (setupRedirected.current) return;
    setupRedirected.current = true;
    fetchModelChoices()
      .then((data) => {
        const catalog = data.catalog ?? [];
        setModelChoices(catalog);
        if (catalog.length === 0) {
          navigate("/config?focus=models", { replace: true });
        }
      })
      .catch(() => {});
  }, [navigate]);

  // 右侧状态栏宽度可拖拽：左缘分隔线拖动，clamp 到 [MIN, MAX]。
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

  // Load session history when session changes (initial mount, 新会话, or
  // workspace switch). Workspace switches now flow through resetForWorkspaceSwitch
  // (called from the state WS handler on session_id change), which sets a
  // new sessionId — so depending on sessionId alone is sufficient.
  //
  // On remount with the *same* session (e.g. navigate Config → Chat), skip
  // reload when the store already has messages — otherwise a race with
  // persistence could briefly wipe live outbound files. Empty store still
  // hydrates from /api/session/messages (which now includes files).
  // hydrate 触发以 workspaceDir 为边界（空间一条线）：切空间/首挂/F5 拉该空间
  // 完整线。同空间 /new 不换边界（同一条线），由实时流 + 下一次 hydrate 尾部
  // 对账续上，无需重拉。
  useEffect(() => {
    const bumpHydrateGeneration = useStore.getState().bumpHydrateGeneration;
    // 切模块再切回（重挂载）时先剥掉内存驻留消息的瞬态标记——否则残留的
    // streaming/optimistic 等会先上屏（A）再被 hydrate 覆盖（B），产生闪变。
    useStore.getState().sanitizeResidentMessages();

    const runHydrate = (targetDir: string, forceFull = false) => {
      const generation = bumpHydrateGeneration();
          // 增量补拉的起点＝**屏幕上已覆盖到的最大 view_seq** 减一段重叠窗口
          //（不是本地游标：游标只表达「最近一次快照覆盖到哪」，内容到哪以屏上为准）。
          // 它只决定「拉多少」，不决定「往哪儿放」——对账一律「只追加 + 只改内容」。
          // 已有游标与内容时走增量 hydrate（当前唯一入口是 /new 的同边界补拉，
          // 见下方 needsHydrate 分支）：只取游标之后的帧追加，已渲染的部分一个字
          // 不动——等价于「补后缀」，不产生 A→B 重建。
      const before = useStore.getState();
      const maxSeq = before.messages.reduce((acc, m) => Math.max(acc, m.seq ?? 0), 0);
      const after =
        !forceFull && before.messages.length > 0 && maxSeq > 0
          ? Math.max(0, maxSeq - HYDRATE_OVERLAP)
          : undefined;
      // 带 workspace_dir：把「读的是哪条线」显式钉死（服务端在该模式下只读这条线，
      // 不切当前会话）——两次往返之间即使视图被切走，也不会把内容换到别的线上。
      fetchSessionMessages(200, after, { workspaceDir: targetDir })
            .then((data) => {
              const st = useStore.getState();
              if (st.workspaceDir !== targetDir) return;
              if (st.hydrateGeneration !== generation) return;
          const epoch = String(data.epoch ?? "").trim();
          // 增量请求取回的却是另一条线（epoch 变＝线已重建）：这次响应整体作废，
          // 改走一次全量——增量按 seq 拼接的前提是「同一条线」。
          if (after !== undefined && epoch && st.lineEpoch && epoch !== st.lineEpoch) {
            runHydrate(targetDir, true);
            return;
          }
          // 回合态与消息同源：权威快照里带回来的 runtime 就是这个空间的实情，
          // 刷新与切空间因此走同一条路径、同一份权威（不再靠心跳推送，推送跳过
          // 时端侧只能拿缓存猜，就会出现「切回来 spinner 没了」这类失真）。
          const rt = data.runtime;
          const rtDir = String(rt?.workspace_dir ?? "").trim();
          // 归属校验：两次往返之间服务端视图可能已经切走，别把别的空间的回合态装
          // 到本端——这和消息的边界守卫是同一把尺的两面。
          const runtimeInBoundary = Boolean(rt) && (!rtDir || rtDir === targetDir);
          // runtime / 折叠区（最终答复 + 子智能体工具行与 diff）/ 线身份全部折进
          // loadHistory 的同一次提交：分两次 set 会先渲染消息、再补 spinner 与展开区，
          // 那就是「先一半后补齐」。原子提交后一屏只画一次。
          loadHistory(data.messages, data.latest_seq, {
            workspaceDir: targetDir,
            generation,
            incremental: after !== undefined,
            epoch,
            ...(runtimeInBoundary && rt ? { runtime: rt } : {}),
            subagentResults: data.subagent_results,
            subagentDiffs: data.subagent_diffs,
            subagentBriefs: data.subagent_briefs,
          });
            })
        .catch((err) => {
          console.error("Failed to hydrate session messages:", err);
          message.error(
            `加载聊天记录失败: ${err instanceof Error ? err.message : String(err)}`,
          );
        });
    };

    if (workspaceDir && workspaceDir !== loadedWorkspace.current) {
      // 仅当「屏幕上已经有这个空间的内容」时才跳过：刷新后 WS state 首帧到达时，
      // mount 那次 hydrate 已完成，跳过即可。切空间会把内容清空，此时必须重拉。
      // （旧判据用 loadedWorkspace.current === null，刷新后它一路是 null，切空间
      // 第一跳就被误判成「已加载」而整条跳过——切过去就是空的。）
      const st = useStore.getState();
      const skipHydrate =
        st.workspaceDir === workspaceDir &&
        st.messages.length > 0 &&
        st.hydratedSeq > 0;
      loadedWorkspace.current = workspaceDir;
      // 这一支要么立刻拉、要么空间里已有权威内容：resetFor* 记下的补拉请求随之结清。
      consumeNeedsHydrate();
      if (skipHydrate) {
        return;
      }
      runHydrate(workspaceDir);
      return;
    }
    // 同边界（/new 等 resetFor* 不换 workspaceDir）但 reset 把在飞 hydrate 丢掉了：
    // 补一次，别让历史与折叠区数据整段不在屏上。
    if (workspaceDir && needsHydrate) {
      consumeNeedsHydrate();
      runHydrate(workspaceDir);
    }
    // 边界未定时（刷新后 state 帧还没到）不拉历史：没定界就取快照，等于拿一个
    // 「还不知道是哪条线」的响应去铺屏，正是「错空间先上屏」。等 state 帧把
    // workspaceDir 落定后再 hydrate；这段窗口里到达的实时帧由 store 的边界缓冲
    // 兜住（订阅先于快照），帧不会丢。
  }, [workspaceDir, loadHistory, needsHydrate, consumeNeedsHydrate]);

  const sendChat = (text: string, imageRefs?: string[], fileRefs?: string[], attachments?: Attachment[]) => {
    // 附件转成气泡可渲染的 ChatFileAttachment（图片给预览 URL，文件给下载 URL）。
    const chatAttachments: ChatFileAttachment[] | undefined = attachments?.map((a) => ({
      file_id: a.ref,
      url: uploadRawUrl(a.ref),
      filename: a.filename,
      mime: "",
      size: a.size,
      is_image: a.kind === "image",
    }));
    // 端上生成消息标识：随 chat 帧发给内核，权威帧原样带回时按它精确认领
    // （不依赖乐观标记/文本比对，重挂载净化或文本被改写都不会再配错）
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
      // 把「这条消息是在哪个空间发出的」一并带上：服务端按归属绑定回合，
      // 不再依赖切换空间那一刻的视图指针（两次异步往返之间有时间窗）。
      workspace_dir: useStore.getState().workspaceDir ?? undefined,
    });
  };

  const sendCommand = (text: string) => {
    // 斜杠命令不走回合管线（无 turn_start/chunk），/compact 等耗时命令
    // 在结果返回前无反馈——置 pendingCommand 驱动 spinner 行，command_result
    // 或 error 到达时清除（store 内）。
    useStore.setState({ pendingCommand: text.split(/\s+/)[0] });
    getWS().send({
      type: "command",
      text,
      // 命令同样带归属：切空间窗口内发出的命令才不会作用到别的空间。
      workspace_dir: useStore.getState().workspaceDir ?? undefined,
    });
  };

  const handleNewSession = async () => {
    if (startingNew) return;
    try {
      setStartingNew(true);
      const result = await startNewSession();
      const newSid = String(result?.session_id || "").trim();
      if (newSid) {
        // 空间一条线：/new 由服务端在线上落分隔标记，本地插一条分隔线并切到新
        // session（旧对话保留在流里）；hydrate 回放空间线时天然含这条分隔线。
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

  const currentModelKey =
    runtime?.provider && runtime?.model ? `${runtime.provider}/${runtime.model}` : undefined;

  const handleModelChange = async (key: string) => {
    if (!key || key === currentModelKey || switchingModel) return;
    try {
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
      setSwitchingModel(false);
    }
  };

  return (
    <div
      style={{
        display: "flex",
        height: "100%",
        background: "var(--coara-surface)",
        overflow: "hidden",
      }}
    >
      {/* 主区：顶栏（模型 + 新会话）+ 消息列表 + 输入 */}
      <div
        style={{
          flex: 1,
          minWidth: 0,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {/* 顶栏（PageShell 槽位 ①+②）：空间身份在左，连接态 / 模型 / 新会话在右。
            空间切换已收进左侧边栏；连接态上顶栏后，切到配置等页面也能看到。 */}
        <PageHeader
          title={
            <span style={{ display: "inline-flex", alignItems: "center", gap: 8, minWidth: 0 }}>
              <FolderOutlined style={{ color: "var(--coara-text-muted)" }} />
              {activeName || "对话"}
            </span>
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
              <Select
                size="small"
                loading={switchingModel}
                disabled={modelChoices.length === 0}
                value={currentModelKey}
                placeholder="选择模型"
                style={{ minWidth: 180, maxWidth: 280 }}
                options={modelChoices.map((c) => ({
                  value: c.key,
                  label: c.label || c.key.replace("/", "·"),
                }))}
                onChange={(v) => void handleModelChange(String(v))}
                title="切换当前工作空间模型"
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
        <VaultPromptCard />
        <TurnSpinner />
        <ChatInput onSend={sendChat} onCommand={sendCommand} disabled={turnActive || pendingCommand !== null} />
      </div>
      {/* 右侧状态栏：对话页专属（左缘可拖拽调宽） */}
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
