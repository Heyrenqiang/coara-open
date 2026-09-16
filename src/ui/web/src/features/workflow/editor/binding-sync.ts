/**
 * Sync data-binding edges → 内核节点 input 模板。
 *
 * 内核图没有独立 data 边：数据流用最小模板表达在 node.input
 * （{{steps.y.text}} / 字面量种子）。画布上的数据绑定边只是模板的
 * 可视化——拖线建绑定时把模板写进目标节点的 input，删除绑定边时把
 * 对应模板从 input 中剥除。
 */

import {
  isDataSourceHandle,
  isDataTargetHandle,
  getStepId,
} from './node-port-schema';
import { token } from './theme';
import type { ReactFlowNode, ReactFlowEdge, PortNodeLike } from './types';

/** Base fields required to construct a React Flow edge. */
interface EdgeCreateParams {
  id: string;
  source: string;
  target: string;
  sourceHandle?: string | null;
  targetHandle?: string | null;
  [key: string]: unknown;
}

/** 数据源出口 → {{…}} 模板。 */
export function templateFromSourceHandle(
  node: PortNodeLike | null | undefined,
  sourceHandle: string | null | undefined,
): string | null {
  if (!sourceHandle) return null;
  if (sourceHandle.startsWith('out:')) {
    const stepId = getStepId(node);
    if (!stepId) return null;
    return `{{steps.${stepId}.${sourceHandle.slice(4)}}}`;
  }
  return null;
}

function isDataBindingEdge(edge: ReactFlowEdge): boolean {
  return isDataSourceHandle(edge.sourceHandle) && isDataTargetHandle(edge.targetHandle);
}

/**
 * 把全部数据绑定边应用到节点 data：in:input 绑定把模板写入 input 字符串。
 * 只对"连线新建的绑定边"（带 data.template）强制写回；仅 data.ref 的
 * 模板推导边不回写——用户可在输入框自由删除推导出的模板，不会被立即还原。
 * 已含该模板则不重复写；已有其他内容且不含该模板时以空格衔接。
 */
export function applyBindingsToNodes(
  nodes: ReactFlowNode[] | null | undefined,
  edges: ReactFlowEdge[] | null | undefined,
): ReactFlowNode[] {
  const dataEdges = (edges || []).filter(isDataBindingEdge);
  if (dataEdges.length === 0) return nodes || [];

  return (nodes || []).map((node) => {
    const bindings = dataEdges.filter((e) => e.target === node.id && e.targetHandle === 'in:input');
    if (!bindings.length) return node;

    let input = String(node.data?.input ?? '');
    let changed = false;
    for (const edge of bindings) {
      const template = edge.data?.template as string | undefined;
      if (!template || input.includes(template)) continue;
      input = input.trim() ? `${input.trim()} ${template}` : template;
      changed = true;
    }
    if (!changed) return node;
    return { ...node, data: { ...node.data, input } };
  });
}

/** 删除绑定边时，把对应模板从目标节点 input 中剥除。 */
export function stripTemplateFromInput(
  nodes: ReactFlowNode[] | null | undefined,
  removed: { target: string; template: string | null }[],
): ReactFlowNode[] {
  if (!removed.length) return nodes || [];
  return (nodes || []).map((node) => {
    const clearings = removed.filter((c) => c.target === node.id);
    if (!clearings.length) return node;
    let input = String(node.data?.input ?? '');
    for (const { template } of clearings) {
      if (!template) continue;
      input = input.split(template).join('').replace(/\s{2,}/g, ' ').trim();
    }
    return { ...node, data: { ...node.data, input } };
  });
}

export function buildDataEdge(params: EdgeCreateParams, template?: string): ReactFlowEdge {
  return {
    ...params,
    type: 'routable',
    animated: true,
    className: 'edge-data-flow',
    style: { stroke: 'var(--wf-color-brand)', strokeWidth: 1, strokeDasharray: '3 3' },
    markerEnd: { type: 'arrowclosed', color: token('color.brand'), width: 20, height: 20 },
    zIndex: 5,
    data: { ...(params.data as Record<string, unknown> | undefined), template },
  };
}

/** 拓扑边（内核边）；on 由 data.on 携带（success | error）。 */
export function buildExecEdge(params: EdgeCreateParams): ReactFlowEdge {
  const on = (params.data as { on?: string } | undefined)?.on === 'error' ? 'error' : 'success';
  if (on === 'error') {
    return {
      ...params,
      type: 'default',
      className: 'edge-exec edge-exec-error',
      label: 'error',
      labelShowBg: true,
      labelStyle: { fill: 'var(--wf-color-error)', fontSize: 10 },
      labelBgStyle: { fill: 'var(--wf-color-bg)' },
      style: { stroke: 'var(--wf-color-error)', strokeWidth: 1.5, strokeDasharray: '6 3' },
      markerEnd: { type: 'arrowclosed', color: token('color.error'), width: 20, height: 20 },
    };
  }
  return {
    ...params,
    type: 'default',
    className: 'edge-exec',
    style: { stroke: 'var(--wf-color-text-tertiary)', strokeWidth: 1.5 },
    markerEnd: { type: 'arrowclosed', color: token('color.text-tertiary'), width: 20, height: 20 },
  };
}
