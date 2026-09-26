/**
 * Shared types for the coara workflow editor (kernel projection).
 *
 * 2026-08-16 内核化：草案文本是内核投影 YAML（nodes + edges），节点只有
 * 智能体一种。这些类型描述内核图文档、React Flow 节点/边负载，以及
 * 编辑器各模块共用的节点 data 形态。
 *
 * NOTE: types are self-contained (no `@xyflow/react` runtime import) so the
 * algorithm modules can be type-checked even before React Flow is installed.
 */

export type { KernelDocument, KernelNodeSpec, KernelEdgeSpec } from '../../../lib/api';

/* ──────────────────────────────────────────────────────────────────────
 * React Flow node/edge data shapes
 * ────────────────────────────────────────────────────────────────────── */

/** The `data` payload carried by a workflow canvas node.
 *  智能体节点：label=节点 id，task/input/routes/max_activations 与内核
 *  节点规格同名同型。
 *  The index signature keeps the type compatible with React Flow's
 *  `Node<NodeData>` constraint (`NodeData extends Record<string, unknown>`). */
export interface NodeData {
  label?: string;
  icon?: string;
  color?: string;
  /** 智能体节点 = task 首行摘要（画布展示用） */
  desc?: string;
  status?: string;
  error?: string;
  /** 'agent' */
  nodeType?: string;
  /** 节点 id（即内核节点名/智能体身份名） */
  stepId?: string;
  /* ---- 内核节点规格（与 KernelNodeSpec 同构，编辑面板直接改这里） ---- */
  task?: string;
  /** 数据进模板：{{steps.y.text}} 引用上游 / 字面量种子 */
  input?: string;
  /** 出边路由模式 all | one */
  routes?: 'all' | 'one';
  /** 节点级激活上限（空/0 = 继承全局） */
  max_activations?: number | null;
  [key: string]: unknown;
}

/** Minimal node shape accepted by port-schema helpers (allows `{ data }` calls). */
export interface PortNodeLike {
  id?: string;
  data?: NodeData;
}

/* ──────────────────────────────────────────────────────────────────────
 * React Flow node/edge shapes (self-contained, no runtime import)
 * ────────────────────────────────────────────────────────────────────── */

/** Minimal node shape consumed/produced by the editor algorithms. */
export interface ReactFlowNode {
  id: string;
  type?: string;
  position: { x: number; y: number };
  data: NodeData;
  parentId?: string;
  width?: number;
  height?: number;
  draggable?: boolean;
  selectable?: boolean;
  style?: Record<string, unknown>;
  [key: string]: unknown;
}

/** Minimal edge shape consumed/produced by the editor algorithms. */
export interface ReactFlowEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle?: string | null;
  targetHandle?: string | null;
  type?: string;
  className?: string;
  label?: string;
  labelShowBg?: boolean;
  labelStyle?: Record<string, unknown>;
  labelBgStyle?: Record<string, unknown>;
  animated?: boolean;
  style?: Record<string, unknown>;
  markerEnd?: Record<string, unknown> | string;
  zIndex?: number;
  data?: Record<string, unknown>;
  [key: string]: unknown;
}

/* ──────────────────────────────────────────────────────────────────────
 * Misc shared shapes
 * ────────────────────────────────────────────────────────────────────── */

/** An input port row for a node. */
export interface InputPort {
  key: string;
  label: string;
  template: string;
  handleId: string;
}

/** An output port row for a node. */
export interface OutputPort {
  key: string;
  label: string;
  template: string;
  handleId: string;
}

/** Exec-port summary for a node. */
export interface ExecPorts {
  hasExecIn: boolean;
  hasExecOut: boolean;
}

/** A row in the expression-suggestion list. */
export interface SuggestionRow {
  template: string;
  label: string;
  group: string;
}

/** Result of inserting an expression at the cursor. */
export interface InsertExpressionResult {
  value: string;
  cursor: number;
}
