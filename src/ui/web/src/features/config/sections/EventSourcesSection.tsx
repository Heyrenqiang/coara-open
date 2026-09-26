import { useEffect, useState } from "react";
import { message } from "antd";
import { fetchEventSources, type EventSourceRow } from "../../../lib/api";
import { ConfigEmpty, ConfigGroup, configMeta, configRow, configTitle } from "./configChrome";

const KIND_LABEL: Record<string, string> = {
  file_watch: "文件监听",
  interval_poll: "间隔轮询",
  webhook: "webhook",
  cron: "定时",
};

interface Props {
  workspaces: Array<{ name: string }>;
}

function sourceMeta(row: EventSourceRow): string {
  const bits = [KIND_LABEL[row.kind] || row.kind];
  if (row.workspace) bits.push(row.workspace);
  bits.push(row.running ? "运行中" : row.enabled ? "待命" : "停用");
  if (row.webhook_url) bits.push(String(row.webhook_url));
  return bits.join(" · ");
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

  return (
    <ConfigGroup title="事件源" first>
      {loading && rows.length === 0 ? (
        <ConfigEmpty>加载中</ConfigEmpty>
      ) : rows.length === 0 ? (
        <ConfigEmpty>暂无事件源</ConfigEmpty>
      ) : (
        rows.map((row) => (
          <div key={row.id} style={configRow}>
            <div style={configTitle}>{row.id}</div>
            <div style={configMeta}>{sourceMeta(row)}</div>
          </div>
        ))
      )}
    </ConfigGroup>
  );
}
