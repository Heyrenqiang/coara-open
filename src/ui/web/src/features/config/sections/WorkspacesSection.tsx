import { useEffect, useState } from "react";
import { Table, Tag, Typography, message } from "antd";
import { FolderOutlined } from "@ant-design/icons";
import { fetchWorkspaces, type WorkspaceRow } from "../../../lib/api";
import { SectionShell } from "./SectionShell";

const { Text } = Typography;

export function WorkspacesSection() {
  const [rows, setRows] = useState<WorkspaceRow[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const data = await fetchWorkspaces();
        setRows(data.workspaces || []);
      } catch (err) {
        message.error(err instanceof Error ? err.message : "加载失败");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const columns = [
    {
      title: "名称",
      dataIndex: "name",
      key: "name",
      width: 140,
      render: (v: string, r: WorkspaceRow) => (
        <span>
          <Text strong>{v}</Text>
          {r.is_default && <Tag color="gold" style={{ marginLeft: 6 }}>默认</Tag>}
        </span>
      ),
    },
    {
      title: "路径",
      dataIndex: "path",
      key: "path",
      ellipsis: true,
      render: (v: string) => <Text code style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      title: "摘要",
      dataIndex: "summary",
      key: "summary",
      ellipsis: true,
      render: (v?: string) => <Text type="secondary" style={{ fontSize: 12 }}>{v || "—"}</Text>,
    },
  ];

  return (
    <SectionShell icon={<FolderOutlined />} title="工作空间" effect="">
      <Table
        size="small"
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: "暂无工作空间" }}
      />
    </SectionShell>
  );
}
