import { Card, Tag } from "antd";
import type { ReactNode } from "react";

interface Props {
  icon: ReactNode;
  title: string;
  /** 生效时机说明，如「即时生效」「新会话生效」「重启生效」 */
  effect: string;
  extra?: ReactNode;
  children: ReactNode;
}

/** 设置中心 section 统一外壳：图标 + 标题 + 生效时机标注。 */
export function SectionShell({ icon, title, effect, extra, children }: Props) {
  return (
    <Card
      title={
        <span>
          {icon}
          <span style={{ marginLeft: 8 }}>{title}</span>
          <Tag color="green" style={{ marginLeft: 10, fontWeight: "normal" }}>
            {effect}
          </Tag>
        </span>
      }
      extra={extra}
    >
      {children}
    </Card>
  );
}
