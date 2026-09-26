import { Card, Tag } from "antd";
import { MessageOutlined } from "@ant-design/icons";

interface MatrixConfig {
  homeserver?: string;
  user?: string;
  notify_room_id?: string;
  guest_rooms?: string[];
  tunnel_enabled?: boolean;
  tunnel_mode?: string;
  tunnel_public_url?: string;
}

interface Props {
  config: MatrixConfig;
}

export function MatrixSection({ config }: Props) {
  return (
    <Card
      title={
        <span>
          <MessageOutlined style={{ marginRight: 8 }} />
          Matrix
        </span>
      }
    >
      <div style={{ fontSize: 13 }}>
        <div style={{ display: "flex", padding: "4px 0" }}>
          <span style={{ color: "var(--coara-text-faint)", minWidth: 100 }}>Homeserver</span>
          <span>{config.homeserver || "未配置"}</span>
        </div>
        <div style={{ display: "flex", padding: "4px 0" }}>
          <span style={{ color: "var(--coara-text-faint)", minWidth: 100 }}>用户</span>
          <span>{config.user || "未配置"}</span>
        </div>
        <div style={{ display: "flex", padding: "4px 0" }}>
          <span style={{ color: "var(--coara-text-faint)", minWidth: 100 }}>隧道</span>
          <span>
            {config.tunnel_enabled !== false ? (
              <>
                <Tag color="green" style={{ fontSize: 11 }}>开启</Tag>
                {config.tunnel_mode === "named" && config.tunnel_public_url && (
                  <span style={{ marginLeft: 4, fontSize: 12 }}>{config.tunnel_public_url}</span>
                )}
              </>
            ) : (
              <Tag style={{ fontSize: 11 }}>关闭</Tag>
            )}
          </span>
        </div>
      </div>
    </Card>
  );
}
