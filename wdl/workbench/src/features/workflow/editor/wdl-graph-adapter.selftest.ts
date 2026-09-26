/**
 * Kernel graph adapter selftest（内核化冒烟）：
 * 构造小图 → documentToReactFlow → 断言节点/边数与渲染细节。
 * Run: npx tsx src/features/workflow/editor/wdl-graph-adapter.selftest.ts
 */
import {
  documentToReactFlow,
  reactFlowToDocument,
  validateDocumentConsistency,
  taskSummary,
  kernelEdgeId,
  edgeIdOccupied,
} from './wdl-graph-adapter';
import type { KernelDocument } from './types';

function assert(cond: unknown, msg: string): void {
  if (!cond) throw new Error(msg);
}

const doc: KernelDocument = {
  name: '评审流水线',
  description: '调研写作评审',
  nodes: {
    调研: { task: '调研内核设计\n输出报告', input: '内核设计', routes: 'all' },
    写作: { task: '写初稿', input: '{{steps.调研.text}}', routes: 'all' },
    发布: { task: '发布', input: '', routes: 'one', max_activations: 5 },
  },
  edges: [
    { from: '调研', to: '写作' },
    { from: '写作', to: '发布', on: 'error' },
  ],
};

// 渲染：3 个智能体节点
{
  const { nodes, edges } = documentToReactFlow(doc);
  assert(nodes.length === 3, `应有 3 个画布节点，实际 ${nodes.length}`);
  const 调研 = nodes.find((n) => n.id === '调研')!;
  assert(调研.data.nodeType === 'agent', '节点应渲染为智能体');
  assert(调研.data.label === '调研' && 调研.data.desc === '调研内核设计', `标题/摘要不符：${JSON.stringify(调研.data)}`);
  const 发布 = nodes.find((n) => n.id === '发布')!;
  assert(发布.data.routes === 'one' && 发布.data.max_activations === 5, 'routes/max_activations 应保留');

  // 边：2 条拓扑边（其中 1 条 error）+ 1 条推导数据边（调研.text→写作）
  assert(edges.length === 3, `应有 3 条画布边（2 拓扑 + 1 数据绑定），实际 ${edges.length}`);
  const err = edges.find((e) => e.source === '写作' && e.target === '发布')!;
  assert(String(err.className).includes('edge-exec-error') && err.label === 'error', `error 边应为红色虚线带标签，实际 ${JSON.stringify(err)}`);
  const dataEdges = edges.filter((e) => e.className === 'edge-data-flow');
  assert(dataEdges.length === 1, `应推导 1 条数据绑定边，实际 ${dataEdges.length}`);
}

// 回写：画布 → 内核图 round-trip
{
  const { nodes, edges } = documentToReactFlow(doc);
  const back = reactFlowToDocument(doc, nodes, edges);
  assert(Object.keys(back.nodes).length === 3, `round-trip 应保留 3 个节点，实际 ${Object.keys(back.nodes).length}`);
  assert(back.edges.length === 2, `round-trip 应保留 2 条边，实际 ${back.edges.length}`);
  const errEdge = back.edges.find((e) => e.on === 'error');
  assert(errEdge && errEdge.from === '写作' && errEdge.to === '发布', 'error 边 round-trip 应保留 on=error');
  assert(back.nodes['发布'].max_activations === 5 && back.nodes['发布'].routes === 'one', '节点规格 round-trip 应保留');
  assert(back.name === '评审流水线' && !('inputs' in back), '文档级字段 round-trip 应保留，且不再有 inputs');
}

// 校验：空 task / 未知节点引用 / 重复边
{
  const issues = validateDocumentConsistency({
    ...doc,
    nodes: { ...doc.nodes, 空: { task: '', input: '', routes: 'all' } },
    edges: [...doc.edges, { from: '调研', to: '写作' }, { from: '调研', to: '幽灵', on: 'error' }],
  });
  assert(issues.some((s) => s.includes('空')), `空 task 应报问题：${issues}`);
  assert(issues.some((s) => s.includes('重复边')), `重复边应报问题：${issues}`);
  assert(issues.some((s) => s.includes('幽灵')), `未知节点引用应报问题：${issues}`);
  assert(validateDocumentConsistency(doc).length === 0, '正常文档应无问题');
}

// task 摘要截断
{
  assert(taskSummary('') === '', '空 task 摘要应为空');
  assert(taskSummary('第一行\n第二行') === '第一行', '摘要应取首行');
  const long = 'a'.repeat(50);
  assert(taskSummary(long).length === 42 && taskSummary(long).endsWith('…'), '长摘要应截断到 42 字符');
}

// 边 id 唯一性 + updateEdgeOn 防重复（review P2：a→b success + error 并存，
// 选中 error 切回 success 时不得与已有 success 边撞 id）
{
  assert(
    kernelEdgeId('a', 'b', 'success') !== kernelEdgeId('a', 'b', 'error'),
    'success/error 边 id 应不同',
  );
  const edges = [
    { id: kernelEdgeId('a', 'b', 'success') },
    { id: kernelEdgeId('a', 'b', 'error') },
  ];
  assert(
    edgeIdOccupied(edges, kernelEdgeId('a', 'b', 'success'), kernelEdgeId('a', 'b', 'error')),
    '切回 success 应检测到目标 id 已被占用',
  );
  assert(
    !edgeIdOccupied(edges, kernelEdgeId('a', 'b', 'success'), kernelEdgeId('a', 'b', 'success')),
    '忽略自身 id 时不应视为冲突',
  );
  assert(
    !edgeIdOccupied(edges, kernelEdgeId('a', 'c', 'success')),
    '不同目标节点不应视为冲突',
  );
}

// 成环图布局必须终止（历史 bug：error 兜底回边 → BFS 抬升死循环 → 画布卡死）
{
  const cyclic: KernelDocument = {
    name: '带回边的流水线',
    nodes: {
      采集: { task: '采集', input: '', routes: 'all' },
      整合: { task: '整合', input: '', routes: 'all' },
      兜底: { task: '失败重试', input: '', routes: 'all' },
    },
    edges: [
      { from: '采集', to: '整合' },
      { from: '整合', to: '兜底', on: 'error' },
      { from: '兜底', to: '采集' }, // 回边成环
    ],
  };
  const start = Date.now();
  const { nodes, edges } = documentToReactFlow(cyclic);
  assert(Date.now() - start < 2000, '成环图布局应在 2 秒内完成');
  assert(nodes.length === 3, `成环图应渲染 3 个节点，实际 ${nodes.length}`);
  assert(edges.length === 3, `成环图应渲染 3 条边，实际 ${edges.length}`);
  // 前向边正常抬层：整合 在 采集 下游
  const xOf = (id: string) => nodes.find((n) => n.id === id)!.position.x;
  assert(xOf('整合') > xOf('采集'), '前向边应抬升下游层');
  // 回边不抬层：兜底 不因此把 采集 顶到更深层（采集 保持第 0 层）
  assert(xOf('采集') === Math.min(xOf('采集'), xOf('整合'), xOf('兜底')), '回边不应把根节点顶层');

  // 纯环图也要终止
  const pureCycle: KernelDocument = {
    name: '纯环',
    nodes: { a: { task: 'a', input: '', routes: 'all' }, b: { task: 'b', input: '', routes: 'all' } },
    edges: [
      { from: 'a', to: 'b' },
      { from: 'b', to: 'a' },
    ],
  };
  const t2 = Date.now();
  const r2 = documentToReactFlow(pureCycle);
  assert(Date.now() - t2 < 2000, '纯环图布局应在 2 秒内完成');
  assert(r2.nodes.length === 2, '纯环图应渲染 2 个节点');
}

console.log('wdl-graph-adapter.selftest: OK');
