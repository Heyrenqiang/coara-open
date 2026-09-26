/**
 * 编排行的展开区：三级结构。
 *
 *   一级 · orchestrator 行本身（折叠态一行：流程名 · 进度 · 状态）
 *   二级 · 节点清单（节点名 · 状态 · 耗时）
 *   三级 · 单个节点展开后，是该节点的内容（过程帧 / 任务指令 / 最终结果）
 *
 * 分工：二级数据来自活动树（节点行带 active / pending / isError / startedAt），
 * 三级内容来自按节点分好的帧（flowNodeGroups）。两者都不命中时退化为平铺，
 * 绝不因为「认不出节点」把内容藏起来。
 */
import { useMemo, useState } from "react";
import { ToolLineAccordion } from "./ToolLineRow";
import { ProcessEntryList } from "./MessageList";
import { buildToolLineGroups, type ToolLineGroup } from "../../lib/toolLineGroups";
import type { FlowNodeGroups } from "../../lib/flowNodeGroups";
import type { TreeRow } from "../../lib/subagentTree";
import type { ChatMessage } from "../../lib/store";
import "./flow-node.css";

/** 节点行的展示态：状态点 + 名称 + 耗时。 */
function nodeStatusLabel(node: TreeRow): string {
  if (node.isError) return "失败";
  if (node.active) return "运行中";
  if (node.pending) return "等待上游";
  if (node.startedAt > 0 && node.endedAt > node.startedAt) {
    const seconds = Math.max(1, Math.round((node.endedAt - node.startedAt) / 1000));
    return seconds >= 60 ? `用时 ${Math.floor(seconds / 60)}m${seconds % 60}s` : `用时 ${seconds}s`;
  }
  return "完成";
}

function nodeMark(node: TreeRow): string {
  if (node.isError) return "✗";
  if (node.active) return "●";
  if (node.pending) return "○";
  return "✓";
}

/** 节点自己的内容：工具行 / diff 帧；任务指令与结果由调用方按需补。 */
function NodeFrames({ frames }: { frames: ChatMessage[] }) {
  const groups: ToolLineGroup[] = useMemo(
    () => buildToolLineGroups({ brief: "", work: [], frames, body: "", result: "" }),
    [frames],
  );
  if (groups.length === 0) return null;
  return <ToolLineAccordion groups={groups} renderBody={(g) => <ProcessEntryList entries={g.entries ?? []} />} />;
}

export function FlowNodePanel({
  nodes,
  groups,
}: {
  nodes: TreeRow[];
  groups: FlowNodeGroups;
}) {
  const [openNode, setOpenNode] = useState<string | null>(null);
  // 未分组帧：认不出节点也照常列出（宁可多一层，不藏内容）
  const rest = groups.ungrouped;

  return (
    <div className="flow-node-panel">
      {nodes.map((node) => {
        const frames = groups.byNode.get(node.nodeId) ?? [];
        const expanded = openNode === node.nodeId;
        return (
          <div key={node.nodeId}>
            <button
              type="button"
              className="flow-node-row"
              onClick={() => setOpenNode(expanded ? null : node.nodeId)}
            >
              <span className="flow-node-mark">{nodeMark(node)}</span>
              <span className="flow-node-name">{node.label || node.nodeId}</span>
              <span className="flow-node-status">{nodeStatusLabel(node)}</span>
            </button>
            {expanded ? (
              <div className="flow-node-body">
                <NodeFrames frames={frames} />
              </div>
            ) : null}
          </div>
        );
      })}
      {rest.length > 0 ? (
        <div className="flow-node-body">
          <NodeFrames frames={rest} />
        </div>
      ) : null}
    </div>
  );
}
