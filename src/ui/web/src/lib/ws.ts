import { clearAuthToken, getAuthToken } from "./auth";

/** tool_complete 的 diff 展示块。 */
export interface DisplayBlock {
  kind: "diff";
  path: string;
  old_text: string;
  new_text: string;
  old_start: number;
  new_start: number;
  is_summary: boolean;
  is_new_file: boolean;
}

/** 内核算好的 diff 行（前端直接渲染）。 */
export interface CanonicalDiffLines {
  path: string;
  added: number;
  removed: number;
  hunks: { kind: "add" | "delete" | "context"; oldNum: number; newNum: number; code: string }[][];
}

export type FlowLiveNodeStatus = "pending" | "running" | "done" | "failed";

export interface FlowLiveNode {
  id: string;
  status: FlowLiveNodeStatus;
  task: string;
  result: string;
  activations?: number;
  agent_id?: string;
  error?: string;
}

interface FlowLiveEdge {
  from: string;
  to: string;
  on?: string;
}

export interface FlowGraphSnapshot {
  name: string;
  schedule: unknown | null;
  hops: number;
  nodes: FlowLiveNode[];
  edges: FlowLiveEdge[];
  wdl?: string;
}

export type ServerMessage =
  | { type: "turn_start"; turn_id: string; source?: string; subject?: string; replayed?: boolean; workspace_dir?: string; session_id?: string }
  | { type: "turn_queued"; turn_id: string; source?: string; subject?: string; replayed?: boolean; workspace_dir?: string; session_id?: string }
  | { type: "chunk"; text: string; source?: string; subject?: string; replayed?: boolean; workspace_dir?: string; session_id?: string }
  | { type: "turn_end"; turn_id: string; reason: "complete" | "interrupted" | "error"; error?: string; source?: string; subject?: string; replayed?: boolean; workspace_dir?: string; session_id?: string }
  | { type: "command_result"; result: CommandResult; subject?: string; workspace_dir?: string; session_id?: string; view_seq?: number }
  | { type: "approval_request"; approval_id: string; question: string; options: ApprovalOption[]; timeout_s?: number; workspace?: string; created_at_ms?: number }
  | { type: "approval_resolved"; approval_id: string; outcome?: string }
  | { type: "tool_start"; tool: string; call_id: string; args?: Record<string, unknown>; origin_scope?: string; session_id?: string; subject?: string }
  | { type: "tool_call"; tool: string; args: Record<string, unknown>; call_id: string; origin_scope?: string; session_id?: string; subject?: string }
  | { type: "tool_result"; call_id: string; summary: string; ok: boolean; origin_scope?: string; session_id?: string; subject?: string; tool?: string }
  | { type: "tool_complete"; call_id: string; display_blocks?: DisplayBlock[]; diff_lines?: CanonicalDiffLines; tool_output?: string; tool_output_truncated?: boolean; tool_output_ref?: string; origin_scope?: string; session_id?: string; subject?: string; source?: string; detached?: boolean }
  | { type: "diff"; diff_lines?: CanonicalDiffLines; display_blocks?: DisplayBlock[]; tool_name?: string; source?: string; subject?: string; replayed?: boolean }
  | { type: "tool"; text: string; ok?: boolean; tool_name?: string; tool_call_id?: string; duration_ms?: number | null; source?: string; subject?: string; replayed?: boolean }
  | { type: "trace_batch"; events: ServerMessage[] }
  | { type: "subagent_chunk"; text: string; tool_call_id?: string; coara_id?: string; subagent_id?: string; workspace_dir?: string; session_id?: string }
  | { type: "subagent_result"; text: string; tool_call_id?: string; coara_id?: string; workspace_dir?: string; session_id?: string }
  | { type: "user_message"; content: string; source?: string; turn_id?: string; replayed?: boolean; workspace_dir?: string; session_id?: string }
  | { type: "chat_chunk"; text: string; turn_id?: string; source?: string }
  | { type: "chat_turn_retracted"; turn_id?: string; reason?: string; session_id?: string }
  | { type: "llm_turn_start"; turn_id?: string; iteration?: number; origin_scope?: string; session_id?: string }
  | { type: "llm_switched"; provider?: string; model?: string; provider_name?: string; model_name?: string; origin_source?: string; session_id?: string }
  | { type: "session_auto_new"; message: string; session_id?: string; reason?: string }
  | { type: "continuation_input_injected"; user_texts?: string[]; user_sources?: string[]; count?: number; source?: string }
  | { type: "error"; message: string; subject?: string }
  | { type: "state"; data: unknown }
  | { type: "info"; text: string }
  | { type: "focus_window"; path?: string }
  | { type: "workspaces_changed"; action?: string; workspace_name?: string }
  | { type: "providers_changed" }
  | { type: "open_workflow_editor"; draft_id: string; workflow_name?: string }
  | { type: "workflow_draft_updated"; draft_id: string; workflow_name?: string }
  | { type: "flow_graph_snapshot"; flow: string; snapshot: FlowGraphSnapshot | null; subject?: string }
  | {
      type: "flow_graph_changed";
      flow?: string;
      action?: string;
      node_id?: string;
      depends_on?: string[];
      routes_to?: string[];
      status?: string;
      subject?: string;
      wdl?: string;
    }
  | {
      type: "subagent_start";
      subagent_type?: string;
      subagent_id?: string;
      description?: string;
      /** pending＝停车；running＝已起跳 */
      status?: string;
      child_coara_id?: string;
      child_session_id?: string;
      session_id?: string;
      subject?: string;
      workspace_dir?: string;
    }
  | {
      type: "subagent_complete";
      subagent_type?: string;
      subagent_id?: string;
      description?: string;
      child_coara_id?: string;
      child_session_id?: string;
      session_id?: string;
      subject?: string;
      workspace_dir?: string;
    }
  | {
      type: "subagent_failed";
      subagent_type?: string;
      subagent_id?: string;
      description?: string;
      error?: string;
      child_coara_id?: string;
      child_session_id?: string;
      session_id?: string;
      subject?: string;
      workspace_dir?: string;
    }
  | {
      type: "background_agent_start";
      task_id?: string;
      subagent_type?: string;
      description?: string;
      child_coara_id?: string;
      parent_tool_call_id?: string;
      parent_activity_id?: string;
      session_id?: string;
      subject?: string;
      workspace_dir?: string;
    }
  | {
      type: "background_agent_complete";
      task_id?: string;
      subagent_type?: string;
      description?: string;
      has_error?: boolean;
      error?: string;
      result_preview?: string;
      child_coara_id?: string;
      origin_source?: string;
      origin_channel?: string;
      session_id?: string;
      subject?: string;
      workspace_dir?: string;
    }
  | {
      type: "background_task_complete";
      task_id?: string;
      kind?: string;
      status?: string;
      description?: string;
      terminal_reason?: string;
      has_error?: boolean;
      error?: string;
      exit_code?: number;
      result_preview?: string;
      result_full?: string;
      log_path?: string;
      origin_source?: string;
      session_id?: string;
      subject?: string;
      workspace_dir?: string;
    }
  | {
      type: "file";
      file_id: string;
      url: string;
      path?: string;
      filename: string;
      mime: string;
      size: number;
      caption?: string;
      is_image?: boolean;
      is_video?: boolean;
      is_audio?: boolean;
    }
  | { type: "pong" };

type ClientMessage =
  | { type: "chat"; text: string; image_refs?: string[]; file_refs?: string[]; subject?: string; workspace_dir?: string; client_msg_id?: string }
  | { type: "interrupt"; subject?: string; workspace_dir?: string }
  | { type: "command"; text: string; subject?: string; workspace_dir?: string }
  | { type: "approval_reply"; approval_id: string; approved: boolean }
  | { type: "ping" }
  | { type: "flow_snapshot"; flow: string; subject?: string };

export interface CommandResult {
  output: string;
  action: "none" | "new_session" | "switch_workspace" | "restart" | "exit";
  data: Record<string, unknown>;
  exit_session: boolean;
}

interface ApprovalOption {
  label: string;
  description: string;
}

type MessageHandler = (msg: ServerMessage) => void;
type ConnectionHandler = (connected: boolean, error?: string | null) => void;

const INITIAL_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;

/** 重连退避（0-based），含 ±30% jitter，封顶 30s。 */
export function reconnectDelayMs(attempts: number, rand: () => number = Math.random): number {
  const base = Math.min(INITIAL_BACKOFF_MS * 2 ** attempts, MAX_BACKOFF_MS);
  const delay = Math.round(base * (0.7 + rand() * 0.6));
  return Math.min(delay, MAX_BACKOFF_MS);
}

const PING_INTERVAL_MS = 20_000;
const STALE_TIMEOUT_MS = 60_000;
/** 回前台快检超时（休眠时心跳计时器冻结）。 */
const WAKE_PROBE_TIMEOUT_MS = 3_000;

const MAX_OUTBOX = 100;
const AUTH_FAILURE_CODES = new Set([1008, 4001, 4002, 4003]);
const SUPERSEDED_CLOSE_CODE = 4000;
const SUPERSEDED_ERROR = "界面已在另一标签页打开，本标签页已断开";

const NO_TOKEN_ERROR = "未授权：请从 coara 启动入口（或桌面图标）打开 Web 界面";
const AUTH_FAILURE_ERROR = "认证失败，请从 coara 启动入口（或桌面图标）重新打开";

class CoaraWS {
  private ws: WebSocket | null = null;
  private url: string;
  private handlers: Set<MessageHandler> = new Set();
  private connHandlers: Set<ConnectionHandler> = new Set();
  private outboxHandlers: Set<(count: number) => void> = new Set();
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private lastMessageAt = 0;
  private outbox: ClientMessage[] = [];
  private closed = false;
  private _connected = false;
  private _connError: string | null = null;
  private _presenceListener: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
  }

  get connected(): boolean {
    return this._connected;
  }

  isConnected(): boolean {
    return this._connected;
  }

  /** superseded / disconnect 后不占单标签坑。 */
  isClosed(): boolean {
    return this.closed;
  }

  connect(): void {
    this.closed = false;
    this._installPresenceProbe();
    this._doConnect();
  }

  /** 回前台：立即 ping，短超时无响应则重连。 */
  private _installPresenceProbe(): void {
    if (this._presenceListener || typeof document === "undefined") return;
    const probe = () => this._probeLiveness();
    const onVisible = () => {
      if (document.visibilityState === "visible") probe();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("pageshow", probe);
    this._presenceListener = () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("pageshow", probe);
    };
  }

  private _probeLiveness(): void {
    if (this.closed) return;
    if (this.ws?.readyState !== WebSocket.OPEN) {
      if (!this._connected) this.reconnectNow();
      return;
    }
    if (Date.now() - this.lastMessageAt <= WAKE_PROBE_TIMEOUT_MS) return;
    const since = this.lastMessageAt;
    this.send({ type: "ping" });
    window.setTimeout(() => {
      if (this.closed || this.ws?.readyState !== WebSocket.OPEN) return;
      if (this.lastMessageAt <= since) {
        console.warn("[coara-ws] wake probe timed out, reconnecting");
        this.ws.close();
      }
    }, WAKE_PROBE_TIMEOUT_MS);
  }

  private _buildWsUrl(): string {
    const token = getAuthToken();
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}/ws${token ? `?token=${encodeURIComponent(token)}` : ""}`;
  }

  private _failAuth(message: string): void {
    clearAuthToken();
    this._connError = message;
    this._notifyConn(false, this._connError);
  }

  private _doConnect(): void {
    if (!getAuthToken()) {
      this._failAuth(NO_TOKEN_ERROR);
      return;
    }
    this.url = this._buildWsUrl();
    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      this.reconnectAttempts = 0;
      this._connected = true;
      this._connError = null;
      this.lastMessageAt = Date.now();
      this._notifyConn(true);
      console.debug("[coara-ws] connected");
      this._flushOutbox();
      this._startHeartbeat();
    };

    this.ws.onmessage = (event: MessageEvent) => {
      this.lastMessageAt = Date.now();
      try {
        const msg = JSON.parse(event.data) as ServerMessage;
        this.handlers.forEach((h) => h(msg));
      } catch (e) {
        console.error("[coara-ws] failed to parse message:", e);
      }
    };

    this.ws.onclose = (event: CloseEvent) => {
      this._stopHeartbeat();
      this._connected = false;
      if (this.closed) {
        this._notifyConn(false);
        return;
      }
      if (event.code === SUPERSEDED_CLOSE_CODE) {
        this.closed = true;
        this._connError = SUPERSEDED_ERROR;
        this._notifyConn(false, this._connError);
        console.debug("[coara-ws] superseded by another tab (close code 4000)");
        return;
      }
      if (AUTH_FAILURE_CODES.has(event.code)) {
        this._failAuth(AUTH_FAILURE_ERROR);
        console.error(`[coara-ws] auth failure (close code ${event.code}), not reconnecting`);
        return;
      }
      this._connError = "连接已断开，正在自动重连…";
      this._notifyConn(false, this._connError);
      console.debug("[coara-ws] disconnected, reconnecting...");
      this._scheduleReconnect();
    };

    this.ws.onerror = (e) => {
      console.error("[coara-ws] error:", e);
    };
  }

  private _scheduleReconnect(): void {
    if (!getAuthToken()) {
      this._failAuth(NO_TOKEN_ERROR);
      return;
    }
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    const delay = reconnectDelayMs(this.reconnectAttempts);
    this.reconnectAttempts++;
    this.reconnectTimer = setTimeout(() => this._doConnect(), delay);
  }

  /** 立即重连：清退避，拆旧 socket 防重复 onclose。 */
  reconnectNow(): void {
    if (!getAuthToken()) {
      this._failAuth(NO_TOKEN_ERROR);
      return;
    }
    if (this.closed) {
      this.connect();
      return;
    }
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.reconnectAttempts = 0;
    const stale = this.ws;
    this.ws = null;
    if (stale) {
      stale.onopen = null;
      stale.onmessage = null;
      stale.onclose = null;
      stale.onerror = null;
      try {
        stale.close();
      } catch {
        // ignore — best effort
      }
    }
    this._connError = null;
    this._doConnect();
  }

  private _startHeartbeat(): void {
    this._stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (this.ws?.readyState !== WebSocket.OPEN) return;
      if (Date.now() - this.lastMessageAt > STALE_TIMEOUT_MS) {
        console.warn("[coara-ws] no server message within staleness window, reconnecting");
        this.ws.close();
        return;
      }
      this.send({ type: "ping" });
    }, PING_INTERVAL_MS);
  }

  private _stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private _flushOutbox(): void {
    if (this.outbox.length === 0) return;
    const queued = this.outbox;
    this.outbox = [];
    this._notifyOutbox();
    for (const msg of queued) this.send(msg);
  }

  send(msg: ClientMessage): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
      return;
    }
    if (this.outbox.length >= MAX_OUTBOX) {
      const dropped = this.outbox.shift();
      console.warn("[coara-ws] outbox full, dropping oldest message:", dropped?.type);
    }
    this.outbox.push(msg);
    this._notifyOutbox();
  }

  get outboxSize(): number {
    return this.outbox.length;
  }

  onOutbox(handler: (count: number) => void): () => void {
    this.outboxHandlers.add(handler);
    handler(this.outbox.length);
    return () => this.outboxHandlers.delete(handler);
  }

  private _notifyOutbox(): void {
    const n = this.outbox.length;
    this.outboxHandlers.forEach((h) => h(n));
  }

  onMessage(handler: MessageHandler): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  onConnection(handler: ConnectionHandler): () => void {
    this.connHandlers.add(handler);
    handler(this._connected, this._connError);
    return () => this.connHandlers.delete(handler);
  }

  private _notifyConn(connected: boolean, error: string | null = null): void {
    this.connHandlers.forEach((h) => h(connected, error));
  }

  close(): void {
    this.closed = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this._stopHeartbeat();
    if (this._presenceListener) {
      this._presenceListener();
      this._presenceListener = null;
    }
    this.outbox = [];
    this._notifyOutbox();
    this.ws?.close();
    this.ws = null;
  }
}

let _instance: CoaraWS | null = null;

export function getWS(): CoaraWS {
  if (!_instance) {
    _instance = new CoaraWS("");
    _instance.connect();
  }
  return _instance;
}
