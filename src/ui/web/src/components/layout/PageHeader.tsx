import type { ReactNode } from "react";
import { Button, Typography } from "antd";
import { ArrowLeftOutlined } from "@ant-design/icons";

const { Title } = Typography;

/**
 * 页面身份头部（PageShell 槽位 ①）。
 *
 * 统一承载：返回 · 标题 · meta（与标题同行的次要信息）· subline（第二行，如面包屑）· actions。
 * 此前 FileView / ToolView 各自复制了一份相同的头部样式，收敛到此处。
 */
interface PageHeaderProps {
  title: ReactNode;
  /** 与标题同行的次要信息：大小、行数、状态… */
  meta?: ReactNode;
  /** 标题下方第二行：面包屑、时间… */
  subline?: ReactNode;
  /** 右侧操作区（多个操作直接并列，间隔由本条统一提供） */
  actions?: ReactNode;
  onBack?: () => void;
  /** 是否绘制底部分隔线（默认 true）。当紧随其后的工具条自带分隔线时传 false，避免双线。 */
  divider?: boolean;
}

export function PageHeader({ title, meta, subline, actions, onBack, divider = true }: PageHeaderProps) {
  return (
    <div
      style={{
        padding: "10px 20px",
        borderBottom: divider ? "1px solid var(--coara-border-faint)" : "none",
        background: "var(--coara-surface)",
        display: "flex",
        alignItems: "center",
        gap: 12,
        flexShrink: 0,
      }}
    >
      {onBack ? (
        <Button
          type="text"
          size="small"
          icon={<ArrowLeftOutlined />}
          onClick={onBack}
          aria-label="返回"
        />
      ) : null}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 10, minWidth: 0 }}>
          <Title level={5} style={{ margin: 0, flexShrink: 0 }}>
            {title}
          </Title>
          {meta}
        </div>
        {subline ? <div style={{ marginTop: 2, minWidth: 0 }}>{subline}</div> : null}
      </div>
      {actions}
    </div>
  );
}
