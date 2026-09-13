/**
 * NodeToolbar — floating action bar shown on hover/selected.
 *
 * 参考 Flowise NodeToolbarActions 的设计理念（hover 显示快捷操作），
 * 但代码原创：[复制] [信息] [删除] 三个按钮，SVG 图标组件来自 icons.tsx。
 */

import { memo, type ReactNode } from 'react';
import { CopyIcon, InfoIcon, TrashIcon } from './icons';
import { cx, swallow } from './dom';

interface ToolbarButtonProps {
  icon: ReactNode;
  label: string;
  onClick?: (e: React.MouseEvent) => void;
  destructive?: boolean;
}

function ToolbarButton({ icon, label, onClick, destructive }: ToolbarButtonProps) {
  return (
    <button
      type="button"
      className={cx('node-toolbar-btn', destructive && 'node-toolbar-btn-danger')}
      title={label}
      aria-label={label}
      onClick={(e) => { swallow(e); onClick && onClick(e); }}
      onMouseDown={(e) => swallow(e)}
    >
      {icon && (
        <span className="node-toolbar-btn-icon">{icon}</span>
      )}
    </button>
  );
}

interface NodeToolbarProps {
  visible: boolean;
  onCopy?: (e: React.MouseEvent) => void;
  onInfo?: (e: React.MouseEvent) => void;
  onDelete?: (e: React.MouseEvent) => void;
}

function NodeToolbarComponent({ visible, onCopy, onInfo, onDelete }: NodeToolbarProps) {
  if (!visible) return null;
  return (
    <div
      className="node-toolbar"
      role="toolbar"
      aria-label="节点操作"
      onMouseDown={(e) => swallow(e)}
    >
      <ToolbarButton
        icon={<CopyIcon size={13} />}
        label="复制配置"
        onClick={onCopy}
      />
      <ToolbarButton
        icon={<InfoIcon size={13} />}
        label="节点详情"
        onClick={onInfo}
      />
      {onDelete && (
        <ToolbarButton
          icon={<TrashIcon size={13} />}
          label="删除节点"
          onClick={onDelete}
          destructive
        />
      )}
    </div>
  );
}

export const NodeToolbar = memo(NodeToolbarComponent);
