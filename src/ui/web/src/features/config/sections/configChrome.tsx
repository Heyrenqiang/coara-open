import type { CSSProperties, ReactNode } from "react";
import { DownOutlined, RightOutlined } from "@ant-design/icons";

/** 与模型页同一套名录：细线分行、14px 标题、12px 辅文、折叠面板。 */

export const configRow: CSSProperties = {
  padding: "12px 0",
  borderBottom: "1px solid var(--coara-border-faint)",
};

export const configTitle: CSSProperties = {
  fontSize: 14,
  fontWeight: 600,
  color: "var(--coara-text)",
  lineHeight: 1.35,
};

export const configMeta: CSSProperties = {
  marginTop: 4,
  fontSize: 12,
  lineHeight: 1.5,
  color: "var(--coara-text-tertiary)",
};

/** 长辅文收成一行，避免把整页撑出横向滚动。 */
export const configClamp: CSSProperties = {
  ...configMeta,
  maxWidth: 720,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

export const configPanel: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 10,
  marginTop: 10,
  padding: "12px 14px",
  border: "1px solid var(--coara-border-faint)",
  borderRadius: 8,
  maxWidth: 640,
};

export function ConfigGroup({
  title,
  first = false,
  children,
}: {
  title: string;
  first?: boolean;
  children: ReactNode;
}) {
  return (
    <section style={{ marginTop: first ? 0 : 28 }}>
      <div style={{ fontSize: 12, color: "var(--coara-text-tertiary)", padding: "2px 0" }}>{title}</div>
      {children}
    </section>
  );
}

export function ConfigEmpty({ children }: { children: ReactNode }) {
  return <div style={{ padding: "8px 0", fontSize: 13, color: "var(--coara-text-tertiary)" }}>{children}</div>;
}

export function ConfigMore({
  open,
  onToggle,
  children,
}: {
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <>
      <div
        role="button"
        onClick={onToggle}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          marginTop: 8,
          fontSize: 12,
          color: "var(--coara-text-tertiary)",
          cursor: "pointer",
          userSelect: "none",
        }}
      >
        {open ? <DownOutlined style={{ fontSize: 10 }} /> : <RightOutlined style={{ fontSize: 10 }} />}
        更多
      </div>
      {open ? <div style={configPanel}>{children}</div> : null}
    </>
  );
}
