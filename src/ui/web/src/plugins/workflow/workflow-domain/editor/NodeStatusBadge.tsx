/**
 * NodeStatusBadge — status indicator on the top-right of a canvas node.
 *
 * 参考 Flowise NodeStatusIndicator 的设计理念（图标+颜色+tooltip），
 * 但代码原创：用 icons.ts 的 SVG 图标，用 theme.ts 的令牌颜色。
 */

import { memo } from 'react';
import { StatusIcon } from './icons';
import { token } from './theme';

const STATUS_COLORS: Record<string, unknown> = {
  running: token('color.status.running'),
  success: token('color.status.success'),
  completed: token('color.status.completed'),
  error: token('color.status.error'),
  failed: token('color.status.failed'),
  pending: token('color.status.pending'),
  waiting: token('color.status.waiting'),
  skipped: token('color.status.skipped'),
};

const STATUS_LABELS_CN: Record<string, string> = {
  running: '运行中',
  success: '完成',
  completed: '完成',
  error: '失败',
  failed: '失败',
  pending: '等待中',
  waiting: '等待中',
  skipped: '已跳过',
  idle: '空闲',
};

interface NodeStatusBadgeProps {
  status?: string;
  error?: string;
}

function NodeStatusBadgeComponent({ status, error }: NodeStatusBadgeProps) {
  if (!status || status === 'idle') return null;
  const color = STATUS_COLORS[status] as string | undefined;
  const label = STATUS_LABELS_CN[status] || status;
  const tooltip = status === 'error' || status === 'failed'
    ? (error ? `${label}: ${error}` : label)
    : label;

  return (
    <div
      className="node-status-badge"
      style={{ background: color }}
      title={tooltip}
      aria-label={`节点状态: ${label}`}
    >
      <span>
        <StatusIcon status={status} size={12} />
      </span>
    </div>
  );
}

export const NodeStatusBadge = memo(NodeStatusBadgeComponent);
export { STATUS_LABELS_CN };
