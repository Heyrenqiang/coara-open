import type { ReactNode } from "react";
import { Typography } from "antd";

const { Text } = Typography;

/**
 * 内容分区。用于把内容区切成有标题的段落（L1 包装层）。
 *
 * - `variant="label"`（默认）：小号大写字重标签 + 内容，轻薄分隔（原 ToolDetailSection 样式）
 * - `variant="card"`：带边框与圆角的卡片容器
 */
export interface SectionCardProps {
  title?: ReactNode;
  /** 标题右侧的附加内容（操作、计数…） */
  extra?: ReactNode;
  variant?: "label" | "card";
  children: ReactNode;
}

const LABEL_STYLE = {
  fontSize: 11,
  fontWeight: 600,
  letterSpacing: 0.4,
  textTransform: "uppercase" as const,
};

export function SectionCard({ title, extra, variant = "label", children }: SectionCardProps) {
  if (variant === "card") {
    return (
      <section
        style={{
          border: "1px solid var(--coara-border-faint)",
          borderRadius: 10,
          background: "var(--coara-surface)",
          padding: "14px 16px",
        }}
      >
        {title ? (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 10,
            }}
          >
            <Text type="secondary" style={LABEL_STYLE}>
              {title}
            </Text>
            {extra}
          </div>
        ) : null}
        {children}
      </section>
    );
  }

  return (
    <section>
      {title ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: 8,
          }}
        >
          <Text type="secondary" style={LABEL_STYLE}>
            {title}
          </Text>
          {extra}
        </div>
      ) : null}
      {children}
    </section>
  );
}
