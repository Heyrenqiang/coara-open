// 编排图域 slice（从 store.ts 抽出）：内存态 agentic 工作流图的纯函数群 + 状态动作。
// 域内自足：只依赖 chatTypes/chatRowUtils 与 ws 类型，不碰 store 其它域。
// store.ts 经 ...createFlowGraphSlice(set) 组合进统一 store，消费点不变。

import type { FlowGraphSnapshot, FlowLiveNode, ServerMessage } from "./ws";
import { flowGraphKey, type FlowLiveGraph } from "./chatTypes";
import { nowISO } from "./chatRowUtils";

export interface FlowGraphSlice {
  /** 过的图（视图卸载时 drop），内存有界。 */
  flowGraphs: Record<string, FlowLiveGraph>;
  /** 用全量快照替换某张图（图不存在时登记空图）。 */
  applyFlowSnapshot: (flow: string, snapshot: FlowGraphSnapshot | null) => void;
  /** 增量 trace 事件（flow_graph_changed / subagent_*）套用到在看的图；
   *  未在看的图忽略（subject=flow 的工作台图除外，首个增量即建空图）。 */
  applyFlowTraceEvent: (msg: ServerMessage) => void;
  /** 卸载一张在看的图（视图 unmount）。 */
  dropFlowGraph: (flow: string) => void;
}

/* ── 图处理纯函数群 ──────────────────────────────────────────────────
 * 仅保留经 flow_snapshot 请求过的图；工作台（subject=flow）例外——
 * 首个增量事件即建空图，画布自动跟随 FlowRoot 主体的图变化。 */

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

type SliceSet = (fn: (s: FlowGraphSlice) => Partial<FlowGraphSlice>) => void;

export function createFlowGraphSlice(set: SliceSet): FlowGraphSlice {
  return {
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
  };
}
