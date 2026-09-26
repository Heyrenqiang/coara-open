/**
 * Kernel graph (nodes + edges) ↔ React Flow.
 *
 * 2026-08-16 内核化：WDL 草案文本是内核投影（nodes + edges），节点只有
 * 智能体一种，控制流全部塌缩为图的形状（并行=扇出、汇聚=扇入、分支=one
 * 路由、循环=回边、重试=error 边）。本模块职责：
 *
 *   - documentToReactFlow：内核图 → 画布节点/边。每个节点一个智能体节点
 *     （标题=node id，摘要=task 首行截断）；边直接映射，on=error 红色
 *     虚线 + "error" 标签；另从 input 模板 {{steps.y.text}} 推导只读数据
 *     绑定边（虚线品牌蓝，画布可视化，不落盘）。
 *   - reactFlowToDocument：画布 → 内核图。exec 边写回 edges；数据绑定边
 *     已由 binding-sync 物化进 node.input 模板，不产生图边。
 *   - 布局是拓扑的纯函数（WDL_SPEC §1）：按执行层分层，同图必同布局。
 *     节点拖动是会话内排版，不持久化。
 */

import {
  isExecSourceHandle,
  isExecTargetHandle,
} from './node-port-schema';
import { token } from './theme';
import type {
  KernelDocument,
  KernelNodeSpec,
  ReactFlowNode,
  ReactFlowEdge,
  NodeData,
} from './types';

/* 圆形节点：圆面 96px + 外置标题/摘要的纵向余量 */
const NODE_WIDTH = 120;
const NODE_HEIGHT = 172;
const GAP_X = 200;
const GAP_Y = 70;

/* 模板引用：{{steps.y.text}}（unicode 词字符，中文可用，与
   core/semantics.py 的 \w+ 对齐——JS 的 \w 不含 CJK，须用 \p{L}\p{N}） */
const ID_CH = '[\\p{L}\\p{M}\\p{N}_-]+';
const STEP_REF_RE = new RegExp(`\\{\\{\\s*steps\\.(${ID_CH})\\.(${ID_CH})\\s*\\}\\}`, 'gu');

/** task 首行截断，作为画布节点摘要。 */
export function taskSummary(task: string | null | undefined): string {
  const firstLine = String(task || '')
    .split(/\r?\n/, 1)[0]
    .trim();
  if (!firstLine) return '';
  return firstLine.length > 42 ? `${firstLine.slice(0, 41)}…` : firstLine;
}

/** 画布智能体节点的 data（与 KernelNodeSpec 同构）。 */
export function kernelNodeData(nodeId: string, spec: KernelNodeSpec): NodeData {
  return {
    label: nodeId,
    nodeType: 'agent',
    stepId: nodeId,
    desc: taskSummary(spec.task),
    status: 'idle',
    color: token('color.node.run-accent') as string | undefined,
    task: String(spec.task || ''),
    input: String(spec.input || ''),
    routes: spec.routes === 'one' ? 'one' : 'all',
    max_activations:
      typeof spec.max_activations === 'number' && spec.max_activations > 0
        ? spec.max_activations
        : null,
    provider: String(spec.provider || ''),
    model: String(spec.model || ''),
  };
}

/* ──────────────────────────────────────────────────────────────────────
 * 布局：按执行层分层（拓扑的纯函数）
 * ────────────────────────────────────────────────────────────────────── */

function layoutLayers(document: KernelDocument | null | undefined): Record<string, { x: number; y: number }> {
  const doc: KernelDocument = document || { nodes: {}, edges: [] };
  const nodes = doc.nodes;
  const ids = Object.keys(nodes);
  const positions: Record<string, { x: number; y: number }> = {};
  if (!ids.length) return positions;

  const docEdges = document?.edges || [];

  // 回边标记（DFS 三色）：指向栈上祖先的边不参与分层——否则成环图
  // （error 兜底/循环回边）会让 BFS 抬升永不停，画布直接卡死
  const adj = new Map<string, number[]>();
  docEdges.forEach((e, i) => {
    if (!e.from || !e.to || !(e.from in nodes) || !(e.to in nodes)) return;
    (adj.get(e.from) ?? adj.set(e.from, []).get(e.from)!).push(i);
  });
  const backEdge = new Set<number>();
  const color = new Map<string, number>(); // 1=栈上(灰) 2=完成(黑)
  const dfs = (u: string) => {
    color.set(u, 1);
    for (const i of adj.get(u) || []) {
      const v = docEdges[i].to;
      const c = color.get(v) || 0;
      if (c === 1) backEdge.add(i);
      else if (c === 0) dfs(v);
    }
    color.set(u, 2);
  };
  ids.forEach((id) => {
    if (!color.get(id)) dfs(id);
  });

  const incoming = new Set<string>();
  docEdges.forEach((e, i) => {
    if (e.to && !backEdge.has(i)) incoming.add(e.to);
  });

  let roots = ids.filter((id) => !incoming.has(id));
  if (!roots.length) roots = [ids[0]]; // 纯环图：任取一个起点

  // BFS 分层（只沿前向边）；层数硬封顶 n 兜底，任何图都必然终止
  const maxLayer = ids.length;
  const layers = new Map<string, number>();
  const queue = [...roots];
  roots.forEach((r) => layers.set(r, 0));
  while (queue.length) {
    const from = queue.shift();
    if (from === undefined) break;
    const layer = layers.get(from) || 0;
    if (layer >= maxLayer) continue;
    docEdges.forEach((e, i) => {
      if (backEdge.has(i) || e.from !== from || !e.to || !(e.to in nodes)) return;
      const nextLayer = layer + 1;
      if (!layers.has(e.to) || layers.get(e.to)! < nextLayer) {
        layers.set(e.to, nextLayer);
        queue.push(e.to);
      }
    });
  }
  ids.forEach((id) => {
    if (!layers.has(id)) layers.set(id, 0);
  });

  const byLayer: Record<number, string[]> = {};
  ids.forEach((id) => {
    const layer = layers.get(id) || 0;
    (byLayer[layer] = byLayer[layer] || []).push(id);
  });

  Object.keys(byLayer).forEach((layerKey) => {
    const layer = Number(layerKey);
    byLayer[layer].forEach((id, index) => {
      positions[id] = {
        x: 300 + layer * (NODE_WIDTH + GAP_X),
        y: 60 + index * (NODE_HEIGHT + GAP_Y),
      };
    });
  });

  return positions;
}

/* ──────────────────────────────────────────────────────────────────────
 * 边构建
 * ────────────────────────────────────────────────────────────────────── */

export function kernelEdgeId(from: string, to: string, on: string): string {
  return `exec_${from}_${to}_${on}`;
}

/** 判断 newId 是否已被现有边占用（updateEdgeOn 切换 on 前防重复 id）。 */
export function edgeIdOccupied(
  edges: Array<{ id: string }> | null | undefined,
  newId: string,
  ignoreEdgeId?: string,
): boolean {
  return (edges || []).some((e) => e.id === newId && e.id !== ignoreEdgeId);
}

/** 内核边 → React Flow 边；on=error 红色虚线 + "error" 小标签。 */
function kernelEdgeToFlowEdge(
  from: string,
  to: string,
  on: 'success' | 'error',
): ReactFlowEdge {
  if (on === 'error') {
    return {
      id: kernelEdgeId(from, to, on),
      source: from,
      target: to,
      sourceHandle: 'exec-out',
      targetHandle: 'exec-in',
      type: 'default',
      className: 'edge-exec edge-exec-error',
      label: 'error',
      labelShowBg: true,
      labelStyle: { fill: 'var(--wf-color-error)', fontSize: 10 },
      labelBgStyle: { fill: 'var(--wf-color-bg)' },
      style: { stroke: 'var(--wf-color-error)', strokeWidth: 1.5, strokeDasharray: '6 3' },
      markerEnd: { type: 'arrowclosed', color: token('color.error'), width: 20, height: 20 },
      data: { on: 'error' },
    };
  }
  return {
    id: kernelEdgeId(from, to, on),
    source: from,
    target: to,
    sourceHandle: 'exec-out',
    targetHandle: 'exec-in',
    type: 'default',
    className: 'edge-exec',
    style: { stroke: 'var(--wf-color-text-tertiary)', strokeWidth: 1.5 },
    markerEnd: { type: 'arrowclosed', color: token('color.text-tertiary'), width: 20, height: 20 },
    data: { on: 'success' },
  };
}

/** 模板引用推导的只读数据绑定边（画布可视化，不写回内核图）。 */
export interface InferredDataEdge {
  /** 完整引用路径：steps.y.text */
  ref: string;
  /** 数据源节点 id：上游智能体 */
  sourceNodeId: string;
  /** 数据目标节点 id */
  targetNodeId: string;
  sourceHandle: string;
}

export function inferDataEdges(document: KernelDocument | null | undefined): InferredDataEdge[] {
  const doc: KernelDocument = document || { nodes: {}, edges: [] };
  const out: InferredDataEdge[] = [];
  const seen = new Set<string>();
  for (const [nodeId, spec] of Object.entries(doc.nodes || {})) {
    const input = String(spec?.input || '');
    if (!input) continue;
    let m: RegExpExecArray | null;
    const stepRe = new RegExp(STEP_REF_RE.source, 'gu');
    while ((m = stepRe.exec(input)) !== null) {
      const upId = m[1];
      if (!(upId in (doc.nodes || {}))) continue; // 引用不存在节点：由 validate 报告
      const key = `steps.${upId}.${m[2]}|${nodeId}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        ref: `steps.${upId}.${m[2]}`,
        sourceNodeId: upId,
        targetNodeId: nodeId,
        sourceHandle: `out:${m[2]}`,
      });
    }
  }
  return out;
}

function dataEdgeToFlowEdge(e: InferredDataEdge, idx: number): ReactFlowEdge {
  return {
    id: `data_${e.ref.replace(/[^\w-]+/g, '_')}_${idx}`,
    source: e.sourceNodeId,
    target: e.targetNodeId,
    sourceHandle: e.sourceHandle,
    targetHandle: 'in:input',
    type: 'routable',
    className: 'edge-data-flow',
    animated: true,
    style: { stroke: 'var(--wf-color-brand)', strokeWidth: 1, strokeDasharray: '3 3' },
    markerEnd: { type: 'arrowclosed', color: token('color.brand'), width: 20, height: 20 },
    zIndex: 5,
    data: { ref: e.ref },
  };
}

/** 内核图 → React Flow 边全集（拓扑边 + 推导数据绑定边）。 */
export function wdlDocumentToFlowEdges(document: KernelDocument | null | undefined): ReactFlowEdge[] {
  const doc: KernelDocument = document || { nodes: {}, edges: [] };
  const edges: ReactFlowEdge[] = [];
  (doc.edges || []).forEach((e) => {
    const from = String(e?.from || '').trim();
    const to = String(e?.to || '').trim();
    if (!from || !to) return;
    if (!(from in (doc.nodes || {})) || !(to in (doc.nodes || {}))) return;
    edges.push(kernelEdgeToFlowEdge(from, to, e.on === 'error' ? 'error' : 'success'));
  });
  inferDataEdges(doc).forEach((e, idx) => edges.push(dataEdgeToFlowEdge(e, idx)));
  return edges;
}

/* ──────────────────────────────────────────────────────────────────────
 * 文档 → 画布
 * ────────────────────────────────────────────────────────────────────── */

export function documentToReactFlow(
  document: KernelDocument | null | undefined,
): { nodes: ReactFlowNode[]; edges: ReactFlowEdge[] } {
  const doc: KernelDocument = document || { nodes: {}, edges: [] };
  const positions = layoutLayers(doc);
  const nodes: ReactFlowNode[] = [];

  for (const [nodeId, spec] of Object.entries(doc.nodes || {})) {
    if (!spec || typeof spec !== 'object') continue;
    nodes.push({
      id: nodeId,
      type: 'custom',
      position: positions[nodeId] || { x: 300, y: 60 },
      data: kernelNodeData(nodeId, spec),
    });
  }

  return { nodes, edges: wdlDocumentToFlowEdges(doc) };
}

/* ──────────────────────────────────────────────────────────────────────
 * 画布 → 文档
 * ────────────────────────────────────────────────────────────────────── */

export function reactFlowToDocument(
  document: KernelDocument | null | undefined,
  nodes: ReactFlowNode[] | null | undefined,
  edges: ReactFlowEdge[] | null | undefined,
): KernelDocument {
  const base: KernelDocument = document || { nodes: {}, edges: [] };
  const outNodes: Record<string, KernelNodeSpec> = {};

  for (const node of nodes || []) {
    const data: NodeData = node.data || {};
    const spec: KernelNodeSpec = {
      task: String(data.task ?? ''),
      input: String(data.input ?? ''),
      routes: data.routes === 'one' ? 'one' : 'all',
    };
    const ma = Number(data.max_activations);
    if (Number.isFinite(ma) && ma > 0) spec.max_activations = Math.round(ma);
    const nodeProvider = String(data.provider ?? '').trim();
    if (nodeProvider) spec.provider = nodeProvider;
    const nodeModel = String(data.model ?? '').trim();
    if (nodeModel) spec.model = nodeModel;
    outNodes[node.id] = spec;
  }

  const outEdges: { from: string; to: string; on: 'success' | 'error' }[] = [];
  const seen = new Set<string>();
  for (const e of edges || []) {
    if (!isExecSourceHandle(e.sourceHandle) || !isExecTargetHandle(e.targetHandle)) continue;
    if (!(e.source in outNodes) || !(e.target in outNodes)) continue;
    const on = (e.data?.on as string) === 'error' ? 'error' : 'success';
    const key = `${e.source}|${e.target}|${on}`;
    if (seen.has(key)) continue;
    seen.add(key);
    outEdges.push({ from: e.source, to: e.target, on });
  }
  outEdges.sort((a, b) =>
    a.from === b.from ? (a.to === b.to ? a.on.localeCompare(b.on) : a.to.localeCompare(b.to)) : a.from.localeCompare(b.from),
  );

  return {
    name: base.name || '',
    description: base.description || '',
    schedule: { ...(base.schedule || { kind: 'manual' }) },
    ...(typeof base.max_activations === 'number' && base.max_activations > 0
      ? { max_activations: base.max_activations }
      : {}),
    nodes: outNodes,
    edges: outEdges,
  };
}

/**
 * 内核图一致性校验（画布侧轻量检查；深度校验由后端 parse 承担）。
 * 返回人类可读的问题列表（空 = 通过）。
 */
export function validateDocumentConsistency(document: KernelDocument | null | undefined): string[] {
  const doc: KernelDocument = document || { nodes: {}, edges: [] };
  const issues: string[] = [];
  const nodes = doc.nodes || {};

  for (const [id, spec] of Object.entries(nodes)) {
    if (!String(spec?.task || '').trim()) {
      issues.push(`节点 "${id}" 的 task 为空`);
    }
  }

  const seen = new Set<string>();
  for (const e of doc.edges || []) {
    const from = String(e?.from || '');
    const to = String(e?.to || '');
    if (from && !(from in nodes)) issues.push(`边引用了不存在的节点 "${from}"`);
    if (to && !(to in nodes)) issues.push(`边引用了不存在的节点 "${to}"`);
    const key = `${from}|${to}|${e?.on || 'success'}`;
    if (seen.has(key)) issues.push(`重复边：${from} → ${to}（on=${e?.on || 'success'}）`);
    seen.add(key);
  }

  // input 模板引用了不存在的节点
  for (const [id, spec] of Object.entries(nodes)) {
    const input = String(spec?.input || '');
    const re = new RegExp(STEP_REF_RE.source, 'gu');
    let m: RegExpExecArray | null;
    while ((m = re.exec(input)) !== null) {
      if (!(m[1] in nodes)) {
        issues.push(`节点 "${id}" 的 input 引用了不存在的节点 "${m[1]}"`);
      }
    }
  }

  return issues;
}
