import { tokenQuery, tokenQueryFragment } from "./auth";

const API_BASE = "";

/** 当日 daily 定制轮播词（后端已做一日抛校验；失败回落空）。 */
export async function fetchCustomLoadingPhrases(): Promise<{ date: string; phrases: string[] }> {
  const empty = { date: "", phrases: [] as string[] };
  try {
    const res = await fetch(`${API_BASE}/api/loading-phrases/custom${tokenQuery()}`);
    if (!res.ok) return empty;
    const data = (await res.json()) as { date?: unknown; phrases?: unknown };
    return {
      date: typeof data?.date === "string" ? data.date : "",
      phrases: Array.isArray(data?.phrases) ? data.phrases.filter((p): p is string => typeof p === "string") : [],
    };
  } catch {
    return empty;
  }
}

/** LLM provider entry as edited by the config UI. */
export interface ProviderConfig {
  base_url: string;
  api_key_env?: string;
  /** Inline API key (preferred over api_key_env). Masked as "***" on fetch. */
  api_key?: string;
  /** Protocol driver: openai | anthropic | responses. */
  driver?: string;
  models: Record<string, any>;
  default_model?: string;
  max_tokens?: number;
  /** Disabled providers stay configured but are hidden from /model. */
  enabled?: boolean;
}

/** Top-level coara config subset edited via /api/v1/config. All fields are
 *  optional — the backend merges whatever is sent. Mirrors the shape used
 *  by features/config/ConfigPanel.tsx. */
export interface CoaraConfig {
  log_level?: string;
  default_provider?: string;
  default_model?: string;
  coara_home?: string;
  vault_enabled?: boolean;
  skills_enabled?: boolean;
  records?: {
    enabled?: boolean;
    default_ttl_days?: number | null;
  };
  context_compression?: {
    threshold?: number;
    preserve_ratio?: number;
    min_compressible_fraction?: number;
  };
  events?: {
    webhook_host?: string;
    webhook_port?: number;
  };
  matrix?: {
    homeserver?: string;
    user?: string;
    password?: string;
    notify_room_id?: string;
  };
  skills?: {
    default_include?: string[];
  };
  security?: {
    call_policy?: Record<string, string[]>;
    sandbox?: {
      enabled_for_untrusted?: boolean;
      blocked_commands?: string[];
      blocked_paths?: string[];
      blocked_hosts?: string[];
      blocked_env?: string[];
    };
  };
}

export async function fetchConfig() {
  const res = await fetch(`${API_BASE}/api/v1/config${tokenQuery()}`);
  if (!res.ok) throw new Error(`加载配置失败（HTTP ${res.status}）`);
  return res.json();
}

export async function fetchProviders() {
  const res = await fetch(`${API_BASE}/api/v1/providers${tokenQuery()}`);
  if (!res.ok) throw new Error(`加载模型提供方失败（HTTP ${res.status}）`);
  return res.json();
}

/** 保存 providers（全量替换语义；"***" 掩码值会被后端还原为原 key，空字符串表示清空）。 */
export async function saveProviders(payload: { providers: Record<string, ProviderConfig> }) {
  const res = await fetch(`${API_BASE}/api/v1/providers${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(detail || `保存模型提供方失败（HTTP ${res.status}）`);
  }
  return res.json() as Promise<{ ok: boolean }>;
}

/** 可用模型组合：所有已配置且有 API key 的 provider 声明的模型；空 = 尚无可用 key。 */
export async function fetchModelChoices() {
  const res = await fetch(`${API_BASE}/api/v1/model-choices${tokenQuery()}`);
  if (!res.ok) throw new Error(`加载可用模型失败（HTTP ${res.status}）`);
  return res.json() as Promise<{ catalog: { provider: string; model: string; key: string; label: string }[] }>;
}

export interface ModelChoice {
  provider: string;
  model: string;
  key: string;
  label: string;
}

export async function fetchSkills() {
  const res = await fetch(`${API_BASE}/api/v1/skills${tokenQuery()}`);
  if (!res.ok) throw new Error(`加载技能列表失败（HTTP ${res.status}）`);
  return res.json() as Promise<{
    skills: Array<{ name: string; description?: string; source?: string }>;
    default_include: string[];
    pool_count: number;
  }>;
}

// ---- 设置中心：reminders / event-sources / workspaces ----

async function del(path: string) {
  const res = await fetch(`${API_BASE}${path}${tokenQuery()}`, { method: "DELETE" });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json();
}

async function getJson(path: string) {
  const res = await fetch(`${API_BASE}${path}${tokenQuery()}`);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json();
}

export interface ReminderRow {
  id: string;
  kind: string;
  message: string;
  next_run_at?: string;
  interval_seconds?: number;
  cron?: string;
  enabled: boolean;
}

export const fetchReminders = () => getJson("/api/v1/reminders");

export interface EventSourceRow {
  id: string;
  enabled: boolean;
  kind: string;
  workspace: string;
  salience?: string;
  handle?: string;
  running?: boolean;
  webhook_url?: string;
  [key: string]: unknown;
}

export const fetchEventSources = () => getJson("/api/v1/event-sources");

export interface WorkspaceRow {
  id: string;
  name: string;
  path: string;
  kind?: string;
  status?: string;
  summary?: string;
  tags?: string[];
  is_default?: boolean;
}

export const fetchWorkspaces = () => getJson("/api/v1/workspaces");

interface WorkspaceEntryRow {
  id: string;
  name: string;
  path: string;
  kind?: string;
  status?: string;
  summary?: string;
  /** 系统主页路由（/records、/usage 等）；空 = 对话空间 */
  home_view?: string | null;
  storefront?: string | null;
  tags?: string[];
}

/** 把一个已存在的目录登记为工作空间。 */
export async function createWorkspaceEntry(name: string, path: string) {
  const res = await fetch(`${API_BASE}/api/v1/workspaces${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, path }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || res.statusText);
  }
  return res.json() as Promise<{ ok: boolean; workspace: WorkspaceEntryRow }>;
}

/** 服务端目录浏览（只读，仅列目录）；path 空串时浏览推荐根目录。 */
interface FsBrowseResult {
  path: string;
  parent: string | null;
  dirs: { name: string; path: string }[];
}

export async function browseFs(path: string): Promise<FsBrowseResult> {
  const qs = path ? `path=${encodeURIComponent(path)}&` : "";
  const res = await fetch(`${API_BASE}/api/fs/browse?${qs}${tokenQueryFragment()}`);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || res.statusText);
  }
  return (await res.json()) as FsBrowseResult;
}

/** 移出登记（只取消登记，磁盘目录保留）。ref 可传 id 或登记名。 */
export const removeWorkspaceEntry = (ref: string) =>
  del(`/api/v1/workspaces/${encodeURIComponent(ref)}`);

/** 改绑目录（目录被删、项目挪位后的恢复）：id 不变，历史档案不断链。 */
export async function rebindWorkspaceEntry(ref: string, path: string) {
  const res = await fetch(`${API_BASE}/api/v1/workspaces/${encodeURIComponent(ref)}/rebind${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) {
    let detail = "";
    try {
      const data = await res.clone().json();
      if (data && typeof data.message === "string") detail = data.message.trim();
    } catch {
      /* ignore */
    }
    throw new Error(detail || `改绑目录失败（HTTP ${res.status}）`);
  }
  return res.json() as Promise<{ ok: boolean }>;
}


// ---- File system APIs ----

export async function fetchWorkspaceList() {
  const res = await fetch(`${API_BASE}/api/workspace/list${tokenQuery()}`);
  if (!res.ok) throw new Error(`加载工作空间列表失败（HTTP ${res.status}）`);
  return res.json() as Promise<{
    workspaces: Array<{
      name: string;
      summary: string;
      path: string;
      mode: string;
      active: boolean;
      storefront?: string;
      home_view?: string;
      kind?: string;
    }>;
    active_name: string | null;
  }>;
}

interface FileViewEntry {
  name: string;
  type: "dir" | "file";
  size: number;
}

/** 整页文件显示页（/file）数据契约——与 GET /api/workspace/file 对齐。 */
export interface FileViewResponse {
  path: string;
  name: string;
  type: "text" | "image" | "office" | "binary" | "directory";
  /** text/office 的文本内容（text 分页返回当前窗口）。 */
  content?: string;
  /** image 的 base64 数据。 */
  data?: string;
  mime?: string;
  /** 文本语言（文件后缀，如 py/ts/md）。 */
  language?: string;
  total_lines?: number;
  offset?: number;
  limit?: number;
  has_more?: boolean;
  /** directory：子项列表 */
  entries?: FileViewEntry[];
  size: number;
  truncated: boolean;
}

export async function fetchFileView(
  path: string,
  offset = 1,
  limit = 500,
): Promise<FileViewResponse> {
  const params = new URLSearchParams({ path, offset: String(offset), limit: String(limit) });
  const t = tokenQuery();
  const url = `${API_BASE}/api/workspace/file${t ? t + "&" + params : "?" + params}`;
  const res = await fetch(url);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    const err = new Error(text || `Failed to read file: ${res.statusText}`);
    (err as Error & { status?: number }).status = res.status;
    throw err;
  }
  return res.json();
}

/** 文件直出地址（inline 显示；download=true 强制下载）。 */
export function fileRawUrl(path: string, download = false): string {
  const params = new URLSearchParams({ path });
  if (download) params.set("download", "1");
  const qs = [tokenQueryFragment(), params.toString()].filter(Boolean).join("&");
  return `${API_BASE}/api/workspace/file-raw${qs ? "?" + qs : ""}`;
}

export async function switchWorkspace(name: string) {
  const res = await fetch(`${API_BASE}/api/workspace/switch${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) {
    // 后端失败时返回 {"error": "<真实原因>"}——直接透传，不用 HTTP 状态码包装，
    // 让 message.error 能定位到具体失败点（锁冲突/未知空间/会话创建失败等）。
    let detail = "";
    try {
      const data = await res.clone().json();
      if (data && typeof data.error === "string") detail = data.error.trim();
    } catch {
      /* 非 JSON 响应体则忽略 */
    }
    throw new Error(detail || `切换工作空间失败（HTTP ${res.status}）`);
  }
  return res.json() as Promise<{
    name?: string;
    workspace_dir?: string;
    session_id?: string;
    /** 目标空间的权威快照：切完就地提交上屏，不必再发 /api/session/messages。
     *  旧服务端/异常态没有它，端侧回退到「清空 + 等 hydrate」。 */
    snapshot?: SessionSnapshot;
  }>;
}

/** Web 顶栏「新会话」——图形等价 /new（斜杠在 Web 端已拦）。 */
export async function startNewSession() {
  const res = await fetch(`${API_BASE}/api/session/new${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  if (!res.ok) throw new Error(`开始新会话失败（HTTP ${res.status}）`);
  return res.json() as Promise<{ session_id?: string; output?: string; action?: string }>;
}

/** Web 顶栏模型选择——图形等价 /model（斜杠在 Web 端已拦）。 */
export async function switchSessionModel(key: string) {
  const res = await fetch(`${API_BASE}/api/session/model${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key }),
  });
  if (!res.ok) {
    let detail = "";
    try {
      const data = await res.clone().json();
      if (data && typeof data.error === "string") detail = data.error.trim();
    } catch {
      /* ignore */
    }
    throw new Error(detail || `切换模型失败（HTTP ${res.status}）`);
  }
  return res.json() as Promise<{
    provider?: string;
    model?: string;
    deferred?: boolean;
    output?: string;
    action?: string;
  }>;
}

export async function uploadFile(file: File) {
  const formData = new FormData();
  formData.append("file", file);
  const t = tokenQuery();
  const res = await fetch(`${API_BASE}/api/upload${t}`, {
    method: "POST",
    body: formData,
  });
  if (!res.ok) throw new Error(`Upload failed: ${res.statusText}`);
  return res.json() as Promise<{
    files: Array<{ filename: string; size: number; ref: string }>;
  }>;
}

/** 上传附件的 inline 显示地址（图片预览 / 文件下载，token 已带）。
 *  上传文件落在工作区 uploads/ 下（真源 src/core/workspace_layout.py），
 *  经 workspace/file-raw 直出。 */
export function uploadRawUrl(ref: string): string {
  return fileRawUrl(`uploads/${ref}`);
}

/** 最近文件条目（跨来源按时间倒序）。 */
export interface RecentFileEntry {
  ts: number;
  origin: string; // outbound（我发你）/ inbound（你发我）
  end: string; // web / cli / matrix
  workspace_id: string | null;
  name: string;
  mime: string;
  size: number;
  path: string;
  kind: string;
}

export async function fetchRecentFiles(opts?: {
  workspace?: string; // 缺省当前空间；"all" = 全部
  before?: number;
  limit?: number;
}): Promise<{ files: RecentFileEntry[]; has_more: boolean }> {
  const params = new URLSearchParams();
  if (opts?.workspace) params.set("workspace", opts.workspace);
  if (opts?.before != null) params.set("before", String(opts.before));
  params.set("limit", String(opts?.limit ?? 30));
  const t = tokenQuery();
  const url = `${API_BASE}/api/recent-files${t ? t + "&" + params : "?" + params}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`加载最近文件失败（HTTP ${res.status}）`);
  return res.json() as Promise<{ files: RecentFileEntry[]; has_more: boolean }>;
}

/** 子智能体折叠区里的一帧（tool / diff）：形状与实时 WS 帧一致，带 view_seq。 */
export interface SubagentFramePayload {
  type: "tool" | "diff";
  view_seq?: number;
  text?: string;
  ok?: boolean;
  tool_name?: string;
  tool_call_id?: string;
  duration_ms?: number | null;
  diff_lines?: import("./ws").CanonicalDiffLines;
  display_blocks?: import("./ws").DisplayBlock[];
}

/** 快照里的一条消息行（快照接口与切空间响应共用同一形状）。 */
interface SessionSnapshotMessage {
  role: string;
  text: string;
  files?: ChatFileAttachmentPayload[];
  diff?: import("./ws").CanonicalDiffLines;
  /** diff 行的工具归属（产出它的那次工具调用 id）：端上按它把 diff 挂到工具行紧后面 */
  tool_call_id?: string;
  tool?: import("./store").ChatToolLine;
  divider?: string;
  divider_time?: string;
  ts?: number;
  seq?: number;
  /** 端上生成的消息标识（随发送上行、服务端落带原样带回）：
   *  hydrate 对账按它精确配对本端实时气泡，不靠文本猜 */
  client_msg_id?: string;
  /** /compact 等会话变更命令落带的结果行：hydrate 仍渲染为命令卡 */
  is_command_result?: boolean;
  attachments?: {
    ref?: string;
    filename?: string;
    size?: number;
    mime?: string;
    is_image?: boolean;
    url?: string;
  }[];
}

/** 空间视图带快照（权威）：/api/session/messages 与切空间响应同源同形状。
 *
 *  端侧只认这一份事实：内容（messages）与它的身份（epoch/workspace_dir/session_id）、
 *  覆盖范围（latest_seq）、以及不进消息流的两种过程信息（subagent_results /
 *  subagent_diffs）必须一起回来、一起提交。 */
export interface SessionSnapshot {
  messages: SessionSnapshotMessage[];
  total?: number;
  /** 快照覆盖到的最后一帧 view_seq：端侧去重与 gap 检测的基准 */
  latest_seq?: number;
  workspace_dir?: string;
  session_id?: string;
  /** 线的身份（workspace_dir::subject[#世代]）：变了＝整条线重建，必须全量重取 */
  epoch?: string;
  /** 子智能体最终答复（tool_call_id → 文本）：不进消息流，只回填 expand 折叠区 */
  subagent_results?: Record<string, string>;
  /** 子智能体自己的工具行 / diff，按发起它的 delegate call_id 归集 */
  subagent_diffs?: Record<string, SubagentFramePayload[]>;
  /** delegate 任务指令（call_id → 文本）：不进消息流，只回填展开区的「任务指令」组 */
  subagent_briefs?: Record<string, string>;
  /** 折叠区封顶裁剪计数（results/diffs/briefs → 被裁条数）：封顶是边界不是静默
   *  丢失——服务端裁掉的历史子智能体产出在此显式标记，端上可提示「更早过程已省略 N 项」 */
  subagent_truncated?: { results?: number; diffs?: number; briefs?: number };
  /** 该空间此刻的回合态摘要：与消息同源一起回来，刷新与切空间共用同一把尺。
   *  注意：只读预取（带 workspace_dir）模式下它描述的是**调用端自己**的回合态。 */
  runtime?: {
    session_id?: string;
    running?: boolean;
    turn_source?: string;
    workspace_dir?: string;
  };
}

export async function fetchSessionMessages(
  limit = 100,
  afterViewSeq?: number,
  opts?: { workspaceDir?: string; beforeViewSeq?: number },
) {
  const params = new URLSearchParams({ limit: String(limit) });
  // 向前翻页：取 before_view_seq 之前的最近 limit 条（加载更早消息）。before 与
  // after 互斥——before 给了就忽略 after（向前翻页与增量补拉语义相反，不混带）。
  const before = opts?.beforeViewSeq;
  if (before != null && before > 0) {
    params.set("before_view_seq", String(before));
  } else if (afterViewSeq != null && afterViewSeq > 0) {
    // 增量 hydrate：带上已渲染到的游标，只取之后的帧（刷新只补后缀，不重建）
    params.set("after_view_seq", String(afterViewSeq));
  }
  // 只读预取：带 workspace_dir 时读那条线（不切当前会话）。用当前空间自己的路径
  // 请求，等于把「读的是哪条线」显式钉死——并发切空间不会把内容换到别的线上。
  // 服务端对非绝对路径直接 400（与文件工具的绝对路径约定一致），因此只在该参数
  // 确实像绝对路径时才带，免得相对路径把整次快照请求打回。
  const wsDir = String(opts?.workspaceDir ?? "").trim();
  if (wsDir && /^([A-Za-z]:[\\/]|\\\\|\/)/.test(wsDir)) params.set("workspace_dir", wsDir);
  const t = tokenQuery();
  const url = `${API_BASE}/api/session/messages${t ? t + "&" + params : "?" + params}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`加载消息失败（HTTP ${res.status}）`);
  return res.json() as Promise<SessionSnapshot>;
}

/** 模块级独立会话历史（source=web-<subject>，与主聊天同文件隔离）。 */
export async function fetchModuleSessionMessages(subject: string, limit = 100) {
  const params = new URLSearchParams({ limit: String(limit), subject });
  const t = tokenQuery();
  const url = `${API_BASE}/api/module-session/messages${t ? t + "&" + params : "?" + params}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`加载模块消息失败（HTTP ${res.status}）`);
  return res.json() as Promise<{
    messages: Array<{
      role: string;
      text: string;
    }>;
    total: number;
  }>;
}

/** Outbound file metadata persisted with conversation history. */
interface ChatFileAttachmentPayload {
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

/** Slash command descriptor returned by /api/commands. */
export interface CommandInfo {
  name: string;
  description: string;
  category: string;
}

export interface SlashPickerOptionInfo {
  insert: string;
  display: string;
  meta: string;
  submit: boolean;
}

export async function fetchCommandList() {
  const res = await fetch(`${API_BASE}/api/commands${tokenQuery()}`);
  if (!res.ok) throw new Error(`加载命令列表失败（HTTP ${res.status}）`);
  return res.json() as Promise<{
    commands: CommandInfo[];
    workspaces: string[];
    pickers: Record<string, SlashPickerOptionInfo[]>;
    picker_commands: string[];
  }>;
}

/** Persisted tool-activity row from /api/trace/events (sidebar hydrate). */
export interface TraceEventRow {
  type: string;
  timestamp?: string;
  turn_id?: string;
  source?: string;
  origin_scope?: string;
  session_id?: string;
  tool?: string;
  args?: Record<string, unknown>;
  call_id?: string;
  summary?: string;
  ok?: boolean;
  reason?: string;
  text?: string;
  content?: string;
  message?: string;
  display_blocks?: import("./ws").DisplayBlock[];
  diff_lines?: import("./ws").CanonicalDiffLines;
  tool_output?: string;
  tool_output_truncated?: boolean;
  tool_output_ref?: string;
  duration_ms?: number;
  is_error?: boolean;
}

/** Recent tool-activity events for the right-hand StatusSidebar. */
export async function fetchToolActivityEvents(limit = 80) {
  const params = new URLSearchParams({
    limit: String(limit),
    kinds: "tool",
  });
  const t = tokenQuery();
  const url = `${API_BASE}/api/trace/events${t ? t + "&" + params : "?" + params}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`加载工具活动失败（HTTP ${res.status}）`);
  return res.json() as Promise<{ events: TraceEventRow[]; total: number }>;
}

/** Read spilled tool output by ref (paginated lines). */
export async function fetchToolOutput(
  ref: string,
  opts?: { offset?: number; limit?: number; session_id?: string },
) {
  const params = new URLSearchParams();
  if (opts?.offset != null) params.set("offset", String(opts.offset));
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  if (opts?.session_id) params.set("session_id", opts.session_id);
  const q = params.toString();
  const t = tokenQuery();
  const url =
    `${API_BASE}/api/tool-output/${encodeURIComponent(ref)}` +
    (t ? t + (q ? "&" + q : "") : q ? "?" + q : "");
  const res = await fetch(url);
  if (!res.ok) throw new Error(`加载工具输出失败（HTTP ${res.status}）`);
  return res.json() as Promise<{
    ref: string;
    tool_name?: string;
    tool_call_id?: string;
    arguments?: Record<string, unknown>;
    content?: string;
    has_more?: boolean;
    offset?: number;
    limit?: number;
    total_lines?: number;
    next_offset?: number | null;
    truncated?: boolean;
  }>;
}

/** Agent-side record entry as returned by /api/records* (scope=agent). */
export interface RecordEntry {
  id: string;
  type: string;
  scope: string;
  sensitivity: string;
  title: string;
  content: string;
  tags: string[];
  status: string;
  source_type: string;
  created_at: string | null;
  last_accessed: string | null;
  access_count: number;
  path: string;
}

/** User collection entry (scope=user). */
export interface CollectionEntry {
  id: string;
  origin: "user";
  title: string;
  summary: string;
  note: string;
  content: string;
  tags: string[];
  source_type: string;
  source_url: string;
  created_at: string | null;
  last_accessed: string | null;
  access_count: number;
  path: string;
  file_name: string;
  file_rel: string;
  has_file: boolean;
}

type RecordsScope = "agent" | "user";

interface RecordListResponse {
  enabled: boolean;
  scope?: RecordsScope;
  entries: RecordEntry[];
  count: number;
}

interface CollectionListResponse {
  enabled: boolean;
  scope: "user";
  entries: CollectionEntry[];
  count: number;
}

export async function fetchRecordList(opts?: {
  query?: string;
  type?: string;
  includeArchived?: boolean;
  limit?: number;
  scope?: "agent";
}): Promise<RecordListResponse> {
  const params = new URLSearchParams();
  params.set("scope", opts?.scope || "agent");
  if (opts?.query) params.set("query", opts.query);
  if (opts?.type) params.set("type", opts.type);
  if (opts?.includeArchived) params.set("include_archived", "1");
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  const qs = [tokenQueryFragment(), params.toString()].filter(Boolean).join("&");
  const res = await fetch(`${API_BASE}/api/records${qs ? `?${qs}` : ""}`);
  if (!res.ok) throw new Error(`加载记录列表失败（HTTP ${res.status}）`);
  return res.json();
}

export async function fetchCollectionList(opts?: {
  query?: string;
  limit?: number;
}): Promise<CollectionListResponse> {
  const params = new URLSearchParams({ scope: "user" });
  if (opts?.query) params.set("query", opts.query);
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  const qs = [tokenQueryFragment(), params.toString()].filter(Boolean).join("&");
  const res = await fetch(`${API_BASE}/api/records${qs ? `?${qs}` : ""}`);
  if (!res.ok) throw new Error(`加载收藏列表失败（HTTP ${res.status}）`);
  return res.json();
}

export async function fetchRecordEntry(
  id: string,
): Promise<{ enabled: boolean; scope?: string; entry: RecordEntry }> {
  const params = new URLSearchParams({ id, scope: "agent" });
  const qs = [tokenQueryFragment(), params.toString()].filter(Boolean).join("&");
  const res = await fetch(`${API_BASE}/api/records/item${qs ? `?${qs}` : ""}`);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `Failed to fetch record: ${res.statusText}`);
  }
  return res.json();
}

export async function fetchCollectionEntry(
  id: string,
): Promise<{ enabled: boolean; scope: "user"; entry: CollectionEntry }> {
  const params = new URLSearchParams({ id, scope: "user" });
  const qs = [tokenQueryFragment(), params.toString()].filter(Boolean).join("&");
  const res = await fetch(`${API_BASE}/api/records/item${qs ? `?${qs}` : ""}`);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `Failed to fetch collection: ${res.statusText}`);
  }
  return res.json();
}

/** 取消用户收藏（scope=user 收藏条目的删除，手机端 recordsDelete(scope="user") 同款落点）。 */
export async function deleteCollection(id: string): Promise<void> {
  return deleteRecord(id, "user");
}

export async function deleteRecord(id: string, scope: RecordsScope = "agent"): Promise<void> {
  const params = new URLSearchParams({ id, scope });
  const qs = [tokenQueryFragment(), params.toString()].filter(Boolean).join("&");
  const res = await fetch(`${API_BASE}/api/records/item${qs ? `?${qs}` : ""}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `Failed to delete record: ${res.statusText}`);
  }
}

export function collectionFileUrl(id: string): string {
  const params = new URLSearchParams({ id });
  const qs = [tokenQueryFragment(), params.toString()].filter(Boolean).join("&");
  return `${API_BASE}/api/records/file${qs ? `?${qs}` : ""}`;
}

export async function archiveRecord(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/records/archive${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `Failed to archive record: ${res.statusText}`);
  }
}

export async function unarchiveRecord(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/records/unarchive${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `Failed to unarchive record: ${res.statusText}`);
  }
}

/** 工作区文件收藏（手点；落盘 records/user/files/）。 */
export async function collectFile(
  path: string,
  note?: string,
): Promise<{ ok: boolean; message: string; id?: string }> {
  const res = await fetch(`${API_BASE}/api/records/collect-file${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, note: note || undefined }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `收藏文件失败：${res.statusText}`);
  }
  return res.json();
}

/** Token totals shared by dashboard aggregates. */
export interface UsageTokenTotals {
  llm_turns: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  /** 0–1；缓存命中输入 / 总输入 */
  cache_hit_rate: number;
  /** 费用（元）：按配置价格估算；未配价模型为 0 */
  cost_miss?: number;
  cost_hit?: number;
  cost_out?: number;
  cost_total?: number;
  // ---- 后端单源展示字段（src/runtime/usage_display.py），前端只渲染不再格式化 ----
  input_display?: string;
  output_display?: string;
  cache_read_display?: string;
  reasoning_display?: string;
  cost_total_display?: string;
  cost_miss_display?: string;
  cost_hit_display?: string;
  cost_out_display?: string;
  /** 命中率文案，如 "64.8%" */
  cache_hit_display?: string;
  /** 0–100 一位小数，供 Progress 宽度 */
  cache_hit_pct?: number;
  /** 命中率配色档位：≥50% good */
  cache_hit_level?: "good" | "normal";
  /** 费用单元格状态：priced / unpriced（有输入未配价）/ zero */
  cost_state?: "priced" | "unpriced" | "zero";
  has_cost_breakdown?: boolean;
}

export interface UsageAgentKindRow extends UsageTokenTotals {
  agent_kind: string;
  label: string;
}

export interface UsageModelRow extends UsageTokenTotals {
  model_key: string;
  label: string;
}

export interface UsageWorkspaceRow extends UsageTokenTotals {
  workspace_id: string;
  workspace_name: string;
}

export interface UsageDayRow extends UsageTokenTotals {
  day: string;
}

export interface UsageDashboardResponse {
  days: number;
  workspace_filter: string | null;
  note: string;
  window: { from: string | null; to: string | null; from_display?: string; to_display?: string };
  totals: UsageTokenTotals;
  by_agent_kind: UsageAgentKindRow[];
  by_model: UsageModelRow[];
  by_workspace: UsageWorkspaceRow[];
  by_day: UsageDayRow[];
  workspaces: Array<{ id: string; name: string }>;
}

export async function fetchUsageDashboard(opts?: {
  days?: number;
  workspaceId?: string;
}): Promise<UsageDashboardResponse> {
  const params = new URLSearchParams();
  if (opts?.days != null) params.set("days", String(opts.days));
  if (opts?.workspaceId) params.set("workspace_id", opts.workspaceId);
  // recent rows are unused by the board; keep payload small
  params.set("recent_limit", "1");
  const qs = [tokenQueryFragment(), params.toString()].filter(Boolean).join("&");
  const res = await fetch(`${API_BASE}/api/usage/dashboard${qs ? `?${qs}` : ""}`);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `Failed to fetch usage dashboard: ${res.statusText}`);
  }
  return res.json();
}

// ---- 模型价格（用量页展示 + 行内编辑；写入独立价格覆盖文件，估算随之更新） ----

export interface UsagePricingEntry {
  model_key: string;
  provider: string;
  model: string;
  pricing: {
    input: number | null;
    cache_hit: number | null;
    output: number | null;
  };
  source: "config" | "override";
}

interface UsagePricingResponse {
  models: UsagePricingEntry[];
}

export async function fetchUsagePricing(): Promise<UsagePricingResponse> {
  const qs = tokenQueryFragment();
  const res = await fetch(`${API_BASE}/api/usage/pricing${qs ? `?${qs}` : ""}`);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `Failed to fetch usage pricing: ${res.statusText}`);
  }
  return res.json();
}

export async function updateUsagePricing(opts: {
  provider: string;
  model: string;
  pricing: Partial<UsagePricingEntry["pricing"]> | null;
}): Promise<{ ok: boolean; model_key: string }> {
  const qs = tokenQueryFragment();
  const res = await fetch(`${API_BASE}/api/usage/pricing${qs ? `?${qs}` : ""}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider: opts.provider, model: opts.model, pricing: opts.pricing }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `Failed to update usage pricing: ${res.statusText}`);
  }
  return res.json();
}

// ---- 消息中心（工作空间动态收件箱） ----

export type UpdateSalience = "low" | "normal" | "high";
export type UpdateStatus = "unread" | "read" | "archived";
type UpdateDisposition = "pending" | "elevated" | "resolved" | "dismissed";

interface ReviewEntry {
  by: string;
  at: string;
  action: string;
  note?: string;
}

export interface UpdateItem {
  message_id: string;
  workspace: string;
  source_id: string;
  event_type: string;
  type: string;
  payload_ref?: string | null;
  status: UpdateStatus;
  title: string;
  text: string;
  display_text?: string;
  salience: UpdateSalience;
  handle_mode?: string;
  source_kind?: string;
  disposition?: UpdateDisposition;
  reviewed_by?: string;
  reviewed_at?: string | null;
  review_note?: string;
  review_history?: ReviewEntry[];
  salience_by?: string;
  salience_at?: string | null;
  payload?: Record<string, unknown>;
  created_at: string;
  read_at?: string | null;
  archived_at?: string | null;
}

interface UpdatesSummary {
  unread: Record<string, number>;
  pending: number;
}

async function fetchUpdates(path: string): Promise<Response> {
  const res = await fetch(`${API_BASE}${path}${path.includes("?") ? "&" : "?"}${tokenQueryFragment()}`);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `请求失败: ${res.statusText}`);
  }
  return res;
}

async function postUpdates(path: string, body: unknown) {
  const res = await fetch(`${API_BASE}${path}${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `操作失败: ${res.statusText}`);
  }
  return res.json();
}

export async function fetchUpdatesSummary(): Promise<UpdatesSummary> {
  const res = await fetchUpdates("/api/updates/summary");
  return res.json();
}

export async function fetchUpdatesList(opts?: {
  workspace?: string;
  status?: UpdateStatus | "all";
  salience?: UpdateSalience;
  limit?: number;
}): Promise<{ items: UpdateItem[] }> {
  const params = new URLSearchParams();
  if (opts?.workspace) params.set("workspace", opts.workspace);
  if (opts?.status) params.set("status", opts.status);
  if (opts?.salience) params.set("salience", opts.salience);
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  const res = await fetchUpdates(`/api/updates/list?${params.toString()}`);
  return res.json();
}

export function markUpdateRead(messageId: string) {
  return postUpdates("/api/updates/read", { message_id: messageId });
}

export function archiveUpdate(messageId: string) {
  return postUpdates("/api/updates/archive", { message_id: messageId });
}

export function reviewUpdate(
  messageId: string,
  text: string
): Promise<{ ok: boolean; delivered: string; workspace: string }> {
  return postUpdates("/api/updates/review", { message_id: messageId, text });
}

/* ── Workflow 编辑器 API ─────────────────────────────────────────────
 * 画布纯 UI：草案 CRUD + WDL parse/emit + 模板 + 活跃草案上报。
 * 执行层（实例 run/cancel/resume、节点模型设置）已剥离为独立 wdl 软件，
 * 前端一律不接。
 * ─────────────────────────────────────────────────────────────────── */

export async function fetchWorkflowDrafts() {
  const res = await fetch(`${API_BASE}/api/workflow-drafts${tokenQuery()}`);
  if (!res.ok) throw new Error(`加载工作流草案失败（HTTP ${res.status}）`);
  return res.json() as Promise<{
    drafts: Array<{ draft_id: string; name: string; updated_at: string }>;
  }>;
}

/** 创建空草案（工作台常驻编辑器）。 */
export async function createWorkflowDraft(name = "workbench") {
  const res = await fetch(`${API_BASE}/api/workflow-drafts${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) throw new Error(`新建工作流草案失败（HTTP ${res.status}）`);
  return res.json() as Promise<{ draft_id: string; name: string; wdl: string }>;
}

/** 上报当前编辑器/工作台打开的草案 id——构建对话编排写穿时复用这份草案，而不是新开一份。 */
export async function reportActiveWorkflowDraft(draftId: string | null): Promise<void> {
  try {
    await fetch(`${API_BASE}/api/workflow-active-draft${tokenQuery()}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft_id: draftId }),
    });
  } catch {
    /* best-effort：上报失败只是写穿退回新建草案，不影响编辑 */
  }
}

/** 内核图（kernel projection）节点：唯一节点类型是智能体。
 *  与后端 /api/workflow-wdl/parse 返回的 document.nodes[id] 对齐（core/model.py Node）。 */
export interface KernelNodeSpec {
  task: string;
  input: string;
  routes: "all" | "one";
  /** 节点级激活上限（覆写全局）；缺省 = 继承全局 */
  max_activations?: number;
  /** 节点级 LLM 覆盖；缺省 = 跟随图级/全局「节点模型」配置 */
  provider?: string;
  model?: string;
}

/** 内核图边：from → to；on 缺省 success，error 表示失败时投递（重试/兜底路由）。 */
export interface KernelEdgeSpec {
  from: string;
  to: string;
  on?: "success" | "error";
}

/** 内核图文档：POST /api/workflow-wdl/parse 的 document 与 /emit 的入参同构。 */
export interface KernelDocument {
  name?: string;
  description?: string;
  schedule?: Record<string, unknown>;
  /** 全局激活上限（缺省 100） */
  max_activations?: number;
  nodes: Record<string, KernelNodeSpec>;
  edges: KernelEdgeSpec[];
}

export interface WorkflowDraft {
  draft_id: string;
  name: string;
  wdl: string;
  updated_at?: string;
  [key: string]: unknown;
}

async function _extractError(res: Response, fallback: string): Promise<Error> {
  try {
    const body = await res.json();
    if (body?.error) return new Error(String(body.error));
  } catch {
    /* ignore JSON parse failures */
  }
  return new Error(fallback);
}

export async function fetchWorkflowDraft(draftId: string): Promise<WorkflowDraft> {
  const res = await fetch(
    `${API_BASE}/api/workflow-drafts/${encodeURIComponent(draftId)}${tokenQuery()}`,
  );
  if (!res.ok) throw await _extractError(res, `Failed to fetch draft: ${res.statusText}`);
  return res.json();
}

/** 共同编辑冲突（409）：草案已被会话侧推进，客户端需刷新基线 */
export class DraftConflictError extends Error {
  constructor(
    message: string,
    public readonly updatedAt: string,
  ) {
    super(message);
    this.name = "DraftConflictError";
  }
}

export async function saveWorkflowDraft(
  draftId: string,
  wdl: string,
  baseUpdatedAt?: string,
): Promise<WorkflowDraft> {
  const res = await fetch(
    `${API_BASE}/api/workflow-drafts/${encodeURIComponent(draftId)}${tokenQuery()}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(baseUpdatedAt ? { wdl, base_updated_at: baseUpdatedAt } : { wdl }),
    },
  );
  if (res.status === 409) {
    let message = "草案已被会话侧更新，画布需刷新后再编辑";
    let updatedAt = "";
    try {
      const body = await res.json();
      if (body?.error) message = String(body.error);
      if (body?.updated_at) updatedAt = String(body.updated_at);
    } catch {
      /* keep defaults */
    }
    throw new DraftConflictError(message, updatedAt);
  }
  if (!res.ok) throw await _extractError(res, `Failed to save draft: ${res.statusText}`);
  return res.json();
}

export async function deleteWorkflowDraft(draftId: string): Promise<{ deleted: boolean }> {
  const res = await fetch(
    `${API_BASE}/api/workflow-drafts/${encodeURIComponent(draftId)}${tokenQuery()}`,
    { method: "DELETE" },
  );
  if (!res.ok) throw await _extractError(res, `Failed to delete draft: ${res.statusText}`);
  return res.json();
}

interface WdlParseResult {
  document: KernelDocument;
  warnings?: string[];
}

export async function parseWdl(wdl: string): Promise<WdlParseResult> {
  const res = await fetch(`${API_BASE}/api/workflow-wdl/parse${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wdl }),
  });
  if (!res.ok) throw await _extractError(res, `Failed to parse WDL: ${res.statusText}`);
  return res.json();
}

interface WdlEmitResult {
  wdl: string;
}

export async function emitWdl(document: KernelDocument): Promise<WdlEmitResult> {
  const res = await fetch(`${API_BASE}/api/workflow-wdl/emit${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ document }),
  });
  if (!res.ok) throw await _extractError(res, `Failed to emit WDL: ${res.statusText}`);
  return res.json();
}

