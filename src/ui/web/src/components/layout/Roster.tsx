import type { CSSProperties, ReactNode } from "react";

/**
 * 全站名录骨架：细线分行语言（与配置页 configChrome 同源，供配置页之外的页面使用）
 *
 * 视觉规则（docs/Web设计体系.md 节奏契约）
 * - 行 垂直 padding 10-12px，行间 1px var(--coara-border-faint)
 * - hover 出 --coara-row-hover 洗色，选中左侧 2px --coara-row-active-bar 竖条 + --coara-accent-subtle 底
 * - 层级 分组标签 12px tertiary、条目标题 14/600、辅文 12px tertiary
 */

export const rosterRowTitle: CSSProperties = {
  fontSize: 14,
  fontWeight: 600,
  color: "var(--coara-text)",
  lineHeight: 1.35,
};

export const rosterRowMeta: CSSProperties = {
  marginTop: 3,
  fontSize: 12,
  lineHeight: 1.5,
  color: "var(--coara-label)",
};

interface RosterRowProps {
  /** 选中态：左侧 2px accent 竖条 + accent-subtle 底 */
  active?: boolean;
  onClick?: () => void;
  /** 左侧主槽（标题行） */
  children: ReactNode;
  /** 右侧槽位（时间、操作…） */
  extra?: ReactNode;
  /** 末行可关底部分隔线 */
  last?: boolean;
}

export function RosterRow({ active = false, onClick, children, extra, last = false }: RosterRowProps) {
  return (
    <div
      role={onClick ? "button" : undefined}
      onClick={onClick}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        minWidth: 0,
        padding: "10px 12px",
        borderLeft: active ? "2px solid var(--coara-row-active-bar)" : "2px solid transparent",
        borderBottom: last ? "none" : "1px solid var(--coara-border-faint)",
        background: active ? "var(--coara-accent-subtle)" : "transparent",
        cursor: onClick ? "pointer" : "default",
        transition: "background var(--coara-transition)",
      }}
      onMouseEnter={(e) => {
        if (!active && onClick) e.currentTarget.style.background = "var(--coara-row-hover)";
      }}
      onMouseLeave={(e) => {
        if (!active && onClick) e.currentTarget.style.background = "transparent";
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>{children}</div>
      {extra ? <div style={{ flexShrink: 0, display: "inline-flex", alignItems: "center", gap: 6 }}>{extra}</div> : null}
    </div>
  );
}

/** 分组标签行：12px tertiary 小字 + 可选计数/右侧槽位，可折叠时左侧出箭头 */
interface RosterGroupProps {
  label: ReactNode;
  count?: number;
  open?: boolean;
  onToggle?: () => void;
  extra?: ReactNode;
}

export function RosterGroup({ label, count, open, onToggle, extra }: RosterGroupProps) {
  return (
    <div
      role={onToggle ? "button" : undefined}
      onClick={onToggle}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        padding: "8px 12px 6px",
        fontSize: 12,
        color: "var(--coara-label)",
        cursor: onToggle ? "pointer" : "default",
        userSelect: "none",
        borderBottom: "1px solid var(--coara-border-faint)",
        background: "var(--coara-surface)",
      }}
    >
      {onToggle ? (
        <span
          style={{
            display: "inline-block",
            fontSize: 10,
            transform: open ? "rotate(90deg)" : "none",
            transition: "transform var(--coara-transition)",
          }}
        >
          ▸
        </span>
      ) : null}
      <span style={{ fontWeight: 600, color: "var(--coara-text-secondary)" }}>{label}</span>
      {typeof count === "number" ? <span>{count}</span> : null}
      {extra ? <span style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 6 }}>{extra}</span> : null}
    </div>
  );
}

/** 名录分区：小号大写标签 + 内容（对齐 SectionCard variant=label 的节奏） */
interface RosterSectionProps {
  title?: ReactNode;
  extra?: ReactNode;
  children: ReactNode;
}

export function RosterSection({ title, extra, children }: RosterSectionProps) {
  return (
    <section>
      {title ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: 8,
            fontSize: 11,
            fontWeight: 600,
            letterSpacing: 0.4,
            textTransform: "uppercase",
            color: "var(--coara-label)",
          }}
        >
          <span>{title}</span>
          {extra}
        </div>
      ) : null}
      {children}
    </section>
  );
}
