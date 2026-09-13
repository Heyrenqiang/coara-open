/** web 端子智能体活动树状态机：CLI ActivityLiveTracker 的 TS 移植。
 *
 * 数据源：WS trace_batch 里的 subagent_* / tool_start / tool_complete 事件
 * （payload 全字段透传，subagent_id / child_coara_id / parent_tool_call_id /
 * coara_id / delegate_mode 齐备）。
 *
 * 与 traceEvents 环形缓冲（MAX 200）解耦：树状态独立维护（节点数远小于
 * 事件数），长回合大量工具事件不会把 subagent_start 挤出导致树建不全。
 *
 * 渲染语义对齐 CLI：
 * - subagent 节点挂到发起它的 delegate 工具行下（parent_tool_call_id）
 * - 子智能体的工具事件经 coara_id → subagent_id 映射挂到子智能体节点下
 * - delegate(wait) 不建行；delegate 行的工具子节点不显示
 * - tool_complete(foreground_async/background delegate) 只是 spawn ack，不关行
 * - janitor/daily 完全静默（建行但记 coara_id，防其工具行漏出）
 * - 只有 complete 没有 start（缓冲丢帧）时合成占位节点，树仍能收敛
 */

/** 与 CLI builtin_agents.CLI_SILENT_SUBAGENT_TYPES 对齐：系统维护 agent 静默。 */
const SILENT_SUBAGENT_TYPES = new Set(["janitor", "daily"]);

export interface TreeNode {
  nodeId: string;
  parentId: string;
  label: string;
  /** subagent / background / delegate / tool(=具体工具名) */
  kind: string;
  coaraId: string;
  subagentType: string;
  /** "" = 运行中；"pending" = 停车（flow 未启动节点，灰显） */
  status: string;
  active: boolean;
  isError: boolean;
  order: number;
  startedAt: number;
}

export interface TreeRow {
  nodeId: string;
  depth: number;
  label: string;
  kind: string;
  active: boolean;
  isError: boolean;
  pending: boolean;
  /** 节点创建时刻（ms），delegate 行已用时间括注用。 */
  startedAt: number;
}

/** 事件帧（trace_batch 元素）的最小结构——宽松读取，缺字段按空串处理。 */
export interface TreeEvent {
  type?: string;
  event_type?: string;
  coara_id?: string;
  message?: string;
  [key: string]: unknown;
}

function str(v: unknown): string {
  return typeof v === "string" ? v : v == null ? "" : String(v);
}

/** 工具调用单行 label（CLI format_tool_call_label 的简化对齐版）：
 *  `tool(主参数)`——主参数取 path/command/query/description/pattern/url 之一，
 *  截断 80 字符；无合适参数则裸工具名。避免与 Python 侧实现漂移的折中：
 *  label 只是显示文本，不影响树结构正确性。 */
export function formatToolLabel(tool: string, args: unknown): string {
  if (!tool) return "?";
  const a = (args && typeof args === "object" ? args : {}) as Record<string, unknown>;
  const KEYS = ["path", "command", "query", "description", "pattern", "url", "name", "task_id"];
  for (const k of KEYS) {
    const v = a[k];
    if (typeof v === "string" && v.trim()) {
      const one = v.trim().replace(/\s+/g, " ");
      const clipped = one.length > 80 ? `${one.slice(0, 80)}…` : one;
      return `${tool}(${clipped})`;
    }
  }
  return tool;
}

export class SubagentTreeTracker {
  private nodes = new Map<string, TreeNode>();
  private coaraToNode = new Map<string, string>();
  private silentCoaraIds = new Set<string>();
  private activeDelegateIds = new Set<string>();
  private orderCounter = 0;

  clear(): void {
    this.nodes.clear();
    this.coaraToNode.clear();
    this.silentCoaraIds.clear();
    this.activeDelegateIds.clear();
  }

  /** 喂入一条 trace 事件（type 或 event_type 字段皆可）。 */
  ingest(evt: TreeEvent): void {
    const type = str(evt.type || evt.event_type);
    switch (type) {
      case "subagent_start":
        this.onAgentStart(evt, "subagent");
        return;
      case "background_agent_start":
        this.onAgentStart(evt, "background");
        return;
      case "subagent_complete":
      case "subagent_failed":
        this.onAgentEnd(evt, type === "subagent_failed", "subagent_id");
        return;
      case "background_agent_complete":
        this.onAgentEnd(evt, Boolean(evt.has_error), "task_id");
        return;
      case "tool_start":
        this.onToolStart(evt);
        return;
      case "tool_complete":
        this.onToolComplete(evt);
        return;
      default:
        return;
    }
  }

  /** 子智能体行 label（CLI _format_agent_label 对齐）：`coaras(任务描述)`，
   *  后台加前缀；无描述时裸类型名。描述截断 40 字符防树行过长。 */
  private agentLabel(agentType: string, description: string, prefix: string): string {
    const desc = description.replace(/\s+/g, " ").trim();
    const clipped = desc.length > 40 ? `${desc.slice(0, 40)}…` : desc;
    const core = clipped ? `${agentType}(${clipped})` : agentType;
    return prefix ? `${prefix}${core}` : core;
  }

  private onAgentStart(evt: TreeEvent, kind: "subagent" | "background"): void {
    const nodeId = str(kind === "subagent" ? evt.subagent_id : evt.task_id);
    const childCoaraId = str(evt.child_coara_id);
    const agentType = str(evt.subagent_type).trim();
    if (SILENT_SUBAGENT_TYPES.has(agentType)) {
      if (childCoaraId) this.silentCoaraIds.add(childCoaraId);
      return;
    }
    if (!nodeId) return;
    const desc = str(evt.description).trim() || str(evt.message).trim();
    const label = this.agentLabel(agentType || "subagent", desc, kind === "background" ? "后台" : "");
    const parentId = this.resolveParentToolId(evt);
    this.upsert(nodeId, parentId, label, kind, childCoaraId, agentType, str(evt.status));
    if (childCoaraId) this.coaraToNode.set(childCoaraId, nodeId);
  }

  private onAgentEnd(evt: TreeEvent, isError: boolean, idField: string): void {
    const nodeId = str(evt[idField]);
    const childCoaraId = str(evt.child_coara_id);
    // 只有 complete 没有 start（缓冲丢帧/半路接入）：合成占位节点再关，保证
    // 父 delegate 行能级联收尾、树不泄漏。
    let parentId = "";
    const existing = nodeId ? this.nodes.get(nodeId) : undefined;
    if (existing) {
      parentId = existing.parentId;
    } else if (nodeId) {
      parentId = this.resolveParentToolId(evt);
      this.upsert(nodeId, parentId, str(evt.message) || "subagent", "subagent", childCoaraId);
    }
    if (!parentId) parentId = this.resolveParentToolId(evt);
    this.finish(nodeId, isError);
    this.finishDelegateAfterChild(parentId, isError);
    if (childCoaraId) {
      this.coaraToNode.delete(childCoaraId);
      this.silentCoaraIds.delete(childCoaraId);
    }
  }

  private onToolStart(evt: TreeEvent): void {
    const toolCallId = str(evt.call_id || evt.tool_call_id);
    const toolName = str(evt.tool || evt.tool_name);
    const coaraId = str(evt.coara_id);
    if (!toolCallId) return;
    if (coaraId && this.silentCoaraIds.has(coaraId)) return;
    if (toolName === "delegate") {
      const args = (evt.args && typeof evt.args === "object" ? evt.args : {}) as Record<string, unknown>;
      if (str(args.action) === "wait") return; // wait 是内部同步点，裸行冗余
    }
    const label = formatToolLabel(toolName, evt.args);
    // 工具的父只有两个来源：子智能体归属（coara_id → 子智能体节点）或显式
    // parent_tool_call_id。不用单 delegate 推断——那是 subagent_start 的专属
    // 兜底，主会话工具在 delegate 活跃期间被推断挂到 delegate 行下是错的。
    let parentId = coaraId ? (this.coaraToNode.get(coaraId) ?? "") : "";
    if (!parentId) parentId = str(evt.parent_tool_call_id || evt.parent_activity_id);
    // delegate 工具自身不挂父（嵌套 delegate 不显示——子智能体行才是它的孩子）
    if (parentId && toolName === "delegate") return;
    this.upsert(toolCallId, parentId, label, toolName || "tool", coaraId);
    if (!parentId && toolName === "delegate") this.activeDelegateIds.add(toolCallId);
  }

  private onToolComplete(evt: TreeEvent): void {
    const toolCallId = str(evt.call_id || evt.tool_call_id);
    if (!toolCallId) return;
    const toolName = str(evt.tool || evt.tool_name);
    const isError = Boolean(evt.is_error) || evt.ok === false;
    // 前台异步/后台 delegate：tool_complete 只是 spawn ack，子智能体还在跑，
    // 等 subagent_complete / background_agent_complete 再关行。
    if (toolName === "delegate" && !isError) {
      const mode = str(evt.delegate_mode).trim();
      if (mode === "foreground_async" || mode === "background") return;
    }
    this.finish(toolCallId, isError);
  }

  private resolveParentToolId(evt: TreeEvent): string {
    const explicit = str(evt.parent_tool_call_id || evt.parent_activity_id);
    if (explicit) return explicit;
    if (this.activeDelegateIds.size === 1) {
      return this.activeDelegateIds.values().next().value as string;
    }
    return "";
  }

  private upsert(
    nodeId: string,
    parentId: string,
    label: string,
    kind: string,
    coaraId: string,
    subagentType = "",
    status = "",
  ): void {
    if (parentId && !this.nodes.has(parentId)) parentId = "";
    const existing = this.nodes.get(nodeId);
    if (existing) {
      existing.parentId = parentId;
      existing.label = label;
      existing.kind = kind;
      existing.coaraId = coaraId || existing.coaraId;
      if (subagentType) existing.subagentType = subagentType;
      existing.status = status;
      existing.active = true;
      existing.isError = false;
      return;
    }
    this.nodes.set(nodeId, {
      nodeId,
      parentId,
      label,
      kind,
      coaraId,
      subagentType,
      status,
      active: true,
      isError: false,
      order: this.orderCounter++,
      startedAt: Date.now(),
    });
  }

  private finish(nodeId: string, isError: boolean): void {
    const node = this.nodes.get(nodeId);
    if (!node) return;
    node.active = false;
    node.isError = isError;
    if (node.kind === "delegate") this.activeDelegateIds.delete(nodeId);
  }

  private finishDelegateAfterChild(parentId: string, isError: boolean): void {
    if (!parentId) return;
    const parent = this.nodes.get(parentId);
    if (!parent || !parent.active) return;
    if (parent.kind !== "delegate" && parent.kind !== "background") return;
    this.finish(parentId, isError);
  }

  /** 当前是否有任何活跃节点（组件据此决定显隐）。 */
  hasActive(): boolean {
    for (const n of this.nodes.values()) if (n.active) return true;
    return false;
  }

  /** 渲染快照：深度缩进的行序列（完成节点也保留，由组件做淡出/收起）。 */
  rows(now: number = Date.now()): TreeRow[] {
    void now;
    const byParent = new Map<string, TreeNode[]>();
    const roots: TreeNode[] = [];
    const sorted = [...this.nodes.values()].sort((a, b) => a.order - b.order);
    for (const n of sorted) {
      if (n.parentId && this.nodes.has(n.parentId)) {
        const list = byParent.get(n.parentId);
        if (list) list.push(n);
        else byParent.set(n.parentId, [n]);
      } else {
        roots.push(n);
      }
    }
    const out: TreeRow[] = [];
    const emit = (node: TreeNode, depth: number) => {
      out.push({
        nodeId: node.nodeId,
        depth,
        label: node.label,
        kind: node.kind,
        active: node.active,
        isError: node.isError,
        pending: node.status === "pending",
        startedAt: node.startedAt,
      });
      const children = byParent.get(node.nodeId) ?? [];
      // 工具行全部直接展示：活跃/已完成都平铺，不再折成「还有 N 个工具…」
      // （主会话工具行已进聊天流，活动树只承载子智能体工作树，折叠反而
      // 让人看不到正在跑什么）。
      for (const c of children) emit(c, depth + 1);
    };
    for (const r of roots) emit(r, 0);
    return out;
  }

  /** 节点已用秒数（delegate 行括注，前端本地计时）。 */
  elapsedSeconds(nodeId: string, now: number = Date.now()): number | null {
    const n = this.nodes.get(nodeId);
    if (!n || !n.active) return null;
    return Math.max(0, Math.floor((now - n.startedAt) / 1000));
  }
}
