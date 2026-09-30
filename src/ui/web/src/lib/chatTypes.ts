// 消息行与相关数据类型（从 store.ts 抽出：纯类型，无状态）。
// store.ts 经 re-export 保持既有 import 路径不变。

import type { CanonicalDiffLines, DisplayBlock, FlowLiveNode } from "./ws";

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
  diff?: CanonicalDiffLines;
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
export interface ModuleSessionState {
  messages: ChatMessage[];
  turnActive: boolean;
  currentTurnId: string | null;
}

/** 内存态 agentic 工作流图（FlowCoordinator），工作台画布与 live WDL 同步用。 */
export interface FlowLiveGraph {
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
  /** 内核前台空间（进程绑定）：不能移出登记，侧边栏不出移出按钮 */
  foreground?: boolean;
  /** 门面形态：warehouse=对话主页 / display=展示主页 / storefront=营业主页 */
  storefront?: string;
  /** 展示/营业主页的路由（如 /usage）；仓库空间为空 = 对话页 */
  home_view?: string;
  /** 空间种类：normal=普通用户空间 / internal=系统空间 / external=对外开放 */
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
  /** Running background task labels (server-authoritative; TurnSpinner rotates them). */
  background_task_labels?: string[];
  /** Running background task details (task_id + label) for list/kill UI. */
  background_task_items?: Array<{
    task_id: string;
    kind?: string;
    label: string;
    description?: string;
    subagent_type?: string;
    origin_source?: string;
  }>;
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
  diff_lines?: CanonicalDiffLines;
  /** Full tool output text (capped server-side) from tool_complete. */
  tool_output?: string;
  tool_output_truncated?: boolean;
  tool_output_ref?: string;
  duration_ms?: number;
  is_error?: boolean;
}
