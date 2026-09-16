/**
 * binding-sync selftest：数据绑定边 → input 模板回写语义。
 *
 * review P2：纯推导边（仅 data.ref，画布从 input 模板推导出来）不得强制
 * 回写模板——用户在 ExpressionField 手动删除 {{steps.x.text}} 后不会被
 * 立即还原；只有连线新建的绑定边（带 data.template）才写回。
 * Run: npx tsx src/features/workflow/editor/binding-sync.selftest.ts
 */
import { applyBindingsToNodes, buildDataEdge } from './binding-sync';
import type { ReactFlowNode, ReactFlowEdge } from './types';

function assert(cond: unknown, msg: string): void {
  if (!cond) throw new Error(msg);
}

function makeAgent(id: string, input: string): ReactFlowNode {
  return { id, position: { x: 0, y: 0 }, data: { nodeType: 'agent', input } };
}

// 纯推导边（仅 data.ref）：不回写——用户删除模板后保持删除状态
{
  const nodes = [makeAgent('A', ''), makeAgent('B', '')];
  const edges: ReactFlowEdge[] = [
    {
      id: 'data_1',
      source: 'A',
      target: 'B',
      sourceHandle: 'out:text',
      targetHandle: 'in:input',
      data: { ref: 'steps.A.text' },
    },
  ];
  const out = applyBindingsToNodes(nodes, edges);
  const b = out.find((n) => n.id === 'B')!;
  assert(b.data.input === '', `纯推导边不应强制写回模板，实际 "${b.data.input}"`);
}

// 连线新建绑定边（带 data.template）：写回模板
{
  const nodes = [makeAgent('A', ''), makeAgent('B', '')];
  const edge = buildDataEdge(
    {
      id: 'data_x',
      source: 'A',
      target: 'B',
      sourceHandle: 'out:text',
      targetHandle: 'in:input',
    },
    '{{steps.A.text}}',
  );
  const out = applyBindingsToNodes(nodes, [edge]);
  const b = out.find((n) => n.id === 'B')!;
  assert(b.data.input === '{{steps.A.text}}', `带 template 的绑定边应写回模板，实际 "${b.data.input}"`);
}

// 已含模板不重复写，且保留已有其他内容
{
  const nodes = [makeAgent('A', ''), makeAgent('B', '{{steps.A.text}} 其它')];
  const edge = buildDataEdge(
    {
      id: 'data_x',
      source: 'A',
      target: 'B',
      sourceHandle: 'out:text',
      targetHandle: 'in:input',
    },
    '{{steps.A.text}}',
  );
  const out = applyBindingsToNodes(nodes, [edge]);
  const b = out.find((n) => n.id === 'B')!;
  assert(b.data.input === '{{steps.A.text}} 其它', `已含模板不应重复写，实际 "${b.data.input}"`);
}

console.log('binding-sync.selftest: OK');
