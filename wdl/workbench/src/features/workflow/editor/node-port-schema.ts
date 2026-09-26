/**
 * Port schema for coara workflow canvas (kernel projection).
 *
 * 2026-08-16 内核化后画布只有智能体一种节点：
 *   - agent（智能体）：exec-in / exec-out 执行口 + in:input 数据入口 +
 *     out:text 数据出口（{{steps.<id>.text}}）
 *
 * 2026-08-16 输入归节点：图级 inputs 声明与输入伪节点已移除，节点 input
 * 是唯一输入接口（{{steps.y.text}} 引用上游 + 字面量种子）。
 *
 * Handle id conventions:
 *   exec-in | exec-out           — 拓扑边（内核边）
 *   in:input                      — 数据进模板目标（写入 node.input）
 *   out:text                      — 上游节点回复文本 {{steps.<id>.text}}
 */

import type {
  PortNodeLike,
  NodeData,
  InputPort,
  OutputPort,
  ExecPorts,
} from './types';

export function isDataTargetHandle(handle: unknown): boolean {
  return typeof handle === 'string' && handle.startsWith('in:');
}

export function isDataSourceHandle(handle: unknown): boolean {
  return typeof handle === 'string' && handle.startsWith('out:');
}

export function isExecSourceHandle(handle: unknown): boolean {
  return handle === 'exec-out';
}

export function isExecTargetHandle(handle: unknown): boolean {
  return handle === 'exec-in';
}

export function getNodeType(node: PortNodeLike | null | undefined): string {
  const data: NodeData = node?.data || {};
  return data.nodeType || 'agent';
}

export function getStepId(node: PortNodeLike | null | undefined): string {
  const data: NodeData = node?.data || {};
  if (data.stepId) return String(data.stepId);
  return node?.id || '';
}

export function getInputPorts(node: PortNodeLike | null | undefined): InputPort[] {
  const data: NodeData = node?.data || {};

  // 智能体节点：单一数据进模板（node.input）
  return [
    {
      key: 'input',
      label: '输入',
      template: String(data.input ?? ''),
      handleId: 'in:input',
    },
  ];
}

export function getOutputPorts(node: PortNodeLike | null | undefined): OutputPort[] {
  const stepId = getStepId(node);

  // 智能体节点：上游可引用的只有回复文本 {{steps.<id>.text}}
  if (stepId) {
    return [
      { key: 'text', label: 'text', template: `{{steps.${stepId}.text}}`, handleId: 'out:text' },
    ];
  }
  return [];
}

export function getExecPorts(_node: PortNodeLike | null | undefined): ExecPorts {
  return { hasExecIn: true, hasExecOut: true };
}
