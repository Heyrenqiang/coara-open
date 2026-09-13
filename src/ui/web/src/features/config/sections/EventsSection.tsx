import { Card } from "antd";
import { BellOutlined } from "@ant-design/icons";

interface EventsConfig {
  webhook_host?: string;
  webhook_port?: number;
}

interface Props {
  config: EventsConfig;
}

export function EventsSection({ config }: Props) {
  return (
    <Card
      title={
        <span>
          <BellOutlined style={{ marginRight: 8 }} />
          事件源
        </span>
      }
    >
      <div style={{ fontSize: 13 }}>
        <div style={{ display: "flex", padding: "4px 0" }}>
          <span style={{ color: "var(--coara-text-faint)", minWidth: 120 }}>Webhook 主机</span>
          <span>{config.webhook_host || "默认"}</span>
        </div>
        <div style={{ display: "flex", padding: "4px 0" }}>
          <span style={{ color: "var(--coara-text-faint)", minWidth: 120 }}>Webhook 端口</span>
          <span>{config.webhook_port ?? "默认"}</span>
        </div>
      </div>
    </Card>
  );
}
