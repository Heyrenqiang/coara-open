import { useCallback, useEffect, useState } from "react";
import { message } from "antd";
import { fetchWorkspaces, type WorkspaceRow } from "coara:shell";
import { useStore } from "coara:shell";
import { ConfigEmpty, configClamp, configMeta, configRow, configTitle } from "./configChrome";

function flagsOf(row: WorkspaceRow): string {
  const bits: string[] = [];
  if (row.is_default) bits.push("默认");
  if (row.kind === "internal") bits.push("系统");
  if (row.missing) bits.push("目录缺失");
  return bits.join(" · ");
}

export function WorkspacesSection() {
  const [rows, setRows] = useState<WorkspaceRow[]>([]);
  const [loading, setLoading] = useState(false);
  const workspacesRevision = useStore((s) => s.workspacesRevision);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchWorkspaces();
      setRows(data.workspaces || []);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, workspacesRevision]);

  if (loading && rows.length === 0) {
    return <ConfigEmpty>加载中</ConfigEmpty>;
  }
  if (rows.length === 0) {
    return <ConfigEmpty>暂无工作空间</ConfigEmpty>;
  }

  return (
    <div>
      {rows.map((row) => {
        const flags = flagsOf(row);
        const extra = [flags, row.summary].filter(Boolean).join(" · ");
        return (
          <div key={row.id} style={configRow}>
            <div style={configTitle}>{row.name}</div>
            <div style={configClamp} title={row.path}>
              {row.path}
            </div>
            {extra ? <div style={configMeta}>{extra}</div> : null}
          </div>
        );
      })}
    </div>
  );
}
