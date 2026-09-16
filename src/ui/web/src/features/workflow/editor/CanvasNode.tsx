/**
 * CanvasNode — 圆形智能体节点（React Flow 渲染器）。
 *
 * 2026-08-18 圆形化：节点的本质是一个智能体，渲染为圆形——类型图标居中，
 * 标题在圆上方、任务摘要在圆下方（均外置居中，不占圆面）。
 * 端口按角度落在圆周上：执行口在左右（180° / 0°，主链路水平穿过），
 * 数据口沿左下 / 右下圆弧分布；数据口空心=未绑定上游，实心=已绑定（template 含 {{…}}）。
 */

import { createContext, memo, useContext, useState } from 'react';
import { Handle, Position, type NodeProps, type Node } from '@xyflow/react';
import { getInputPorts, getOutputPorts, getExecPorts, getNodeType } from './node-port-schema';
import { NodeStatusBadge, STATUS_LABELS_CN } from './NodeStatusBadge';
import { NodeToolbar } from './NodeToolbar';
import { NodeTypeIcon } from './icons';
import { cx, copyToClipboard } from './dom';
import type { NodeData, PortNodeLike, InputPort, OutputPort } from './types';

/** React Flow node type carrying our NodeData. */
export type FlowNode = Node<NodeData, 'custom'>;

/** 圆面直径（px）：标题/摘要外置，圆面只放图标 */
const CIRCLE_SIZE = 96;

/** Node-level callbacks provided by WorkflowEditor via context so that
 *  node.data stays referentially stable and React.memo can skip re-renders. */
interface NodeCallbacks {
  onInfo?: (stepId: string) => void;
  onDelete?: (stepId: string) => void;
}

export const NodeCallbacksContext = createContext<NodeCallbacks>({});

/** 角度 → 圆周上的百分比坐标（0°=右侧，y 轴向下为正顺时针） */
function angleToPos(angleDeg: number): { left: string; top: string } {
  const rad = (angleDeg * Math.PI) / 180;
  return {
    left: `${50 + 50 * Math.cos(rad)}%`,
    top: `${50 + 50 * Math.sin(rad)}%`,
  };
}

/** n 个端口在 center 角两侧对称铺开（总跨度 spread 度） */
function arcAngles(count: number, center: number, spread: number): number[] {
  if (count <= 1) return [center];
  const start = center - spread / 2;
  return Array.from({ length: count }, (_, i) => start + (spread * i) / (count - 1));
}

interface PortHandleProps {
  handleType: 'target' | 'source';
  position: Position;
  id: string;
  label: string;
  template?: string;
  angle: number;
  exec?: boolean;
}

function PortHandle({ handleType, position, id, label, template, angle, exec }: PortHandleProps) {
  const bound = !!template && template.includes('{{');
  return (
    <Handle
      type={handleType}
      position={position}
      id={id}
      className={cx(exec ? 'handle-exec' : 'handle-data', !exec && bound && 'port-bound')}
      style={{ ...angleToPos(angle), transform: 'translate(-50%, -50%)' }}
      title={template || label}
    />
  );
}

function CanvasNodeComponent({ id, data, selected }: NodeProps<FlowNode>) {
  const node: PortNodeLike = { id, data };
  const nodeType = getNodeType(node);
  const stepId = data.stepId || id;
  const inputPorts: InputPort[] = getInputPorts(node);
  const outputPorts: OutputPort[] = getOutputPorts(node);
  const execPorts = getExecPorts(node);
  const [isHovered, setIsHovered] = useState(false);
  const showToolbar = selected || isHovered;

  // onInfo / onDelete are provided by WorkflowEditor via NodeCallbacksContext
  // (keeps node.data referentially stable so React.memo can bail out).
  const callbacks = useContext(NodeCallbacksContext);
  const onInfo = callbacks.onInfo;
  const onDelete = callbacks.onDelete;

  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    await copyToClipboard(
      JSON.stringify(
        {
          task: data.task,
          input: data.input,
          routes: data.routes,
          ...(data.max_activations ? { max_activations: data.max_activations } : {}),
          ...(data.provider ? { provider: data.provider } : {}),
          ...(data.model ? { model: data.model } : {}),
        },
        null,
        2,
      ),
    );
  };
  const handleInfo = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (onInfo) onInfo(stepId);
  };
  const handleDelete = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (onDelete) onDelete(stepId);
  };

  // 执行口水平穿心（左进右出）；数据口贴左下 / 右下圆弧，避开执行口与下方摘要
  const inputAngles = arcAngles(inputPorts.length, 150, 44);
  const outputAngles = arcAngles(outputPorts.length, 30, 44);

  return (
    <div
      className={cx(
        'custom-node',
        selected && 'selected',
        `node-type-${nodeType}`,
      )}
      style={{
        position: 'relative',
        width: CIRCLE_SIZE,
        height: CIRCLE_SIZE,
        boxSizing: 'border-box',
      }}
      role="group"
      aria-label={`${data.label || stepId}，状态 ${STATUS_LABELS_CN[data.status || 'idle'] || data.status || '空闲'}`}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
    >
      <NodeToolbar
        visible={showToolbar}
        onCopy={handleCopy}
        onInfo={handleInfo}
        onDelete={handleDelete}
      />

      <NodeStatusBadge status={data.status} error={data.error as string | undefined} />

      <span className="node-circle-icon" aria-hidden>
        <NodeTypeIcon type={nodeType} style={{ fontSize: 26 }} />
      </span>

      {execPorts.hasExecIn && (
        <PortHandle
          handleType="target"
          position={Position.Left}
          id="exec-in"
          label="执行输入"
          angle={180}
          exec
        />
      )}

      {inputPorts.map((port, i) => (
        <PortHandle
          key={port.handleId || `in:${port.key}`}
          handleType="target"
          position={Position.Left}
          id={port.handleId || `in:${port.key}`}
          label={port.label}
          template={port.template}
          angle={inputAngles[i]}
        />
      ))}

      <div className="node-title-external">
        <span className="node-title-external-text">{data.label || stepId}</span>
        {data.routes === 'one' && (
          <span className="node-routes-tag" title="one 路由：每次激活只选一条出边">
            one
          </span>
        )}
      </div>
      <div
        className={cx('node-desc-external', !data.desc && 'node-desc-muted')}
        title={data.desc || undefined}
      >
        {data.desc || '在右侧填写任务'}
      </div>

      {outputPorts.map((port, i) => (
        <PortHandle
          key={port.handleId || `out:${port.key}`}
          handleType="source"
          position={Position.Right}
          id={port.handleId || `out:${port.key}`}
          label={port.label}
          template={port.template}
          angle={outputAngles[i]}
        />
      ))}

      {execPorts.hasExecOut && (
        <PortHandle
          handleType="source"
          position={Position.Right}
          id="exec-out"
          label="执行输出"
          angle={0}
          exec
        />
      )}
    </div>
  );
}

export const CanvasNode = memo(CanvasNodeComponent);
