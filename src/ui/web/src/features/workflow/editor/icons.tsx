/**
 * SVG icon system as React components.
 *
 * 原为返回 SVG 字符串的工厂（消费方经 dangerouslySetInnerHTML 注入），
 * 现改为直接渲染 SVG 元素的 React 组件。图形与配色语义保持原样：
 * 一律用 currentColor 继承文字色，尺寸默认 16px。
 */

import type { CSSProperties, ReactNode } from 'react';
import {
  PlayCircleOutlined,
  RobotOutlined,
  BorderOutlined,
} from '@ant-design/icons';

const DEFAULT_SIZE = 16;

interface IconProps {
  size?: number;
  style?: CSSProperties;
  className?: string;
}

type IconComponent = (props: IconProps) => ReactNode;

interface SvgProps extends IconProps {
  spin?: boolean;
  children: ReactNode;
}

function Svg({ size = DEFAULT_SIZE, style, className, spin = false, children }: SvgProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      style={{ display: 'block', ...style }}
      className={className}
    >
      {spin && (
        <animateTransform
          attributeName="transform"
          type="rotate"
          from="0 8 8"
          to="360 8 8"
          dur="0.8s"
          repeatCount="indefinite"
        />
      )}
      {children}
    </svg>
  );
}

// --- Status icons ---

export function SpinnerIcon(props: IconProps) {
  return (
    <Svg {...props} spin>
      <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="2" strokeDasharray="28" strokeLinecap="round" opacity="0.3" />
      <path d="M14 8a6 6 0 0 0-6-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </Svg>
  );
}

export function CheckIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3 8.5l3.5 3.5L13 5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </Svg>
  );
}

export function AlertIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.5" opacity="0.4" />
      <path d="M8 4.5v4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      <circle cx="8" cy="11" r="0.8" fill="currentColor" />
    </Svg>
  );
}

export function HourglassIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4 3h8M4 13h8M5 3c0 3 3 4 3 5s-3 2-3 5M11 3c0 3-3 4-3 5s3 2 3 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </Svg>
  );
}

export function MinusIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.5" opacity="0.4" />
      <path d="M5 8h6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </Svg>
  );
}

// --- Toolbar icons ---

export function CopyIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="5" y="5" width="8" height="8" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M3 11V3.5C3 3 3.5 2.5 4 2.5h6.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </Svg>
  );
}

export function TrashIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3 5h10M6 5V3.5C6 3 6.5 2.5 7 2.5h2c0.5 0 1 0.5 1 1V5M5 5l0.5 8c0 0.5 0.5 1 1 1h3c0.5 0 1-0.5 1-1L11 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </Svg>
  );
}

export function InfoIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M8 7v4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="8" cy="5" r="0.8" fill="currentColor" />
    </Svg>
  );
}

/** Map a status string to its icon component. */
const STATUS_ICONS: Record<string, IconComponent | null> = {
  running: SpinnerIcon,
  success: CheckIcon,
  completed: CheckIcon,
  error: AlertIcon,
  failed: AlertIcon,
  pending: HourglassIcon,
  waiting: HourglassIcon,
  skipped: MinusIcon,
  idle: null,
};

/** Render a status icon by status string; renders nothing for idle/unknown. */
export function StatusIcon({ status, ...props }: IconProps & { status: string }) {
  const Cmp = STATUS_ICONS[status];
  return Cmp ? <Cmp {...props} /> : null;
}

/* ──────────────────────────────────────────────────────────────────────
 * Node type icons — 节点类型语义图标，统一走 @ant-design/icons
 * ────────────────────────────────────────────────────────────────────── */

type AntdIconComponent = (props: { style?: CSSProperties; className?: string }) => ReactNode;

const NODE_TYPE_ICONS: Record<string, AntdIconComponent> = {
  agent: RobotOutlined,
  run: PlayCircleOutlined,
};

/** 节点类型 → antd 图标。未知类型回退为中性方框。 */
export function NodeTypeIcon({
  type,
  style,
  className,
}: {
  type: string;
  style?: CSSProperties;
  className?: string;
}) {
  const Cmp = NODE_TYPE_ICONS[type] ?? BorderOutlined;
  return <Cmp style={style} className={className} />;
}
