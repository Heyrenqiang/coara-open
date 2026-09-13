// WebSocket client — WDL 工作台引擎事件通道（wdl/server.py /ws）。
//
// 服务端把 WdlEngine.on_event 的进度事件原样转发：
// workflow_started / node_started / node_completed / node_failed /
// workflow_completed / workflow_failed / workflow_cancelled。

/** 画布节点实时状态（引擎事件投影）。 */
export type FlowLiveNodeStatus = "pending" | "running" | "done" | "failed";

export interface FlowLiveNode {
  id: string;
  status: FlowLiveNodeStatus;
  task: string;
  result: string;
  /** Fan-in activation count. */
  activations?: number;
  agent_id?: string;
  /** Last failure message when status is failed. */
  error?: string;
}

/** 引擎事件帧。payload 是引擎原负载（task_id/step_id/error/context 等）。 */
export type ServerMessage = {
  type: string;
  instance_id?: string;
  payload?: Record<string, unknown>;
};

export type ClientMessage = { type: "ping" };

type MessageHandler = (msg: ServerMessage) => void;
type ConnectionHandler = (connected: boolean, error?: string | null) => void;

// Reconnect tuning: exponential backoff (1s → 30s cap) with ±30% jitter.
const INITIAL_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;

export function reconnectDelayMs(attempts: number, rand: () => number = Math.random): number {
  const base = Math.min(INITIAL_BACKOFF_MS * 2 ** attempts, MAX_BACKOFF_MS);
  const delay = Math.round(base * (0.7 + rand() * 0.6));
  return Math.min(delay, MAX_BACKOFF_MS);
}

export class WdlWS {
  private ws: WebSocket | null = null;
  private url: string;
  private handlers: Set<MessageHandler> = new Set();
  private connHandlers: Set<ConnectionHandler> = new Set();
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  private _connected = false;
  private _connError: string | null = null;

  constructor(url: string) {
    this.url = url;
  }

  get connected(): boolean {
    return this._connected;
  }

  connect(): void {
    this.closed = false;
    this._doConnect();
  }

  private _doConnect(): void {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    this.url = this.url || `${proto}//${window.location.host}/ws`;
    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      this.reconnectAttempts = 0;
      this._connected = true;
      this._connError = null;
      this._notifyConn(true);
      console.debug("[wdl-ws] connected");
    };

    this.ws.onmessage = (event: MessageEvent) => {
      try {
        const msg = JSON.parse(event.data) as ServerMessage;
        this.handlers.forEach((h) => h(msg));
      } catch (e) {
        console.error("[wdl-ws] failed to parse message:", e);
      }
    };

    this.ws.onclose = () => {
      this._connected = false;
      if (this.closed) {
        this._notifyConn(false);
        return;
      }
      this._connError = "连接已断开，正在自动重连…";
      this._notifyConn(false, this._connError);
      this._scheduleReconnect();
    };

    this.ws.onerror = (e) => {
      console.error("[wdl-ws] error:", e);
    };
  }

  private _scheduleReconnect(): void {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    const delay = reconnectDelayMs(this.reconnectAttempts);
    this.reconnectAttempts++;
    this.reconnectTimer = setTimeout(() => this._doConnect(), delay);
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
    this.ws?.close();
    this.ws = null;
  }
}

// Singleton — one WS connection for the entire app.
let _instance: WdlWS | null = null;

export function getWS(): WdlWS {
  if (!_instance) {
    _instance = new WdlWS("");
    _instance.connect();
  }
  return _instance;
}
