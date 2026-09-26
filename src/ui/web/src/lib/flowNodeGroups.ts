/**
 * 编排行的二级分组：orchestrator 行 → 节点 → 该节点的三组（任务 / 过程 / 结果）。
 *
 * 三级折叠的第二级数据层。节点身份不靠切字符串（flow 名本身可能含 `-`），而是拿
 * 内核事件给出的节点表匹配：
 *   1. 帧 `subagent_id` 命中节点签发标识（`flow-<flow>-<node>`）
 *   2. 否则用帧 `coara_id` 命中节点实例 id（节点事件带 child_coara_id；结果帧只带它）
 *   3. 都不命中 → 归入未分组，绝不丢内容
 */
import type { ChatMessage } from "./store";

/** 节点引用：内核侧签发的三种身份，任一带上即可参与匹配。 */
export interface FlowNodeRef {
  /** 节点名（图上的 id）。 */
  nodeId: string;
  /** 内核签发的子智能体标识 `flow-<flow>-<node>`。 */
  subagentId?: string;
  /** 节点实例的 coara_id（结果帧只带它）。 */
  coaraId?: string;
}

export interface FlowNodeGroups {
  /** nodeId → 该节点的帧，保持输入顺序。 */
  byNode: Map<string, ChatMessage[]>;
  /** 匹配不到任何节点的帧（老数据 / 未登记节点），调用方按未分组展示。 */
  ungrouped: ChatMessage[];
}

/** 帧上的身份字段（宽松取，兼容不同帧形态）。 */
function frameIdentity(frame: ChatMessage): { subagentId: string; coaraId: string } {
  const raw = frame as unknown as Record<string, unknown>;
  const subagentId = String(raw.subagent_id ?? raw.subagentId ?? "");
  const coaraId = String(raw.coara_id ?? raw.coaraId ?? "");
  return { subagentId, coaraId };
}

/** 按节点表把帧分组；节点顺序取节点表顺序（未分组的排在最后）。 */
export function groupFramesByNode(frames: ChatMessage[], nodes: FlowNodeRef[]): FlowNodeGroups {
  const bySubagent = new Map<string, string>();
  const byCoara = new Map<string, string>();
  const byNode = new Map<string, ChatMessage[]>();
  for (const node of nodes) {
    if (!node?.nodeId) continue;
    if (!byNode.has(node.nodeId)) byNode.set(node.nodeId, []);
    if (node.subagentId) bySubagent.set(String(node.subagentId), node.nodeId);
    if (node.coaraId) byCoara.set(String(node.coaraId), node.nodeId);
  }

  const ungrouped: ChatMessage[] = [];
  for (const frame of frames) {
    const { subagentId, coaraId } = frameIdentity(frame);
    const owner =
      (subagentId ? bySubagent.get(subagentId) : undefined) ??
      (coaraId ? byCoara.get(coaraId) : undefined);
    if (!owner) {
      ungrouped.push(frame);
      continue;
    }
    const bucket = byNode.get(owner);
    if (bucket) bucket.push(frame);
    else ungrouped.push(frame);
  }
  return { byNode, ungrouped };
}

/** 节点是否已经收了内容（用于「空节点也显示在清单里」的判定）。 */
export function nodeHasContent(groups: FlowNodeGroups, nodeId: string): boolean {
  return (groups.byNode.get(nodeId)?.length ?? 0) > 0;
}
