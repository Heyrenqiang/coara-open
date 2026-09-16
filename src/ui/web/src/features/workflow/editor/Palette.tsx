/**
 * Palette — node palette / toolbar for dragging new nodes onto the canvas.
 *
 * 2026-08-16 内核化：图上只有智能体一种节点——控制流是图的形状
 * （扇出/扇入/回边/error 边），不再是可拖的节点类型。
 */

import { Button } from 'antd';
import { CloseOutlined } from '@ant-design/icons';
import { token } from './theme';
import { NodeTypeIcon } from './icons';

interface PaletteItemType {
  type: string;
  label: string;
  icon: string;
  color: unknown;
  desc: string;
}

/** 画布节点类型：唯一一种——智能体节点。 */
const NODE_TYPES_LIST: PaletteItemType[] = [
  {
    type: 'agent',
    label: '智能体节点',
    icon: '▶',
    color: token('color.node.run-accent'),
    desc: '一个智能体执行任务；并行/分支/循环由连线形状表达',
  },
];

export interface PaletteProps {
  width: number;
  onClose: () => void;
  onDragStart: (e: React.DragEvent, nodeType: PaletteItemType) => void;
  /** 右边缘拖宽 */
  onResizePointerDown?: (e: React.PointerEvent) => void;
}

export function Palette({ width, onClose, onDragStart, onResizePointerDown }: PaletteProps) {
  return (
    <div
      className="side-panel-floating side-left palette-panel"
      style={{ width, overflow: 'visible' }}
    >
      {onResizePointerDown && (
        <div
          className="panel-edge-resize panel-edge-resize-e"
          title="拖动边缘调整宽度"
          role="separator"
          aria-orientation="vertical"
          onPointerDown={(e) => {
            e.preventDefault();
            e.stopPropagation();
            (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
            onResizePointerDown(e);
          }}
        />
      )}
      <div className="panel-header">
        <span>节点库</span>
        <Button
          type="text"
          size="small"
          className="btn-ghost btn-small"
          title="关闭节点库"
          icon={<CloseOutlined />}
          onClick={onClose}
        />
      </div>
      <div className="panel-scroll">
        {NODE_TYPES_LIST.map((item) => (
          <div
            key={item.type}
            className="palette-item"
            draggable
            onDragStart={(e) => onDragStart(e, item)}
          >
            <div className={`palette-icon palette-icon-${item.type}`}>
              <NodeTypeIcon type={item.type} />
            </div>
            <div className="palette-text-wrap">
              <div className="palette-text">{item.label}</div>
              <div className="palette-desc">{item.desc}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
