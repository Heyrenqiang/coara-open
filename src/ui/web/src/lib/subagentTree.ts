/** 子智能体活动树（CLI ActivityLiveTracker 的 TS 移植）。与 traceEvents 环缓冲解耦。 */
const SILENT_SUBAGENT_TYPES = new Set(["janitor", "daily"]);

interface TreeNode {
  nodeId: string;
  parentId: string;
  label: string;
  kind: string;
  coaraId: string;
  subagentType: string;
  /** ""=运行中；"pending"=停车（灰显） */
  status: string;
  active: boolean;
  isError: boolean;
  order: number;
  startedAt: number;
  /** 终态时刻；0＝还没结束（或收尾时只撤了 active）。 */
  endedAt: number;
  lastEvent: number;
}

export interface TreeRow {
  nodeId: string;
  depth: number;
  label: string;
  kind: string;
  /** 该节点的实例 id（帧上带 coara_id）：编排节点分组用。 */
  coaraId: string;
  active: boolean;
  isError: boolean;
  pending: boolean;
  startedAt: number;
  endedAt: number;
}

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

/** `tool(主参)`，截断 80；无参则裸名。 */
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
  /** 结构版本号：任何会改变树行的变更都 +1；rows() 据此复用上一次的派生结果
   *  （同一个数组引用 = 前端 memo/useMemo 不重算，也不触发工具行重渲染）。 */
  private version = 0;
  private rowsCache: { version: number; rows: TreeRow[] } | null = null;

  /** 标记结构已变（作废 rows 缓存）。 */
  private bump(): void {
    this.version += 1;
  }

  /** 树行快照：结构未变（version 相同）时返回上一次的同一个数组引用。 */
  rows(now: number = Date.now()): TreeRow[] {
    void now;
    const cached = this.rowsCache;
    if (cached && cached.version === this.version) return cached.rows;
    const rows = this.buildRows();
    this.rowsCache = { version: this.version, rows };
    return rows;
  }

  clear(): void {
    this.nodes.clear();
    this.coaraToNode.clear();
    this.silentCoaraIds.clear();
    this.activeDelegateIds.clear();
    this.bump();
  }

  /** 回合收尾：只撤主根行；仍在跑的委派容器留到终态帧（toolRunning 依赖 active）。 */
  clearRootStatus(): void {
    for (const node of this.nodes.values()) {
      if (node.parentId && this.nodes.has(node.parentId)) continue;
      const keepRunning =
        node.active &&
        (node.kind === "delegate" ||
          node.kind === "subagent" ||
          node.kind === "background" ||
          this.activeDelegateIds.has(node.nodeId));
      if (!keepRunning) node.active = false;
    }
    this.bump();
    this.pruneFinished();
  }

  /** 回收已结束且无子行的节点（自叶向根）。单遍：子数计数 + 非活跃叶子队列，
   *  删掉一个叶子就把父节点计数减一、减到 0 且父不活跃则接着入队——取代原来的
   *  「每轮全表扫、扫到不再变化为止」（O(n²) → O(n)）。 */
  private pruneFinished(): void {
    const childCount = new Map<string, number>();
    for (const node of this.nodes.values()) {
      const parentId = node.parentId;
      if (parentId && this.nodes.has(parentId)) {
        childCount.set(parentId, (childCount.get(parentId) ?? 0) + 1);
      }
    }
    const queue: string[] = [];
    for (const [nodeId, node] of this.nodes) {
      if (!node.active && (childCount.get(nodeId) ?? 0) === 0) queue.push(nodeId);
    }
    let removed = false;
    while (queue.length > 0) {
      const nodeId = queue.pop() as string;
      const node = this.nodes.get(nodeId);
      if (!node || node.active) continue;
      if ((childCount.get(nodeId) ?? 0) > 0) continue;
      this.nodes.delete(nodeId);
      this.activeDelegateIds.delete(nodeId);
      removed = true;
      const parentId = node.parentId;
      if (!parentId) continue;
      const left = (childCount.get(parentId) ?? 0) - 1;
      childCount.set(parentId, left);
      const parent = this.nodes.get(parentId);
      if (parent && !parent.active && left === 0) queue.push(parentId);
    }
    if (!removed) return;
    for (const [coaraId, id] of [...this.coaraToNode]) {
      if (!this.nodes.has(id)) this.coaraToNode.delete(coaraId);
    }
    this.bump();
  }

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
    // 无 start 丢帧：合成占位再关，防树泄漏
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
      if (str(args.action) === "wait") return;
    }
    const label = formatToolLabel(toolName, evt.args);
    let parentId = coaraId ? (this.coaraToNode.get(coaraId) ?? "") : "";
    if (!parentId) parentId = str(evt.parent_tool_call_id || evt.parent_activity_id);
    if (parentId && toolName === "delegate") return;
    this.upsert(toolCallId, parentId, label, toolName || "tool", coaraId);
    if (!parentId && toolName === "delegate") this.activeDelegateIds.add(toolCallId);
  }

  private onToolComplete(evt: TreeEvent): void {
    const toolCallId = str(evt.call_id || evt.tool_call_id);
    if (!toolCallId) return;
    const toolName = str(evt.tool || evt.tool_name);
    const isError = Boolean(evt.is_error) || evt.ok === false;
    // foreground_async/background：spawn ack，等终态帧再关
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
    this.bump();
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
      existing.lastEvent = Date.now();
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
      endedAt: 0,
      lastEvent: Date.now(),
    });
  }

  private finish(nodeId: string, isError: boolean): void {
    const node = this.nodes.get(nodeId);
    if (!node) return;
    node.active = false;
    node.isError = isError;
    if (!node.endedAt) node.endedAt = Date.now();
    this.bump();
    if (node.kind === "delegate") this.activeDelegateIds.delete(nodeId);
  }

  private finishDelegateAfterChild(parentId: string, isError: boolean): void {
    if (!parentId) return;
    const parent = this.nodes.get(parentId);
    if (!parent || !parent.active) return;
    if (parent.kind !== "delegate" && parent.kind !== "background") return;
    this.finish(parentId, isError);
  }

  /** 心跳 active_delegations 幂等补建（刷新后无 start 帧时圆点不灭）。 */
  upsertActive(rows: Array<Record<string, unknown>>): boolean {
    let changed = false;
    for (const row of rows) {
      const nodeId = str(row.task_id);
      if (!nodeId) continue;
      const subagentType = str(row.subagent_type).trim();
      if (SILENT_SUBAGENT_TYPES.has(subagentType)) continue;
      const isBg = Boolean(row.background);
      const parentCallId = str(row.parent_tool_call_id);
      if (parentCallId && !this.nodes.has(parentCallId)) {
        this.upsert(parentCallId, "", "delegate", "delegate", "");
        this.activeDelegateIds.add(parentCallId);
        changed = true;
      }
      const existing = this.nodes.get(nodeId);
      if (existing) {
        if (!existing.active) {
          existing.active = true;
          changed = true;
        }
        existing.lastEvent = Date.now();
        continue;
      }
      const label = this.agentLabel(
        subagentType || "subagent",
        str(row.description),
        isBg ? "后台" : "",
      );
      this.upsert(
        nodeId,
        parentCallId,
        label,
        isBg ? "background" : "subagent",
        str(row.child_coara_id),
        subagentType,
      );
      const created = this.nodes.get(nodeId);
      if (created) {
        created.startedAt = Number(row.started_at) > 0 ? Number(row.started_at) * 1000 : Date.now();
        changed = true;
      }
    }
    if (changed) this.bump();
    return changed;
  }

  /** 无活跃委派祖先且超 timeoutMs 无事件 → inactive（终态丢帧兜底）。 */
  pruneStale(timeoutMs = 600_000, now: number = Date.now()): boolean {
    let changed = false;
    for (const node of this.nodes.values()) {
      if (!node.active) continue;
      if (now - node.lastEvent < timeoutMs) continue;
      if (this.hasActiveDelegateAncestor(node)) continue;
      node.active = false;
      if (node.kind === "delegate") this.activeDelegateIds.delete(node.nodeId);
      changed = true;
    }
    if (changed) {
      this.bump();
      this.pruneFinished();
    }
    return changed;
  }

  private hasActiveDelegateAncestor(node: TreeNode): boolean {
    const seen = new Set<string>();
    let parentId = node.parentId;
    while (parentId && !seen.has(parentId)) {
      seen.add(parentId);
      const parent = this.nodes.get(parentId);
      if (!parent) return false;
      const isContainer =
        parent.kind === "delegate" || parent.kind === "subagent" || parent.kind === "background";
      if (isContainer && parent.active) return true;
      parentId = parent.parentId;
    }
    return false;
  }

  hasActive(): boolean {
    for (const n of this.nodes.values()) if (n.active) return true;
    return false;
  }

  private buildRows(): TreeRow[] {
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
        coaraId: node.coaraId,
        active: node.active,
        isError: node.isError,
        pending: node.status === "pending",
        startedAt: node.startedAt,
        endedAt: node.endedAt,
      });
      const children = byParent.get(node.nodeId) ?? [];
      for (const c of children) emit(c, depth + 1);
    };
    for (const r of roots) emit(r, 0);
    return out;
  }

  elapsedSeconds(nodeId: string, now: number = Date.now()): number | null {
    const n = this.nodes.get(nodeId);
    if (!n || !n.active) return null;
    return Math.max(0, Math.floor((now - n.startedAt) / 1000));
  }
}
