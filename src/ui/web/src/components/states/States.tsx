import type { ReactNode } from "react";
import { Spin } from "antd";
import { FileOutlined } from "@ant-design/icons";

/**
 * 页面四态（空 / 加载 / 错误 / 无权限）的统一表达。
 *
 * 设计契约见 docs/Web设计体系.md §4：每个页面都必须处理四态。
 * 「无权限」不单列组件 —— 用 ErrorState 表达（403 文案由调用方给）。
 *
 * `fill` 决定布局：true（默认）用 `flex:1` 撑满内容区居中；
 * 内容区已自带滚动/内边距时（如列表内的小块加载）传 `fill={false}`，
 * 走「内联块」形态 —— 纵向留白居中，不撑高。
 */

const ERROR_ICON = <FileOutlined style={{ fontSize: 40, color: "var(--coara-border)" }} />;
const EMPTY_ICON = <FileOutlined style={{ fontSize: 48, color: "var(--coara-text-tertiary)" }} />;

interface StateBlockProps {
  icon?: ReactNode;
  /** 主文案（14px） */
  title?: ReactNode;
  /** 次文案（12px，弱化） */
  description?: ReactNode;
  /** 操作按钮等 */
  action?: ReactNode;
  /** 撑满内容区居中（默认 true） */
  fill?: boolean;
}

/** 居中态基座：各状态组件共用，保证四态外观一致。 */
function StateBlock({ icon, title, description, action, fill = true }: StateBlockProps) {
  return (
    <div
      style={{
        ...(fill
          ? { flex: 1 }
          : { width: "100%", padding: "48px 0" }),
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 12,
        color: "var(--coara-text-secondary)",
      }}
    >
      {icon}
      {title ? <div style={{ fontSize: 14 }}>{title}</div> : null}
      {description ? (
        <div style={{ fontSize: 12, color: "var(--coara-text-tertiary)" }}>{description}</div>
      ) : null}
      {action}
    </div>
  );
}

interface LoadingStateProps {
  tip?: ReactNode;
  /** 撑满内容区居中（默认 true）；列表内小块加载传 false */
  fill?: boolean;
}

export function LoadingState({ tip = "加载中…", fill = true }: LoadingStateProps) {
  return (
    <div
      style={{
        ...(fill ? { flex: 1 } : { width: "100%", padding: "48px 0" }),
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Spin size={fill ? "large" : "default"} tip={tip} />
    </div>
  );
}

export function ErrorState({
  message,
  icon = ERROR_ICON,
  action,
  fill = true,
}: {
  message: ReactNode;
  icon?: ReactNode;
  action?: ReactNode;
  fill?: boolean;
}) {
  return <StateBlock icon={icon} title={message} action={action} fill={fill} />;
}

export function EmptyState({ title, description, icon = EMPTY_ICON, action, fill = true }: StateBlockProps) {
  return <StateBlock icon={icon} title={title} description={description} action={action} fill={fill} />;
}

