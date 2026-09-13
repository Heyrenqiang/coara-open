import { useEffect, useState } from "react";
import { Table, Tag, Typography, message } from "antd";
import { ApiOutlined } from "@ant-design/icons";
import { fetchEventSources, type EventSourceRow } from "../../../lib/api";
import { SectionShell } from "./SectionShell";

const { Text } = Typography;

const KIND_META: Record<string, { label: string; color: string }> = {
  file_watch: { label: "文件监听", color: "blue" },
  interval_poll: { label: "间隔轮询", color: "green" },
  webhook: { label: "webhook", color: "purple" },
  cron: { label: "定时", color: "orange" },
};

interface Props {
  workspaces: Array<{ name: string }>;
}

export function EventSourcesSection(_props: Props) {
  const [rows, setRows] = useState<EventSourceRow[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const data = await fetchEventSources();
        setRows(data.event_sources || []);
      } catch (err) {
        message.error(err instanceof Error ? err.message : "加载失败");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 200, render: (v: string) => <Text code style={{ fontSize: 12 }}>{v}</Text> },
    {
      title: "类型",
      dataIndex: "kind",
      key: "kind",
      width: 100,
      render: (v: string) => {
        const meta = KIND_META[v] || { label: v, color: "default" };
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    { title: "工作空间", dataIndex: "workspace", key: "workspace", width: 110 },
    {
      title: "状态",
      key: "running",
      width: 80,
      render: (_: unknown, r: EventSourceRow) =>
        r.running ? <Tag color="success">运行中</Tag> : r.enabled ? <Tag>待命</Tag> : <Tag>停用</Tag>,
    },
  ];

  return (
    <SectionShell icon={<ApiOutlined />} title="事件源" effect="">
      <Table
        size="small"
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: "暂无事件源" }}
      />
    </SectionShell>
  );
}
