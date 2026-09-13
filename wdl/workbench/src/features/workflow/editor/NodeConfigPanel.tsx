/**
 * NodeConfigPanel — config panel for the selected node (kernel graph).
 *
 * 智能体节点四项：任务 task（多行）、输入 input（模板）、路由 routes
 * （all/one）、激活上限 max_activations（可选）。无选中时编辑文档级
 * 说明/全局激活上限/启动方式。
 */

import { memo, useState, type ReactNode } from 'react';
import { ConfigField } from './ConfigField';
import { ExpressionField } from './ExpressionField';
import { SchedulePanel, type ScheduleValue } from './SchedulePanel';
import type { KernelDocument, ReactFlowNode, ReactFlowEdge } from './types';

interface ConfigSectionProps {
  title: string;
  children: ReactNode;
  defaultOpen?: boolean;
}

function ConfigSection({ title, children, defaultOpen = true }: ConfigSectionProps) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={`config-section${open ? '' : ' config-section-collapsed'}`}>
      <div
        className="config-section-header"
        onClick={() => setOpen((o) => !o)}
        role="button"
        aria-expanded={open}
      >
        <span>{title}</span>
        <span className="config-section-toggle">▾</span>
      </div>
      {open && <div className="config-section-body">{children}</div>}
    </div>
  );
}

export interface NodeConfigPanelProps {
  selectedNode: ReactFlowNode | null;
  wdlDocument: KernelDocument;
  nodes: ReactFlowNode[];
  edges: ReactFlowEdge[];
  updateSelectedNode: (field: string, value: unknown) => void;
  /** 更新文档级 schedule */
  updateDocumentSchedule?: (schedule: ScheduleValue | null) => void;
  /** 更新文档级字段（description / max_activations） */
  updateDocumentMeta?: (patch: { description?: string; max_activations?: number | null }) => void;
}

export const NodeConfigPanel = memo(function NodeConfigPanel({
  selectedNode,
  wdlDocument,
  nodes,
  edges,
  updateSelectedNode,
  updateDocumentSchedule,
  updateDocumentMeta,
}: NodeConfigPanelProps) {
  // 节点级 LLM 覆盖：WDL 软件无模型目录接口，手写 provider/model，留空跟随 providers.yaml 默认

  if (!selectedNode) {
    return (
      <div className="node-config-panel">
        <div className="form-hint">点画布上的节点可改任务与路由；下面设置整份工作流。</div>
        <div className="form-group">
          <label>说明</label>
          <input
            className="config-input"
            value={String(wdlDocument.description || '')}
            placeholder="可选"
            onChange={(e) => updateDocumentMeta?.({ description: e.target.value })}
          />
        </div>
        <div className="form-group">
          <label>全局激活上限</label>
          <input
            className="config-input"
            type="number"
            min={1}
            value={
              typeof wdlDocument.max_activations === 'number' && wdlDocument.max_activations > 0
                ? String(wdlDocument.max_activations)
                : ''
            }
            placeholder="默认 100"
            onChange={(e) => {
              const v = Number(e.target.value);
              updateDocumentMeta?.({ max_activations: Number.isFinite(v) && v > 0 ? Math.round(v) : null });
            }}
          />
        </div>
        <SchedulePanel
          title="整图何时启动"
          variant="document"
          value={(wdlDocument.schedule as ScheduleValue) || { kind: 'manual' }}
          onChange={(next) => updateDocumentSchedule?.(next || { kind: 'manual' })}
        />
      </div>
    );
  }

  const data = selectedNode.data;

  // 智能体节点
  const maxActivations =
    typeof data.max_activations === 'number' && data.max_activations > 0
      ? String(data.max_activations)
      : '';

  return (
    <div className="node-config-panel">
      <ConfigSection key="task" title="任务">
        <ConfigField
          label=""
          type="textarea"
          value={String(data.task ?? '')}
          onChange={(val) => updateSelectedNode('task', val)}
          placeholder="完整任务指令：目标 / 范围 / 方法 / 规则 / 验收 / 回报格式"
        />
      </ConfigSection>

      <ConfigSection key="input" title="输入（数据进模板）">
        <ExpressionField
          label=""
          value={String(data.input ?? '')}
          onChange={(v) => updateSelectedNode('input', v)}
          multiline
          nodes={nodes}
          edges={edges}
          selectedNodeId={selectedNode.id}
          placeholder="可写 {{steps.y.text}} 引用上游；也可直接写字面量种子"
        />
      </ConfigSection>

      <ConfigSection key="routes" title="出边路由">
        <div className="config-inline-row">
          <label>routes</label>
          <select
            className="config-input"
            value={data.routes === 'one' ? 'one' : 'all'}
            onChange={(e) => updateSelectedNode('routes', e.target.value === 'one' ? 'one' : 'all')}
          >
            <option value="all">all · 全部出边投递（并行扇出）</option>
            <option value="one">one · 每次激活选一条（分支路由）</option>
          </select>
        </div>
        <div className="form-hint">
          {data.routes === 'one'
            ? '交互运行时由节点 deliver(next=…) 挑后继；无人值守按静态顺序取第一条'
            : '完成后向全部出边投递到达'}
        </div>
      </ConfigSection>

      <ConfigSection key="llm" title="节点模型" defaultOpen={false}>
        <div className="config-inline-row">
          <label>provider</label>
          <input
            className="config-input"
            value={String(data.provider ?? '')}
            placeholder="留空跟随 providers.yaml 默认"
            onChange={(e) => updateSelectedNode('provider', e.target.value || null)}
          />
        </div>
        <div className="config-inline-row">
          <label>model</label>
          <input
            className="config-input"
            value={String(data.model ?? '')}
            placeholder="留空取该 provider 默认模型"
            onChange={(e) => updateSelectedNode('model', e.target.value || null)}
          />
        </div>
        <div className="form-hint">
          默认跟随 providers.yaml 全局配置；仅当该节点有特殊模型需求时才覆盖。
        </div>
      </ConfigSection>

      <ConfigSection key="limits" title="激活上限" defaultOpen={false}>
        <ConfigField
          label="max_activations"
          type="number"
          value={maxActivations}
          onChange={(val) => {
            const v = Number(val);
            updateSelectedNode('max_activations', Number.isFinite(v) && v > 0 ? Math.round(v) : null);
          }}
          placeholder="留空继承全局上限"
        />
      </ConfigSection>
    </div>
  );
});
