/**
 * WDL template suggestions for expression fields (kernel projection).
 *
 * 内核图数据流只有一类引用：{{steps.<id>.text}}（上游节点回复文本）。
 * 建议列表按拓扑上游过滤。
 */

import type {
  ReactFlowNode,
  ReactFlowEdge,
  SuggestionRow,
  InsertExpressionResult,
} from './types';

export function isExecFlowEdge(edge: ReactFlowEdge | null | undefined): boolean {
  return edge?.sourceHandle === 'exec-out' && edge?.targetHandle === 'exec-in';
}

/** Node ids reachable before `nodeId` along kernel edges (reverse walk, 含回边上游)。 */
export function upstreamStepIds(
  nodeId: string,
  edges: ReactFlowEdge[] | null | undefined,
): Set<string> {
  const rev: Record<string, string[]> = {};
  for (const e of edges || []) {
    if (!isExecFlowEdge(e)) continue;
    if (!e.target) continue;
    (rev[e.target] = rev[e.target] || []).push(e.source);
  }
  const seen = new Set<string>();
  const upstream = new Set<string>();
  const stack = [nodeId];
  while (stack.length) {
    const id = stack.pop();
    if (id === undefined) continue;
    for (const src of rev[id] || []) {
      if (seen.has(src)) continue;
      seen.add(src);
      upstream.add(src);
      stack.push(src);
    }
  }
  return upstream;
}

function stepSuggestions(node: ReactFlowNode | null | undefined): SuggestionRow[] {
  if (!node) return [];
  const stepId = node.data?.stepId || node.id;
  const label = node.data?.label || stepId;
  return [
    {
      template: `{{steps.${stepId}.text}}`,
      label: `${label} · text`,
      group: '上游节点',
    },
  ];
}

/** Context for building expression suggestions. */
export interface SuggestionContext {
  nodes: ReactFlowNode[] | null | undefined;
  edges: ReactFlowEdge[] | null | undefined;
  selectedNodeId: string;
}

export function buildExpressionSuggestions(ctx: SuggestionContext): SuggestionRow[] {
  const { nodes, edges, selectedNodeId } = ctx;
  const nodeById = Object.fromEntries(
    (nodes || []).map((n) => [n.id, n]),
  ) as Record<string, ReactFlowNode>;
  const upstream = upstreamStepIds(selectedNodeId, edges);
  const rows: SuggestionRow[] = [];
  for (const id of upstream) {
    rows.push(...stepSuggestions(nodeById[id]));
  }

  const deduped: SuggestionRow[] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    if (seen.has(row.template)) continue;
    seen.add(row.template);
    deduped.push(row);
  }
  return deduped;
}

/** Text after last unclosed `{{` before cursor, or null. */
export function expressionQueryAtCursor(
  value: string | null | undefined,
  cursor?: number | null,
): string | null {
  const text = String(value ?? '');
  const pos = cursor ?? text.length;
  const before = text.slice(0, pos);
  const openIdx = before.lastIndexOf('{{');
  if (openIdx < 0) return null;
  const afterOpen = before.slice(openIdx + 2);
  if (afterOpen.includes('}}')) return null;
  return afterOpen;
}

export function filterSuggestions(
  suggestions: SuggestionRow[],
  query: string | null | undefined,
): SuggestionRow[] {
  const q = (query || '').trim().toLowerCase();
  if (!q) return suggestions;
  return suggestions.filter((s) => {
    const hay = `${s.template} ${s.label} ${s.group}`.toLowerCase();
    return hay.includes(q);
  });
}

/**
 * Insert template at cursor, replacing an in-progress `{{…` fragment when present.
 */
export function insertExpressionAtCursor(
  value: string | null | undefined,
  cursor: number | null | undefined,
  template: string,
): InsertExpressionResult {
  const text = String(value ?? '');
  const pos = cursor ?? text.length;
  const before = text.slice(0, pos);
  const after = text.slice(pos);
  const openIdx = before.lastIndexOf('{{');
  if (openIdx >= 0 && !before.slice(openIdx).includes('}}')) {
    const closeInAfter = after.indexOf('}}');
    const suffix = closeInAfter >= 0 ? after.slice(closeInAfter + 2) : after;
    const newValue = text.slice(0, openIdx) + template + suffix;
    const newCursor = openIdx + template.length;
    return { value: newValue, cursor: newCursor };
  }
  const newValue = before + template + after;
  return { value: newValue, cursor: before.length + template.length };
}
