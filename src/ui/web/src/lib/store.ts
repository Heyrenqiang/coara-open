// Global app state via Zustand.
//
// Tracks: connection status, chat messages (streaming), current turn state,
// pending interaction prompts (approval), runtime info, workspace
// list, and trace events (including tool calls).

import { create } from "zustand";
import {
  fetchToolActivityEvents,
  fetchWorkspaceList,
  type SessionSnapshot,
  type SubagentFramePayload,
  type TraceEventRow,
} from "./api";
import { reshufflePhrases } from "./loadingPhrases";
import { SubagentTreeTracker, type TreeRow } from "./subagentTree";
import type { AccountStatus } from "./account";
import type {
  CommandResult,
  DisplayBlock,
  ServerMessage,
} from "./ws";

/* Conversation bubbles: Web chat shows Web-originated turns (direct WS
 *  messages, source="web"); CLI mirrors Web/Matrix conversations as operator
 *  console; phone shows its own room. Trace-path chat messages (user_message /
 *  chat_chunk) are never rendered here, and turn_start / turn_end are honored
 *  only for source="web". 例外：Web 消息注入（接续）其他前端的活跃回合时，
 *  该回合经回投镜像把后续输出以 chunk 推回本端——chunk 自动建泡，与手机端
 *  回房间的语义一致。Restart hydrate (GET /api/session/messages) filters to
 *  source=web — L1 会话事件带（session_events.jsonl）投影仍覆盖每个前端。
 *  The trace feed still records everything. */

/** 一条工具行（聊天流内的 ✓ tool(...)）：由服务端工具行帧落视图后回放，
 *  或实时帧直接插入。label 由内核与 CLI 同源生成，前端不做二次拼装。 */
export interface ChatToolLine {
  label: string;
  ok: boolean;
  tool_name?: string;
  tool_call_id?: string;
  duration_ms?: number | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  /** diff 行产出它的那次工具调用 id（与同工具 `tool` 行的 `tool.tool_call_id` 同值）：
   *  渲染时按它把 diff 挂到工具行紧后面——投递顺序里两者之间可能插别的东西。
   *  老数据（空）按原顺序排。 */
  tool_call_id?: string;
  turn_id?: string;
  streaming?: boolean;
  /** True when this streaming message was auto-created locally because a
   *  chunk arrived before its turn_start. Used (instead of the turn_id
   *  "auto-" prefix) to claim the message when turn_start/turn_end arrive. */
  autoCreated?: boolean;
  /** True for messages produced by slash commands (/help, /status, ...).
   *  Rendered with a monospace card style instead of markdown. */
  isCommandResult?: boolean;
  /** Phone-style timeline divider label (model / workspace switch).
   *  When set, MessageList renders a hairline divider instead of the command card. */
  dividerLabel?: string;
  /** 时间线分隔线的时间标签（手机端同款格式化，如「昨天 09:12」）。 */
  dividerTime?: string;
  /** 后端下发的分隔帧落盘时刻（秒，Unix）；仅 hydrate 行有，前端格式化用。 */
  dividerTs?: number;
      /** 只有时间的空档线（长时间无新消息）：由服务端空档帧投影而来，
       *  渲染成一条带时间的细线，与任何用户动作无关。 */
      dividerTimeOnly?: boolean;
  /** Files delivered via send_file → Web (images/videos/docs). */
  files?: ChatFileAttachment[];
  /** 用户上传的附件（图片/文件），在用户气泡中显示缩略图/卡片。 */
  attachments?: ChatFileAttachment[];
  /** 回合锁排队中（其他前端占着回合）：气泡显示排队占位而非空白。 */
  queued?: boolean;
  /** True 表示本地乐观气泡：发送后立即上屏、尚无服务端 turn_id。
   *  user_message 权威帧（source=web）到达时按此认领并回填 turn_id（S1）。 */
  optimistic?: boolean;
  /** 端上生成、随消息发给内核、由权威帧原样带回的消息标识（client_msg_id）。
   *  认领的首选判据：有它就不再靠「猜」（乐观标记会被重挂载净化掉、文本可能被
   *  服务端改写、序号那时还没轮到）—— 精确配对，查表而不是概率。 */
  clientMsgId?: string;
  /** True 表示接续输入尚未注入 LLM 上下文（回合进行中发送的跟话，排队等
   *  当前迭代结束才注入）。气泡显示「待注入」徽标；continuation_input_injected
   *  事件到达时按文本匹配清除（LLM 已看到这句话）。 */
  pendingInject?: boolean;
  /** 代码 diff 块（tool_complete 产生）：作为聊天流独立内容块渲染，
   *  区别于普通正文气泡。有值时本消息即为 diff 块（text 为空）。 */
  diff?: import("./ws").CanonicalDiffLines;
  /** 工具行（✓ tool(...)）：聊天流内联显示，插在正文段落之间，与 CLI
   *  scrollback 同构。有值时本消息即为工具行（text 为空）。 */
  tool?: ChatToolLine;
  /** 录像带磁带 seq（hydrate 的已落带消息才有；实时尾部未落带无 seq）。
   *  hydrate 内部对账/排序的严格键，替代同文文本匹配。 */
  seq?: number;
  /** 服务端回合来源标记（帧透传）：turn_end 归属比对用——旧回合迟到的
   *  turn_end 不得误清新回合的 turnActive/活动树。 */
  source?: string;
  /** 稳定内容身份（computeMsgKey）：跨实时渲染与 hydrate 两处不变。
   *  reconcile 按它配对同一逻辑消息做原位更新（id 复用、不整表重建），
   *  消除刷新后 A→B 跳变。创建/内容定型时算一次缓存到本字段。 */
  key?: string;
  /** 乐观用户气泡发送失败（服务端在回合开始前早失败，只回 error 无
   *  user_message 认领帧）：气泡标「发送失败」，可点重试原文。 */
  sendFailed?: boolean;
  /** 回合被撤回（服务端 chat_turn_retracted，如中途切空间把该回合内容从线上抹掉）：
   *  已显示的行不删、只隐藏——顺序不变（已显示前缀不变式），渲染层直接不画。 */
  retracted?: boolean;
}

/** 一个模块级独立会话的消息与 turn 状态（按 subject 键控）。 */
export interface ModuleSessionState {
  messages: ChatMessage[];
  turnActive: boolean;
  currentTurnId: string | null;
}

/** Outbound file pushed from the runtime to the browser chat. */
export interface ChatFileAttachment {
  file_id: string;
  url: string;
  filename: string;
  mime: string;
  size: number;
  caption?: string;
  is_image?: boolean;
  is_video?: boolean;
  is_audio?: boolean;
}

export interface PendingInteraction {
  kind: "approval";
  approval_id: string;
  question: string;
  options?: { label: string; description: string }[];
  allow_free_text?: boolean;
  free_text_label?: string | null;
  timeout_s?: number;
  workspace?: string;
  created_at_ms?: number;
}

export interface WorkspaceInfo {
  name: string;
  summary: string;
  path: string;
  active: boolean;
  /** 门面形态：warehouse=对话主页 / display=展示主页 / storefront=营业主页 */
  storefront?: string;
  /** 展示/营业主页的路由（如 /usage）；仓库空间为空 = 对话页 */
  home_view?: string;
  /** 空间种类：managed=用户对话空间 / internal=系统空间（配置/消息/记录/用量等） */
  kind?: string;
}

export interface RuntimeInfo {
  session_id: string;
  status: string;
  provider: string;
  model: string;
  running: boolean;
  /** Active turn launch source on the viewed session (web/cli/matrix/…). */
  turn_source?: string;
  /** Current foreground workspace name (kept in sync on CLI/LLM /ws switch). */
  workspace_name?: string;
  workspace_dir?: string;
  /** Provider-reported context fill (same as CLI bottom toolbar). */
  context_used_tokens?: number;
  context_window_tokens?: number;
  /** Session cumulative cache hit ratio 0..1, or null if unknown. */
  context_cache_hit_ratio?: number | null;
}

/** Vault unlock prompt state — shown as a dedicated card (password never enters LLM). */
export interface VaultPrompt {
  initialized: boolean;
  locked: boolean;
  title: string;
  question: string;
  hint: string;
}

/** Vault unlock result — briefly shown after submitting a password. */
export interface VaultResult {
  ok: boolean;
  message: string;
  created?: boolean;
}

/** Trace event entry for StatusSidebar tool activity (+ turn markers). */
export interface TraceEventEntry {
  id: string;
  event_type: string;
  timestamp: string;
  turn_id?: string;
  source?: string;
  /** ``main_loop`` = Root / workspace foreground; ``subagent_loop`` = delegate. */
  origin_scope?: string;
  session_id?: string;
  /** 模块会话主体标记（如 "config"）；缺省/其他 = 主会话。 */
  subject?: string;
  tool?: string;
  args?: Record<string, unknown>;
  call_id?: string;
  summary?: string;
  ok?: boolean;
  reason?: string;
  text?: string;
  content?: string;
  display_blocks?: DisplayBlock[];
  /** coara-computed diff lines from tool_complete (Web detail / sidebar). */
  diff_lines?: import("./ws").CanonicalDiffLines;
  /** Full tool output text (capped server-side) from tool_complete. */
  tool_output?: string;
  tool_output_truncated?: boolean;
  tool_output_ref?: string;
  duration_ms?: number;
  is_error?: boolean;
}

interface AppState {
  // Connection
  connected: boolean;
  setConnected: (v: boolean) => void;
  /** Terminal connection error (auth failure / reconnect cap reached) —
   *  null while connected or while a reconnect is still being attempted. */
  connError: string | null;
  setConnError: (e: string | null) => void;

  // 账户（软闸）：null = 尚未拉取；不登录也能用，仅个人页内容需登录
  account: AccountStatus | null;
  setAccount: (a: AccountStatus | null) => void;

  // Chat
  messages: ChatMessage[];
  turnActive: boolean;
  currentTurnId: string | null;
  /** 斜杠命令执行中（如 /compact）：无回合事件，单独驱动 spinner 行。 */
  pendingCommand: string | null;
  /** 当前回合开始时间（ms，performance/Date.now），spinner 已用时间用。 */
  turnStartedAt: number | null;
  /** 当前会话 ID —— 空间一条线上的会话段标注（/new 换段）。不作数据边界。 */
  sessionId: string | null;
  /** 显示数据的边界键：当前 web 视图空间目录（WS state workspace_dir 驱动）。
   *  同空间 /new 不变、切空间才变。一个空间 = 一条消息线。 */
  workspaceDir: string | null;
  /** 各空间的会话键（workspaceDir → sessionId）：只解决「切回来第一帧就用对键」，
   *  否则边界守卫会把那一侧的帧全丢掉。内容一概不在这里——本地那份随时可能过期，
   *  拿它上屏就是用旧事实顶替、hydrate 回来再换（A 跳 B）；视图内容一律等权威给。 */
  sessionIdByWorkspace: Record<string, string | null>;
  /** 最近一次权威快照覆盖到的磁带 seq（只由 loadHistory 的 commit 写入）。
   *  不再参与任何「帧该不该上屏」的裁量——裁量改按「列表里有没有同序号的行」
   *  （见 _hasRowWithSeq），本地不再维护游标。 */
  hydratedSeq: number;
  /** 当前空间的内容是否已由权威快照落定（「一条入屏通道」的骨架态开关）：
   *  换边界（切空间 / 新会话段 / 首次定界）置 false，loadHistory 的 commit 是
   *  唯一置 true 的点。false 且无消息时渲染骨架——不先把欢迎卡或旧缓存上屏。 */
  viewReady: boolean;
  /** 当前骨架期的起点（ms）：超阈值仍无权威提交＝后端没连上，界面要给明确文案与
   *  重连入口（不能无限骨架）。null＝不在骨架期。 */
  skeletonSince: number | null;
  /** 已因换边界/新会话丢弃在飞 hydrate，而该空间还没有权威提交：需要补一次 hydrate，
   *  否则历史整段不在屏上（ChatView 消费后置回 false；commit 也会清）。 */
  needsHydrate: boolean;
  consumeNeedsHydrate: () => void;
  /** 线的身份（epoch = workspace_dir::subject[#世代]）：快照 / 切空间响应带回。
   *  与本地不同＝整条线已重建——本地内容、折叠区、游标全部属旧线，必须全量重取。 */
  lineEpoch: string | null;
  /** Monotonic token: bumps on session switch and each hydrate start. Stale
   *  REST responses must not overwrite a newer session/generation. */
  hydrateGeneration: number;
  bumpHydrateGeneration: () => number;

  // Interaction
  pendingInteraction: PendingInteraction | null;

  // Runtime
  runtime: RuntimeInfo | null;

  // Workspace
  workspaces: WorkspaceInfo[];
  activeName: string | null;

  // Trace events (live WS + sidebar hydrate — tool activity / chat turn markers)
  traceEvents: TraceEventEntry[];
  /** 子智能体工作行的派生快照（CLI 活动树同构）：由 subagent 生命周期与 tool
   *  事件经 SubagentTreeTracker 派生，回合内实时刷新。渲染面是 delegate 工具行的
   *  展开区——「工作行」组与 ◌ 运行中判定都读它，没有独立的活动树面板。 */
  subagentRows: TreeRow[];
  /** 子智能体的中间产出与最终答复，按发起它的 delegate 工具行归集（只 web 侧）。 */
  subagentOutput: Record<string, { text: string; result: string }>;
  /** 子智能体自己产生的工具行 / diff，按「发起它的那条 delegate 工具行 call_id」归集：
   *  它们不进主消息流（messages），只在对应 delegate 行的展开面板里出现。
   *  元素复用消息行的 tool / diff 两种形态（同一套渲染），按 view_seq 去重排序。 */
  subagentDiffs: Record<string, ChatMessage[]>;
  /** delegate 的任务指令（brief），按「那条 delegate 工具行的 call_id」归集：指令
   *  讲的是「让它去做什么」，不是对话正文，只在该行展开面板的「任务指令」组里出现。
   *  同 call 只留最新一条；文本可能很长且带换行，展示时按 pre-wrap 保留原样。 */
  subagentBriefs: Record<string, string>;
  /** Bumps only when sidebar-relevant trace events arrive (not every chat_chunk). */
  activityEpoch: number;
  /** 工具活动首次 hydrate 是否完成：未完成前侧栏显示加载占位（而非「暂无」），
   *  避免「暂无工具调用」先闪一下再被真实列表替换。 */
  toolActivityReady: boolean;

  // Vault unlock prompt (shown as a dedicated card; password never enters LLM)
  vaultPrompt: VaultPrompt | null;
  vaultResult: VaultResult | null;
  clearVaultPrompt: () => void;
  clearVaultResult: () => void;

  // Navigation — set by server-pushed navigation messages and consumed by an
  // in-Router effect that calls navigate(). Keeping this in the store avoids
  // coupling the store to react-router and lets the WS handler stay side-effect free.
  pendingNav: string | null;
  consumeNav: () => void;

  // 消息中心角标：跨空间高显著未读数（AppLayout 轮询 summary 刷新）
  updatesPending: number;
  setUpdatesPending: (n: number) => void;

  // 模块级独立会话（WS subject 键控）：每个 agentic 模块一份独立消息/turn 状态，
  // 与主会话互不可见。key = subject（"config" / ...）。
  moduleSessions: Record<string, ModuleSessionState>;
  ensureModuleSession: (subject: string) => ModuleSessionState;
  loadModuleHistory: (subject: string, messages: { role: string; text: string }[]) => void;
  addModuleUserMessage: (subject: string, text: string, attachments?: ChatFileAttachment[]) => void;
  resetModuleSession: (subject: string) => void;

  // Actions
  handleServerMessage: (msg: ServerMessage) => void;
  clearInteraction: () => void;
  setWorkspaces: (ws: WorkspaceInfo[], active: string | null) => void;
  loadHistory: (
    messages: {
      role: string;
      text: string;
      files?: ChatFileAttachment[];
      diff?: import("./ws").CanonicalDiffLines;
      /** diff 行的工具归属（产出它的那次工具调用 id，与同工具 tool 行同值） */
      tool_call_id?: string;
      tool?: ChatToolLine;
      divider?: string;
      divider_time?: string;
      ts?: number;
      seq?: number;
      /** 端上生成的消息标识（随发送上行、服务端落带原样带回）：hydrate 对账按它
       *  精确配对本端实时气泡，不靠文本猜 */
      client_msg_id?: string;
      /** /compact 等会话变更命令落带的结果行：hydrate 仍渲染为命令卡 */
      is_command_result?: boolean;
      attachments?: { ref?: string; filename?: string; size?: number; mime?: string; is_image?: boolean; url?: string }[];
    }[],
    latestSeq?: number,
    opts?: {
      workspaceDir?: string | null;
      generation?: number;
      incremental?: boolean;
      /** 快照同源带回的回合态：与 messages 同一次 set 落定（原子提交）。 */
      runtime?: SnapshotRuntime;
      /** 快照同源带回的子智能体最终答复（tool_call_id → 文本）：同样并入同一次
       *  提交的 subagentOutput（服务端不再把它投影成消息，展开区只此一个来源）。 */
      subagentResults?: Record<string, string>;
      /** 快照同源带回的折叠区帧（子智能体的工具行 / diff），同上一次提交落定。 */
      subagentDiffs?: Record<string, SubagentFramePayload[]>;
      /** 快照同源带回的任务指令（call_id → 文本）：同样在这一批里落定。 */
      subagentBriefs?: Record<string, string>;
      /** 快照同源带回的线身份：与本地不同＝线重建，本地内容整体作废。 */
      epoch?: string;
    },
  ) => void;
  addUserMessage: (text: string, imageRefs?: string[], attachments?: ChatFileAttachment[], clientMsgId?: string) => void;
  clearTraceEvents: () => void;
  /** Load recent tool-activity events into the sidebar buffer (WS connect / workspace switch). */
  hydrateToolActivity: () => Promise<void>;
  /** 切工作空间：带上目标空间的会话键、内容清空，然后等 hydrate 权威校正。
   *  workspaceDir 为数据边界键；切空间本身不插线（线只由显式动作产生）。
   *  带 snapshot（切空间响应自带目标空间快照）时就地一次提交上屏，不再等 hydrate。 */
  resetForWorkspaceSwitch: (workspaceDir: string, snapshot?: SessionSnapshot | null) => void;
  /** 同空间 /new：不重建视图（同一 workspaceDir 同一条线），仅插分隔线并把该空间
   *  的会话键换成新段，等下一次 hydrate 尾部追加。label 缺省「新会话」。 */
  resetForNewSession: (sessionId: string, label?: string) => void;
  /** Web 顶栏切模型后刷新 runtime 展示（本端不插线，见实现内注释）。 */
  applyLocalModelSwitch: (provider: string, model: string, deferred?: boolean) => void;
  /** 净化内存驻留消息的瞬态标记（streaming/optimistic/pendingInject/queued/autoCreated）。
   *  ChatView 重挂载（切模块再切回）时调用：残留瞬态标记会先上屏（A）再被 hydrate
   *  覆盖（B）造成闪变——重渲染前剥掉。 */
  sanitizeResidentMessages: () => void;
}

/** 时间线时间标签（对齐手机端 formatDividerLabel 规则）：
 *  当天「今天 HH:mm」；昨天「昨天 HH:mm」；今年「M月d日 周X HH:mm」；跨年加年份。
 *  当天也带「今天」锚点：跨天回来（凌晨帧 + 下午再看）只显示 HH:mm 会与
 *  「刚刚的对话」对不上，日期锚点消除歧义。 */
function formatTimelineTimeLabel(ms: number): string {
  const d = new Date(ms);
  const now = new Date();
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate();
  if (sameDay(now, d)) return `今天 ${hm}`;
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (sameDay(yesterday, d)) return `昨天 ${hm}`;
  const weekday = ["日", "一", "二", "三", "四", "五", "六"][d.getDay()];
  const md = `${d.getMonth() + 1}月${d.getDate()}日`;
  if (now.getFullYear() === d.getFullYear()) return `${md} 周${weekday} ${hm}`;
  return `${d.getFullYear()}年${md} 周${weekday} ${hm}`;
}

/** 手机端同款融合：会话/模型切换分隔线与时间隔断合并到一行（「新会话  09:12」）。
 *  分隔线时间取「下一行的内容时间」——即分隔发生的位置，而非分隔帧写入时间
 *  （系统深夜写分隔帧时后者会失真）。后端已下发 divider_time 时直接用。 */
function withTimelineTimes(messages: ChatMessage[]): ChatMessage[] {
  return messages.map((m) => {
    if (!m.dividerLabel) return m;
    // 时间标签优先级：后端已下发 > 分隔帧落盘时刻格式化；均无则空
    // （取「下一行内容时间」在前端无时间源——消息行不带 ts，只能由后端补）。
    const explicit = m.dividerTime ?? "";
    if (explicit) return { ...m, dividerTime: explicit };
    if (m.dividerTs && m.dividerTs > 0) {
      return { ...m, dividerTime: formatTimelineTimeLabel(m.dividerTs * 1000) };
    }
    return m;
  });
}

/** 相邻分隔线融合：一条分隔线紧跟着另一条时，只留信息更全的一条
 *  （连续 /new 不产生两行紧贴的分隔线）。 */
function mergeAdjacentDividers(messages: ChatMessage[]): ChatMessage[] {
  const out: ChatMessage[] = [];
  for (const m of messages) {
    const prev = out[out.length - 1];
    if (m.dividerLabel && prev?.dividerLabel) {
      // 后到的标签信息更新，覆盖前者；时间保留后者（更近）
      out[out.length - 1] = {
        ...m,
        dividerTime: m.dividerTime ?? prev.dividerTime,
      };
      continue;
    }
    out.push(m);
  }
  return out;
}

/** hydrate 快照融合（手机端 buildChatListItems 同款）：
 *  连续相邻的分隔线合并为一条；分隔线缺时间标签时取下一行的内容时间
 *  （分隔发生的位置），有则沿用后端下发的 divider_time。 */
function fuseDividersWithTime(messages: ChatMessage[]): ChatMessage[] {
  return mergeAdjacentDividers(withTimelineTimes(messages));
}

/** Map model/workspace switch command results to phone-style divider labels.
 *  Other slash outputs stay as the monospace command card. */
function timelineDividerLabelFromCommand(result: CommandResult): string | null {
  const data = (result?.data ?? {}) as Record<string, unknown>;
  const output = String(result?.output ?? "").trim();
  const provider = typeof data.provider === "string" ? data.provider.trim() : "";
  const model = typeof data.model === "string" ? data.model.trim() : "";
  const isModelAck =
    output.startsWith("已切换 →") ||
    output.startsWith("已切换 ->") ||
    output.startsWith("已标记切换 →") ||
    output.startsWith("已标记切换 ->");
  if (isModelAck) {
    const key =
      provider && model
        ? `${provider}·${model}`
        : output
            .replace(/^已标记切换\s*(?:→|->)\s*/, "")
            .replace(/^已切换\s*(?:→|->)\s*/, "")
            .replace(/\s*[（(].*$/, "")
            .trim()
            .replace(/\//g, "·");
    if (!key) return null;
    return data.deferred ? `将切换模型 ${key}` : `已切换模型 ${key}`;
  }
  if (result?.action === "switch_workspace") {
    const name = typeof data.name === "string" ? data.name.trim() : "";
    if (name) return `已切换到工作空间 ${name}`;
  }
  return null;
}

/** FNV-1a 32-bit：消息内容身份的轻量哈希（同步、无加密需求，碰撞域内
 *  由 reconcile 的组内下标双射消歧——碰撞只导致错误配对，不导致崩溃）。 */
function fnv1a(s: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, "0");
}

/** 稳定内容身份：mapped（hydrate 权威行）与实时行按同一算法算 key 才能配对。
 *  素材规则（双侧严格同源）：
 *  - 分隔线 `d|<label>`：同 label 即同一逻辑实体（融合的多对一塌缩天然吸收）
 *  - diff 块 `f|<diff JSON>`
 *  - 用户气泡 `u|<trim 文本>|<附件 ref 列表>`（turn_id 不参与——mapped 行不带，
 *    认领前后 key 不变，同文重复由 reconcile 序数消歧）
 *  - assistant 正文 `a|<trim 文本>` */
function computeMsgKey(m: {
  role: "user" | "assistant";
  text: string;
  diff?: import("./ws").CanonicalDiffLines;
  dividerLabel?: string;
  attachments?: ChatFileAttachment[];
  tool?: ChatToolLine;
}): string {
  let raw: string;
  if (m.dividerLabel) {
    raw = `d|${m.dividerLabel}`;
  } else if (m.diff) {
    raw = `f|${JSON.stringify(m.diff)}`;
  } else if (m.tool) {
    // 工具行身份 = label + 成败：同一工具重复调用（同 label）由 reconcile
    // 序数消歧，时长不参与（不改变逻辑身份）。
    raw = `t|${m.tool.label}|${m.tool.ok ? 1 : 0}`;
  } else if (m.role === "user") {
    const refs = (m.attachments ?? []).map((a) => a.file_id || a.filename).join(",");
    raw = `u|${m.text.trim()}|${refs}`;
  } else {
    raw = `a|${m.text.trim()}`;
  }
  return `k:${fnv1a(raw)}`;
}

// Random UUIDs keep ids unique across HMR reloads (module state is reset,
// so a counter could hand out duplicate keys); fall back to timestamp+random
// where crypto.randomUUID is unavailable (older browsers / non-secure ctx).
function nextId(): string {
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? `msg-${crypto.randomUUID()}`
    : `msg-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** 行的 React key / 滚动锚键（docs/消息渲染契约.md I4）：**服务端键优先**——
 *  内容行 `s<view_seq>`、工具行 `t<tool_call_id>`、diff 行 `d<tool_call_id>`、
 *  已带端上标识的用户行 `c<client_msg_id>`；本地 uuid（`l<id>`）只在「尚无服务端
 *  键」时兜底，拿到服务端键的那一次落定就地升级（首条用户泡的认领）。
 *  这样一来：hydrate 对账只改内容不换键 ⇒ 不重挂；重建时整表换键是唯一允许的时机。 */
export function chatRowKey(m: ChatMessage): string {
  if (m.seq !== undefined) return `s${m.seq}`;
  const callId = String(m.tool?.tool_call_id || m.tool_call_id || "");
  if (callId) return `${m.tool ? "t" : "d"}${callId}`;
  if (m.clientMsgId) return `c${m.clientMsgId}`;
  return `l${m.id}`;
}

function nextTraceId(): string {
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? `tr-${crypto.randomUUID()}`
    : `tr-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

// Timer id for the vault_result auto-clear — tracked so a new vault_prompt
// or vault_result cancels the pending clear instead of wiping fresh state.
let vaultResultTimer: ReturnType<typeof setTimeout> | null = null;

const MAX_TRACE_EVENTS = 200;

/** Maximum chat messages kept in memory. Older messages are trimmed
 *  to prevent unbounded memory growth in long sessions. */
const MAX_MESSAGES = 300;

// Pre-built set for O(1) lookup — avoids creating a new Set on every WS message.
const TRACE_TYPES = new Set([
  "turn_start", "turn_end", "user_message", "chat_chunk", "chat_turn_retracted",
  "tool_start", "tool_call", "tool_result", "tool_complete", "llm_turn_start", "error",
  "session_auto_new",
]);

function nowISO(): string {
  return new Date().toISOString();
}

/** Trace types that affect the right-hand status sidebar tool list.
 *  Excludes high-frequency chat_chunk so streaming does not re-render the sidebar. */
const SIDEBAR_TRACE_TYPES = new Set([
  "user_message",
  "tool_start",
  "tool_call",
  "tool_result",
  "tool_complete",
  "llm_turn_start",
  "turn_end",
]);

/**
 * web 端是否显示某个 assistant 输出（文本气泡 / 工具调用 / 回合生命周期）的
 * 唯一判定：跟随产生它的回合来源——source 为 "web" 才显示。
 *
 * 文本与工具调用是同一回合的产物，共用这一条规则（一个道理一个逻辑）：
 * 用户输入来自 web → 该回合的所有输出都在 web 端显示；来自 CLI/手机 → 都不显示。
 * 聊天区（user_message/turn_start/turn_end）与工具侧栏（tool_*）一律走这里。
 */
function isWebSource(source: string | undefined): boolean {
  return source === "web";
}

/** 帧的 workspace 边界守卫：followup 合成流等路径的帧可能属于已切走的旧
 *  空间（视图切走后仍在跑的回合输出）——不属于当前空间边界的帧必须丢弃，
 *  否则旧空间回合输出投到已切走视图的浏览器（跨空间串话）。
 *  无 workspace_dir 字段的帧放行（直发帧/旧协议不带边界字段，由 source 门禁挡）。 */
function _isFrameInWorkspace(msg: ServerMessage, workspaceDir: string | null): boolean {
  const frameDir = (msg as { workspace_dir?: unknown }).workspace_dir;
  // 严格：缺空间归属的帧不放行。服务端所有出站帧（回合流、跟话镜像、唤醒流）
  // 都已盖 workspace_dir，放行无字段帧正是「切空间后别的空间的内容画到眼前」
  // 的那条路。
  if (typeof frameDir !== "string" || !frameDir.trim()) return false;
  if (!workspaceDir) return false;
  return frameDir.trim() === workspaceDir.trim();
}

/** 帧的会话归属守卫：帧显式带 session_id 且与本会话不同 → 其它会话的帧，
 *  不触碰本聊天区（跟随话跨会话镜像隔离）。无 session_id 字段放行。 */
function _isFrameInSession(msg: ServerMessage, sessionId: string | null): boolean {
  const sid = (msg as { session_id?: unknown }).session_id;
  if (typeof sid !== "string" || !sid.trim()) return true;
  if (!sessionId) return false;
  return sid.trim() === sessionId.trim();
}

/** 边界守卫集结：turn_start/chunk/turn_end/user_message 共用的前置过滤。
 *  返回 false 表示该帧不属于当前空间/会话边界，必须整体丢弃。 */
function _frameInBoundary(
  msg: ServerMessage,
  st: { workspaceDir: string | null; sessionId: string | null },
): boolean {
  const ok = _isFrameInWorkspace(msg, st.workspaceDir) && _isFrameInSession(msg, st.sessionId);
  if (!ok) _noteForeignTurnState(msg);
  return ok;
}

/** 别的空间的回合边界帧不进渲染，但它带着「那个空间此刻用哪个会话键」——顺手记进
 *  那个空间的会话键映射：否则那边 /new 过以后，切过去时端侧还拿旧键去匹配帧，
 *  那一侧的帧全被判成「别的会话」丢掉（界面上就是输出与 spinner 一起消失）。
 *  只记会话键；回合态一律由权威给（端侧既不缓存、也不从缓存恢复——缓存里那个
 *  「上次离开时」的回合态会让切回来先亮一圈假的，再被 hydrate 校正，就是那次 A 跳 B）。 */
function _noteForeignTurnState(msg: ServerMessage): void {
  const type = String((msg as { type?: unknown }).type ?? "");
  if (type !== "turn_start" && type !== "turn_end") return;
  const rawDir = (msg as { workspace_dir?: unknown }).workspace_dir;
  const dir = typeof rawDir === "string" ? rawDir.trim() : "";
  if (!dir) return;
  const state = useStore.getState();
  const known = state.sessionIdByWorkspace[dir];
  if (known === undefined) return;
  // 只对「别的空间」生效——同空间时本端正看着它，权威是本端自己的 sessionId。
  const frameSid = String((msg as { session_id?: unknown }).session_id ?? "");
  if (!(dir !== (state.workspaceDir ?? "") && frameSid)) return;
  useStore.setState({
    sessionIdByWorkspace: { ...state.sessionIdByWorkspace, [dir]: frameSid },
  });
}

/** 把尾部未被认领的乐观用户气泡标为「发送失败」（服务端早失败只回 error
 *  无 user_message 认领帧时调用，防乐观泡永久 optimistic 残留）。返回是否
 *  有气泡被标记——用于决定 error 帧是否再追加一条 △ 错误消息（已折叠进
 *  气泡失败样式时不重复追加）。
 *
 *  例外：带 `pendingInject` 的气泡是「回合进行中发出的接续输入」——它已经进了
 *  服务器那条队列（等注入到正在跑的回合里），error 帧说的是那个回合的事，不是
 *  这次发送失败。标错还会顺手把 optimistic 清掉，等它的 user_message 权威帧到达时
 *  认领判据就失效，同一句话会被再建一条（重复显示）。 */
function _markTailOptimisticFailed(
  messages: ChatMessage[],
): { messages: ChatMessage[]; marked: boolean } {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role === "user" && m.optimistic && !m.turn_id && m.pendingInject === true) {
      return { messages, marked: false };
    }
    if (m.role === "user" && m.optimistic && !m.turn_id && !m.sendFailed) {
      const updated = messages.slice();
      updated[i] = { ...m, sendFailed: true, optimistic: false, pendingInject: false };
      return { messages: updated, marked: true };
    }
    // 越过「本次发送可能已产生的痕迹」继续向上找；遇到已落定的用户/助手
    // 正文气泡即停止（更早的乐观泡属于历史回合，不应被本次失败波及）。
    if (m.role === "user" && !m.optimistic) break;
  }
  return { messages, marked: false };
}

/** When >0, trace entries are buffered and flushed once (trace_batch path). */
let _traceBatchDepth = 0;
let _traceBatchBuffer: TraceEventEntry[] = [];
let _traceBatchBumpSidebar = false;
/** 批内有树事件待合并刷新（批尾统一 set 一次，对齐 trace_batch 防冻结）。 */
let _traceBatchBumpTree = false;

/** 子智能体活动树状态机：独立于 traceEvents 环形缓冲维护（节点数远小于
 *  事件数，长回合工具事件不会把 subagent_start 挤出导致树建不全）。 */
const _subagentTree = new SubagentTreeTracker();
const SUBAGENT_TREE_TYPES = new Set([
  "subagent_start",
  "subagent_complete",
  "subagent_failed",
  "background_agent_start",
  "background_agent_complete",
  "tool_start",
  "tool_complete",
]);
/** 树事件里的开帧类：非活跃回合到达时树状态机必须已 clear，开帧只能造出
 *  挂不住的孤儿行——这类帧一并门掉（关帧走 tracker 的 no-op 容错即可）。 */
const SUBAGENT_TREE_OPENER_TYPES = new Set([
  "subagent_start",
  "background_agent_start",
  "tool_start",
]);

/** 终端 ANSI 控制序列（CSI / OSC）：shell 等工具的原始输出常带色码，浏览器
 *  按文本渲染不解析，ESC 后的残留字符会显示成莫名符号——trace 入库前剥除。 */
const ANSI_OSC_RE = /\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g;
const ANSI_CSI_RE = /\u001b\[[0-9;?]*[ -/]*[@-~]/g;

/** 原位剥除工具文本字段中的 ANSI 控制序列（summary / content / text / tool_output）。 */
function sanitizeTraceText(entry: TraceEventEntry): TraceEventEntry {
  for (const key of ["summary", "content", "text", "tool_output"] as const) {
    const value = entry[key];
    if (typeof value === "string") {
      (entry as unknown as Record<string, unknown>)[key] = value
        .replace(ANSI_OSC_RE, "")
        .replace(ANSI_CSI_RE, "");
    }
  }
  return entry;
}

function toTraceEntry(msg: ServerMessage): TraceEventEntry {
  const raw = msg as Record<string, unknown>;
  // Normalize tool_start / tool_call field aliases from the live WS payload.
  const tool =
    (typeof raw.tool === "string" && raw.tool) ||
    (typeof raw.tool_name === "string" && raw.tool_name) ||
    undefined;
  const callId =
    (typeof raw.call_id === "string" && raw.call_id) ||
    (typeof raw.tool_call_id === "string" && raw.tool_call_id) ||
    undefined;
  const args =
    (raw.args as Record<string, unknown> | undefined) ||
    (raw.arguments as Record<string, unknown> | undefined);
  const diffLines = raw.diff_lines as TraceEventEntry["diff_lines"] | undefined;
  const toolOutput = typeof raw.tool_output === "string" ? raw.tool_output : undefined;
  const toolOutputRef =
    typeof raw.tool_output_ref === "string" ? raw.tool_output_ref : undefined;
  const durationMs = typeof raw.duration_ms === "number" ? raw.duration_ms : undefined;
  const entry: TraceEventEntry = {
    id: nextTraceId(),
    event_type: msg.type,
    timestamp: nowISO(),
    ...("turn_id" in msg ? { turn_id: msg.turn_id } : {}),
    ...("source" in msg ? { source: msg.source } : {}),
    ...("origin_scope" in msg ? { origin_scope: msg.origin_scope } : {}),
    ...("session_id" in msg ? { session_id: msg.session_id } : {}),
    ...("subject" in msg && typeof (msg as { subject?: unknown }).subject === "string"
      ? { subject: (msg as { subject: string }).subject }
      : {}),
    ...(tool ? { tool } : {}),
    ...(args ? { args } : {}),
    ...(callId ? { call_id: callId } : {}),
    ...("summary" in msg ? { summary: msg.summary } : {}),
    ...("ok" in msg ? { ok: msg.ok } : {}),
    ...("reason" in msg ? { reason: msg.reason } : {}),
    ...("text" in msg ? { text: msg.text } : {}),
    ...("content" in msg ? { content: msg.content } : {}),
    ...("message" in msg ? { content: msg.message } : {}),
    ...("display_blocks" in msg ? { display_blocks: msg.display_blocks } : {}),
    ...(diffLines != null ? { diff_lines: diffLines } : {}),
    ...(toolOutput !== undefined ? { tool_output: toolOutput } : {}),
    ...("tool_output_truncated" in raw
      ? { tool_output_truncated: Boolean(raw.tool_output_truncated) }
      : {}),
    ...(toolOutputRef !== undefined ? { tool_output_ref: toolOutputRef } : {}),
    ...(durationMs !== undefined ? { duration_ms: durationMs } : {}),
    ...("is_error" in raw ? { is_error: Boolean(raw.is_error) } : {}),
  };
  return sanitizeTraceText(entry);
}

function trimTraceEvents(events: TraceEventEntry[]): TraceEventEntry[] {
  if (events.length <= MAX_TRACE_EVENTS) return events;
  return events.slice(events.length - MAX_TRACE_EVENTS);
}

/** Append a trace event to the store, trimming to MAX_TRACE_EVENTS.
 *  Uses a single array allocation instead of spread+splice. */
function pushTraceEvent(
  set: (partial: Partial<AppState>) => void,
  get: () => AppState,
  entry: TraceEventEntry,
): void {
  if (_traceBatchDepth > 0) {
    _traceBatchBuffer.push(entry);
    if (SIDEBAR_TRACE_TYPES.has(entry.event_type)) {
      _traceBatchBumpSidebar = true;
    }
    return;
  }
  const events = get().traceEvents;
  const next = trimTraceEvents(
    events.length < MAX_TRACE_EVENTS
      ? [...events, entry]
      : [...events.slice(events.length - MAX_TRACE_EVENTS + 1), entry],
  );
  if (SIDEBAR_TRACE_TYPES.has(entry.event_type)) {
    set({ traceEvents: next, activityEpoch: get().activityEpoch + 1 });
  } else {
    set({ traceEvents: next });
  }
}

function flushTraceBatch(
  set: (partial: Partial<AppState>) => void,
  get: () => AppState,
): void {
  if (_traceBatchBumpTree) {
    _traceBatchBumpTree = false;
    set({ subagentRows: _subagentTree.rows() });
  }
  if (_traceBatchBuffer.length === 0) {
    _traceBatchBumpSidebar = false;
    return;
  }
  const batch = _traceBatchBuffer;
  const merged = trimTraceEvents(get().traceEvents.concat(batch));
  _traceBatchBuffer = [];
  if (_traceBatchBumpSidebar) {
    _traceBatchBumpSidebar = false;
    set({ traceEvents: merged, activityEpoch: get().activityEpoch + 1 });
  } else {
    set({ traceEvents: merged });
  }
}

/** Append a chat message to the store, trimming to MAX_MESSAGES.
 *  Prevents unbounded memory growth in long sessions. */
function appendMessage(
  messages: ChatMessage[],
  msg: ChatMessage,
): ChatMessage[] {
  const result = [...messages, msg];
  if (result.length > MAX_MESSAGES) {
    return result.slice(result.length - MAX_MESSAGES);
  }
  return result;
}

/** 「已显示前缀不变式」（docs/消息渲染契约.md I2/I4/I8）：
 *  行一旦上屏，位置就冻结——后续任何落定只允许 ① 尾部追加新行、② 就地改某行内容；
 *  绝不重排、绝不跨位搬移已显示行。整表重排只允许发生在「整条线重建」那一次
 *  （刷新全量、切空间、epoch 变化），且重建后的顺序必须等于 view_seq 升序。
 *
 *  为什么不再「每次落定都归一次序」：迟到帧的序号可能小于已显示行，全局排序就会
 *  把用户正在读的那条往下推、把新行插进历史中间（用户报的「顺序会串、会跳」正是
 *  这一条）。代价是：迟到/补齐的行只按到达序落在尾部，历史位置暂时不对，等下一次
 *  整线重建（刷新 / 切空间 / epoch 变）按 view_seq 纠正。 */

/** 列表里是否已经有这个序号的行（存在性判据，替代旧的游标闸门）：
 *  有 ⇒ 这一帧已经上屏过（实时 / 重连回放 / 快照叠加三路），整帧丢弃；没有 ⇒ 放行。
 *  判据只看「行在不在」，不依赖任何游标——因此不存在「假空洞」与补齐往返。 */
function _hasRowWithSeq(list: ChatMessage[], seq: number): boolean {
  for (const m of list) if (m.seq === seq) return true;
  return false;
}

/** 裁剪到 MAX_MESSAGES（丢最旧；其余行的相对顺序与 id 一律不动）。 */
function _capMessages(list: ChatMessage[]): ChatMessage[] {
  if (list.length <= MAX_MESSAGES) return list;
  return list.slice(list.length - MAX_MESSAGES);
}

/** 重建顺序：按 view_seq 升序（无序号行按稳定序留在末尾）。只给整线重建用。 */
function _sortBySeqAsc(list: ChatMessage[]): ChatMessage[] {
  if (list.length < 2) return list;
  return [...list].sort(
    (a, b) => (a.seq ?? Number.MAX_SAFE_INTEGER) - (b.seq ?? Number.MAX_SAFE_INTEGER),
  );
}

/** 追加落定（内容行唯一入口）：同 seq 已上屏 → 幂等丢弃；否则**追加在尾部**。
 *  返回原引用＝这一帧没有产生新行（顺序与引用都不动，避免无谓重渲染）。 */
function _appendRow(list: ChatMessage[], row: ChatMessage): ChatMessage[] {
  if (row.seq !== undefined && _hasRowWithSeq(list, row.seq)) return list;
  return _capMessages([...list, row]);
}

/** 权威快照与已显示行的对账（只追加 + 只改内容）：
 *  - 命中既有行（seq → clientMsgId → 内容 key，一对一枚举）→ 就地覆盖内容并回填
 *    序号，**id 与位置都不动**（React key 取服务端键，因此既不重挂也不跳位）
 *  - 未命中 → 追加在尾部（快照内部已是 view_seq 升序；绝不插进屏幕中间）
 *  - 既有行不在这份快照里 → 原地保留（已显示的内容不因对账消失）
 *  *replace* 只在整条线重建时给 true：那一次整表换成快照，顺序＝view_seq 升序。 */
function _mergeSnapshotRows(
  current: ChatMessage[],
  rows: ChatMessage[],
  replace: boolean,
): ChatMessage[] {
  if (replace || current.length === 0) return _capMessages(_sortBySeqAsc(rows));
  const bySeq = new Map<number, number>();
  const byClientId = new Map<string, number>();
  const byKey = new Map<string, number[]>();
  current.forEach((m, i) => {
    if (m.seq !== undefined) bySeq.set(m.seq, i);
    if (m.clientMsgId) byClientId.set(m.clientMsgId, i);
    const k = m.key ?? computeMsgKey(m);
    const bucket = byKey.get(k);
    if (bucket) bucket.push(i);
    else byKey.set(k, [i]);
  });
  const used = new Set<number>();
  const out = current.slice();
  for (const row of rows) {
    let hit: number | undefined;
    if (row.seq !== undefined) hit = bySeq.get(row.seq);
    if (hit === undefined && row.clientMsgId) hit = byClientId.get(row.clientMsgId);
    if (hit === undefined) {
      hit = byKey.get(row.key ?? computeMsgKey(row))?.find((i) => !used.has(i));
    }
    if (hit === undefined) {
      out.push(row);
      continue;
    }
    used.add(hit);
    const prev = out[hit];
    out[hit] = {
      ...row,
      id: prev.id,
      // 权威快照行不带 turn_id：认领过的回合绑定不能因此丢（丢了会退化成
      // 「无回合的行」，后续 turn_end 收尾与再认领判据都失效）
      ...(row.turn_id === undefined && prev.turn_id !== undefined ? { turn_id: prev.turn_id } : {}),
    };
  }
  return _capMessages(out);
}

/** 落定一条内容行：追加 +（触顶时）回收折叠区数据。 */
function _withRow(list: ChatMessage[], row: ChatMessage): ChatMessage[] {
  const next = _appendRow(list, row);
  // 行数触顶＝这一笔把最旧的行挤掉了：折叠区里只服务于这些行的数据顺势回收
  // （不放这里的话，长会话里折叠区会随 call 累积，永远不释放）。
  if (next.length === MAX_MESSAGES && list.length >= MAX_MESSAGES) {
    _pruneFolding(next, useStore.getState().subagentRows);
  }
  return next;
}

/** 折叠区回收时无条件保留的「最近 call」数（按每张映射的插入序取尾部）：delegate 刚
 *  起步时它的工具行可能还没出现在 messages 里，只按 messages 裁剪会把刚到的指令 /
 *  过程帧一起抹掉；保留最近这几个既防误删，也仍然给长会话划了上限。 */
const FOLDING_KEEP_RECENT = 8;

/** 折叠区数据的回收：只保留 messages 里仍有 delegate 工具行的 call_id、活动树里正在
 *  跑的 call_id，以及最近 FOLDING_KEEP_RECENT 个出现过 call。没有可删的就什么都不做。 */
function _pruneFolding(messages: ChatMessage[], treeRows: TreeRow[]): void {
  const keep = new Set<string>();
  for (const m of messages) {
    const callId = m.tool?.tool_call_id;
    if (callId) keep.add(String(callId));
  }
  for (const r of treeRows) {
    if (r.nodeId) keep.add(r.nodeId);
  }
  const st = useStore.getState();
  for (const map of [st.subagentOutput, st.subagentDiffs, st.subagentBriefs]) {
    const keys = Object.keys(map);
    for (const k of keys.slice(Math.max(0, keys.length - FOLDING_KEEP_RECENT))) keep.add(k);
  }
  const pruned = <T>(map: Record<string, T>): Record<string, T> | null => {
    const keys = Object.keys(map);
    if (keys.length === 0) return null;
    const kept = keys.filter((k) => keep.has(k));
    if (kept.length === keys.length) return null;
    const next: Record<string, T> = {};
    for (const k of kept) next[k] = map[k];
    return next;
  };
  const subagentOutput = pruned(st.subagentOutput);
  const subagentDiffs = pruned(st.subagentDiffs);
  const subagentBriefs = pruned(st.subagentBriefs);
  if (!subagentOutput && !subagentDiffs && !subagentBriefs) return;
  useStore.setState({
    ...(subagentOutput ? { subagentOutput } : {}),
    ...(subagentDiffs ? { subagentDiffs } : {}),
    ...(subagentBriefs ? { subagentBriefs } : {}),
  });
}

/** 会产生内容行的帧（要按 seq 判重/丢弃的那一类）。不含 turn_start/turn_end 这类
 *  无行的状态帧：它们没有行可以重复，丢了反而丢状态（见 seq 闸门）。 */
const CONTENT_ROW_FRAME_TYPES = new Set([
  "user_message",
  "chunk",
  "tool",
  "diff",
  "error",
  "file",
  "command_result",
]);

/* ── 模块级独立会话（按 WS subject 键控）─────────────────────────────
 * 镜像主聊天的流式消息处理，但写入该模块独立的 moduleSessions[subject]，
 * 与主会话状态互不可见。每个 agentic 模块（config/...）一份。 */
function _patchModuleSession(
  get: () => AppState,
  set: (partial: Partial<AppState>) => void,
  subject: string,
  patch: Partial<ModuleSessionState>,
): void {
  const sessions = get().moduleSessions;
  const prev =
    sessions[subject] ?? { messages: [], turnActive: false, currentTurnId: null };
  set({ moduleSessions: { ...sessions, [subject]: { ...prev, ...patch } } });
}

function handleModuleChatEvent(
  subject: string,
  msg: ServerMessage,
  set: (partial: Partial<AppState>) => void,
  get: () => AppState,
): void {
  const session =
    get().moduleSessions[subject] ?? { messages: [], turnActive: false, currentTurnId: null };
  const msgs = session.messages;

  switch (msg.type) {
    case "turn_start": {
      if (msgs.some((m) => m.turn_id === msg.turn_id && m.streaming)) break;
      const autoIdx = msgs.findIndex(
        (m) => m.streaming && m.role === "assistant" && m.autoCreated,
      );
      if (autoIdx >= 0) {
        const updated = msgs.slice();
        updated[autoIdx] = { ...msgs[autoIdx], turn_id: msg.turn_id };
        _patchModuleSession(get, set, subject, {
          turnActive: true,
          currentTurnId: msg.turn_id,
          messages: updated,
        });
        break;
      }
      _patchModuleSession(get, set, subject, {
        turnActive: true,
        currentTurnId: msg.turn_id,
        messages: appendMessage(msgs, {
          id: nextId(),
          role: "assistant",
          text: "",
          turn_id: msg.turn_id,
          streaming: true,
        }),
      });
      break;
    }
    case "chunk": {
      const tid = session.currentTurnId;
      let targetIdx = -1;
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].streaming && msgs[i].role === "assistant" && (!tid || msgs[i].turn_id === tid)) {
          targetIdx = i;
          break;
        }
      }
      if (targetIdx < 0) {
        for (let i = msgs.length - 1; i >= 0; i--) {
          if (msgs[i].streaming && msgs[i].role === "assistant") {
            targetIdx = i;
            break;
          }
        }
      }
      if (targetIdx < 0) {
        const autoTurnId = tid || `auto-${nextId()}`;
        _patchModuleSession(get, set, subject, {
          turnActive: true,
          currentTurnId: autoTurnId,
          messages: appendMessage(msgs, {
            id: nextId(),
            role: "assistant",
            text: msg.text,
            turn_id: autoTurnId,
            streaming: true,
            autoCreated: true,
          }),
        });
      } else {
        const updated = msgs.slice();
        updated[targetIdx] = { ...msgs[targetIdx], text: msgs[targetIdx].text + msg.text };
        _patchModuleSession(get, set, subject, { messages: updated });
      }
      break;
    }
    case "turn_end": {
      const tid = msg.turn_id;
      const updated = msgs.map((m) =>
        m.streaming && m.role === "assistant" && (tid ? m.turn_id === tid || m.autoCreated : true)
          ? { ...m, streaming: false }
          : m,
      );
      _patchModuleSession(get, set, subject, {
        messages: updated,
        turnActive: false,
        currentTurnId: null,
      });
      break;
    }
    case "error":
      _patchModuleSession(get, set, subject, {
        messages: appendMessage(msgs, {
          id: nextId(),
          role: "assistant",
          text: msg.message,
        }),
      });
      break;
  }
}

/** Chat-ish event types routed to a module session when a subject is present. */
const MODULE_CHAT_TYPES = new Set(["turn_start", "chunk", "turn_end", "error"]);

/**
 * 回放帧是否与 store 已有内容重复（刷新双显的判定）。
 *
 * 分层：**带 view_seq 的回放帧走不到这里**——它们在 handleServerMessage 的
 * 「view_seq 对账」段就已被处置（≤ 游标＝快照已覆盖 → 内容行整帧丢弃）。因此
 * 下面按内容比对的几个分支实际只服务**没有序号的回放帧**（cli-attached 不落盘、
 * 落盘失败被 suppress 吞掉）：那类帧没有序号可对账，只能按内容对一次。
 * turn_start/turn_end 不产生内容行、不在 CONTENT_ROW_FRAME_TYPES 里，会整帧
 * 走到这里，它们的按回合判定始终有效（旧回合的迟到 turn_end 不得清新回合）。
 */
/** 无 view_seq 的过程帧白名单（服务端刻意的「不落带帧」）：
 *  - subagent_chunk：子智能体中间产出，只实时投递、不落带
 *    （web_views.to_view_frame 对它直接 return None），因此永远没有 view_seq，
 *    也永远不会出现在快照里。
 *  任何按 view_seq 的裁量（快照覆盖裁剪、gap 补齐、按内容去重）都必须先把它
 *  排除：否则它会被当成「已被快照覆盖 / 来源不明」整类丢掉，而它没有任何落带
 *  副本可以补回——delegate 工具行的展开区就永久缺一段。
 *  注：subagent_result **不在**白名单——它落带（有 view_seq），最终答复经快照的
 *  subagent_results 映射回到折叠区，按 seq 处理是安全的。 */
const UNPERSISTED_FRAME_TYPES = new Set(["subagent_chunk"]);

function _isUnpersistedProcessFrame(msg: ServerMessage): boolean {
  return UNPERSISTED_FRAME_TYPES.has(msg.type);
}

/** 快照带回的子智能体最终答复（tool_call_id → 文本）并入实时结果：
 *  - 快照值非空＝权威，覆盖同 call 的现有 result
 *  - 快照值为空＝不是更权威的内容，保留现有实时值
 *  - 快照里有、本地没有的 call 新建 {text: "", result}（中间产出丢过也还在）
 *  返回 null 表示快照没带这份映射（或为空）：调用方据此不写该字段，避免无谓重渲染。
 *  为什么它必须和 messages 同一次 set 落定：展开区的内容与消息是同一条线的两面，
 *  分两次提交会出现「消息已在、折叠区还是空的」这种半截画面。 */
function _mergeSubagentResults(
  existing: Record<string, { text: string; result: string }>,
  snapshot: Record<string, string> | undefined,
): Record<string, { text: string; result: string }> | null {
  if (!snapshot) return null;
  const entries = Object.entries(snapshot);
  if (entries.length === 0) return null;
  const merged: Record<string, { text: string; result: string }> = { ...existing };
  for (const [callId, raw] of entries) {
    const text = typeof raw === "string" ? raw : "";
    if (!text.trim()) continue;
    merged[callId] = { text: merged[callId]?.text ?? "", result: text };
  }
  return merged;
}

/** 帧的线内序号（view_seq）：只有落带帧才有；0 ＝ 无序号，不参与 seq 裁量。 */
function _frameViewSeq(msg: ServerMessage): number {
  const raw = (msg as { view_seq?: unknown }).view_seq;
  const n = typeof raw === "number" ? raw : Number(raw);
  return Number.isFinite(n) && n > 0 ? Math.trunc(n) : 0;
}

/** 折叠区的一帧（子智能体自己的工具行 / diff）→ 消息行形态：复用主消息流的工具行
 *  与 diff 渲染（同一形状、同一套组件），端上不为折叠区再写一套。 */
function _subagentFrameMessage(frame: SubagentFramePayload): ChatMessage | null {
  const seq =
    typeof frame.view_seq === "number" && frame.view_seq > 0
      ? Math.trunc(frame.view_seq)
      : undefined;
  if (frame.type === "tool") {
    const label = String(frame.text ?? "").trim();
    if (!label) return null;
    const tool: ChatToolLine = {
      label,
      ok: frame.ok !== false,
      tool_name: String(frame.tool_name ?? ""),
      tool_call_id: String(frame.tool_call_id ?? ""),
      duration_ms: frame.duration_ms ?? null,
    };
    return {
      id: nextId(),
      role: "assistant",
      text: "",
      tool,
      key: computeMsgKey({ role: "assistant", text: "", tool }),
      ...(seq !== undefined ? { seq } : {}),
    };
  }
  const diff = frame.diff_lines;
  if (!diff || !diff.hunks || diff.hunks.length === 0) return null;
  const diffCallId = String(frame.tool_call_id ?? "");
  return {
    id: nextId(),
    role: "assistant",
    text: "",
    diff,
    key: computeMsgKey({ role: "assistant", text: "", diff }),
    ...(diffCallId ? { tool_call_id: diffCallId } : {}),
    ...(seq !== undefined ? { seq } : {}),
  };
}

/** 折叠区帧合并：按 view_seq 去重（同一帧实时 / 断连回放 / 快照三路都可能送到），
 *  无序号帧按内容 key 兜底去重；结果按 seq 升序（无序号者排最后＝最新到达）。 */
function _mergeSubagentFrames(
  existing: ChatMessage[] | undefined,
  incoming: ChatMessage[],
): ChatMessage[] {
  const out = existing ? existing.slice() : [];
  for (const item of incoming) {
    const dup =
      item.seq !== undefined
        ? out.some((m) => m.seq === item.seq)
        : out.some((m) => m.key !== undefined && m.key === item.key);
    if (!dup) out.push(item);
  }
  return out.sort(
    (a, b) => (a.seq ?? Number.MAX_SAFE_INTEGER) - (b.seq ?? Number.MAX_SAFE_INTEGER),
  );
}

/** 快照带回的折叠区帧并入实时归集。返回 null 表示快照没带这份映射——调用方据此
 *  不写该字段，免得把实时攒下的内容清掉。 */
function _mergeSubagentDiffsSnapshot(
  existing: Record<string, ChatMessage[]>,
  snapshot: Record<string, SubagentFramePayload[]> | undefined,
): Record<string, ChatMessage[]> | null {
  if (!snapshot) return null;
  const callIds = Object.keys(snapshot);
  if (callIds.length === 0) return null;
  const merged: Record<string, ChatMessage[]> = { ...existing };
  for (const callId of callIds) {
    const incoming = (snapshot[callId] ?? [])
      .map((f) => _subagentFrameMessage(f))
      .filter((m): m is ChatMessage => m !== null);
    if (incoming.length === 0 && merged[callId] === undefined) continue;
    merged[callId] = _mergeSubagentFrames(merged[callId], incoming);
  }
  return merged;
}

/** 把一帧子智能体工具行 / diff 并进它父 delegate 行的折叠区（不进主消息流）。 */
function _ingestSubagentFrame(parentCallId: string, msg: ServerMessage): void {
  const entry = _subagentFrameMessage(msg as unknown as SubagentFramePayload);
  if (!entry) return;
  const st = useStore.getState();
  useStore.setState({
    subagentDiffs: {
      ...st.subagentDiffs,
      [parentCallId]: _mergeSubagentFrames(st.subagentDiffs[parentCallId], [entry]),
    },
  });
}

/** brief 帧判据：delegate 的任务指令不是对话正文（它属于那条 delegate 行的展开区）。
 *  - 新契约：帧显式带 ``delegate_brief: true``
 *  - 老数据兜底：正文以 ``<任务指令>`` 开头（服务端过滤之前落下的帧）
 *  返回 null 表示这不是 brief 帧。callId 可能为空（老帧不带父 call_id）。 */
function _delegateBrief(msg: ServerMessage): { callId: string; text: string } | null {
  const flagged = (msg as { delegate_brief?: unknown }).delegate_brief === true;
  const content = String((msg as { content?: unknown }).content ?? "");
  if (!flagged && !content.trimStart().startsWith("<任务指令>")) return null;
  return {
    callId: String((msg as { parent_tool_call_id?: unknown }).parent_tool_call_id || ""),
    text: content,
  };
}

/** 把一条 delegate 指令记进它父行的折叠区（同 call_id 取最新＝后到的权威）。 */
function _ingestSubagentBrief(callId: string, text: string): void {
  if (!callId || !text.trim()) return;
  const st = useStore.getState();
  if (st.subagentBriefs[callId] === text) return; // 回放/快照重复：不做无谓 set
  useStore.setState({ subagentBriefs: { ...st.subagentBriefs, [callId]: text } });
}

/** 快照带回的任务指令并入实时归集：非空值权威覆盖同 call 的现有值，空值不动
 *  （空不是「没有指令」的表达）。返回 null＝快照没带这份映射，调用方不写该字段。 */
function _mergeSubagentBriefs(
  existing: Record<string, string>,
  snapshot: Record<string, string> | undefined,
): Record<string, string> | null {
  if (!snapshot) return null;
  const entries = Object.entries(snapshot);
  if (entries.length === 0) return null;
  const merged: Record<string, string> = { ...existing };
  for (const [callId, raw] of entries) {
    const text = typeof raw === "string" ? raw : "";
    if (!text.trim()) continue;
    merged[callId] = text;
  }
  return merged;
}

/** 子智能体中间产出帧的序号水位：按 tool_call_id 记「已收到哪个 (turn_id, seq)」。
 *  重连回放会把同一批帧原样再送一次，靠文本判重会误伤（不同片段可能同文），靠
 *  replayed 标志会把丢帧窗口内真正的新帧一起丢掉——而 (turn_id, seq) 帧上本来
 *  就有，且天然单调，用它判重既精确又不误伤。 */
const _subagentChunkMarks = new Map<string, { turnId: string; seq: number }>();

/** 仅供 selftest：清掉过程帧序号水位（模块级状态，setState 复位不了）。 */
export function resetSubagentChunkMarksForTest(): void {
  _subagentChunkMarks.clear();
}

/** 边界未定期（刷新/切空间头几百毫秒，state 帧还没到）的帧缓冲。
 *
 * 此时 workspaceDir 为 null，内容帧过不了 _frameInBoundary，会被整体丢弃——
 * 「刷新后少一段输出」「回合中刷新丢掉正在流的正文」都出自这条路。改为：边界
 * 未定的内容帧先按到达序入缓冲，state 帧把边界落定后原样回放。回放走的是同一个
 * handleServerMessage（守卫、去重、乐观认领全都不绕），因此缓冲只是把同一批帧
 * 推迟到边界落定，不是第二份真相。
 *
 * 上限 300：溢出丢最旧并告警——宁可漏掉最早那屏旧内容，也不让缓冲无限增长。
 * 控制类帧（state / trace_batch / approval_* / navigation / 模块对话 / vault）
 * 不入缓冲：它们不参与内容渲染，边界未定时照常处理。
 */
const PENDING_FRAME_LIMIT = 300;
let _pendingFrames: ServerMessage[] = [];
/** 回放中：回放的帧不再进缓冲（否则守卫未通过时会二次入列，回放永远走不完）。 */
let _replayingPendingFrames = false;
/** 同一轮积压只告警一次：逐帧刷同样的日志会把真正的诊断信息淹掉。 */
let _pendingOverflowWarned = false;

/** 内容/回合类帧：受「边界已定」约束，边界未定时入缓冲。 */
const BOUNDARY_GATED_FRAME_TYPES = new Set([
  "user_message",
  "turn_start",
  "turn_queued",
  "chunk",
  "chat_chunk",
  "turn_end",
  "chat_turn_retracted",
  "subagent_chunk",
  "subagent_result",
  "tool",
  "diff",
  "command_result",
  "error",
  "info",
  "file",
]);

function _bufferPendingFrame(msg: ServerMessage): void {
  _pendingFrames.push(msg);
  if (_pendingFrames.length <= PENDING_FRAME_LIMIT) return;
  // 溢出丢最旧，但优先丢「能从快照补回」的落带帧：无 view_seq 的过程帧没有落带、
  // 也没有第二个来源，丢了就是展开区永久缺一段。整条都是过程帧时才退化为丢最旧。
  const droppableAt = _pendingFrames.findIndex((f) => !_isUnpersistedProcessFrame(f));
  _pendingFrames.splice(droppableAt >= 0 ? droppableAt : 0, 1);
  if (!_pendingOverflowWarned) {
    _pendingOverflowWarned = true;
    console.warn(
      `[store] pending frame buffer overflow: dropped oldest droppable frame(s) (limit ${PENDING_FRAME_LIMIT})`,
    );
  }
}

/** 边界落定后按到达序回放缓冲；回放帧走同一套守卫与去重，不绕行、不二次入缓冲。
 *  WS 本身有序，按到达序回放即为真实时间线；缓冲只是把同一批帧推迟到边界落定。 */
function _flushPendingFrames(): void {
  if (_pendingFrames.length === 0) return;
  const frames = _pendingFrames;
  _pendingFrames = [];
  _pendingOverflowWarned = false;
  _replayingPendingFrames = true;
  try {
    const handle = useStore.getState().handleServerMessage;
    for (const frame of frames) handle(frame);
  } finally {
    _replayingPendingFrames = false;
  }
}

/** 仅供 selftest：缓冲是模块级状态，setState 复位不了，用例之间需显式清空。 */
export function resetPendingFramesForTest(): void {
  _pendingFrames = [];
  _pendingOverflowWarned = false;
  _replayingPendingFrames = false;
}

/** 权威快照（/api/session/messages）随消息一起带回来的回合态。
 *  与 messages 在同一次提交里落定——分次 set 就是「先渲染一半再补齐」。 */
interface SnapshotRuntime {
  session_id?: string;
  running?: boolean;
  turn_source?: string;
  workspace_dir?: string;
}

export const useStore = create<AppState>((set, get) => ({  connected: false,
  setConnected: (v) => set({ connected: v }),
  connError: null,
  setConnError: (e) => set({ connError: e }),

  messages: [],
  turnActive: false,
  currentTurnId: null,
  turnStartedAt: null,
  pendingCommand: null,
  sessionId: null,
  workspaceDir: null,
  sessionIdByWorkspace: {},
  hydratedSeq: 0,
  viewReady: false,
  skeletonSince: Date.now(),
  needsHydrate: false,
  consumeNeedsHydrate: () => set({ needsHydrate: false }),
  lineEpoch: null,
  hydrateGeneration: 0,
  bumpHydrateGeneration: () => {
    const next = get().hydrateGeneration + 1;
    set({ hydrateGeneration: next });
    return next;
  },

  // 模块级独立会话（按 subject 键控）。初始为空，按需创建。
  moduleSessions: {},
  ensureModuleSession: (subject) => {
    const existing = get().moduleSessions[subject];
    if (existing) return existing;
    const fresh: ModuleSessionState = { messages: [], turnActive: false, currentTurnId: null };
    set({ moduleSessions: { ...get().moduleSessions, [subject]: fresh } });
    return fresh;
  },
  loadModuleHistory: (subject, messages) => {
    const session = get().ensureModuleSession(subject);
    // 历史端点只含已落盘的回合；进行中的回合（用户消息+流式输出）只在本地。
    // 重新挂载拉历史时，若本地消息更多（有进行中的内容），不做整体覆盖，
    // 否则进行中的回合会凭空消失直到回合结束才重现。
    if (session.messages.length > messages.length) return;
    const mapped: ChatMessage[] = messages.map((m) => ({
      id: nextId(),
      role: m.role as "user" | "assistant",
      text: m.text,
    }));
    set({
      moduleSessions: {
        ...get().moduleSessions,
        [subject]: { ...session, messages: mapped },
      },
    });
  },
  addModuleUserMessage: (subject, text, attachments) => {
    const session = get().ensureModuleSession(subject);
    set({
      moduleSessions: {
        ...get().moduleSessions,
        [subject]: {
          ...session,
          messages: appendMessage(session.messages, {
            id: nextId(),
            role: "user",
            text,
            ...(attachments && attachments.length > 0 ? { attachments } : {}),
          }),
        },
      },
    });
  },
  resetModuleSession: (subject) => {
    set({
      moduleSessions: {
        ...get().moduleSessions,
        [subject]: { messages: [], turnActive: false, currentTurnId: null },
      },
    });
  },
  pendingInteraction: null,
  runtime: null,
  workspaces: [],
  activeName: null,
  traceEvents: [],
  subagentRows: [],
  subagentOutput: {},
  subagentDiffs: {},
  subagentBriefs: {},
  activityEpoch: 0,
  toolActivityReady: false,
  vaultPrompt: null,
  vaultResult: null,
  clearVaultPrompt: () => set({ vaultPrompt: null }),
  clearVaultResult: () => set({ vaultResult: null }),
  pendingNav: null,
  consumeNav: () => set({ pendingNav: null }),
  account: null,
  setAccount: (a) => set({ account: a }),
  updatesPending: 0,
  setUpdatesPending: (n) => set({ updatesPending: Math.max(0, n) }),

  handleServerMessage: (msg: ServerMessage) => {
    // trace_batch: coalesce N events into one React update. Recursing with
    // per-event set() freezes the UI during busy turns (StatusSidebar etc.).
    if (msg.type === "trace_batch" && msg.events) {
      _traceBatchDepth += 1;
      try {
        for (const evt of msg.events) {
          get().handleServerMessage(evt);
        }
      } finally {
        _traceBatchDepth -= 1;
        if (_traceBatchDepth === 0) {
          flushTraceBatch(set, get);
        }
      }
      return;
    }

    // 模块级会话分流：带 subject 且非主会话的对话事件进对应模块会话，
    // 不触碰主聊天状态（messages / turnActive / currentTurnId）。
    const msgSubject = (msg as { subject?: string }).subject;
    if (MODULE_CHAT_TYPES.has(msg.type) && msgSubject && msgSubject !== "root") {
      handleModuleChatEvent(msgSubject, msg, set, get);
      return;
    }

    // 边界未定（刷新/切空间初期）：内容帧先入缓冲，等 state 帧把 workspaceDir
    // 落定后回放。不放这里挡一下，这些帧过不了守卫、会静默丢在门口。
    if (!_replayingPendingFrames && !get().workspaceDir && BOUNDARY_GATED_FRAME_TYPES.has(msg.type)) {
      _bufferPendingFrame(msg);
      return;
    }

    // ---- 行存在性裁量（「已显示的行不再重复上屏」的硬半边）----
    // 落带帧都带 view_seq（同一条线内唯一、单调）。判据刻意不用游标：只要
    // **列表里已经有同序号的行**，这一帧就是已经上屏过的那条（实时 / 重连回放 /
    // 快照叠加三路都会送同一帧），整帧丢弃防双显；否则放行——它是一次全新内容，
    // 由各自的落定路径**追加在尾部**（绝不插进历史）。
    // 没有游标 ⇒ 不存在「假空洞」：不再有帧被扣进缓冲等一次补齐往返，
    // 也没有「补齐推不动游标就永久卡住」这条路。无 view_seq 的帧（子智能体过程帧
    // 白名单等）不参与裁量，直接放行。
    const frameSeq = _frameViewSeq(msg);
    if (
      frameSeq > 0 &&
      CONTENT_ROW_FRAME_TYPES.has(msg.type) &&
      _frameInBoundary(msg, get()) &&
      _hasRowWithSeq(get().messages, frameSeq)
    ) {
      return;
    }

    const parentCallId = String((msg as { parent_tool_call_id?: unknown }).parent_tool_call_id || "");
    if (parentCallId && (msg.type === "tool" || msg.type === "diff")) {
      if (_frameInBoundary(msg, get())) _ingestSubagentFrame(parentCallId, msg);
      return;
    }

    // 子智能体活动树：subagent 生命周期与 tool_start/tool_complete 喂给树状态机
    // （渲染面是 delegate 工具行的展开区）。来源过滤与侧栏同规则——web 聊天区只显
    // web 来源回合的活动；他端（cli/matrix）发起的子智能体不镜像；无 source
    // 的兼容期事件放行（宁多勿丢）。批内只喂不 set（合并到批尾一次更新，与
    // trace_batch 防 UI 冻结的设计一致）。
    if (SUBAGENT_TREE_TYPES.has(msg.type)) {
      const src = (msg as { source?: string }).source;
      // 回合门禁：树只在回合活跃期间被喂。非活跃回合到达的帧只有两种——
      // 迟到关帧（树已 clear，tracker no-op 容错，喂了也无变化，不喂更省）
      // 与无关开帧（新空间首事件等，会建孤儿行把已清空的树复活）——整类门掉。
      const gate = (src && !isWebSource(src)) || (!get().turnActive && SUBAGENT_TREE_OPENER_TYPES.has(msg.type));
      if (!gate) {
        _subagentTree.ingest(msg as unknown as import("./subagentTree").TreeEvent);
        if (_traceBatchDepth === 0) {
          set({ subagentRows: _subagentTree.rows() });
        } else {
          _traceBatchBumpTree = true;
        }
      }
    }

    // Push chat/sidebar-relevant events into the in-memory activity buffer.
    // Tool_call/tool_result feed StatusSidebar; chat_chunk is kept for turn markers.
    if (TRACE_TYPES.has(msg.type)) {
      const entry = toTraceEntry(msg);
      // web 侧栏只显示 web 来源回合的工具调用（与聊天区「只显 web 对话」同一规则）：
      // 工具事件的 source 继承其回合来源（后端 _emit_trace 统一补）；
      // 他端来源（cli/matrix/background）的工具调用不进侧栏。
      // 无 source 的旧事件（兼容期）保留——宁多勿丢，避免误伤历史显示。
      // 他端/他空间会话的事件（服务端标 detached）不进本端侧栏：切空间后
      // 别的空间的工具调用不得串显在「最近工具调用」里（活动树仍已 ingest，
      // 关行事件不受影响）。
      if ((msg as { detached?: boolean }).detached === true) {
        // skip sidebar buffer only
      } else if (entry.event_type.startsWith("tool_") && entry.source && !isWebSource(entry.source)) {
        // fall through to switch below (state updates), but skip the sidebar buffer.
      } else if (entry.event_type === "turn_start" || entry.event_type === "turn_end") {
        const tid = entry.turn_id;
        if (tid) {
          const events = get().traceEvents;
          const existingIdx = events.findIndex(
            (e) => e.event_type === entry.event_type && e.turn_id === tid
          );
          if (existingIdx >= 0) {
            const existing = events[existingIdx];
            // Replace if new one has source and existing doesn't.
            if (entry.source && !existing.source) {
              if (_traceBatchDepth > 0) {
                // In a batch, drop the older duplicate from the live array and
                // buffer the richer entry once.
                const newEvents = events.slice();
                newEvents[existingIdx] = entry;
                set({ traceEvents: newEvents });
              } else {
                const newEvents = [...events];
                newEvents[existingIdx] = entry;
                set({ traceEvents: newEvents });
              }
            }
            // Else: skip the duplicate.
            // Fall through to switch below for state updates.
          } else {
            pushTraceEvent(set, get, entry);
          }
        } else {
          pushTraceEvent(set, get, entry);
        }
      } else {
        pushTraceEvent(set, get, entry);
      }
    }

    switch (msg.type) {
      case "user_message": {
        // 指令进折叠：delegate 的任务指令不是对话正文（它属于那条 delegate 行的
        // 展开区），先于一切正文逻辑判掉——它的文本还可能被认领逻辑当成乐观泡。
        // 缺父 call_id 的老帧无处归集，只能丢弃（留在正文流更糟：它不该出现在这里）。
        const brief = _delegateBrief(msg);
        if (brief) {
          if (brief.callId && isWebSource(msg.source) && _frameInBoundary(msg, get())) {
            _ingestSubagentBrief(brief.callId, brief.text);
          }
          break;
        }
        // S1 服务端确认：web 自己的 user_message 权威帧到达时，认领本地的同一行并
        // 回填 turn_id / 序号——本地显示与服务端持久化对齐，不再漂移。
        // 他端 source（CLI/Matrix）不触碰 web 的气泡（web 聊天区只显 web 对话）。
        if (!isWebSource(msg.source)) break;
        if (!_frameInBoundary(msg, get())) break;
        const msgs = get().messages;
        const content = (msg as { content?: string }).content;
        // 帧的线内序号：落到行上（顺序的权威键；已上屏的行不因它再被搬动）
        const msgSeq = _frameViewSeq(msg);
        const rawClientId = (msg as { client_msg_id?: unknown }).client_msg_id;
        const msgClientId = typeof rawClientId === "string" ? rawClientId.trim() : "";
        const frameText = typeof content === "string" ? content.trim() : "";
        // 认领判据（在尾部窗口内从新往旧找第一条「这条帧在本地的版本」）：
        //  ① 乐观泡：本端刚发出去、还没绑定回合的即时回显；
        //  ② 序号相同：`seq` 是这条线上的位置，命中即同一句（hydrate 换来的权威行走这条）；
        //  ③ 无序号 + 无回合 + 文本相同（trim 后）：remount 的净化把乐观标记抹掉后仍有救。
        // 为什么不能只认 ①：hydrate / 快照对账会把乐观泡换成权威行，换完之后乐观标记与
        // turn_id 都没了；此时只按乐观标记找会认领失败，接着「按 turn_id 建新泡」的判据
        // 也因为本地没有带该 turn_id 的行而判成「不存在」——同一句话于是被显示两遍
        //（用户反馈的接续输入重复，正是这条路）。
        // 为什么是尾部窗口：同文的老气泡不该被这条帧认领走（老行都带 seq，由 ② 精确命中）。
        const CLAIM_TAIL_WINDOW = 24;
        const tailStart = Math.max(0, msgs.length - CLAIM_TAIL_WINDOW);
        let claimed = false;
        for (let i = msgs.length - 1; i >= tailStart; i--) {
          const m = msgs[i];
          if (m.role !== "user" || m.turn_id) continue;
          // ① 端上标识精确配对：首选判据——不受重挂载净化、文本改写、序号未到影响，
          //    把「猜这条帧对应哪一行」变成一次查表。
          const byClientId = msgClientId !== "" && m.clientMsgId === msgClientId;
          // 以下三条是无标识帧（老数据 / 他端来源）的兜底启发式：
          const byEcho = m.optimistic === true;
          const bySeq = msgSeq > 0 && m.seq === msgSeq;
          const byText = m.seq === undefined && frameText !== "" && m.text.trim() === frameText;
          if (!byClientId && !byEcho && !bySeq && !byText) continue;
          const updated = msgs.slice();
          updated[i] = {
            ...m,
            turn_id: msg.turn_id,
            optimistic: false,
            // 权威帧到达＝这条输入已被服务端收进本回合的线上：顺手摘掉「待注入」徽标。
            // 注入事件（continuation_input_injected）按文本匹配清除，而权威帧会把文本
            // 覆盖成权威内容，竞态下那一路会落空——这里是同一件事的可靠时机。
            ...(m.pendingInject ? { pendingInject: false } : {}),
            // 早前的失败标记同样作废：服务端已经确认收到这条输入（标了 sendFailed 的
            // 那条本地泡如果留着，钉住它的那次 error 帧其实是别的错）。
            ...(m.sendFailed ? { sendFailed: false } : {}),
            ...(typeof content === "string" && content.trim() ? { text: content } : {}),
            // 已有 seq 的行（hydrate 换来的权威行）不回填：它本来就带着线上的位置。
            ...(msgSeq > 0 && m.seq === undefined ? { seq: msgSeq } : {}),
          };
          // 认领后这行就是落带行：它本来就停在尾部（乐观泡是追加在尾部的），
          // 因此只改内容、不搬位置（已显示前缀不变式）。同一帧再送一次时，
          // 帧级「行存在性」裁量已经挡住，不会走到这里。
          set({ messages: updated });
          claimed = true;
          break;
        }
        // 回放/断连重连路径：本地没有这条帧对应的行（刷新后列表为空、已 hydrate、
        // 或这份帧还没被认领过）——新建一条，否则刷新后「只见回复不见提问」。
        // 去重两道：① 线上序号相同＝同一帧的本地行已存在；② 同 turn_id 的用户行已存在。
        // 少了 ①，hydrate 换来的权威行会让这里判成「不存在」，同一句话再建一条＝重复显示。
        if (!claimed) {
          const tid = msg.turn_id;
          const existing = get().messages;
          const already =
            (msgSeq > 0 && existing.some((m) => m.role === "user" && m.seq === msgSeq)) ||
            (tid ? existing.some((m) => m.role === "user" && m.turn_id === tid) : false);
          if (!already) {
            if (typeof content === "string" && content.trim()) {
              set({
                messages: _withRow(get().messages, {
                  id: nextId(),
                  role: "user",
                  text: content,
                  key: computeMsgKey({ role: "user", text: content }),
                  ...(tid ? { turn_id: tid } : {}),
                  ...(msgSeq > 0 ? { seq: msgSeq } : {}),
                }),
              });
            }
          }
        }
        break;
      }

      case "turn_start": {
        // Web chat is local-only: only this frontend's own turns (direct WS
        // messages carry source="web") may create streaming bubbles. Trace
        // events from CLI/Matrix/event AND source-less turns (subagents,
        // internal auto turns) are never mirrored — a source-less turn_start
        // would otherwise hijack currentTurnId and swallow the user's stream.
        if (!isWebSource(msg.source)) break;
        if (!_frameInBoundary(msg, get())) break;
        const msgs = get().messages;
        // 正文块先于 turn_start 到达时已自建 autoCreated 气泡（auto-N 占位
        // turn_id）：回填真实 turn_id，供 turn_end 对齐认领。只认尾部那个
        // （最近一次正文块）；更早的 auto 气泡由 turn_end 的 autoCreated 兜底收尾。
        const tailIdx = msgs.length - 1;
        const tailMsg = tailIdx >= 0 ? msgs[tailIdx] : null;
        if (tailMsg && tailMsg.role === "assistant" && tailMsg.autoCreated && tailMsg.turn_id?.startsWith("auto-")) {
          const updated = msgs.slice();
          updated[tailIdx] = { ...tailMsg, turn_id: msg.turn_id };
          set({ turnActive: true, currentTurnId: msg.turn_id, turnStartedAt: get().turnStartedAt ?? Date.now(), messages: updated });
          break;
        }
        // 正文还没到：只标记回合进行中（供输入锁定/排队提示），不创建空气泡——
        // 正文块到达时自建气泡。
        set({ turnActive: true, currentTurnId: msg.turn_id, turnStartedAt: get().turnStartedAt ?? Date.now() });
        break;
      }

      case "turn_queued": {
        // 后端回合锁排队（CLI/Matrix 占着回合）：正文还没到，先显示「排队中」
        // 占位气泡，用户不再面对空白无反馈。正文 chunk 到达时替换掉此占位。
        if (!isWebSource(msg.source)) break;
        if (!_frameInBoundary(msg, get())) break;
        const msgs = get().messages;
        if (!msgs.some((m) => m.turn_id === msg.turn_id && m.queued)) {
          const queuedSeq = _frameViewSeq(msg);
          set({
            turnActive: true,
            currentTurnId: msg.turn_id,
            turnStartedAt: get().turnStartedAt ?? Date.now(),
            messages: _withRow(msgs, {
              id: nextId(),
              role: "assistant",
              text: "",
              turn_id: msg.turn_id,
              streaming: true,
              queued: true,
              ...(queuedSeq > 0 ? { seq: queuedSeq } : {}),
            }),
          });
        }
        break;
      }

      case "chunk": {
        // 与 turn_start/turn_end 同门：非 web 源（含无 source 的脏帧）不得亮 spinner /
        // 不得往主聊天区塞气泡。CLI 同空间回合、重连误回放都会踩这里。
        if (!isWebSource(msg.source)) break;
        // followup 合成流等路径可能把旧空间/旧会话的回合输出投到已切走视图
        // （跨空间串话）：带边界字段的帧必须与当前边界一致才消费。
        if (!_frameInBoundary(msg, get())) break;
        // 一轮 LLM turn 只输出一块正文（服务端整段单次路由 emit），因此
        // 每条正文 chunk 帧就是一个独立的显示块——直接新建气泡，而不是续写
        // 尾部 streaming 气泡。这样同一用户回合内多个 LLM turn 的正文
        // （a 段 → 工具 → b 段）自然分成 a 一个气泡、b 一个气泡。
        const state = get();
        const msgs = state.messages;
        const currentTurnId = state.currentTurnId;
        // turn_start 可能还没经 trace 批处理到达（~100ms 延迟）：沿用
        // currentTurnId 或占位 auto id，后续 turn_start/turn_end 按 turn_id
        // 对齐认领。
        const autoTurnId = currentTurnId || `auto-${nextId()}`;
        // 帧的线内序号：写进行里，迟到/回放帧才能落回正确位置。
        const msgSeq = _frameViewSeq(msg);
        // 尾部若是「排队中」占位气泡（queued，无正文）：用首块正文替换它，
        // 而不是在占位后再叠一个气泡。
        const tail = msgs.length > 0 ? msgs[msgs.length - 1] : null;
        if (tail && tail.role === "assistant" && tail.queued && !tail.text) {
          const updated = msgs.slice();
          updated[msgs.length - 1] = {
            ...tail,
            text: msg.text,
            key: computeMsgKey({ role: "assistant", text: msg.text }),
            queued: false,
            streaming: false,
            autoCreated: true,
            ...(msgSeq > 0 ? { seq: msgSeq } : {}),
          };
          set({
            turnActive: true,
            currentTurnId: autoTurnId,
            turnStartedAt: get().turnStartedAt ?? Date.now(),
            messages: updated,
          });
          break;
        }
        set({
          turnActive: true,
          currentTurnId: autoTurnId,
          turnStartedAt: get().turnStartedAt ?? Date.now(),
          messages: _withRow(msgs, {
            id: nextId(),
            role: "assistant",
            text: msg.text,
            key: computeMsgKey({ role: "assistant", text: msg.text }),
            turn_id: autoTurnId,
            // 正文块整段到达即完整：不标 streaming（无打字光标），回合是否
            // 还在进行由 turnActive 表达。
            streaming: false,
            autoCreated: true,
            ...(msgSeq > 0 ? { seq: msgSeq } : {}),
          }),
        });
        break;
      }

      case "continuation_input_injected": {
        // 接续输入已注入 LLM 上下文（开新段）：按 user_texts 文本匹配清掉对应
        // 用户气泡的 pendingInject 徽标（LLM 已看到这句话）。注入的 user_texts
        // 已剥远端标记，与气泡文本直接可比。
        const injected = (msg as { user_texts?: unknown }).user_texts;
        if (Array.isArray(injected) && injected.length > 0) {
          const injectedTexts = new Set(injected.map((t) => String(t || "").trim()).filter(Boolean));
          if (injectedTexts.size > 0) {
            const updated = get().messages.map((m) =>
              m.role === "user" && m.pendingInject && injectedTexts.has(m.text.trim())
                ? { ...m, pendingInject: false }
                : m,
            );
            set({ messages: updated });
          }
        }
        // 子智能体的最终结果不在这里上屏：它只活在 delegate 工具行的展开区
        // （subagent_result 帧 → subagentOutput[tool_call_id].result）。主页再
        // 追加一条「结果气泡」＝同一件事两处呈现，且刷新后与视图带不一致。
        break;
      }

      case "chat_turn_retracted": {
        // Mid-turn workspace switch strips agent history; the mirrored chat rows must
        // stop being rendered. **已显示的行不删、只隐藏**（已显示前缀不变式）：删行
        // 会让它下面每一行上移一格，正是用户报的「跳」。
        const tid = msg.turn_id;
        const state = get();
        const rows = state.messages;
        const hadTurn = Boolean(tid && rows.some((m) => m.turn_id === tid));
        const isCurrent = Boolean(tid && state.currentTurnId === tid);
        if (!hadTurn && !isCurrent) break;
        // 撤回范围与原实现一致（只从尾部认领那些「本轮刚产生的痕迹」），
        // 差别只是「标记隐藏」而不是「删掉」。
        const doomed = new Set<number>();
        rows.forEach((m, i) => {
          if (tid && m.turn_id === tid) doomed.add(i);
        });
        for (let i = rows.length - 1; i >= 0; i--) {
          const m = rows[i];
          const tailTrait =
            (m.role === "user" && !m.turn_id) ||
            (m.role === "assistant" &&
              // diff 块 text 为空但是有效内容块，不算「空气泡」
              !m.diff &&
              (m.streaming === true || !m.text.trim()));
          if (!tailTrait) break;
          doomed.add(i);
        }
        if (doomed.size === 0) break;
        set({
          messages: rows.map((m, i) =>
            doomed.has(i) ? { ...m, retracted: true, streaming: false, queued: false } : m,
          ),
          turnActive: false,
          currentTurnId: null,
          turnStartedAt: null,
        });
        break;
      }

      case "turn_end": {
        // Only this frontend's own turn_end (direct WS, source="web") may
        // clear streaming state. Trace-path turn_end from other frontends or
        // source-less subagent turns must not touch turnActive/currentTurnId —
        // otherwise a subagent finishing would prematurely end the user's
        // own streaming bubble and unlock the input mid-turn.
        if (!isWebSource(msg.source)) break;
        const tid = msg.turn_id;
        // 迟到 turn_end 门禁：tid 明确且不属当前回合时只清气泡/徽标，
        // 不碰 turnActive/subagentRows——否则会清掉新回合的 spinner 与活动树。
        const staleTurnEnd = Boolean(tid && get().currentTurnId && tid !== get().currentTurnId);
        // 清掉「排队中」占位泡的排队态（回合被打断/出错、正文始终未到达）——
        // 只清标记不删行：删了会让它下面的行上移一格（跳）。
        const msgs = get().messages.map((m) => {
          const base = m.role === "assistant" && m.queued && !m.text ? { ...m, queued: false, streaming: false } : m;
          if (base.streaming && base.role === "assistant" && (tid ? base.turn_id === tid || base.autoCreated : true)) {
            return { ...base, streaming: false };
          }
          // 回合终止即注入已成定局：清掉残留「待注入」徽标。注入事件与
          // user_message 权威帧竞态时（注入极快、文本被权威帧覆盖），文本
          // 匹配清除会落空，回合结束是唯一可靠的兜底时机。
          if (base.role === "user" && base.pendingInject) {
            return { ...base, pendingInject: false };
          }
          return base;
        });
        if (staleTurnEnd) {
          set({ messages: msgs });
          break;
        }
        _subagentTree.clear();
        set({ messages: msgs, turnActive: false, currentTurnId: null, turnStartedAt: null, subagentRows: [] });
        break;
      }

      case "subagent_chunk": {
        if (!_frameInBoundary(msg, get())) break;
        const cid = String((msg as { tool_call_id?: string }).tool_call_id || "");
        const piece = String((msg as { text?: string }).text || "");
        if (!cid || !piece) break;
        // (turn_id, seq) 单调水位判重：同一帧被重连回放时键不变 ⇒ 丢弃；丢帧窗口内
        // 真正的新帧键更大 ⇒ 照收。为什么不用文本判重（不同片段可能同文）也不用
        // replayed 标志（它会把新帧一起丢）：这两个字段帧上本来就有，且天然单调。
        const turnId = String((msg as { turn_id?: string }).turn_id || "");
        const seq = Number((msg as { seq?: number }).seq || 0);
        if (seq > 0) {
          const mark = _subagentChunkMarks.get(cid);
          if (mark && mark.turnId === turnId && seq <= mark.seq) break;
          _subagentChunkMarks.set(cid, { turnId, seq });
        } else if ((msg as { replayed?: boolean }).replayed === true) {
          // 老服务端帧上没有序号：退化为「重放丢弃」——新帧不带 replayed，仍放行。
          break;
        }
        const prev = get().subagentOutput[cid] ?? { text: "", result: "" };
        set({
          subagentOutput: {
            ...get().subagentOutput,
            [cid]: { ...prev, text: prev.text + piece },
          },
        });
        break;
      }

      case "subagent_result": {
        if (!_frameInBoundary(msg, get())) break;
        const cid = String((msg as { tool_call_id?: string }).tool_call_id || "");
        const body = String((msg as { text?: string }).text || "");
        if (!cid || !body) break;
        const prev = get().subagentOutput[cid] ?? { text: "", result: "" };
        set({ subagentOutput: { ...get().subagentOutput, [cid]: { ...prev, result: body } } });
        break;
      }

      case "tool_call":
        // Tool calls feed StatusSidebar via traceEvents; Chat shows final text only.
        break;

      case "tool_result":
        // Tool results likewise feed the sidebar buffer, not chat bubbles.
        break;

      case "tool": {
        // 工具行进聊天流：插在正文段落之间（CLI scrollback 同构）。与 chunk
        // 同门——只认 web 来源回合，他端工具行不入本端聊天区。
        const toolSource = (msg as { source?: string }).source;
        if (toolSource !== undefined && !isWebSource(toolSource)) break;
        if (!_frameInBoundary(msg, get())) break;
        const label = String((msg as { text?: string }).text || "").trim();
        if (!label) break;
        const tool: ChatToolLine = {
          label,
          ok: (msg as { ok?: boolean }).ok !== false,
          tool_name: String((msg as { tool_name?: string }).tool_name || ""),
          tool_call_id: String((msg as { tool_call_id?: string }).tool_call_id || ""),
          duration_ms: (msg as { duration_ms?: number }).duration_ms ?? null,
        };
        const toolSeq = _frameViewSeq(msg);
        set({
          messages: _withRow(get().messages, {
            id: nextId(),
            role: "assistant",
            text: "",
            tool,
            key: computeMsgKey({ role: "assistant", text: "", tool }),
            ...(toolSeq > 0 ? { seq: toolSeq } : {}),
          }),
        });
        break;
      }

      case "diff": {
        // 与 chunk/turn_* 同门：只显示 web 回合产出的 diff（他端工具 diff 不进聊天区）。
        const diffSource = (msg as { source?: string }).source;
        if (diffSource !== undefined && !isWebSource(diffSource)) break;
        // 边界守卫（与 chunk 同一把尺）：切空间后旧空间回合的 diff 不得画到眼前。
        if (!_frameInBoundary(msg, get())) break;
        const diff = (msg as { diff_lines?: import("./ws").CanonicalDiffLines }).diff_lines;
        if (!diff || !diff.hunks || diff.hunks.length === 0) break;
        const diffSeq = _frameViewSeq(msg);
        // 产出它的工具调用 id：落定后按它挂到那条工具行紧后面（不靠投递相邻）。
        const diffCallId = String((msg as { tool_call_id?: string }).tool_call_id || "");
        set({
          messages: _withRow(get().messages, {
            id: nextId(),
            role: "assistant",
            text: "",
            diff,
            key: computeMsgKey({ role: "assistant", text: "", diff }),
            ...(diffCallId ? { tool_call_id: diffCallId } : {}),
            ...(diffSeq > 0 ? { seq: diffSeq } : {}),
          }),
        });
        break;
      }

      case "command_result": {
        // 本连接单播回执：先清 pendingCommand（否则边界丢帧会让 /compact spinner 永挂）。
        // 再按空间/会话边界决定是否上屏——缺归属的旧帧也放行（服务端已补盖）。
        set({ pendingCommand: null });
        if (!msgSubject || msgSubject === "root") {
          const frameDir = (msg as { workspace_dir?: unknown }).workspace_dir;
          if (typeof frameDir === "string" && frameDir.trim()) {
            if (!_frameInBoundary(msg, get())) break;
          }
        }
        const dividerLabel = timelineDividerLabelFromCommand(msg.result);
        const cmdSeq = _frameViewSeq(msg);
        const bubble: ChatMessage = dividerLabel
          ? {
              id: nextId(),
              role: "assistant",
              text: msg.result.output,
              dividerLabel,
              dividerTime: formatTimelineTimeLabel(Date.now()),
              key: computeMsgKey({ role: "assistant", text: msg.result.output, dividerLabel }),
              ...(cmdSeq > 0 ? { seq: cmdSeq } : {}),
            }
          : {
              id: nextId(),
              role: "assistant",
              text: msg.result.output,
              isCommandResult: true,
              key: computeMsgKey({ role: "assistant", text: msg.result.output }),
              ...(cmdSeq > 0 ? { seq: cmdSeq } : {}),
            };
        // 命令可请求页面导航（如 /login → 登录页），复用 pendingNav 通道
        const nav = (msg.result?.data as Record<string, unknown> | undefined)?.navigate;
        if (typeof nav === "string" && nav) {
          set({ pendingNav: nav });
        }
        if (msgSubject && msgSubject !== "root") {
          const session = get().ensureModuleSession(msgSubject);
          set({
            moduleSessions: {
              ...get().moduleSessions,
              [msgSubject]: {
                ...session,
                messages: appendMessage(session.messages, bubble),
              },
            },
          });
        } else {
          // 命令输出按时间顺序落位：有按时间追加的语义（帧顺序即真相），
          // 同时过一遍顺序归一——它是内容行，不能把有序的正文段落挤乱。
          set({ messages: _withRow(get().messages, bubble) });
        }
        break;
      }

      // session_auto_new is intentionally not rendered as a chat bubble.
      // The backend has already started a fresh session; the next state/heartbeat
      // will update session_id and resetForWorkspaceSwitch will clear the chat.
      // Showing it here would cause a flicker or immediate disappearance.
      // The event still appears in the sidebar activity buffer via TRACE_TYPES.

      case "approval_request":
        set({
          pendingInteraction: {
            kind: "approval",
            approval_id: msg.approval_id,
            question: msg.question,
            options: msg.options,
            timeout_s: msg.timeout_s,
            workspace: msg.workspace,
            created_at_ms: msg.created_at_ms,
          },
        });
        break;

      // Server pushes approval_resolved when a pending prompt times out or is
      // cancelled server-side — the modal must close instead of hanging forever.
      case "approval_resolved": {
        // 服务端终态回推（已批准/已拒绝/超时/取消）：关掉永挂的审批 Modal（仅当仍是当前 pending）。
        const pending = get().pendingInteraction;
        if (!pending || pending.approval_id !== msg.approval_id) break;
        const handledElsewhere = msg.outcome === "approved" || msg.outcome === "rejected";
        set({
          pendingInteraction: null,
          messages: _appendRow(get().messages, {
            id: nextId(),
            role: "assistant",
            text: handledElsewhere
              ? "该确认已在其它端处理。"
              : "该确认已失效（超时或已取消），请重试。",
            key: computeMsgKey({ role: "assistant", text: `approval_resolved:${msg.approval_id}` }),
          }),
        });
        break;
      }

      case "error": {
        // 本连接错误回执：先清 pendingCommand，再按边界决定是否上屏。
        // 服务端 _error_frame 已盖归属；缺字段的旧帧仍清 spinner，避免命令永挂。
        set({ pendingCommand: null });
        const frameDir = (msg as { workspace_dir?: unknown }).workspace_dir;
        if (typeof frameDir === "string" && frameDir.trim()) {
          if (!_frameInBoundary(msg, get())) break;
        }
        // 服务端早失败（未起回合，无 user_message 认领帧）时，把尾部乐观用户泡
        // 折叠标为发送失败——错误已内聚到气泡样式，不再追加 △ 错误消息。
        const failed = _markTailOptimisticFailed(get().messages);
        const errSeq = _frameViewSeq(msg);
        set({
          messages: failed.marked
            ? failed.messages
            : _withRow(failed.messages, {
                id: nextId(),
                role: "assistant",
                text: msg.message,
                ...(errSeq > 0 ? { seq: errSeq } : {}),
              }),
        });
        // 错误可携带导航（如无 provider 引导到配置页），复用 pendingNav 通道
        {
          const nav = (msg as { data?: { navigate?: unknown } }).data?.navigate;
          if (typeof nav === "string" && nav) set({ pendingNav: nav });
        }
        break;
      }

      case "file": {
        // 边界守卫（与 chunk 同一把尺）：切空间后旧空间的附件不得挂到本端气泡上。
        if (!_frameInBoundary(msg, get())) break;
        // send_file → Web: attach to the active assistant bubble when possible
        // so media appears under the streaming reply instead of a blank card.
        // 只挂到「最近一条用户气泡之后」的助手气泡，避免回合中跟话后仍把
        // 附件挂到跟话上方的旧助手气泡（图出现在用户消息上面）。
        const attachment: ChatFileAttachment = {
          file_id: msg.file_id,
          url: msg.url,
          filename: msg.filename,
          mime: msg.mime,
          size: msg.size,
          caption: msg.caption || "",
          is_image: !!msg.is_image,
          is_video: !!msg.is_video,
          is_audio: !!msg.is_audio,
        };
        const msgs = [...get().messages];
        let lastUser = -1;
        for (let i = msgs.length - 1; i >= 0; i--) {
          if (msgs[i].role === "user") {
            lastUser = i;
            break;
          }
        }
        let idx = -1;
        for (let i = msgs.length - 1; i > lastUser; i--) {
          const m = msgs[i];
          if (m.role === "assistant" && (m.streaming || m.autoCreated || get().turnActive)) {
            idx = i;
            break;
          }
        }
        if (idx >= 0) {
          const target = msgs[idx];
          msgs[idx] = {
            ...target,
            files: [...(target.files || []), attachment],
          };
          set({ messages: msgs });
        } else {
          const fileSeq = _frameViewSeq(msg);
          set({
            messages: _withRow(msgs, {
              id: nextId(),
              role: "assistant",
              text: attachment.caption || "",
              files: [attachment],
              ...(fileSeq > 0 ? { seq: fileSeq } : {}),
            }),
          });
        }
        break;
      }

      case "info": {
        // 边界守卫（与 chunk 同一把尺）：他空间/他会话的提示不得落到本端视图。
        if (!_frameInBoundary(msg, get())) break;
        const infoSeq = _frameViewSeq(msg);
        set({
          messages: _withRow(get().messages, {
            id: nextId(),
            role: "assistant",
            text: msg.text,
            ...(infoSeq > 0 ? { seq: infoSeq } : {}),
          }),
        });
        break;
      }

      case "focus_window": {
        // 托盘 / CLI / 外进程唤起：已有标签只置前，可选跳转 path。
        const path = typeof msg.path === "string" ? msg.path.trim() : "";
        if (path) {
          set({ pendingNav: path.startsWith("/") ? path : `/${path}` });
        }
        try {
          window.focus();
        } catch {
          /* ignore */
        }
        break;
      }

      case "workspaces_changed":
        // Registry add/remove/rename (CLI or ws tool): refetch the list so
        // the sidebar never shows ghost entries.
        fetchWorkspaceList()
          .then((data) => get().setWorkspaces(data.workspaces, data.active_name))
          .catch((err) => console.error("Failed to refresh workspace list:", err));
        break;

      case "state": {
        const data = msg.data as {
          runtime?: RuntimeInfo;
          sessions?: unknown[];
        };
        if (data?.runtime) {
          set({ runtime: data.runtime });
          // Fallback: if backend heartbeat says root is not running but we
          // still think a turn is active, turn_end was likely lost (trace
          // flush race, WS disconnect mid-turn, etc.). Correct turnActive
          // so the input box doesn't get stuck showing "queued".
          if (data.runtime.running === false && get().turnActive) {
            set({ turnActive: false, currentTurnId: null, turnStartedAt: null });
          }
          // 反向恢复：仅当心跳说「web 自己的回合」在跑时才亮 spinner。
          // running  alone = 该会话任意端在跑（CLI/Matrix 同空间也会 true），
          // 旧逻辑会把别人的回合当成 Web 在转圈。
          const turnSrc = data.runtime.turn_source;
          if (data.runtime.running === true && isWebSource(turnSrc) && !get().turnActive) {
            set({ turnActive: true, turnStartedAt: get().turnStartedAt ?? Date.now() });
          }
          // 已误亮（例如旧心跳）：同空间非 web 回合在跑 → 灭掉 Web spinner
          if (
            get().turnActive &&
            data.runtime.running === true &&
            turnSrc != null &&
            turnSrc !== "" &&
            !isWebSource(turnSrc)
          ) {
            set({ turnActive: false, currentTurnId: null, turnStartedAt: null });
          }
          // Detect session change. When the session changes (e.g. CLI ran
          // /new or /ws switch, or the LLM switched workspace via a tool), the
          // browser is still showing the old workspace's messages/trace/turn
          // state. We distinguish two cases by workspace:
          //  - same workspace (/new): keep old messages and insert a timeline
          //    divider (对齐手机端), no clear / no re-hydrate;
          //  - different workspace: resetForWorkspaceSwitch clears all of that
          //    so the new workspace starts with a clean slate; ChatView's
          //    useEffect reloads history for the new session.
          //
          // 数据边界以 workspaceDir 为准（空间一条线）：切空间换边界、同空间 /new 不换。
          // session_id 仅作线上会话段标注（/new 换段），不再当数据容器边界。
          // 心跳每 ~2s 推一次同 sid/同 dir，只在真实边界/会话段变化时动作，
          // 否则会把本地刚加的用户消息清掉。
          const newSid = data.runtime.session_id;
          const newDir = data.runtime.workspace_dir || null;
          const curSid = get().sessionId;
          const curDir = get().workspaceDir;
          if (newSid && newSid !== curSid) {
            if (curDir === null) {
              // 首次拿到边界（启动/刷新）：初始化而非切换——只记录 sid/dir，
              // 不 reset（否则把刚 hydrate 的工具/历史清空）。内容不预填任何本地
              // 缓存，一律等 hydrate 权威给：刷新与切换同一把尺。
              set({ sessionId: newSid, workspaceDir: newDir });
            } else if (newDir && newDir !== curDir) {
              // 真实空间切换（/ws switch、他端切空间）：换边界，恢复/加载目标空间视图。
              get().resetForWorkspaceSwitch(newDir);
              // 目标空间的会话以服务端这一帧为准：缓存里的 sessionId 可能已经过期
              // （他端 /new 过），用旧值会让这个空间的帧被边界守卫全部丢掉。
              set({ sessionId: newSid });
              void get().hydrateToolActivity();
                } else {
                  // 同空间换会话：只更新会话段标注，不插线。这里的换会话多半是内核
                  // 在你发消息那一刻做的空闲自动 /new——插线就正好落在你刚发出去的
                  // 那条消息下方，读起来像「这条线因为我发了消息才出现」。分隔线只该
                  // 由显式动作产生，不由用户输入产生；显式点「新会话」那条线由按钮插。
                  // 缓存里的会话键一并跟着走，否则切走再切回会拿旧键去匹配帧。
                  const st = get();
                  const cacheDir = st.workspaceDir;
                  set({
                    sessionId: newSid,
                    ...(cacheDir && st.sessionIdByWorkspace[cacheDir] !== undefined
                      ? {
                          sessionIdByWorkspace: {
                            ...st.sessionIdByWorkspace,
                            [cacheDir]: newSid,
                          },
                        }
                      : {}),
                  });
                }
          } else if (newDir && newDir !== curDir) {
            // sid 未变但边界变了（罕见：同 session 被挂到别的空间）：按切空间处理。
            get().resetForWorkspaceSwitch(newDir);
            void get().hydrateToolActivity();
          }
          // 边界已落定（首次定界 / 切空间）：回放边界未定期间到达的内容帧。
          // 这是「订阅先于快照」的另一半——没有这一步，那批帧全被守卫丢掉。
          if (get().workspaceDir) _flushPendingFrames();
          // Keep the workspace Select in sync when switch happened outside
          // the Web UI (CLI /ws switch, LLM ws tool). Without this, chat/session
          // follows the new foreground but the dropdown stays on the old name.
          const wsName = data.runtime.workspace_name;
          if (wsName && wsName !== get().activeName) {
            const workspaces = get().workspaces.map((ws) => ({
              ...ws,
              active: ws.name === wsName,
            }));
            set({ activeName: wsName, workspaces });
          }
        }
        break;
      }

      case "pong":
        break;

      case "vault_prompt":
        // Server is asking the user to unlock the vault. Show the dedicated
        // password card. The password is sent back via vault_reply and is
        // NEVER forwarded to the LLM. A new prompt also supersedes any
        // pending auto-clear of a previous vault_result toast.
        if (vaultResultTimer) {
          clearTimeout(vaultResultTimer);
          vaultResultTimer = null;
        }
        set({
          vaultPrompt: {
            initialized: msg.initialized,
            locked: msg.locked,
            title: msg.title,
            question: msg.question,
            hint: msg.hint,
          },
        });
        break;

      case "vault_result":
        // Server replied to a vault_reply — show success/failure briefly.
        set({
          vaultResult: {
            ok: msg.ok,
            message: msg.message,
            created: msg.created,
          },
          // Dismiss the prompt card on success; keep it open on failure so
          // the user can retry.
          vaultPrompt: msg.ok ? null : get().vaultPrompt,
        });
        // Auto-clear the result toast after 4 seconds. Cancel any pending
        // clear from a previous result first so the new toast isn't wiped.
        if (vaultResultTimer) clearTimeout(vaultResultTimer);
        vaultResultTimer = setTimeout(() => {
          vaultResultTimer = null;
          if (get().vaultResult?.ok === msg.ok) {
            set({ vaultResult: null });
          }
        }, 4000);
        break;
    }
  },

  clearInteraction: () => set({ pendingInteraction: null }),
  setWorkspaces: (ws, active) => set({ workspaces: ws, activeName: active }),
  loadHistory: (messages, latestSeq, opts) => {
    const st = get();
    if (
      opts?.workspaceDir !== undefined &&
      opts.workspaceDir !== null &&
      st.workspaceDir !== opts.workspaceDir
    ) {
      return;
    }
    if (opts?.generation !== undefined && st.hydrateGeneration !== opts.generation) {
      return;
    }
    // 线身份：epoch 变了＝整条线已重建（服务端换代）——本地 messages 与折叠区都属
    // 旧线，一律作废重建。增量结果此时直接作废（调用方据 epoch 变化改走全量）。
    const epoch = typeof opts?.epoch === "string" && opts.epoch.trim() ? opts.epoch.trim() : null;
    const lineRebuilt = Boolean(epoch && st.lineEpoch !== null && epoch !== st.lineEpoch);
    if (lineRebuilt && opts?.incremental) return;
    // 单行 epoch 说明：同一 workspaceDir::subject 的世代推进才换 epoch，切空间由
    // 调用方先清边界（lineEpoch 置 null），那种情况不在此处判重建。
    // The backend returns messages in chronological storage order. Render as-is.
    // 每条已落带消息带磁带 seq——hydrate 内部对账的严格键。
    // 分隔线只由显式动作落帧：历史里的 divider 帧照常渲染（它是那条线的锚点），
    // 不存在自动产生的空档线。
    const mapped: ChatMessage[] = messages.map((m) => ({
      id: nextId(),
      role: m.role as "user" | "assistant",
      text: m.text,
      key: computeMsgKey({
        role: m.role as "user" | "assistant",
        text: m.text,
        diff: m.diff,
        dividerLabel: m.divider,
        tool: m.tool,
        attachments: (m.attachments ?? []).map((a) => ({
          file_id: String(a.ref ?? a.filename ?? ""),
          url: String(a.url ?? ""),
          filename: String(a.filename ?? a.ref ?? ""),
          mime: String(a.mime ?? ""),
          size: Number(a.size ?? 0),
          is_image: Boolean(a.is_image),
        })),
      }),
      ...(m.files && m.files.length > 0 ? { files: m.files } : {}),
      ...(m.diff ? { diff: m.diff } : {}),
      // diff 行的工具归属（落带/回放时按它归集到折叠区；主消息流按帧到达序就地落定）
      ...(m.tool_call_id ? { tool_call_id: m.tool_call_id } : {}),
      ...(m.tool ? { tool: m.tool } : {}),
      ...(m.divider ? { dividerLabel: m.divider } : {}),
      ...(m.divider_time ? { dividerTime: m.divider_time } : {}),
      ...(m.divider && typeof m.ts === "number" && m.ts > 0 ? { dividerTs: m.ts } : {}),
      ...(m.seq ? { seq: m.seq } : {}),
      ...(m.is_command_result ? { isCommandResult: true } : {}),
      // 端上标识（权威行也带着它）：hydrate 对账的首选配对键
      ...(typeof m.client_msg_id === "string" && m.client_msg_id.trim()
        ? { clientMsgId: m.client_msg_id.trim() }
        : {}),
      ...(m.attachments && m.attachments.length > 0
        ? {
            attachments: m.attachments.map((a) => ({
              file_id: String(a.ref ?? a.filename ?? ""),
              url: String(a.url ?? ""),
              filename: String(a.filename ?? a.ref ?? ""),
              mime: String(a.mime ?? ""),
              size: Number(a.size ?? 0),
              is_image: Boolean(a.is_image),
            })),
          }
        : {}),
    }));
    // 时间隔断融合（手机端同款）：同一消息行的「会话/模型切换 + 时间」合并
    // 到一条分隔线。时间取下一行的内容时间（分隔发生的位置）。
    const fused = fuseDividersWithTime(mapped);
    // hydrate 游标：这次快照覆盖到的最新已落带 seq。比它新的属实时尾部。
    const cursor = latestSeq ?? mapped.reduce((acc, m) => Math.max(acc, m.seq ?? 0), 0);
    // 线重建时本地列表整体作废：不参与配对、也不保留实时尾（它们都属旧线）。
    const current = lineRebuilt ? [] : get().messages;
    // 整表重建的唯一判据：epoch 变了（线已重建）、或本地还没有任何已显示行
    // （刷新首屏、切空间清空后第一份快照）。除这两条外一律「只追加 + 只改内容」。
    const replace = lineRebuilt || current.length === 0;
    // hydrate 是权威：每次 loadHistory 后把当前空间的会话键记进映射（切回来时
    // 第一帧就用对键）。键 = workspaceDir；内容不存——本地那份随时可能过期。
    const syncCache = () => {
          const dir = get().workspaceDir;
          if (dir) {
            const st = get();
            set({
              sessionIdByWorkspace: {
                ...st.sessionIdByWorkspace,
                [dir]: st.sessionId,
              },
            });
          }
        };
    const commit = (msgs: ChatMessage[]) => {
      const rt = opts?.runtime;
      // turnActive 口径与实时帧一致：只认 web 发起的回合（CLI/手机同空间跑不亮
      // 浏览器的圈；turn_source 为空＝不是 web 回合，同样不亮）。
      const webTurn = rt ? Boolean(rt.running) && isWebSource(rt.turn_source) : get().turnActive;
      // 非活跃回合的 hydrate 同步清活动树（对齐切空间/新会话路径）——树是回合
      // 内实时态，静止回合的权威快照不应残留上一回合的树行（防刷新后死树复活）。
      if (!webTurn) _subagentTree.clear();
      // 游标/身份推进成功：清掉「这个空洞补不动」的记忆，下一个空洞照常补。
      // 内容、runtime、线身份、折叠区（最终答复 + 子智能体工具行/diff）必须同一次
      // 落定：分次 set 会先渲染消息、再补 spinner 与会话段（先一半后补齐＝跳变）。
      const patch: Partial<AppState> = {
        // 顺序由调用方（_mergeSnapshotRows）定好：重建时按 view_seq 升序，
        // 增量对账时是「已显示原序 + 尾部追加」。这里不再做任何全表重排。
        messages: msgs,
        hydratedSeq: cursor,
        // 权威快照落地＝当前空间的内容已定：骨架态的唯一解除点。
        viewReady: true,
        skeletonSince: null,
        needsHydrate: false,
      };
      if (epoch) patch.lineEpoch = epoch;
      if (lineRebuilt) {
        // 旧线的折叠区一并作废：快照没带就是空的，不带旧线残留。
        patch.subagentOutput = _mergeSubagentResults({}, opts?.subagentResults) ?? {};
        patch.subagentDiffs = _mergeSubagentDiffsSnapshot({}, opts?.subagentDiffs) ?? {};
        patch.subagentBriefs = _mergeSubagentBriefs({}, opts?.subagentBriefs) ?? {};
      } else {
        const subagentOutput = _mergeSubagentResults(get().subagentOutput, opts?.subagentResults);
        if (subagentOutput) patch.subagentOutput = subagentOutput;
        const subagentDiffs = _mergeSubagentDiffsSnapshot(get().subagentDiffs, opts?.subagentDiffs);
        if (subagentDiffs) patch.subagentDiffs = subagentDiffs;
        const subagentBriefs = _mergeSubagentBriefs(get().subagentBriefs, opts?.subagentBriefs);
        if (subagentBriefs) patch.subagentBriefs = subagentBriefs;
      }
      if (rt) {
        patch.sessionId = rt.session_id ?? get().sessionId;
        patch.turnActive = webTurn;
        patch.currentTurnId = webTurn ? (get().currentTurnId ?? null) : null;
        patch.turnStartedAt = webTurn ? (get().turnStartedAt ?? Date.now()) : null;
      }
      if (!webTurn) patch.subagentRows = [];
      set(patch);
      // 折叠区数据随事件裁剪联动回收：快照可能把老的 delegate 行挤出窗口，那些
      // call 的产出/答复/指令/过程帧再留着也没人渲染。
      _pruneFolding(patch.messages ?? [], get().subagentRows);
      syncCache();
    };
    // 对账落地只剩这一条路（重建判据见 replace）：只追加 + 只改内容
    // —— 命中既有行就地覆盖（id 与位置都不动），命不中的追加在尾部，
    // 快照里没有的既有行原地保留。已显示行的相对顺序因此永不变化。
    commit(_mergeSnapshotRows(current, fused, replace));
  },
  addUserMessage: (text, _imageRefs, attachments, clientMsgId) => {
    // 回合进行中发送的是接续输入（排队等注入 LLM 上下文）：标 pendingInject，
    // 气泡显「待注入」徽标，注入事件到达后清除。新回合首条输入不带此标。
    const pending = get().turnActive;
    set({
      messages: _appendRow(get().messages, {
        id: nextId(),
        role: "user",
        text,
        key: computeMsgKey({ role: "user", text, attachments }),
        optimistic: true,
        // 端上标识随消息发出去，权威帧原样带回时按它精确认领（不再靠猜）
        ...(clientMsgId ? { clientMsgId } : {}),
        ...(attachments && attachments.length > 0 ? { attachments } : {}),
        ...(pending ? { pendingInject: true } : {}),
      }),
    });
  },
  clearTraceEvents: () => set({ traceEvents: [], activityEpoch: get().activityEpoch + 1 }),
  hydrateToolActivity: async () => {
    try {
      const { events } = await fetchToolActivityEvents(80);
      const mapped: TraceEventEntry[] = events.map((row: TraceEventRow) =>
        sanitizeTraceText({
          id: nextTraceId(),
          event_type: row.type,
          timestamp: row.timestamp || nowISO(),
          ...(row.turn_id !== undefined ? { turn_id: row.turn_id } : {}),
          ...(row.source !== undefined ? { source: row.source } : {}),
          ...(row.origin_scope !== undefined ? { origin_scope: row.origin_scope } : {}),
          ...(row.session_id !== undefined ? { session_id: row.session_id } : {}),
          ...(row.tool !== undefined ? { tool: row.tool } : {}),
          ...(row.args !== undefined ? { args: row.args } : {}),
          ...(row.call_id !== undefined ? { call_id: row.call_id } : {}),
          ...(row.summary !== undefined ? { summary: row.summary } : {}),
          ...(row.ok !== undefined ? { ok: row.ok } : {}),
          ...(row.reason !== undefined ? { reason: row.reason } : {}),
          ...(row.text !== undefined ? { text: row.text } : {}),
          ...(row.content !== undefined
            ? { content: row.content }
            : row.message !== undefined
              ? { content: row.message }
              : {}),
          ...(row.display_blocks !== undefined ? { display_blocks: row.display_blocks } : {}),
          ...(row.diff_lines !== undefined ? { diff_lines: row.diff_lines } : {}),
          ...(row.tool_output !== undefined ? { tool_output: row.tool_output } : {}),
          ...(row.tool_output_truncated !== undefined
            ? { tool_output_truncated: row.tool_output_truncated }
            : {}),
          ...(row.tool_output_ref !== undefined ? { tool_output_ref: row.tool_output_ref } : {}),
          ...(row.duration_ms !== undefined ? { duration_ms: row.duration_ms } : {}),
          ...(row.is_error !== undefined ? { is_error: row.is_error } : {}),
        }),
      );
      // Prefer live buffer if WS already delivered newer tool rows; otherwise seed sidebar.
      if (mapped.length === 0) return;
      const live = get().traceEvents;
      const hasLiveTools = live.some(
        (e) =>
          e.event_type === "tool_start" ||
          e.event_type === "tool_call" ||
          e.event_type === "tool_result",
      );
      if (hasLiveTools) return;
      set({ traceEvents: mapped, activityEpoch: get().activityEpoch + 1 });
    } catch (err) {
      console.error("Failed to hydrate tool activity:", err);
    } finally {
      // 无论拉到数据与否，hydrate 流程已走完——置 ready 让侧栏从加载占位
      // 切换到真实内容（列表或「暂无」），消除「暂无」先闪一下的跳变。
      if (!get().toolActivityReady) {
        set({ toolActivityReady: true });
      }
    }
  },
  resetForWorkspaceSwitch: (workspaceDir: string, snapshot?: SessionSnapshot | null) => {
    // /new、切空间换一批加载词（对齐 CLI reshuffle 时机）。
    reshufflePhrases();
    // 切走前只带走「这个空间用哪个会话键」：内容一律不缓存（本地那份随时可能过期，
    // 拿它上屏就是用旧事实顶替，hydrate 回来再换——那正是 A 跳 B）。回到目标空间
    // 时视图是空的，等 hydrate 一次到位。
    const prev = get();
    const prevDir = prev.workspaceDir;
    const cache = { ...prev.sessionIdByWorkspace };
    if (prevDir && prevDir !== workspaceDir) {
      cache[prevDir] = prev.sessionId;
    }
    const cachedSessionId = cache[workspaceDir];
    _subagentTree.clear();
    // 切空间是「换视图」，不是在当前线上插线：每个空间各自一条线，目标空间的
    // 线由 hydrate 权威给出。服务端切空间不落分隔帧，客户端自己插的那条会被
    // 下一次 hydrate 冲掉（线闪一下就没了），还会顺手污染该空间的缓存视图。
    // 分隔线只属于有落盘的两种场景：新会话、模型切换。
    set({
      workspaceDir,
      sessionIdByWorkspace: cache,
      // 空线上屏：目标空间的内容一律等 hydrate 权威给（宁可慢一拍，不铺旧料）。
      messages: [],
      hydratedSeq: 0,
      // 换边界即退回骨架态：目标空间的内容还没被权威快照落定。
      viewReady: false,
      skeletonSince: Date.now(),
      // 在飞 hydrate 会被下面的 generation 递增丢弃：记一笔，让 ChatView 补一次
      //（带快照时不必要——这份快照就是权威提交）。
      needsHydrate: !snapshot,
      // 线身份与折叠区一律不继承上一个空间：epoch 属旧线，折叠帧按旧线的
      // call_id 归集，留着就是跨空间串。
      lineEpoch: null,
      hydrateGeneration: prev.hydrateGeneration + 1,
      traceEvents: [],
      subagentRows: [],
      subagentOutput: {},
      subagentDiffs: {},
      subagentBriefs: {},
      activityEpoch: prev.activityEpoch + 1,
      // 回合态一律先置空闲，由权威（hydrate 的 runtime / 实时帧）说了算。
      turnActive: false,
      currentTurnId: null,
      turnStartedAt: null,
      // 缺就置空，绝不继承上一个空间的会话键——那是跨空间串。切空间响应带快照时
      // 以快照的会话键为准（它才是目标空间此刻的真实会话）。
      sessionId: snapshot?.session_id ?? cachedSessionId ?? null,
      pendingInteraction: null,
      // 模块主体（配置助手等）独立于前台会话与工作空间，
      // 不随 /new 或工作空间切换清空（web_server: 模块主体不绑前台会话）。
    });
    // 切空间响应自带目标空间快照：就地走 loadHistory 那一次提交（内容 + runtime +
    // 线身份 + 折叠区一批落定），不必再发 /api/session/messages——「一次切换只允许
    // 出现两种画面：骨架 → 目标内容」靠的就是这个。没有快照（旧服务端/异常）时
    // 退回原流程：清空边界，等 ChatView 的 hydrate 接手。
    if (snapshot && Array.isArray(snapshot.messages)) {
      // 切空间响应自带目标空间快照：就地走 loadHistory 那一次提交（内容 + runtime +
      // 线身份 + 折叠区一批落定），不必再发 /api/session/messages——「一次切换只允许
      // 出现两种画面：骨架 → 目标内容」靠的就是这个。没有快照（旧服务端/异常）时
      // 退回原流程：清空边界 + needsHydrate 交给 ChatView 补一次 hydrate。
      get().loadHistory(snapshot.messages, snapshot.latest_seq, {
        workspaceDir,
        incremental: false,
        epoch: snapshot.epoch,
        subagentResults: snapshot.subagent_results,
        subagentDiffs: snapshot.subagent_diffs,
        subagentBriefs: snapshot.subagent_briefs,
        ...(snapshot.runtime ? { runtime: snapshot.runtime } : {}),
      });
    }
    // lastWorkspace 不再需要落 localStorage：视图边界与内容一律来自服务端。
  },

  resetForNewSession: (sessionId, label = "新会话") => {
    reshufflePhrases();
    // 新会话是本端看得见的动作，但「上下文被清空」这件事看不见——线上多一条带
    // 时间的线给它做锚点。这是 web 端唯一出线的触发：切空间、发消息、模型切换、
    // 时间空档都不出线。
    const divider: ChatMessage = {
      id: nextId(),
      role: "assistant",
      text: "",
      dividerLabel: label,
      dividerTime: formatTimelineTimeLabel(Date.now()),
      key: computeMsgKey({ role: "assistant", text: "", dividerLabel: label }),
    };
    const prevRows = get().messages;
    const lastRow = prevRows[prevRows.length - 1];
    // 连续 /new：就地改最后那条分隔线的标签与时间（只改内容、不删行、不换 id），
    // 否则追加一条新线——两条紧贴的分隔线没有信息量（原 mergeAdjacentDividers 的
    // 语义用「改内容」实现，因为删已显示行会移动它后面的行）。
    const next =
      lastRow?.dividerLabel !== undefined
        ? [
            ...prevRows.slice(0, -1),
            {
              ...lastRow,
              dividerLabel: label,
              dividerTime: divider.dividerTime,
              key: computeMsgKey({ role: "assistant", text: "", dividerLabel: label }),
            },
          ]
        : _appendRow(prevRows, divider);
    // 同空间新会话：不切缓存不重建视图（同一 workspaceDir 同一条线），旧对话保留
    // 在消息流里，插一条分隔线 + 更新会话段标注。后续新消息实时追加；hydrate
    // 尾部对账续上（含服务端下发的分隔线帧）。
    const dir = get().workspaceDir;
    _subagentTree.clear();
    set({
      sessionId,
      ...(dir
        ? { sessionIdByWorkspace: { ...get().sessionIdByWorkspace, [dir]: sessionId } }
        : {}),
      messages: next,
      hydrateGeneration: get().hydrateGeneration + 1,
      traceEvents: [],
      subagentRows: [],
      subagentOutput: {},
      subagentDiffs: {},
      subagentBriefs: {},
      activityEpoch: get().activityEpoch + 1,
      // 换会话段＝换边界：内容还没被新边界的权威快照落定，退回骨架态。
      // 游标与已显示内容都留着：/new 是同一条线上的分段，已显示行一律不动。
      viewReady: false,
      skeletonSince: Date.now(),
      // 在飞 hydrate 被 generation 递增丢弃：同边界不会自动重拉，补一次（E）。
      needsHydrate: true,
      turnActive: false,
      currentTurnId: null,
      // 回合起始时间随回合态一起清：漏了它，新会话的第一个回合按上一个回合的
      // 起点算已用时间（spinner 一上来就显示几分钟）。
      turnStartedAt: null,
      pendingInteraction: null,
    });
    // 缓存键是 workspaceDir，同空间 /new 不换键——lastWorkspace 无需更新。
  },

  applyLocalModelSwitch: (provider, model, _deferred = false) => {
    const key = provider && model ? `${provider}·${model}` : model || provider;
    if (!key) return;
    // 模型切换一律不插线（本端与他端同口径）：顶栏与状态栏就写着当前模型，
    // llm_switched 事件只刷新 runtime，不产生分隔线。
    const rt = get().runtime;
    set({
      runtime: rt
        ? {
            ...rt,
            ...(provider ? { provider } : {}),
            ...(model ? { model } : {}),
          }
        : rt,
    });
  },

  sanitizeResidentMessages: () => {
    const cur = get().messages;
    // 只剥确实存在的瞬态标记，避免无变化时触发多余 set（引用不变 → 不重渲染）。
    // sendFailed 不剥：失败态是终态（非瞬态），剥掉会让失败样式在整理后丢失。
    let touched = false;
    const cleaned = cur.map((m) => {
      if (!(m.streaming || m.queued || m.optimistic || m.pendingInject || m.autoCreated)) {
        return m;
      }
      touched = true;
      const { streaming: _s, queued: _q, optimistic: _o, pendingInject: _p, autoCreated: _a, ...rest } = m;
      return rest as ChatMessage;
    });
    if (touched) {
      set({ messages: cleaned });
    }
  },
}));
