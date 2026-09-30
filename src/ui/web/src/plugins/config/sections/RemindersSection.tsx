import { useEffect, useState } from "react";
import { message } from "antd";
import { fetchReminders, type ReminderRow } from "coara:shell";
import { ConfigEmpty, ConfigGroup, configMeta, configRow, configTitle } from "./configChrome";

const KIND_LABEL: Record<string, string> = {
  one_time: "一次性",
  interval: "间隔",
  cron: "定时",
};

function whenLabel(row: ReminderRow): string {
  if (row.next_run_at) return row.next_run_at.replace("T", " ").slice(0, 19);
  if (row.cron) return row.cron;
  if (row.interval_seconds) return `每 ${row.interval_seconds} 秒`;
  return "";
}

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

  return (
    <ConfigGroup title="提醒">
      {loading && rows.length === 0 ? (
        <ConfigEmpty>加载中</ConfigEmpty>
      ) : rows.length === 0 ? (
        <ConfigEmpty>暂无提醒</ConfigEmpty>
      ) : (
        rows.map((row) => {
          const bits = [KIND_LABEL[row.kind] || row.kind, whenLabel(row), row.enabled ? "启用" : "停用"].filter(Boolean);
          return (
            <div key={row.id} style={configRow}>
              <div style={configTitle}>{row.message || row.id}</div>
              <div style={configMeta}>{bits.join(" · ")}</div>
            </div>
          );
        })
      )}
    </ConfigGroup>
  );
}
