import { useEffect, useState } from "react";
import { Table, Tag, Typography, message } from "antd";
import { BellOutlined } from "@ant-design/icons";
import { fetchReminders, type ReminderRow } from "../../../lib/api";
import { SectionShell } from "./SectionShell";

const { Text } = Typography;

const KIND_LABEL: Record<string, { label: string; color: string }> = {
  one_time: { label: "一次性", color: "blue" },
  interval: { label: "间隔", color: "green" },
  cron: { label: "cron", color: "purple" },
};

export function RemindersSection() {
  const [rows, setRows] = useState<ReminderRow[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const data = await fetchReminders();
        setRows(data.reminders || []);
      } catch (err) {
        message.error(err instanceof Error ? err.message : "加载失败");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const columns = [
    {
      title: "类型",
      dataIndex: "kind",
      key: "kind",
      width: 80,
      render: (v: string) => {
        const meta = KIND_LABEL[v] || { label: v, color: "default" };
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    { title: "内容", dataIndex: "message", key: "message", ellipsis: true },
    {
      title: "下次执行",
      dataIndex: "next_run_at",
      key: "next_run_at",
      width: 170,
      render: (v?: string) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {v ? v.replace("T", " ").slice(0, 19) : "—"}
        </Text>
      ),
    },
    {
      title: "状态",
      key: "enabled",
      width: 70,
      render: (_: unknown, r: ReminderRow) =>
        r.enabled ? <Tag color="success">启用</Tag> : <Tag>停用</Tag>,
    },
  ];

  return (
    <SectionShell icon={<BellOutlined />} title="提醒" effect="">
      <Table
        size="small"
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: "暂无提醒" }}
      />
    </SectionShell>
  );
}
