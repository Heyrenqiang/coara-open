// Zustand: chat / runtime / traces.

import { create } from "zustand";
import { tokenQueryFragment } from "./auth";
import {
  fetchSessionMessages,
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
  FlowGraphSnapshot,
  FlowLiveNode,
  ServerMessage,
} from "./ws";

/** Web chat shows source=web turns only; inject-mirror chunks auto-bubble. Trace keeps all. */

/** 工具行：服务端 label，前端不二次拼装。 */
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
  /** diff 配对 tool_call_id；空＝老数据按原序。 */
  tool_call_id?: string;
  /** 帧的子智能体归属（编排节点分组用）。 */
  subagent_id?: string;
  coara_id?: string;
  turn_id?: string;
  streaming?: boolean;
  /** chunk 早于 turn_start 建泡；turn_* 认领。 */
  autoCreated?: boolean;
  /** 斜杠命令卡。 */
  isCommandResult?: boolean;
  /** 分隔线标签（非命令卡）。 */
  dividerLabel?: string;
  /** 分隔线时间标签。 */
  dividerTime?: string;
  /** 分隔帧落盘秒（hydrate）。 */
  dividerTs?: number;
  /** 空档时间线。 */
  dividerTimeOnly?: boolean;
  /** send_file 出站附件。 */
  files?: ChatFileAttachment[];
  /** 用户上传附件。 */
  attachments?: ChatFileAttachment[];
  /** 排队占位。 */
  queued?: boolean;
  /** 乐观泡：权威 user_message 认领。 */
  optimistic?: boolean;
  /** client_msg_id：认领首选。 */
  clientMsgId?: string;
  /** 跟话待注入；continuation 清徽标。 */
  pendingInject?: boolean;
  /** 独立 diff 块。 */
  diff?: import("./ws").CanonicalDiffLines;
  /** 内联工具行。 */
  tool?: ChatToolLine;
  /** 磁带 seq：hydrate 对账键。 */
  seq?: number;
  /** turn_end 归属；旧 tid 不得清新回合。 */
  source?: string;
  /** computeMsgKey：reconcile 原位配对。 */
  key?: string;
  /** 发送失败（无认领帧）。 */
  sendFailed?: boolean;
  /** retracted：隐藏不删（前缀冻结）。 */
  retracted?: boolean;
}

/** 一个模块级独立会话的消息与 turn 状态（按 subject 键控）。 */
interface ModuleSessionState {
  messages: ChatMessage[];
  turnActive: boolean;
  currentTurnId: string | null;
}

/** 内存态 agentic 工作流图（FlowCoordinator），工作台画布与 live WDL 同步用。 */
interface FlowLiveGraph {
  name: string;
  hops: number;
  nodes: Record<string, FlowLiveNode>;
  edges: { from: string; to: string; on?: string }[];
  /** True 表示该图的 flow_graph_snapshot 应答已到达（含 null 快照——
   *  图不在内存中）。 */
  loaded: boolean;
  updatedAt: string;
  /** FlowCoordinator 最新 canonical WDL（编辑器实时同步）。 */
  wdl?: string;
}

/** flow 图的 store 键：按 subject 分域（主会话图 vs flow 工作台图）。 */
export function flowGraphKey(flow: string, subject?: string): string {
  return subject === "flow" ? `flow:${flow}` : flow;
}

/** Outbound file pushed from the runtime to the browser chat. */
export interface ChatFileAttachment {
  file_id: string;
  url: string;
  /** 出站文件的原绝对路径；有它才进 /file，uuid 不能当 uploads 相对名 */
  path?: string;
  filename: string;
  mime: string;
  size: number;
  caption?: string;
  is_image?: boolean;
  is_video?: boolean;
  is_audio?: boolean;
}

interface PendingInteraction {
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
  /** 内核前台空间（进程绑定）：不能移出登记，侧边栏不出移出按钮 */
  foreground?: boolean;
  /** 门面形态：warehouse=对话主页 / display=展示主页 / storefront=营业主页 */
  storefront?: string;
  /** 展示/营业主页的路由（如 /usage）；仓库空间为空 = 对话页 */
  home_view?: string;
  /** 空间种类：managed=用户对话空间 / internal=系统空间（配置/消息/记录/用量等） */
  kind?: string;
  /** 目录已被删除（仅用户对话空间会标记）：侧边栏置灰，点击走恢复流程 */
  missing?: boolean;
}

export interface RuntimeInfo {
  session_id: string;
  status: string;
  provider: string;
  model: string;
  running: boolean;
  /** Active turn launch source on the viewed session (web/cli/matrix/…). */
  turn_source?: string;
  /** Active turn start wall-clock (epoch seconds); spinner resumes from this after refresh. */
  turn_started_at?: number;
  /** Current foreground workspace name (kept in sync on CLI/LLM /ws switch). */
  workspace_name?: string;
  workspace_dir?: string;
  /** Provider-reported context fill (same as CLI bottom toolbar). */
  context_used_tokens?: number;
  context_window_tokens?: number;
  /** True when used tokens are baseline/compact estimate (UI shows ~). */
  context_estimated?: boolean;
  /** Session cumulative cache hit ratio 0..1, or null if unknown. */
  context_cache_hit_ratio?: number | null;
  /** Session cumulative cost in CNY (0 until first priced turn). */
  session_cost?: number;
  /** Background task count (subagents + bash bg + bg aides); idle spinner shows bg animation when >0. */
  background_tasks?: number;
  /** 在跑子智能体的展示行（服务端权威快照）：刷新/重连后据此幂等重建活动树行，
   *  否则「回合已结束、子智能体还在跑」窗口里 delegate 行首圆点静止。 */
  active_delegations?: Array<{
    task_id?: string;
    subagent_type?: string;
    description?: string;
    parent_tool_call_id?: string;
    child_coara_id?: string;
    background?: boolean;
    started_at?: number;
  }>;
  /** Session cumulative prompt/output tokens. */
  session_prompt_tokens?: number;
  session_output_tokens?: number;
}

/** Trace event entry for StatusSidebar tool activity (+ turn markers). */
interface TraceEventEntry {
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
  /** 向前翻页窗口：头部之前是否可能还有更早的历史（初始 false；loadHistory 的
   *  commit 按「快照满页」置位，loadEarlier 按「返回不足一页」清零）。 */
  hasMoreHistory: boolean;
  /** 当前已加载的最小 view_seq（0＝未知）；prependHistory 后更新为 prepend 后最小值，
   *  loadEarlier 以它为向前翻页游标。 */
  earliestSeq: number;
  /** 「加载更早消息」请求在飞：MessageList 用它防重复点击并显示加载态。 */
  earlierLoading: boolean;
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

  // Navigation — set by server-pushed navigation messages and consumed by an
  pendingNav: string | null;
  consumeNav: () => void;

  // 消息中心角标：跨空间高显著未读数（AppLayout 轮询 summary 刷新）
  updatesPending: number;
  setUpdatesPending: (n: number) => void;

  // 编排写穿：draft_id → 递增计数（workflow_draft_updated 驱动编辑器/列表刷新）
  workflowDraftRevisions: Record<string, number>;

  // providers 配置写穿：providers_changed 驱动配置页重拉（显示与实际配置同步）
  providersRevision: number;
  // 工作空间登记写穿：workspaces_changed 驱动配置页工作空间表重拉
  workspacesRevision: number;

  // 过的图（视图卸载时 drop），内存有界。
  flowGraphs: Record<string, FlowLiveGraph>;
  /** 用全量快照替换某张图（图不存在时登记空图）。 */
  applyFlowSnapshot: (flow: string, snapshot: FlowGraphSnapshot | null) => void;
  /** 增量 trace 事件（flow_graph_changed / subagent_*）套用到在看的图；
   *  未在看的图忽略（subject=flow 的工作台图除外，首个增量即建空图）。 */
  applyFlowTraceEvent: (msg: ServerMessage) => void;
  /** 卸载一张在看的图（视图 unmount）。 */
  dropFlowGraph: (flow: string) => void;

  moduleSessions: Record<string, ModuleSessionState>;
  ensureModuleSession: (subject: string) => ModuleSessionState;
  loadModuleHistory: (subject: string, messages: { role: string; text: string }[]) => void;
  addModuleUserMessage: (subject: string, text: string, attachments?: ChatFileAttachment[]) => void;

  // Actions
  handleServerMessage: (msg: ServerMessage) => void;
  clearInteraction: () => void;
  setWorkspaces: (ws: WorkspaceInfo[], active: string | null) => void;
  loadHistory: (
    messages: SnapshotMessageRow[],
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
      /** 快照同源带回的子智能体过程正文（call_id → 拼接文本）：并入 subagentOutput.text。 */
      subagentTexts?: Record<string, string>;
      /** 快照同源带回的线身份：与本地不同＝线重建，本地内容整体作废。 */
      epoch?: string;
      /** 截断影响）。判「头部之前还有更早历史」用它，别用返回行数——返回行数 跟当次请求的 limit 走，切空间快照的窗口（100）比翻页页大小（200）小， */
      total?: number;
    },
  ) => void;
  /** 向前翻页：把更早的历史行 prepend 到头部（按 seq 去重）。不动 hydratedSeq /
   *  viewReady / runtime——向前翻页不影响尾部游标与骨架态。 */
  prependHistory: (messages: SnapshotMessageRow[], opts?: { workspaceDir?: string | null }) => void;
  /** 带 workspaceDir 与 hydrateGeneration 守卫（与 loadHistory 同款，防切空间竞态）； 加载期间 earlierLoading 为 true（防重复点击）；返回行数不足一页 → 到 */
  loadEarlier: () => Promise<void>;
  addUserMessage: (text: string, imageRefs?: string[], attachments?: ChatFileAttachment[], clientMsgId?: string) => void;
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

/** 时间线标签：今天/昨天/M月d日；当天也带「今天」防跨天歧义。 */
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

/** 向前翻页一页的行数：与服务端快照窗口同一把尺（loadEarlier 每次取一页，
 *  「返回不足一页」＝历史到头的判据；hydrate 满页 ⇒ 头部之前还有更早历史）。 */
export const HISTORY_PAGE_LIMIT = 200;

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

/** diff 行的轻量身份：路径 / 增删计数 / 首 hunk 前 512 字符。
 *  原先这里是 `JSON.stringify(diff)`——大 diff 每次落定都要序列化整份文本，
 *  在「每帧一行」的热路径上是不必要的分配与 GC 压力。 */
function diffIdentity(diff: import("./ws").CanonicalDiffLines): string {
  const hunks = Array.isArray(diff.hunks) ? (diff.hunks as unknown[]) : [];
  // 规范形态是「hunk 的数组的数组」；非规范/未来变体按平铺处理，照样给得出身份
  const head = hunks.length > 0 && Array.isArray(hunks[0]) ? (hunks[0] as unknown[]) : hunks;
  let sample = "";
  for (const entry of head) {
    if (sample.length >= 512) break;
    const line = entry as { kind?: unknown; oldNum?: unknown; newNum?: unknown; code?: unknown };
    if (line && typeof line === "object" && typeof line.code === "string") {
      sample += `${String(line.kind)}${String(line.oldNum)},${String(line.newNum)}:${line.code}`;
    } else {
      // 兜底：只序列化这一条（有界），不序列化整份 diff
      sample += boundedJson(line).slice(0, 128);
    }
  }
  return `${diff.path}|${diff.added}|${diff.removed}|${hunks.length}|${sample.slice(0, 512)}`;
}

function boundedJson(value: unknown): string {
  try {
    const s = JSON.stringify(value);
    return typeof s === "string" ? s : "";
  } catch {
    return "";
  }
}

/** 素材规则（双侧严格同源）： - 分隔线 `d|<label>`：同 label 即同一逻辑实体（融合的多对一塌缩天然吸收） - diff 块 `f|c:<tool_call_id>`（无 id 时退化为轻量结构哈希） - 用户气泡 `u|<trim 文本>|<附件 */
function computeMsgKey(m: {
  role: "user" | "assistant";
  text: string;
  diff?: import("./ws").CanonicalDiffLines;
  /** 产出这条 diff 的工具调用 id（身份首选：同一次调用唯一，双侧同源） */
  tool_call_id?: string;
  dividerLabel?: string;
  attachments?: ChatFileAttachment[];
  tool?: ChatToolLine;
}): string {
  let raw: string;
  if (m.dividerLabel) {
    raw = `d|${m.dividerLabel}`;
  } else if (m.diff) {
    raw = `f|${m.tool_call_id ? `c:${m.tool_call_id}` : diffIdentity(m.diff)}`;
  } else if (m.tool) {
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
function nextId(): string {
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? `msg-${crypto.randomUUID()}`
    : `msg-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** 内容行 `s<view_seq>`、工具行 `t<tool_call_id>`、diff 行 `d<tool_call_id>`、 已带端上标识的用户行 `c<client_msg_id>`；本地 uuid（`l<id> */
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

const MAX_TRACE_EVENTS = 200;

/** 内存上限；须 > 快照窗(200)+一回合实时尾，否则 hydrate 会把裁掉行复活到尾部。 */
const MAX_MESSAGES = 1000;

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

/** web 端是否显示某个 assistant 输出（文本气泡 / 工具调用 / 回合生命周期）的 */
export function isWebSource(source: string | undefined): boolean {
  return source === "web";
}

/** 空间（视图切走后仍在跑的回合输出）——不属于当前空间边界的帧必须丢弃， 否则旧空间回合输出投到已切走视图的浏览器（跨空间串话）。 */
function _isFrameInWorkspace(msg: ServerMessage, workspaceDir: string | null): boolean {
  const frameDir = (msg as { workspace_dir?: unknown }).workspace_dir;
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

/** 那个空间的会话键映射：否则那边 /new 过以后，切过去时端侧还拿旧键去匹配帧， 那一侧的帧全被判成「别的会话」丢掉（界面上就是输出与 spinner 一起消失）。 只记会话键；回合态一律由权威给（端侧既不缓存、也不从缓 */
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

/** 无 user_message 认领帧时调用，防乐观泡永久 optimistic 残留）。返回是否 有气泡被标记 */
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
    // 越过「本次发送可能已产生的痕迹」继续向上找；遇到已落定的用户/助手 正文气泡即停止（更早的乐观泡属于历史回合，不应被本次失败波及）
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

/** 行一旦上屏，位置就冻结——后续任何落定只允许 ① 尾部追加新行、② 就地改某行内容； 绝不重排、绝不跨位搬移已显示行。整表重排只允许发生在「整条线重建」那一次 （刷新全量、切空间、epoch 变化），且重建后的顺序必须等 */

/** 列表里是否已经有这个序号的行（存在性判据，替代旧的游标闸门）：
 *  有 ⇒ 这一帧已经上屏过（实时 / 重连回放 / 快照叠加三路），整帧丢弃；没有 ⇒ 放行。
 *  判据只看「行在不在」，不依赖任何游标——因此不存在「假空洞」与补齐往返。 */
function _hasRowWithSeq(list: ChatMessage[], seq: number): boolean {
  return _seqSetOf(list).has(seq);
}

/** 序号集合缓存：键＝列表引用（store 每次变更都换引用），因此不会读到过期集合。
 *  一次 O(n) 建集后，同引用下的判重全是 O(1)——「列表里有没有这一行」是每帧都要
 *  问的问题（_appendRow 与帧裁量），原先每次全表扫是热路径上的固定开销。 */
let _seqIndex: { list: ChatMessage[]; seqs: Set<number> } | null = null;

function _seqSetOf(list: ChatMessage[]): Set<number> {
  if (_seqIndex && _seqIndex.list === list) return _seqIndex.seqs;
  const seqs = new Set<number>();
  for (const m of list) {
    if (m.seq !== undefined) seqs.add(m.seq);
  }
  _seqIndex = { list, seqs };
  return seqs;
}

/** 追加后把新序号并进缓存（只有「没裁掉旧行」时才能增量续接，否则整集作废重算）。 */
function _seqIndexExtend(
  prevList: ChatMessage[],
  nextList: ChatMessage[],
  seq: number | undefined,
): void {
  if (seq === undefined || nextList.length !== prevList.length + 1) {
    _seqIndex = null;
    return;
  }
  const seqs = _seqSetOf(prevList);
  seqs.add(seq);
  _seqIndex = { list: nextList, seqs };
}

/** 裁剪到 MAX_MESSAGES（丢最旧；其余行的相对顺序与 id 一律不动）。 */
function _capMessages(list: ChatMessage[]): ChatMessage[] {
  if (list.length <= MAX_MESSAGES) return list;
  return list.slice(list.length - MAX_MESSAGES);
}

/** - **服务端键重复**（seq / clientMsgId / tool_call_id，即 ``chatRowKey`` 的非 ``l<id>`` 分支）：同一条线上多份 = 上屏通道出了第二份真相。console. */
const _dupReported = new Set<string>();

/** 重复行自检的采样：原先每次落定都全表建两个 Map（含逐行 chatRowKey 字符串），
 *  是每帧固定开销；重复气泡是慢病——开发构建每次都查，生产构建每 N 次落定查一次。 */
const DUP_CHECK_SAMPLE = 32;
const _DUP_CHECK_EVERY = import.meta.env?.DEV === false ? DUP_CHECK_SAMPLE : 1;
let _dupCheckTick = 0;

function _assertNoDuplicateRows(list: ChatMessage[]): void {
  if (_DUP_CHECK_EVERY > 1 && ++_dupCheckTick % _DUP_CHECK_EVERY !== 0) return;
  const byRowKey = new Map<string, number>();
  for (const m of list) {
    const rk = chatRowKey(m);
    byRowKey.set(rk, (byRowKey.get(rk) ?? 0) + 1);
  }
  const byContentKey = new Map<string, number>();
  for (const m of list) {
    if (m.retracted) continue;
    const k = m.key ?? computeMsgKey(m);
    byContentKey.set(k, (byContentKey.get(k) ?? 0) + 1);
  }
  const dupRowKeys = [...byRowKey.entries()].filter(([, n]) => n > 1);
  const dupContentKeys = [...byContentKey.entries()].filter(([, n]) => n > 1);
  if (dupRowKeys.length === 0 && dupContentKeys.length === 0) return;
  const signature = `${dupRowKeys.map(([k, n]) => `${k}x${n}`).join(",")}#${dupContentKeys
    .map(([k, n]) => `${k}x${n}`)
    .join(",")}`;
  if (dupRowKeys.length === 0) {
    // 纯内容键重复：合法同文，不上报（见上方两级判据）。指纹**不含重复计数**
    const contentSig = dupContentKeys.map(([k]) => k).join(",");
    if (_dupReported.has(contentSig)) return;
    _dupReported.add(contentSig);
    console.debug(`[dup-rows] duplicate content keys (allowed): ${contentSig}`);
    return;
  }
  console.error(`[dup-rows] duplicate bubbles on screen: ${signature}`);
  if (_dupReported.has(signature)) return;
  _dupReported.add(signature);
  const st = useStore.getState();
  const summarize = (m: ChatMessage) =>
    `${chatRowKey(m)}|${(m.key ?? computeMsgKey(m)).slice(0, 10)}|${(m.text ?? "").slice(0, 24)}`;
  const window = list.slice(-12).map(summarize);
  void fetch(`/api/client-error?${tokenQueryFragment()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      event: "duplicate_bubble",
      message: `同屏出现重复气泡 rowKeys=${dupRowKeys.length} contentKeys=${dupContentKeys.length}`,
      session_id: st.sessionId ?? "",
      metadata: {
        workspace_dir: st.workspaceDir ?? "",
        view_ready: st.viewReady,
        dup_row_keys: dupRowKeys.map(([k, n]) => `${k}x${n}`),
        dup_content_keys: dupContentKeys.map(([k, n]) => `${k}x${n}`),
        tail_window: window,
      },
    }),
  }).catch(() => undefined);
}

/** 重建顺序：按 view_seq 升序（无序号行按稳定序留在末尾）。只给整线重建用。 */
function _sortBySeqAsc(list: ChatMessage[]): ChatMessage[] {
  if (list.length < 2) return list;
  return [...list].sort(
    (a, b) => (a.seq ?? Number.MAX_SAFE_INTEGER) - (b.seq ?? Number.MAX_SAFE_INTEGER),
  );
}

/** 权威快照（/api/session/messages）的一行消息：loadHistory 与 prependHistory
 *  共用的入参形状（原 loadHistory 内联类型抽出，避免两处漂移）。 */
interface SnapshotMessageRow {
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
}

/** 快照行 → ChatMessage 的映射（原 loadHistory 内联 map 原样抽出）：loadHistory 的
 *  mapped 与 prependHistory 共用同一个构造函数，两条路径的字段口径不会漂移。
 *  不做分隔线融合（fuseDividersWithTime）——融合是跨行操作，由调用方对整批行做。 */
function _mapSnapshotRows(messages: SnapshotMessageRow[]): ChatMessage[] {
  return messages.map((m) => ({
    id: nextId(),
    role: m.role as "user" | "assistant",
    text: m.text,
    key: computeMsgKey({
      role: m.role as "user" | "assistant",
      text: m.text,
      diff: m.diff,
      tool_call_id: m.tool_call_id,
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
}

/** 向前翻页 prepend：同 seq 不重复；超限丢最旧。 */
function _prependSnapshotRows(current: ChatMessage[], rows: ChatMessage[]): ChatMessage[] {
  if (rows.length === 0) return current;
  const existing = new Set<number>();
  for (const m of current) {
    if (m.seq !== undefined) existing.add(m.seq);
  }
  const fresh = rows.filter((r) => r.seq === undefined || !existing.has(r.seq));
  if (fresh.length === 0) return current;
  const merged = _capMessages([...fresh, ...current]);
  _assertFrozenPrefixInvariant(current, merged, "prependSnapshotRows");
  _assertNoDuplicateRows(merged);
  return merged;
}

/** 追加落定（内容行唯一入口）：同 seq 已上屏 → 幂等丢弃；否则**追加在尾部**。
 *  返回原引用＝这一帧没有产生新行（顺序与引用都不动，避免无谓重渲染）。 */
function _appendRow(list: ChatMessage[], row: ChatMessage): ChatMessage[] {
  if (row.seq !== undefined && _hasRowWithSeq(list, row.seq)) return list;
  const next = _capMessages([...list, row]);
  _seqIndexExtend(list, next, row.seq);
  _assertNoDuplicateRows(next);
  return next;
}

/** - 命中既有行（seq → clientMsgId → 内容 key，一对一枚举）→ 就地覆盖内容并回填 序号，**id 与位置都不动**（React key 取服务端键，因此既不重挂也不跳位） - 未命中 → 追加在尾 */
/** 配对——空文本与极短文本（`a|`、queued/turn_start 占位/未分类帧、以及 「在的」「好的」这类同文短回复）的身份不可靠，内容 key 相同但其实是不同帧， 撞上列表里任意另一条同文行就误配，真正的落带 */
const _BYKEY_MIN_ASSISTANT_TEXT_LEN = 16;

/** byKey 配对的就近窗口：候选本地行与快照行的**相对位置**距离不超过 ±N。
 *  防止把很靠前的同文长回复（同一 session 里反复出现的同一段正文）错配到
 *  快照尾部的新落带帧上。位置尺度见 ``_pickNearestByKey``。 */
const _BYKEY_NEAR_WINDOW = 50;

/** 该行是否允许参与 byKey 配对（assistant 空文本 / 短文本（trim 后 ≤16 字符）
 *  禁配；其余行照常）。这些行的身份不可靠——queued 占位、turn_start 前的空泡、
 *  未分类帧、反复出现的同文短回复都可能撞 key 必错配；靠 seq / clientMsgId 对账。 */
function _byKeyEligible(m: ChatMessage): boolean {
  if (m.role === "assistant" && !m.diff && !m.tool && !m.dividerLabel) {
    return m.text.trim().length > _BYKEY_MIN_ASSISTANT_TEXT_LEN;
  }
  return true;
}

/** ``_BYKEY_NEAR_WINDOW`` 内的候选（都不在窗口内＝返回 undefined，按孤儿追加）。 位置尺度必须**两侧可比**：两侧都有线上序号时比 seq；否则比「距各自列表尾部的 行数」（``len - */
function _pickNearestByKey(
  bucket: number[],
  used: Set<number>,
  candidates: ChatMessage[],
  target: ChatMessage,
  targetIdx: number,
  targetLen: number,
): number | undefined {
  let best: number | undefined;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const i of bucket) {
    if (used.has(i)) continue;
    const cand = candidates[i];
    const bySeq = cand.seq !== undefined && target.seq !== undefined;
    const a = bySeq ? (cand.seq as number) : candidates.length - 1 - i;
    const b = bySeq ? (target.seq as number) : targetLen - 1 - targetIdx;
    const dist = Math.abs(a - b);
    if (dist <= _BYKEY_NEAR_WINDOW && dist < bestDist) {
      best = i;
      bestDist = dist;
    }
  }
  return best;
}

/** 判据是身份集合求差，不是数量对比：空行在 byKey 里被禁配，但它们仍会经 seq/clientMsgId 命中，也会合法地经孤儿路径**新增** */
function _assertEmptyAssistantRowCountStable(
  before: ChatMessage[],
  after: ChatMessage[],
  caller: string,
): void {
  const isEmptyAssistantRow = (m: ChatMessage) =>
    m.role === "assistant" &&
    !m.diff &&
    !m.tool &&
    !m.dividerLabel &&
    !m.retracted &&
    m.text.trim().length === 0;
  const identity = (m: ChatMessage) => (m.seq !== undefined ? `seq:${m.seq}` : `id:${m.id}`);
  const beforeKeys = new Set(before.filter(isEmptyAssistantRow).map(identity));
  if (beforeKeys.size === 0) return;
  if (before.length > MAX_MESSAGES) return;
  const afterCounts = new Map<string, number>();
  for (const m of after) {
    if (!isEmptyAssistantRow(m)) continue;
    const k = identity(m);
    afterCounts.set(k, (afterCounts.get(k) ?? 0) + 1);
  }
  const missing = [...beforeKeys].filter((k) => !afterCounts.has(k));
  const copied = [...beforeKeys].filter((k) => (afterCounts.get(k) ?? 0) > 1);
  if (missing.length === 0 && copied.length === 0) return;
  console.error(
    `[empty-rows] ${caller}: 已显示空行被吞=${missing.length} 被复制=${copied.length} ` +
      `(${missing.join(",")}|${copied.join(",")}；查 seq/clientMsgId 命中是否漏配)`,
  );
}

function _mergeSnapshotRows(
  current: ChatMessage[],
  rows: ChatMessage[],
  replace: boolean,
): ChatMessage[] {
  if (replace || current.length === 0) {
    const base = _sortBySeqAsc(rows);
    if (current.length === 0) return _capMessages(base);
    // 权威重建 + live 行接管：快照行是基底（顺序权威），本地 live 行按
    const bySeq = new Map<number, number>();
    const byClientId = new Map<string, number>();
    const byKey = new Map<string, number[]>();
    base.forEach((m, i) => {
      if (m.seq !== undefined) bySeq.set(m.seq, i);
      if (m.clientMsgId) byClientId.set(m.clientMsgId, i);
      if (!_byKeyEligible(m)) return;
      const k = m.key ?? computeMsgKey(m);
      const bucket = byKey.get(k);
      if (bucket) bucket.push(i);
      else byKey.set(k, [i]);
    });
    const usedBase = new Set<number>();
    const orphans: ChatMessage[] = [];
    const out = base.slice();
    current.forEach((m, curIdx) => {
      let hit: number | undefined;
      if (m.seq !== undefined) hit = bySeq.get(m.seq);
      if (hit === undefined && m.clientMsgId) hit = byClientId.get(m.clientMsgId);
      if (hit === undefined && _byKeyEligible(m)) {
        hit = _pickNearestByKey(
          byKey.get(m.key ?? computeMsgKey(m)) ?? [],
          usedBase,
          base,
          m,
          curIdx,
          current.length,
        );
      }
      if (hit === undefined) {
        orphans.push(m);
        return;
      }
      usedBase.add(hit);
      out[hit] = {
        ...out[hit],
        id: m.id,
        // 基底行缺的回合绑定/端上标识从 live 行继承（与 merge 路径同一兜底）
        ...(out[hit].turn_id === undefined && m.turn_id !== undefined ? { turn_id: m.turn_id } : {}),
        ...(out[hit].clientMsgId === undefined && m.clientMsgId !== undefined
          ? { clientMsgId: m.clientMsgId }
          : {}),
      };
    });
    //（契约允许的整线重建那一次），插入归位不违反已显示前缀不变式。
    const withSeq = orphans.filter((m) => m.seq !== undefined);
    const noSeq = orphans.filter((m) => m.seq === undefined);
    for (const m of withSeq) {
      let lo = 0;
      let hi = out.length;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        const s = out[mid].seq;
        if (s !== undefined && s < (m.seq as number)) lo = mid + 1;
        else hi = mid;
      }
      out.splice(lo, 0, m);
    }
    const rebuilt = _capMessages([...out, ...noSeq]);
    _assertNoDuplicateRows(rebuilt);
    _assertEmptyAssistantRowCountStable(current, rebuilt, "mergeSnapshotRows:replace");
    return rebuilt;
  }
  // 水位地板：当前已显示行的最小 seq
  let seqFloor = Number.POSITIVE_INFINITY;
  for (const m of current) {
    if (m.seq !== undefined && m.seq < seqFloor) seqFloor = m.seq;
  }
  if (!Number.isFinite(seqFloor)) seqFloor = -1;
  const bySeq = new Map<number, number>();
  const byClientId = new Map<string, number>();
  const byKey = new Map<string, number[]>();
  current.forEach((m, i) => {
    if (m.seq !== undefined) bySeq.set(m.seq, i);
    if (m.clientMsgId) byClientId.set(m.clientMsgId, i);
    if (!_byKeyEligible(m)) return;
    const k = m.key ?? computeMsgKey(m);
    const bucket = byKey.get(k);
    if (bucket) bucket.push(i);
    else byKey.set(k, [i]);
  });
  const used = new Set<number>();
  const out = current.slice();
  rows.forEach((row, rowIdx) => {
    if (row.seq !== undefined && row.seq < seqFloor) return;
    let hit: number | undefined;
    if (row.seq !== undefined) hit = bySeq.get(row.seq);
    if (hit === undefined && row.clientMsgId) hit = byClientId.get(row.clientMsgId);
    if (hit === undefined && _byKeyEligible(row)) {
      hit = _pickNearestByKey(
        byKey.get(row.key ?? computeMsgKey(row)) ?? [],
        used,
        current,
        row,
        rowIdx,
        rows.length,
      );
    }
    if (hit === undefined) {
      out.push(row);
      return;
    }
    used.add(hit);
    const prev = out[hit];
    out[hit] = {
      ...row,
      id: prev.id,
      // 权威快照行不带 turn_id
      ...(row.turn_id === undefined && prev.turn_id !== undefined ? { turn_id: prev.turn_id } : {}),
    };
  });
  const merged = _capMessages(out);
  _assertFrozenPrefixInvariant(current, merged, "mergeSnapshotRows:reconcile");
  _assertNoDuplicateRows(merged);
  _assertEmptyAssistantRowCountStable(current, merged, "mergeSnapshotRows:reconcile");
  return merged;
}

/** 「追加即冻结」允许迟到帧落在尾部（设计行为，见迟到帧用例），但绝不允许 既有行之间的相对次序被重排。判据是前后对比 */
function _assertFrozenPrefixInvariant(before: ChatMessage[], after: ChatMessage[], caller: string): void {
  if (before.length < 2) return;
  const posAfter = new Map<string, number>();
  after.forEach((m, i) => posAfter.set(chatRowKey(m), i));
  let prevPos = -1;
  let prevKey = "";
  for (const m of before) {
    const k = chatRowKey(m);
    const pos = posAfter.get(k);
    if (pos === undefined) continue; // 被裁掉的旧行不参与（_capMessages 丢最旧是合法）
    if (pos < prevPos) {
      const fingerprint = after
        .slice(Math.max(0, prevPos - 1), pos + 2)
        .map((x) => `${x.seq ?? "?"}:${(x.text ?? "").slice(0, 16)}`)
        .join(" | ");
      console.error(
        `[seq-order] frozen-prefix violated at ${caller}: row ${k} moved before ${prevKey}. window: ${fingerprint}`,
      );
      return;
    }
    prevPos = pos;
    prevKey = k;
  }
}

/** 落定一条内容行：追加 +（触顶时）回收折叠区数据。 */
function _withRow(list: ChatMessage[], row: ChatMessage): ChatMessage[] {
  const next = _appendRow(list, row);
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

/* ── Flow 工作台图（内存态 agentic workflow graph）────────────────────
 * 图状态只维护在这里（单一事实源），工作台画布按其图名选取。
 * 仅保留经 flow_snapshot 请求过的图；工作台（subject=flow）例外——
 * 首个增量事件即建空图，画布自动跟随 FlowRoot 主体的图变化。
 * ─────────────────────────────────────────────────────────────────── */

function emptyFlowGraph(name: string, loaded = false): FlowLiveGraph {
  return { name, hops: 0, nodes: {}, edges: [], loaded, updatedAt: nowISO() };
}

function graphFromSnapshot(snap: FlowGraphSnapshot): FlowLiveGraph {
  const nodes: Record<string, FlowLiveNode> = {};
  for (const n of snap.nodes || []) {
    nodes[n.id] = {
      id: n.id,
      status: n.status,
      task: n.task || "",
      result: n.result || "",
      activations: typeof n.activations === "number" ? n.activations : 0,
      agent_id: n.agent_id || `sa-flow-${n.id}`,
      ...(n.error ? { error: n.error } : {}),
    };
  }
  return {
    name: snap.name,
    hops: snap.hops || 0,
    nodes,
    edges: (snap.edges || []).map((e) => ({
      from: e.from,
      to: e.to,
      ...(e.on && e.on !== "success" ? { on: e.on } : {}),
    })),
    loaded: true,
    updatedAt: nowISO(),
    ...(typeof snap.wdl === "string" && snap.wdl.trim() ? { wdl: snap.wdl } : {}),
  };
}

/** 把 flow-<flowName>-<nodeId> 形态的 subagent_id 匹配到在看的图。
 *  最长名优先，图名带 '-' 也能正确解析。 */
function flowNameFromSubagentId(
  subagentId: string,
  watched: string[],
): string | null {
  if (!subagentId.startsWith("flow-")) return null;
  const rest = subagentId.slice("flow-".length);
  const sorted = [...watched].sort((a, b) => b.length - a.length);
  for (const name of sorted) {
    if (rest.startsWith(name + "-")) return name;
  }
  return null;
}

/** 不可变更新一张在看的图；不在看则 no-op。 */
function updateFlowGraph(
  graphs: Record<string, FlowLiveGraph>,
  flow: string,
  updater: (g: FlowLiveGraph) => FlowLiveGraph,
): Record<string, FlowLiveGraph> {
  const g = graphs[flow];
  if (!g) return graphs;
  const next = updater(g);
  return next === g ? graphs : { ...graphs, [flow]: next };
}

/** flow_graph_changed：spawn → 加节点 + depends_on 边；其余动作只刷新
 *  已存在节点的状态。 */
function applyGraphChanged(
  graphs: Record<string, FlowLiveGraph>,
  msg: Extract<ServerMessage, { type: "flow_graph_changed" }>,
): Record<string, FlowLiveGraph> {
  const flow = flowGraphKey(msg.flow || "", msg.subject);
  const nodeId = msg.node_id;
  if (!flow || !nodeId) return graphs;
  if (msg.subject === "flow" && !graphs[flow]) {
    graphs = { ...graphs, [flow]: emptyFlowGraph(flow) };
  }
  const liveWdl =
    typeof msg.wdl === "string" && msg.wdl.trim() ? msg.wdl : undefined;
  return updateFlowGraph(graphs, flow, (g) => {
    const existing = g.nodes[nodeId];
    if (msg.action === "spawn" || !existing) {
      const nodes: Record<string, FlowLiveNode> = {
        ...g.nodes,
        [nodeId]: {
          id: nodeId,
          status: (msg.status as FlowLiveNode["status"]) || "pending",
          task: "",
          result: "",
          activations: 0,
          agent_id: `sa-flow-${nodeId}`,
        },
      };
      const edges: { from: string; to: string; on?: string }[] = [];
      const seen = new Set<string>();
      for (const e of g.edges) {
        if (e.to !== nodeId) {
          edges.push(e);
          seen.add(`${e.from}\u0000${e.to}`);
        }
      }
      for (const dep of msg.depends_on || []) {
        const key = `${dep}\u0000${nodeId}`;
        if (!seen.has(key) && nodes[dep]) {
          edges.push({ from: dep, to: nodeId });
          seen.add(key);
        }
      }
      return {
        ...g,
        nodes,
        edges,
        updatedAt: nowISO(),
        ...(liveWdl ? { wdl: liveWdl } : {}),
      };
    }
    if (msg.status && existing.status !== msg.status) {
      return {
        ...g,
        nodes: {
          ...g.nodes,
          [nodeId]: {
            ...existing,
            status: msg.status as FlowLiveNode["status"],
          },
        },
        updatedAt: nowISO(),
        ...(liveWdl ? { wdl: liveWdl } : {}),
      };
    }
    if (liveWdl && liveWdl !== g.wdl) {
      return { ...g, wdl: liveWdl, updatedAt: nowISO() };
    }
    return g;
  });
}

/** subagent_start/complete/failed：把 subagent_id flow-<flow>-<node> 映射
 *  为节点状态更新。错过 spawn 增量时顺带补建节点。 */
function applySubagentStatus(
  graphs: Record<string, FlowLiveGraph>,
  msg: Extract<
    ServerMessage,
    { type: "subagent_start" | "subagent_complete" | "subagent_failed" }
  >,
): Record<string, FlowLiveGraph> {
  const subagentId = msg.subagent_id || "";
  const flow = flowNameFromSubagentId(subagentId, Object.keys(graphs));
  if (!flow) return graphs;
  const nodeId = subagentId.slice(`flow-${flow}-`.length);
  const status: FlowLiveNode["status"] =
    msg.type === "subagent_start"
      ? msg.status === "pending"
        ? "pending"
        : "running"
      : msg.type === "subagent_failed"
        ? "failed"
        : "done";
  const agentId = `sa-flow-${nodeId}`;
  return updateFlowGraph(graphs, flow, (g) => {
    const existing = g.nodes[nodeId];
    if (!existing) {
      return {
        ...g,
        nodes: {
          ...g.nodes,
          [nodeId]: {
            id: nodeId,
            status,
            task: msg.description || "",
            result: "",
            activations: 0,
            agent_id: agentId,
            ...(msg.type === "subagent_failed" && msg.error
              ? { error: msg.error }
              : {}),
          },
        },
        updatedAt: nowISO(),
      };
    }
    const nextError =
      msg.type === "subagent_failed" && msg.error ? msg.error : existing.error;
    const nextResult =
      msg.type === "subagent_failed" && msg.error
        ? `[失败] ${msg.error}`
        : existing.result;
    if (
      existing.status === status &&
      existing.error === nextError &&
      existing.result === nextResult &&
      existing.agent_id === agentId
    ) {
      return g;
    }
    return {
      ...g,
      nodes: {
        ...g.nodes,
        [nodeId]: {
          ...existing,
          status,
          agent_id: existing.agent_id || agentId,
          result: nextResult,
          ...(nextError ? { error: nextError } : {}),
        },
      },
      updatedAt: nowISO(),
    };
  });
}

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

/** 回放帧是否与 store 已有内容重复（刷新双显的判定）。 分层：**带 view_seq 的回放帧走不到这里** */
/** - subagent_chunk：子智能体中间产出，只实时投递、不落带 （web_views.to_view_frame 对它直接 return None），因此永远没有 view_seq， 也永远不会出现在快照里。 任 */
const UNPERSISTED_FRAME_TYPES = new Set(["subagent_chunk"]);

function _isUnpersistedProcessFrame(msg: ServerMessage): boolean {
  return UNPERSISTED_FRAME_TYPES.has(msg.type);
}

/** - 快照值非空＝权威，覆盖同 call 的现有 result - 快照值为空＝不是更权威的内容，保留现有实时值 - 快照里有、本地没有的 call 新建 {text: "", result}（中间产出丢过也还在） 返回 */
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

/** 帧上的子智能体归属（有才带）：编排节点分组靠它把帧分到各自节点。 */
function _frameNodeIdentity(frame: SubagentFramePayload): { subagent_id?: string; coara_id?: string } {
  const subagent_id = String(frame.subagent_id ?? "");
  const coara_id = String(frame.coara_id ?? "");
  const out: { subagent_id?: string; coara_id?: string } = {};
  if (subagent_id) out.subagent_id = subagent_id;
  if (coara_id) out.coara_id = coara_id;
  return out;
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
      ..._frameNodeIdentity(frame),
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
    key: computeMsgKey({ role: "assistant", text: "", diff, tool_call_id: diffCallId }),
    ...(diffCallId ? { tool_call_id: diffCallId } : {}),
    ...(seq !== undefined ? { seq } : {}),
    ..._frameNodeIdentity(frame),
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

/** - 新契约：帧显式带 ``delegate_brief: true`` - 老数据兜底：正文以 ``<任务指令>`` 开头（服务端过滤之前落下的帧） */
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

/** 快照带回的子智能体过程正文并入 subagentOutput.text。权威但**不回退**：增量快照
 *  只覆盖窗口内的后缀，若本地已有更长文本（实时累积）就保留本地，避免刷新式缩短。
 *  返回 null＝快照没带这份映射，调用方不写该字段。 */
function _mergeSubagentTexts(
  existing: Record<string, { text: string; result: string }>,
  snapshot: Record<string, string> | undefined,
): Record<string, { text: string; result: string }> | null {
  if (!snapshot) return null;
  const entries = Object.entries(snapshot);
  if (entries.length === 0) return null;
  const merged: Record<string, { text: string; result: string }> = { ...existing };
  for (const [callId, raw] of entries) {
    const text = typeof raw === "string" ? raw : "";
    if (!text) continue;
    const prev = merged[callId] ?? { text: "", result: "" };
    if (text.length >= prev.text.length) merged[callId] = { ...prev, text };
  }
  return merged;
}

/** 重连回放会把同一批帧原样再送一次，靠文本判重会误伤（不同片段可能同文），靠 replayed 标志会把丢帧窗口内真正的新帧一起丢掉——而 (turn_id, seq) 帧上本来 */
const _subagentChunkMarks = new Map<string, { turnId: string; seq: number }>();

/** 仅供 selftest：清掉过程帧序号水位（模块级状态，setState 复位不了）。 */
export function resetSubagentChunkMarksForTest(): void {
  _subagentChunkMarks.clear();
}

/** 此时 workspaceDir 为 null，内容帧过不了 _frameInBoundary，会被整体丢弃 */
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
  // 溢出丢最旧，但优先丢「能从快照补回」的落带帧
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
  turn_started_at?: number;
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
  hasMoreHistory: false,
  earliestSeq: 0,
  earlierLoading: false,
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
  workflowDraftRevisions: {},
  providersRevision: 0,
  workspacesRevision: 0,
  flowGraphs: {},
  applyFlowSnapshot: (flow, snapshot) => {
    set((s) => ({
      flowGraphs: {
        ...s.flowGraphs,
        [flow]: snapshot ? graphFromSnapshot(snapshot) : emptyFlowGraph(flow, true),
      },
    }));
  },
  applyFlowTraceEvent: (msg) => {
    if (msg.type === "flow_graph_changed") {
      set((s) => ({ flowGraphs: applyGraphChanged(s.flowGraphs, msg) }));
      return;
    }
    if (
      msg.type === "subagent_start" ||
      msg.type === "subagent_complete" ||
      msg.type === "subagent_failed"
    ) {
      set((s) => ({ flowGraphs: applySubagentStatus(s.flowGraphs, msg) }));
    }
  },
  dropFlowGraph: (flow) => {
    set((s) => {
      if (!s.flowGraphs[flow]) return {};
      const flowGraphs = { ...s.flowGraphs };
      delete flowGraphs[flow];
      return { flowGraphs };
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
  pendingNav: null,
  consumeNav: () => set({ pendingNav: null }),
  account: null,
  setAccount: (a) => set({ account: a }),
  updatesPending: 0,
  setUpdatesPending: (n) => set({ updatesPending: Math.max(0, n) }),

  handleServerMessage: (msg: ServerMessage) => {
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

    const msgSubject = (msg as { subject?: string }).subject;
    if (MODULE_CHAT_TYPES.has(msg.type) && msgSubject && msgSubject !== "root") {
      handleModuleChatEvent(msgSubject, msg, set, get);
      return;
    }

    // 边界未定（刷新/切空间初期）：内容帧先入缓冲，等 state 帧把 workspaceDir 落定后回放
    if (!_replayingPendingFrames && !get().workspaceDir && BOUNDARY_GATED_FRAME_TYPES.has(msg.type)) {
      _bufferPendingFrame(msg);
      return;
    }

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

    // trace_batch 防 UI 冻结的设计一致）。
    if (SUBAGENT_TREE_TYPES.has(msg.type)) {
      const src = (msg as { source?: string }).source;
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

    if (TRACE_TYPES.has(msg.type)) {
      const entry = toTraceEntry(msg);
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
                const newEvents = events.slice();
                newEvents[existingIdx] = entry;
                set({ traceEvents: newEvents });
              } else {
                const newEvents = [...events];
                newEvents[existingIdx] = entry;
                set({ traceEvents: newEvents });
              }
            }
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
        // 展开区），先于一切正文逻辑判掉——它的文本还可能被认领逻辑当成乐观泡。
        const brief = _delegateBrief(msg);
        if (brief) {
          if (brief.callId && isWebSource(msg.source) && _frameInBoundary(msg, get())) {
            _ingestSubagentBrief(brief.callId, brief.text);
          }
          break;
        }
        // S1 服务端确认：web 自己的 user_message 权威帧到达时，认领本地的同一行并 他端 source（CLI/Matrix）不触碰 web 的气泡（web 聊天区只显 web 对话）
        if (!isWebSource(msg.source)) break;
        if (!_frameInBoundary(msg, get())) break;
        const msgs = get().messages;
        const content = (msg as { content?: string }).content;
        // 帧的线内序号：落到行上（顺序的权威键；已上屏的行不因它再被搬动）
        const msgSeq = _frameViewSeq(msg);
        const rawClientId = (msg as { client_msg_id?: unknown }).client_msg_id;
        const msgClientId = typeof rawClientId === "string" ? rawClientId.trim() : "";
        const frameText = typeof content === "string" ? content.trim() : "";
        //（用户反馈的接续输入重复，正是这条路）。
        const CLAIM_TAIL_WINDOW = 24;
        const tailStart = Math.max(0, msgs.length - CLAIM_TAIL_WINDOW);
        let claimed = false;
        for (let i = msgs.length - 1; i >= tailStart; i--) {
          const m = msgs[i];
          if (m.role !== "user" || m.turn_id) continue;
          const byClientId = msgClientId !== "" && m.clientMsgId === msgClientId;
          const byEcho = m.optimistic === true;
          const bySeq = msgSeq > 0 && m.seq === msgSeq;
          const byText = m.seq === undefined && frameText !== "" && m.text.trim() === frameText;
          if (!byClientId && !byEcho && !bySeq && !byText) continue;
          const updated = msgs.slice();
          updated[i] = {
            ...m,
            turn_id: msg.turn_id,
            optimistic: false,
            // pendingInject 不在这里清：跟话入队即回投 user_message，真正「LLM 已读到」
            // 的信号是 continuation_input_injected，到达前转圈保留（09-26 修复转圈一闪而过）。
            ...(m.sendFailed ? { sendFailed: false } : {}),
            ...(typeof content === "string" && content.trim() ? { text: content } : {}),
            // 已有 seq 的行（hydrate 换来的权威行）不回填：它本来就带着线上的位置。
            ...(msgSeq > 0 && m.seq === undefined ? { seq: msgSeq } : {}),
          };
          // 帧级「行存在性」裁量已经挡住，不会走到这里。
          set({ messages: updated });
          claimed = true;
          break;
        }
        // 或这份帧还没被认领过）——新建一条，否则刷新后「只见回复不见提问」。
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
        // events from CLI/Matrix/event AND source-less turns (subagents,
        if (!isWebSource(msg.source)) break;
        if (!_frameInBoundary(msg, get())) break;
        const msgs = get().messages;
        // turn_id）：回填真实 turn_id，供 turn_end 对齐认领。只认尾部那个
        const tailIdx = msgs.length - 1;
        const tailMsg = tailIdx >= 0 ? msgs[tailIdx] : null;
        if (tailMsg && tailMsg.role === "assistant" && tailMsg.autoCreated && tailMsg.turn_id?.startsWith("auto-")) {
          const updated = msgs.slice();
          updated[tailIdx] = { ...tailMsg, turn_id: msg.turn_id };
          set({ turnActive: true, currentTurnId: msg.turn_id, turnStartedAt: get().turnStartedAt ?? Date.now(), messages: updated });
          break;
        }
        // 正文块到达时自建气泡。
        set({ turnActive: true, currentTurnId: msg.turn_id, turnStartedAt: get().turnStartedAt ?? Date.now() });
        break;
      }

      case "turn_queued": {
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
        // 与 turn_start/turn_end 同门
        if (!isWebSource(msg.source)) break;
        // followup 合成流等路径可能把旧空间/旧会话的回合输出投到已切走视图 （跨空间串话）
        if (!_frameInBoundary(msg, get())) break;
        // （a 段 → 工具 → b 段）自然分成 a 一个气泡、b 一个气泡。
        const state = get();
        const msgs = state.messages;
        const currentTurnId = state.currentTurnId;
        // 对齐认领。
        const autoTurnId = currentTurnId || `auto-${nextId()}`;
        // 帧的线内序号：写进行里，迟到/回放帧才能落回正确位置。
        const msgSeq = _frameViewSeq(msg);
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
            streaming: false,
            autoCreated: true,
            ...(msgSeq > 0 ? { seq: msgSeq } : {}),
          }),
        });
        break;
      }

      case "continuation_input_injected": {
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
        // 追加一条「结果气泡」＝同一件事两处呈现，且刷新后与视图带不一致。
        break;
      }

      case "chat_turn_retracted": {
        // 会让它下面每一行上移一格，正是用户报的「跳」。
        const tid = msg.turn_id;
        const state = get();
        const rows = state.messages;
        const hadTurn = Boolean(tid && rows.some((m) => m.turn_id === tid));
        const isCurrent = Boolean(tid && state.currentTurnId === tid);
        if (!hadTurn && !isCurrent) break;
        // 撤回范围与原实现一致（只从尾部认领那些「本轮刚产生的痕迹」）， 差别只是「标记隐藏」而不是「删掉」
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
        // own streaming bubble and unlock the input mid-turn.
        if (!isWebSource(msg.source)) break;
        const tid = msg.turn_id;
        // 迟到 turn_end 门禁：tid 明确且不属当前回合时只清气泡/徽标， 不碰 turnActive/subagentRows
        const staleTurnEnd = Boolean(tid && get().currentTurnId && tid !== get().currentTurnId);
        // 只清标记不删行：删了会让它下面的行上移一格（跳）。
        const msgs = get().messages.map((m) => {
          const base = m.role === "assistant" && m.queued && !m.text ? { ...m, queued: false, streaming: false } : m;
          if (base.streaming && base.role === "assistant" && (tid ? base.turn_id === tid || base.autoCreated : true)) {
            return { ...base, streaming: false };
          }
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
        // 后台子智能体常在回合结束后继续跑，整树清空会让它的 delegate 行首呼吸点
        _subagentTree.clearRootStatus();
        set({
          messages: msgs,
          turnActive: false,
          currentTurnId: null,
          turnStartedAt: null,
          subagentRows: _subagentTree.rows(),
        });
        break;
      }

      case "subagent_chunk": {
        if (!_frameInBoundary(msg, get())) break;
        const cid = String((msg as { tool_call_id?: string }).tool_call_id || "");
        const piece = String((msg as { text?: string }).text || "");
        if (!cid || !piece) break;
        // 真正的新帧键更大 ⇒ 照收。为什么不用文本判重（不同片段可能同文）也不用
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
            key: computeMsgKey({ role: "assistant", text: "", diff, tool_call_id: diffCallId }),
            ...(diffCallId ? { tool_call_id: diffCallId } : {}),
            ...(diffSeq > 0 ? { seq: diffSeq } : {}),
          }),
        });
        break;
      }

      case "command_result": {
        // 本连接单播回执：先清 pendingCommand（否则边界丢帧会让 /compact spinner 永挂）。 再按空间/会话边界决定是否上屏
        set({ pendingCommand: null });
        if (!msgSubject || msgSubject === "root") {
          const frameDir = (msg as { workspace_dir?: unknown }).workspace_dir;
          if (typeof frameDir === "string" && frameDir.trim()) {
            if (!_frameInBoundary(msg, get())) break;
          }
        }
        // 命令可请求页面导航（如 /login → 登录页），复用 pendingNav 通道
        const nav = (msg.result?.data as Record<string, unknown> | undefined)?.navigate;
        if (typeof nav === "string" && nav) {
          set({ pendingNav: nav });
        }
        // 压缩成功（静默回执 + data.compressed）：画「已压缩」分割线，与落带的 divider 帧同语义
        const cmdData = (msg.result?.data ?? {}) as Record<string, unknown>;
        if (cmdData.compressed === true) {
          const cmdSeqC = _frameViewSeq(msg);
          const dividerMsg: ChatMessage = {
            id: nextId(),
            role: "assistant",
            text: "",
            dividerLabel: "已压缩",
            dividerTime: formatTimelineTimeLabel(Date.now()),
            key: computeMsgKey({ role: "assistant", text: "", dividerLabel: "已压缩" }),
            ...(cmdSeqC > 0 ? { seq: cmdSeqC } : {}),
          };
          set({ messages: _withRow(get().messages, dividerMsg) });
          break;
        }
        // 静默命令（零正文，意图在 data 里）：只执行导航等副作用，不产生命令卡
        if (!msg.result.output.trim()) {
          break;
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
          // 命令输出按时间顺序落位：有按时间追加的语义（帧顺序即真相）， 同时过一遍顺序归一
          set({ messages: _withRow(get().messages, bubble) });
        }
        break;
      }

      // session_auto_new is intentionally not rendered as a chat bubble.

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
        // 本连接错误回执：先清 pendingCommand，再按边界决定是否上屏
        set({ pendingCommand: null });
        const frameDir = (msg as { workspace_dir?: unknown }).workspace_dir;
        if (typeof frameDir === "string" && frameDir.trim()) {
          if (!_frameInBoundary(msg, get())) break;
        }
        // 服务端早失败（未起回合，无 user_message 认领帧）时，把尾部乐观用户泡 折叠标为发送失败
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
        // 附件挂到跟话上方的旧助手气泡（图出现在用户消息上面）。
        const attachment: ChatFileAttachment = {
          file_id: msg.file_id,
          url: msg.url,
          ...(typeof msg.path === "string" && msg.path ? { path: msg.path } : {}),
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
        const path = typeof msg.path === "string" ? msg.path.trim() : "";
        if (path) {
          set({ pendingNav: path.startsWith("/") ? path : `/${path}` });
        }
        try {
          window.focus();
        } catch {
          /* ignore */
        }
        void import("./ws").then(({ getWS }) => {
          const ws = getWS();
          if (!ws.isConnected() && !ws.isClosed()) {
            ws.reconnectNow();
          }
        });
        break;
      }

      case "workspaces_changed":
        set((s) => ({ workspacesRevision: (s.workspacesRevision ?? 0) + 1 }));
        fetchWorkspaceList()
          .then((data) => get().setWorkspaces(data.workspaces, data.active_name))
          .catch((err) => console.error("Failed to refresh workspace list:", err));
        break;

      case "providers_changed":
        set((s) => ({ providersRevision: (s.providersRevision ?? 0) + 1 }));
        break;

      case "open_workflow_editor":
        set({ pendingNav: `/workflow/editor/${msg.draft_id}` });
        break;

      case "workflow_draft_updated":
        // 编排写穿：会话侧改图已落盘——编辑器/列表按 revision 刷新
        set((s) => ({
          workflowDraftRevisions: {
            ...s.workflowDraftRevisions,
            [msg.draft_id]: (s.workflowDraftRevisions[msg.draft_id] ?? 0) + 1,
          },
        }));
        break;

      // 的工作台图除外，首个增量即建空图），闲置流量不累积图状态。
      case "flow_graph_snapshot":
        get().applyFlowSnapshot(flowGraphKey(msg.flow, msg.subject), msg.snapshot ?? null);
        break;

      case "flow_graph_changed":
      case "subagent_start":
      case "subagent_complete":
      case "subagent_failed":
        get().applyFlowTraceEvent(msg);
        break;

      case "state": {
        const data = msg.data as {
          runtime?: RuntimeInfo;
          sessions?: unknown[];
        };
        if (data?.runtime) {
          set({ runtime: data.runtime });
          // 一次丢帧兜底判死（对齐 CLI 的 600s 静默阈值）。
          if (_subagentTree.upsertActive(data.runtime.active_delegations ?? [])) {
            set({ subagentRows: _subagentTree.rows() });
          }
          if (_subagentTree.pruneStale()) {
            set({ subagentRows: _subagentTree.rows() });
          }
          // so the input box doesn't get stuck showing "queued".
          if (data.runtime.running === false && get().turnActive) {
            set({ turnActive: false, currentTurnId: null, turnStartedAt: null });
          }
          // 旧逻辑会把别人的回合当成 Web 在转圈。
          const turnSrc = data.runtime.turn_source;
          if (data.runtime.running === true && isWebSource(turnSrc) && !get().turnActive) {
            const serverStart = Number(data.runtime.turn_started_at) || 0;
            set({
              turnActive: true,
              turnStartedAt: get().turnStartedAt ?? (serverStart > 0 ? serverStart * 1000 : Date.now()),
            });
          }
          // spinner 行的静态小标识表达，见 TurnSpinner）。
          if (get().turnActive && data.runtime.running === true && !isWebSource(turnSrc)) {
            set({ turnActive: false, currentTurnId: null, turnStartedAt: null });
          }
          // 否则会把本地刚加的用户消息清掉。
          const newSid = data.runtime.session_id;
          const newDir = data.runtime.workspace_dir || null;
          const curSid = get().sessionId;
          const curDir = get().workspaceDir;
          if (newSid && newSid !== curSid) {
            if (curDir === null) {
              // 缓存，一律等 hydrate 权威给：刷新与切换同一把尺。
              set({ sessionId: newSid, workspaceDir: newDir });
            } else if (newDir && newDir !== curDir) {
              // 真实空间切换（/ws switch、他端切空间）：换边界，恢复/加载目标空间视图。
              get().resetForWorkspaceSwitch(newDir);
              // （他端 /new 过），用旧值会让这个空间的帧被边界守卫全部丢掉。
              set({ sessionId: newSid });
              void get().hydrateToolActivity();
                } else {
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
          // 边界已落定（首次定界 / 切空间）：回放边界未定期间到达的内容帧。 这是「订阅先于快照」的另一半
          if (get().workspaceDir) _flushPendingFrames();
          // Keep the workspace Select in sync when switch happened outside
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
    // 线身份：epoch 变了＝整条线已重建（服务端换代）
    const epoch = typeof opts?.epoch === "string" && opts.epoch.trim() ? opts.epoch.trim() : null;
    const lineRebuilt = Boolean(epoch && st.lineEpoch !== null && epoch !== st.lineEpoch);
    if (lineRebuilt && opts?.incremental) return;
    // 不存在自动产生的空档线。
    const mapped: ChatMessage[] = _mapSnapshotRows(messages);
    const fused = fuseDividersWithTime(mapped);
    // hydrate 游标：这次快照覆盖到的最新已落带 seq。比它新的属实时尾部。
    const cursor = latestSeq ?? mapped.reduce((acc, m) => Math.max(acc, m.seq ?? 0), 0);
    // 线重建时本地列表整体作废：不参与配对、也不保留实时尾（它们都属旧线）。
    const current = lineRebuilt ? [] : get().messages;
    // 它们不是权威内容——这份首屏快照才是。判据错了的后果＝增量路径按水位地板丢
    const replace = lineRebuilt || current.length === 0 || (!st.viewReady && !opts?.incremental);
    // hydrate 是权威：每次 loadHistory 后把当前空间的会话键记进映射（切回来时 第一帧就用对键）。键 = workspaceDir；内容不存
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
      // turnActive 口径与实时帧一致
      const webTurn = rt ? Boolean(rt.running) && isWebSource(rt.turn_source) : get().turnActive;
      // 非活跃回合的 hydrate 同步清活动树（对齐切空间/新会话路径）
      if (!webTurn) _subagentTree.clear();
      // 游标/身份推进成功：清掉「这个空洞补不动」的记忆，下一个空洞照常补。
      const patch: Partial<AppState> = {
        // 顺序由调用方（_mergeSnapshotRows）定好
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
        const rebuiltOutput = _mergeSubagentResults({}, opts?.subagentResults) ?? {};
        patch.subagentOutput = _mergeSubagentTexts(rebuiltOutput, opts?.subagentTexts) ?? rebuiltOutput;
        patch.subagentDiffs = _mergeSubagentDiffsSnapshot({}, opts?.subagentDiffs) ?? {};
        patch.subagentBriefs = _mergeSubagentBriefs({}, opts?.subagentBriefs) ?? {};
      } else {
        const subagentOutput = _mergeSubagentResults(get().subagentOutput, opts?.subagentResults);
        const subagentOutputWithTexts = _mergeSubagentTexts(
          subagentOutput ?? get().subagentOutput,
          opts?.subagentTexts,
        );
        if (subagentOutputWithTexts) patch.subagentOutput = subagentOutputWithTexts;
        else if (subagentOutput) patch.subagentOutput = subagentOutput;
        const subagentDiffs = _mergeSubagentDiffsSnapshot(get().subagentDiffs, opts?.subagentDiffs);
        if (subagentDiffs) patch.subagentDiffs = subagentDiffs;
        const subagentBriefs = _mergeSubagentBriefs(get().subagentBriefs, opts?.subagentBriefs);
        if (subagentBriefs) patch.subagentBriefs = subagentBriefs;
      }
      if (rt) {
        patch.sessionId = rt.session_id ?? get().sessionId;
        patch.turnActive = webTurn;
        patch.currentTurnId = webTurn ? (get().currentTurnId ?? null) : null;
        const serverStart = Number(rt.turn_started_at) || 0;
        patch.turnStartedAt = webTurn
          ? (get().turnStartedAt ?? (serverStart > 0 ? serverStart * 1000 : Date.now()))
          : null;
      }
      if (!webTurn) patch.subagentRows = [];
      // 时退回按行数判（原先口径）。增量补拉只补后缀，头部不变（维持原值）。
      if (!opts?.incremental) {
        patch.hasMoreHistory =
          typeof opts?.total === "number" && opts.total >= 0
            ? opts.total > messages.length
            : messages.length >= HISTORY_PAGE_LIMIT;
      }
      let minSeq = Number.POSITIVE_INFINITY;
      for (const m of mapped) {
        if (m.seq !== undefined && m.seq > 0 && m.seq < minSeq) minSeq = m.seq;
      }
      for (const m of msgs) {
        if (m.seq !== undefined && m.seq > 0 && m.seq < minSeq) minSeq = m.seq;
      }
      if (Number.isFinite(minSeq)) patch.earliestSeq = minSeq;
      set(patch);
      _pruneFolding(patch.messages ?? [], get().subagentRows);
      syncCache();
    };
    // 快照里没有的既有行原地保留。已显示行的相对顺序因此永不变化。
    commit(_mergeSnapshotRows(current, fused, replace));
  },

  prependHistory: (messages, opts) => {
    const st = get();
    // 与 loadHistory 同款边界守卫：响应回来时空间已切走就整体丢弃。
    if (
      opts?.workspaceDir !== undefined &&
      opts.workspaceDir !== null &&
      st.workspaceDir !== opts.workspaceDir
    ) {
      return;
    }
    // 映射与 loadHistory 同一构造函数（_mapSnapshotRows）；分隔线融合只对这批 更早行内部做
    const mapped = fuseDividersWithTime(_mapSnapshotRows(messages));
    const next = _prependSnapshotRows(st.messages, mapped);
    if (next === st.messages) return;
    let minSeq = st.earliestSeq;
    for (const m of next) {
      if (m.seq !== undefined && m.seq > 0 && (minSeq <= 0 || m.seq < minSeq)) minSeq = m.seq;
    }
    // 只动 messages 与翻页游标
    set({ messages: next, earliestSeq: minSeq });
  },

  loadEarlier: async () => {
    const st = get();
    if (st.earlierLoading) return;
    const dir = st.workspaceDir;
    if (!dir) return;
    // 翻页游标：已加载的最小 seq（earliestSeq 未维护到时退回现算）
    let earliest = st.earliestSeq;
    if (earliest <= 0) {
      for (const m of st.messages) {
        if (m.seq !== undefined && m.seq > 0 && (earliest <= 0 || m.seq < earliest)) earliest = m.seq;
      }
    }
    if (earliest <= 1) {
      set({ hasMoreHistory: false });
      return;
    }
    const generation = st.hydrateGeneration;
    set({ earlierLoading: true });
    try {
      const data = await fetchSessionMessages(HISTORY_PAGE_LIMIT, undefined, {
        workspaceDir: dir,
        beforeViewSeq: earliest,
      });
      const now = get();
      // 切空间 / 换边界竞态守卫（与 loadHistory 同款）：过期响应整体丢弃。
      if (now.workspaceDir !== dir) return;
      if (now.hydrateGeneration !== generation) return;
      const rows = Array.isArray(data.messages) ? data.messages : [];
      get().prependHistory(rows, { workspaceDir: dir });
      // 不足一页＝历史到头，收起「加载更早消息」入口。
      if (rows.length < HISTORY_PAGE_LIMIT) set({ hasMoreHistory: false });
    } catch (err) {
      console.error("Failed to load earlier messages:", err);
    } finally {
      const now = get();
      if (now.workspaceDir === dir) set({ earlierLoading: false });
    }
  },

  addUserMessage: (text, _imageRefs, attachments, clientMsgId) => {
    // 回合进行中发送的是接续输入（排队等注入 LLM 上下文）
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
      // 无论拉到数据与否，hydrate 流程已走完
      if (!get().toolActivityReady) {
        set({ toolActivityReady: true });
      }
    }
  },
  resetForWorkspaceSwitch: (workspaceDir: string, snapshot?: SessionSnapshot | null) => {
    // /new、切空间换一批加载词（对齐 CLI reshuffle 时机）。
    reshufflePhrases();
    // 时视图是空的，等 hydrate 一次到位。
    const prev = get();
    const prevDir = prev.workspaceDir;
    const cache = { ...prev.sessionIdByWorkspace };
    if (prevDir && prevDir !== workspaceDir) {
      cache[prevDir] = prev.sessionId;
    }
    const cachedSessionId = cache[workspaceDir];
    _subagentTree.clear();
    // 分隔线只属于有落盘的两种场景：新会话、模型切换。
    set({
      workspaceDir,
      sessionIdByWorkspace: cache,
      // 空线上屏：目标空间的内容一律等 hydrate 权威给（宁可慢一拍，不铺旧料）。
      messages: [],
      hydratedSeq: 0,
      // 向前翻页窗口属旧空间：换边界一律清回初始，由目标空间的 hydrate 重算。
      hasMoreHistory: false,
      earliestSeq: 0,
      earlierLoading: false,
      // 换边界即退回骨架态：目标空间的内容还没被权威快照落定。
      viewReady: false,
      skeletonSince: Date.now(),
      // 在飞 hydrate 会被下面的 generation 递增丢弃：记一笔，让 ChatView 补一次 （带快照时不必要
      needsHydrate: !snapshot,
      // 线身份与折叠区一律不继承上一个空间
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
      sessionId: snapshot?.session_id ?? cachedSessionId ?? null,
      pendingInteraction: null,
    });
    // 退回原流程：清空边界，等 ChatView 的 hydrate 接手。
    if (snapshot && Array.isArray(snapshot.messages)) {
      // 出现两种画面：骨架 → 目标内容」靠的就是这个。没有快照（旧服务端/异常）时
      get().loadHistory(snapshot.messages, snapshot.latest_seq, {
        workspaceDir,
        incremental: false,
        epoch: snapshot.epoch,
        // 切空间快照的窗口是服务端默认（100），比翻页页大小（200）小
        total: snapshot.total,
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
      // 换会话段＝换边界：内容还没被新边界的权威快照落定，退回骨架态
      viewReady: false,
      skeletonSince: Date.now(),
      // 在飞 hydrate 被 generation 递增丢弃：同边界不会自动重拉，补一次（E）。
      needsHydrate: true,
      turnActive: false,
      currentTurnId: null,
      turnStartedAt: null,
      pendingInteraction: null,
    });
    // 缓存键是 workspaceDir，同空间 /new 不换键——lastWorkspace 无需更新。
  },

  applyLocalModelSwitch: (provider, model, _deferred = false) => {
    const key = provider && model ? `${provider}·${model}` : model || provider;
    if (!key) return;
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
