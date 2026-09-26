import { useCallback, useEffect, useState } from "react";
import { Button, Segmented, Tag, Typography, message } from "antd";
import {
  FileImageOutlined,
  FileOutlined,
  FileTextOutlined,
  CodeOutlined,
  PlayCircleOutlined,
  AudioOutlined,
  DownloadOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { fetchRecentFiles, fileRawUrl, type RecentFileEntry } from "../lib/api";
import { EmptyState, LoadingState } from "../components/states/States";
import { formatDateTime } from "../lib/format";

const { Text } = Typography;

function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function kindIcon(kind: string) {
  switch (kind) {
    case "image":
      return <FileImageOutlined />;
    case "video":
      return <PlayCircleOutlined />;
    case "audio":
      return <AudioOutlined />;
    case "doc":
      return <FileTextOutlined />;
    case "code":
      return <CodeOutlined />;
    default:
      return <FileOutlined />;
  }
}

const ORIGIN_LABEL: Record<string, string> = {
  inbound: "你发的",
  outbound: "我发的",
};
const END_LABEL: Record<string, string> = {
  web: "Web",
  cli: "CLI",
  matrix: "手机",
};

/** 最近文件列表：跨来源按时间倒序的收发流水（默认本空间，可看全部）。
 *  记录页「最近」页签的内容组件（非路由页）——外壳与滚动由父页 PageShell 承载，
 *  这里只渲染工具条 + 列表。 */
export function RecentFilesList() {
  const [scope, setScope] = useState<string>("current");
  const [files, setFiles] = useState<RecentFileEntry[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const load = useCallback(async (sc: string) => {
    setLoading(true);
    try {
      const data = await fetchRecentFiles({
        workspace: sc === "all" ? "all" : undefined,
        limit: 30,
      });
      setFiles(data.files);
      setHasMore(data.has_more);
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(scope);
  }, [scope, load]);

  const loadMore = async () => {
    if (files.length === 0) return;
    setLoadingMore(true);
    try {
      const before = files[files.length - 1].ts;
      const data = await fetchRecentFiles({
        workspace: scope === "all" ? "all" : undefined,
        before,
        limit: 30,
      });
      setFiles((prev) => [...prev, ...data.files]);
      setHasMore(data.has_more);
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingMore(false);
    }
  };

  const toolbar = (
    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
      <Segmented
        value={scope}
        onChange={(v) => setScope(String(v))}
        options={[
          { value: "current", label: "本空间" },
          { value: "all", label: "全部" },
        ]}
      />
      <Button icon={<ReloadOutlined />} onClick={() => void load(scope)} loading={loading} />
    </div>
  );

  return (
    <div
      style={{
        padding: "12px 20px",
        height: "100%",
        overflowY: "auto",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "flex-end",
          marginBottom: 12,
        }}
      >
        {toolbar}
      </div>

      {loading ? (
        <LoadingState fill={false} tip="加载中…" />
      ) : files.length === 0 ? (
        <EmptyState fill={false} description="暂无最近文件" />
      ) : (
        <>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {files.map((f, i) => (
              <div
                key={`${f.ts}-${i}`}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  padding: "10px 12px",
                  borderRadius: 8,
                  background: "var(--coara-surface)",
                  border: "1px solid var(--coara-border-faint)",
                }}
              >
                <span style={{ fontSize: 20, color: "var(--coara-text-muted)" }}>
                  {kindIcon(f.kind)}
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontWeight: 500,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={f.name}
                  >
                    {f.name}
                  </div>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {formatDateTime(new Date(f.ts * 1000).toISOString())}
                    {f.size ? ` · ${formatSize(f.size)}` : ""}
                  </Text>
                </div>
                <Tag color={f.origin === "inbound" ? "blue" : "green"} style={{ marginInlineEnd: 0 }}>
                  {ORIGIN_LABEL[f.origin] ?? f.origin}
                </Tag>
                <Tag style={{ marginInlineEnd: 0 }}>{END_LABEL[f.end] ?? f.end}</Tag>
                <Button
                  type="text"
                  size="small"
                  icon={<DownloadOutlined />}
                  href={fileRawUrl(f.path, true)}
                  title="下载"
                />
              </div>
            ))}
          </div>
          {hasMore && (
            <div style={{ textAlign: "center", marginTop: 16 }}>
              <Button onClick={() => void loadMore()} loading={loadingMore}>
                查看更早
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
