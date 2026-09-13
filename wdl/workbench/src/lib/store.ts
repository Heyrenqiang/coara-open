// WDL 工作台全局态（zustand）— 只保留画布迁移所需的最小集合：
// 每实例节点实时状态（WS 引擎事件投影，驱动画布徽章与节点运转面板）。

import { create } from "zustand";
import type { FlowLiveNode } from "./ws";

export interface RunLiveState {
  nodes: Record<string, FlowLiveNode>;
  status: string;
  updatedAt: string;
}

interface WorkbenchState {
  /** instance_id → 节点实时状态表 */
  runLiveNodes: Record<string, RunLiveState>;
  mergeRunNode: (instanceId: string, node: FlowLiveNode) => void;
  setRunStatus: (instanceId: string, status: string) => void;
}

export const useStore = create<WorkbenchState>((set) => ({
  runLiveNodes: {},
  mergeRunNode: (instanceId, node) =>
    set((s) => {
      const prev = s.runLiveNodes[instanceId] ?? {
        nodes: {},
        status: "running",
        updatedAt: "",
      };
      return {
        runLiveNodes: {
          ...s.runLiveNodes,
          [instanceId]: {
            ...prev,
            nodes: { ...prev.nodes, [node.id]: node },
            updatedAt: new Date().toISOString(),
          },
        },
      };
    }),
  setRunStatus: (instanceId, status) =>
    set((s) => {
      const prev = s.runLiveNodes[instanceId];
      if (!prev) return s;
      return {
        runLiveNodes: {
          ...s.runLiveNodes,
          [instanceId]: { ...prev, status, updatedAt: new Date().toISOString() },
        },
      };
    }),
}));
