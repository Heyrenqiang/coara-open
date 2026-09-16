import { useCallback, useEffect, useMemo, useState, type MouseEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import { Button, Segmented, Typography, message } from "antd";
import {
  DownloadOutlined,
  FileOutlined,
  FolderOutlined,
  StarFilled,
  StarOutlined,
} from "@ant-design/icons";
import {
  collectFile,
  deleteCollection,
  fetchCollectionList,
  fetchFileView,
  fileRawUrl,
  fetchToolOutput,
  type FileViewResponse,
} from "../lib/api";
import { fileViewRoute } from "../lib/fileLink";
import { formatDateTime } from "../lib/format";
import { normalizeMathDelimiters } from "../lib/mathNormalize";
import {
  parentFsPath,
  pathBreadcrumbs,
  tryParseCsvPreview,
  tryPrettyJson,
} from "../lib/fileViewHelpers";
import { MarkdownLink } from "../components/MarkdownLink";
import { markdownUrlTransform } from "../lib/markdownUrl";
import { DiffBlocksView } from "../components/DiffBlocksView";
import { PageShell } from "../components/layout/PageShell";
import { PageHeader } from "../components/layout/PageHeader";
import { SectionCard } from "../components/layout/SectionCard";
import { EmptyState, ErrorState, LoadingState } from "../components/states/States";
import { findToolActivityByCallId, type ToolActivity } from "../lib/toolActivity";
import { useStore } from "../lib/store";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
} from "@ant-design/icons";

const { Text } = Typography;

const PAGE_SIZE = 500;

/** 已收藏星标的填充色：复用 warning 语义令牌（琥珀色，与手机端收藏高亮同族） */
const COLLECT_ACCENT = "var(--coara-warning)";

function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

/** 拼接子路径：跟随父路径的分隔风格（Win \\ vs POSIX /）。 */
function joinFsPath(parent: string, name: string): string {
  if (!parent) return name;
  const useWin = /\\/.test(parent) && !parent.startsWith("/");
  const sep = useWin ? "\\" : "/";
  return `${parent.replace(/[/\\]+$/, "")}${sep}${name}`;
}

/** 代码高亮：复用 ReactMarkdown + rehype-highlight 管线（index.css 里已有 hljs 主题与
 *  .chat-markdown pre/code 样式），把源码包进围栏代码块渲染。
 *  围栏长度取「内容中最长反引号串 + 1」，保证内容里的 ``` 不会提前闭合围栏。 */
function CodeBlock({ content, language }: { content: string; language?: string }) {
  const md = useMemo(() => {
    let maxRun = 0;
    for (const m of content.matchAll(/`+/g)) {
      maxRun = Math.max(maxRun, m[0].length);
    }
    const fence = "`".repeat(Math.max(3, maxRun + 1));
    const lang = language && /^[a-zA-Z0-9#+-]+$/.test(language) ? language : "";
    return `${fence}${lang}\n${content}\n${fence}`;
  }, [content, language]);
  return (
    <div className="chat-markdown" style={{ fontSize: 14 }}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
        {md}
      </ReactMarkdown>
    </div>
  );
}

/** Markdown 渲染视图：与聊天气泡同一套插件组合（链接同样走文件路径接管）。 */
const renderedMarkdownComponents = { a: MarkdownLink };

function MarkdownRendered({ content }: { content: string }) {
  const normalized = useMemo(() => normalizeMathDelimiters(content), [content]);
  return (
    <div className="chat-markdown" style={{ maxWidth: 860, margin: "0 auto" }}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeHighlight, rehypeKatex]}
        components={renderedMarkdownComponents}
        urlTransform={markdownUrlTransform}
      >
        {normalized}
      </ReactMarkdown>
    </div>
  );
}

interface LoadError {
  status: number;
  message: string;
}

function errorText(err: LoadError): string {
  if (err.status === 404) return "文件不存在或已被移动";
  if (err.status === 403) return "没有权限访问该文件";
  if (err.status === 401) return "登录状态失效，请刷新页面重新进入";
  return err.message || "读取文件失败";
}

/**
 * 整页文件显示页（/file?path=<urlencoded>）。
 * 数据契约见 api.ts fetchFileView / fileRawUrl——按后端返回的 type 分派渲染：
 * text（代码/JSON/CSV/md）/ image（file-raw）/ office / binary
 * （pdf 内嵌、音视频直链播放、其余下载入口）/ directory（面包屑 + 列表）。
 */
export function FileView() {
  const [searchParams] = useSearchParams();
  const path = searchParams.get("path") ?? "";
  const viewMode = searchParams.get("view") ?? "";
  const navigate = useNavigate();

  const [data, setData] = useState<FileViewResponse | null>(null);
  const [error, setError] = useState<LoadError | null>(null);
  const [loading, setLoading] = useState(false);
  // 文本分页：已加载内容累加；nextOffset 为 null 表示没有后续
  const [textContent, setTextContent] = useState("");
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [mdMode, setMdMode] = useState<"rendered" | "source">("rendered");
  const [collecting, setCollecting] = useState(false);
  // 已收藏条目 id（scope=user 收藏列表中 source_url 匹配当前 path 的那条）；null=未收藏
  const [collectedId, setCollectedId] = useState<string | null>(null);

  // 已收藏判定（与手机端 CollectActions.kt 同款）：收藏库（scope=user）里存在
  // source_url 以该路径结尾的条目；Windows 路径分隔符不一致时按统一斜杠比较
  const findCollectedId = useCallback(
    (entries: { id: string; source_url: string }[]): string | null => {
      if (!path) return null;
      const norm = (s: string) => s.replace(/\\/g, "/");
      const hit = entries.find(
        (e) =>
          e.source_url.endsWith(path) ||
          e.source_url.endsWith(`:${path}`) ||
          norm(e.source_url).endsWith(norm(path)),
      );
      return hit?.id ?? null;
    },
    [path],
  );

  useEffect(() => {
    setCollectedId(null);
    if (!path || viewMode === "tool") return;
    let cancelled = false;
    fetchCollectionList({ limit: 200 })
      .then((res) => {
        if (!cancelled && res.enabled) setCollectedId(findCollectedId(res.entries));
      })
      .catch(() => {
        /* 收藏状态读取失败不阻断文件页 */
      });
    return () => {
      cancelled = true;
    };
  }, [path, viewMode, findCollectedId]);

  const onToggleCollect = useCallback(async () => {
    if (!path || viewMode === "tool") return;
    setCollecting(true);
    try {
      if (collectedId) {
        await deleteCollection(collectedId);
        setCollectedId(null);
        message.success("已取消收藏");
      } else {
        const res = await collectFile(path);
        message.success(res.message || "已收藏");
        // 回读拿收藏 id，让星标立即切为可取消态（与手机端同款）
        try {
          const list = await fetchCollectionList({ limit: 200 });
          if (list.enabled) setCollectedId(findCollectedId(list.entries));
        } catch {
          /* 回读失败则保持未收藏态，下次进页会恢复 */
        }
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setCollecting(false);
    }
  }, [path, viewMode, collectedId, findCollectedId]);

  useEffect(() => {
    setData(null);
    setTextContent("");
    setNextOffset(null);
    setMdMode("rendered");
    if (!path) {
      setError({ status: 0, message: "缺少 path 参数" });
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchFileView(path, 1, PAGE_SIZE)
      .then((res) => {
        if (cancelled) return;
        setData(res);
        if (res.type === "text") {
          setTextContent(res.content ?? "");
          setNextOffset(
            res.has_more ? (res.offset ?? 1) + (res.limit ?? PAGE_SIZE) : null,
          );
        }
      })
      .catch((err: Error & { status?: number }) => {
        if (!cancelled) {
          setError({ status: err.status ?? -1, message: err.message });
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [path]);

  const loadMore = useCallback(async () => {
    if (nextOffset == null || loadingMore) return;
    setLoadingMore(true);
    try {
      const res = await fetchFileView(path, nextOffset, PAGE_SIZE);
      if (res.type === "text") {
        setTextContent((prev) => prev + (res.content ?? ""));
        setNextOffset(
          res.has_more ? (res.offset ?? nextOffset) + (res.limit ?? PAGE_SIZE) : null,
        );
      }
    } catch (err) {
      setError({
        status: (err as Error & { status?: number }).status ?? -1,
        message: (err as Error).message,
      });
    } finally {
      setLoadingMore(false);
    }
  }, [path, nextOffset, loadingMore]);

  const isMarkdown =
    data?.type === "text" && (data.language === "md" || data.language === "markdown");

  const header = (
    <PageHeader
      onBack={() => navigate(-1)}
      title={data?.name || (path ? path.split(/[\\/]/).pop() : "文件")}
      meta={
        data ? (
          <Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>
            {formatFileSize(data.size)}
            {data.type === "text" && data.total_lines != null
              ? ` · 共 ${data.total_lines} 行`
              : data.type === "directory"
                ? ` · ${(data.entries ?? []).length} 项${data.truncated ? "+" : ""}`
                : ""}
          </Text>
        ) : null
      }
      subline={
        path ? (
            <nav
              aria-label="路径"
              style={{
                fontSize: 12,
                color: "var(--coara-text-secondary)",
                display: "flex",
                flexWrap: "wrap",
                alignItems: "center",
                gap: 2,
                minWidth: 0,
              }}
            >
              {pathBreadcrumbs(path).map((c, i, arr) => (
                <span key={c.path} style={{ display: "inline-flex", alignItems: "center", gap: 2 }}>
                  {i > 0 ? <span style={{ opacity: 0.5 }}>/</span> : null}
                  {i < arr.length - 1 ? (
                    <a
                      href={fileViewRoute(c.path)}
                      onClick={(e) => {
                        if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0) return;
                        e.preventDefault();
                        navigate(fileViewRoute(c.path));
                      }}
                      style={{ color: "var(--coara-link)", textDecoration: "none" }}
                      title={c.path}
                    >
                      {c.label}
                    </a>
                  ) : (
                    <Text
                      type="secondary"
                      copyable={{ text: path, tooltips: ["复制路径", "已复制"] }}
                      style={{ fontSize: 12, margin: 0 }}
                      title={path}
                    >
                      {c.label}
                    </Text>
                  )}
                </span>
              ))}
            </nav>
        ) : null
      }
      actions={
        <>
          {isMarkdown ? (
            <Segmented
              size="small"
              value={mdMode}
              onChange={(v) => setMdMode(v as "rendered" | "source")}
              options={[
                { label: "渲染", value: "rendered" },
                { label: "源码", value: "source" },
              ]}
            />
          ) : null}
          {path && data?.type !== "directory" ? (
            <Button
              size="small"
              icon={<DownloadOutlined />}
              href={fileRawUrl(path, true)}
            >
              下载
            </Button>
          ) : null}
          {path && viewMode !== "tool" && data?.type !== "directory" ? (
            <Button
              size="small"
              icon={
                collectedId ? (
                  <StarFilled style={{ color: COLLECT_ACCENT }} />
                ) : (
                  <StarOutlined />
                )
              }
              loading={collecting}
              onClick={() => void onToggleCollect()}
              title={collectedId ? "取消收藏" : "收藏"}
            >
              {collectedId ? "已收藏" : "收藏"}
            </Button>
          ) : null}
        </>
      }
    />
  );

  let body: React.ReactNode = null;
  if (loading) {
    body = <LoadingState />;
  } else if (error) {
    body = (
      <ErrorState
        message={errorText(error)}
        action={
          <Button size="small" onClick={() => navigate(-1)}>
            返回
          </Button>
        }
      />
    );
  } else if (data) {
    if (data.type === "image") {
      body = (
        <div
          style={{
            flex: 1,
            overflow: "auto",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 24,
            background: "var(--coara-bg-subtle)",
          }}
        >
          <img
            src={fileRawUrl(path)}
            alt={data.name}
            style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }}
          />
        </div>
      );
    } else if (data.type === "office") {
      body = (
        <div style={{ flex: 1, overflow: "auto", padding: "20px 24px" }}>
          <MarkdownRendered content={data.content ?? ""} />
        </div>
      );
    } else if (data.type === "directory") {
      const entries = data.entries ?? [];
      const parent = parentFsPath(path);
      const rowStyle = {
        display: "flex" as const,
        alignItems: "center" as const,
        gap: 10,
        padding: "8px 10px",
        borderRadius: 8,
        color: "inherit",
        textDecoration: "none",
      };
      const hoverOn = (e: MouseEvent<HTMLAnchorElement>) => {
        e.currentTarget.style.background = "var(--coara-bg-subtle)";
      };
      const hoverOff = (e: MouseEvent<HTMLAnchorElement>) => {
        e.currentTarget.style.background = "transparent";
      };
      body = (
        <div style={{ flex: 1, overflow: "auto", padding: "8px 16px 24px" }}>
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {parent ? (
              <li>
                <a
                  href={fileViewRoute(parent)}
                  onClick={(e) => {
                    if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0) return;
                    e.preventDefault();
                    navigate(fileViewRoute(parent));
                  }}
                  style={rowStyle}
                  onMouseEnter={hoverOn}
                  onMouseLeave={hoverOff}
                >
                  <FolderOutlined style={{ color: "var(--coara-warning)" }} />
                  <span style={{ flex: 1 }}>..</span>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    上级
                  </Text>
                </a>
              </li>
            ) : null}
            {entries.length === 0 && !parent ? (
              <Text type="secondary" style={{ display: "block", padding: 24, textAlign: "center" }}>
                空文件夹
              </Text>
            ) : null}
            {entries.map((ent) => {
              const childPath = joinFsPath(path, ent.name);
              const isDir = ent.type === "dir";
              return (
                <li key={ent.name}>
                  <a
                    href={fileViewRoute(childPath)}
                    onClick={(e) => {
                      if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0) return;
                      e.preventDefault();
                      navigate(fileViewRoute(childPath));
                    }}
                    style={rowStyle}
                    onMouseEnter={hoverOn}
                    onMouseLeave={hoverOff}
                  >
                    {isDir ? (
                      <FolderOutlined style={{ color: "var(--coara-warning)" }} />
                    ) : (
                      <FileOutlined style={{ color: "var(--coara-text-faint)" }} />
                    )}
                    <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                      {ent.name}
                      {isDir ? "/" : ""}
                    </span>
                    {!isDir && ent.size > 0 ? (
                      <Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>
                        {formatFileSize(ent.size)}
                      </Text>
                    ) : null}
                  </a>
                </li>
              );
            })}
          </ul>
          {entries.length === 0 && parent ? (
            <Text type="secondary" style={{ display: "block", padding: "8px 10px", fontSize: 12 }}>
              空文件夹
            </Text>
          ) : null}
          {data.truncated ? (
            <Text type="secondary" style={{ display: "block", padding: "8px 10px", fontSize: 12 }}>
              仅显示前 {entries.length} 项
            </Text>
          ) : null}
        </div>
      );
    } else if (data.type === "text") {
      const lang = (data.language ?? "").toLowerCase();
      const prettyJson = lang === "json" ? tryPrettyJson(textContent) : null;
      const csvPreview =
        lang === "csv" || lang === "tsv" ? tryParseCsvPreview(textContent, lang) : null;
      body = (
        <div style={{ flex: 1, overflow: "auto", padding: "16px 24px" }}>
          {isMarkdown && mdMode === "rendered" ? (
            <MarkdownRendered content={textContent} />
          ) : prettyJson != null ? (
            <CodeBlock content={prettyJson} language="json" />
          ) : csvPreview != null ? (
            <div style={{ overflow: "auto" }}>
              <table
                style={{
                  borderCollapse: "collapse",
                  fontSize: 13,
                  width: "100%",
                  maxWidth: "100%",
                }}
              >
                <thead>
                  <tr>
                    {csvPreview.headers.map((h, i) => (
                      <th
                        key={i}
                        style={{
                          textAlign: "left",
                          padding: "6px 10px",
                          borderBottom: "1px solid var(--coara-border)",
                          background: "var(--coara-table-head)",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {h || `列${i + 1}`}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {csvPreview.rows.map((row, ri) => (
                    <tr key={ri}>
                      {row.map((cell, ci) => (
                        <td
                          key={ci}
                          style={{
                            padding: "6px 10px",
                            borderBottom: "1px solid var(--coara-border-soft)",
                            verticalAlign: "top",
                            maxWidth: 280,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                          }}
                          title={cell}
                        >
                          {cell}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
              {(csvPreview.truncatedRows || csvPreview.truncatedCols) && (
                <Text type="secondary" style={{ display: "block", marginTop: 12, fontSize: 12 }}>
                  预览已截断
                  {csvPreview.truncatedRows ? "（行）" : ""}
                  {csvPreview.truncatedCols ? "（列）" : ""}
                  ，完整内容请下载
                </Text>
              )}
            </div>
          ) : (
            <CodeBlock content={textContent} language={data.language} />
          )}
          <div style={{ display: "flex", justifyContent: "center", padding: "12px 0 20px" }}>
            {nextOffset != null ? (
              <Button size="small" loading={loadingMore} onClick={() => void loadMore()}>
                加载更多
                {data.total_lines != null
                  ? `（已加载 ${Math.min(nextOffset - 1, data.total_lines)} / ${data.total_lines} 行）`
                  : ""}
              </Button>
            ) : data.total_lines != null && data.total_lines > PAGE_SIZE ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                已加载全部 {data.total_lines} 行
              </Text>
            ) : null}
          </div>
        </div>
      );
    } else {
      // binary
      const mime = data.mime ?? "";
      const rawUrl = fileRawUrl(path);
      if (mime === "application/pdf") {
        body = (
          <iframe
            title={data.name}
            src={rawUrl}
            style={{ flex: 1, width: "100%", border: "none", background: "var(--coara-bg-subtle)" }}
          />
        );
      } else if (mime.startsWith("video/")) {
        body = (
          <div
            style={{
              flex: 1,
              overflow: "auto",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              padding: 24,
              background: "var(--coara-code-canvas)",
            }}
          >
            <video controls src={rawUrl} style={{ maxWidth: "100%", maxHeight: "100%" }} />
          </div>
        );
      } else if (mime.startsWith("audio/")) {
        body = (
          <div
            style={{
              flex: 1,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              padding: 24,
            }}
          >
            <audio controls src={rawUrl} style={{ width: "100%", maxWidth: 560 }} />
          </div>
        );
      } else {
        body = (
          <EmptyState
            title={
              <>
                {mime || "二进制文件"} · {formatFileSize(data.size)}
              </>
            }
            description="不支持在线预览 · 可下载"
            action={
              <Button icon={<DownloadOutlined />} href={fileRawUrl(path, true)}>
                下载文件
              </Button>
            }
          />
        );
      }
    }
  }

  // Tool-view mode: render a tool call's full output / diff (no file fetch).
  if (searchParams.get("view") === "tool") {
    const callId = searchParams.get("call_id") ?? "";
    return <ToolView callId={callId} />;
  }

  // 各查看器自带滚动与内边距（padding 因内容类型而异：目录树 8/16、正文 20/24、
  // Markdown 16/24），故内容区滚动交还查看器，PageShell 不代管。
  return (
    <PageShell header={header} scroll="hidden">
      {body}
    </PageShell>
  );
}

function ToolView({ callId }: { callId: string }) {
  const navigate = useNavigate();
  const activityEpoch = useStore((s) => s.activityEpoch);
  const tc = useMemo(
    () => findToolActivityByCallId(useStore.getState().traceEvents, callId),
    [callId, activityEpoch],
  );

  const [spillContent, setSpillContent] = useState<string | null>(null);
  const [spillLoading, setSpillLoading] = useState(false);
  const [spillError, setSpillError] = useState<string | null>(null);
  const [spillNext, setSpillNext] = useState<number | null>(null);

  const loadSpill = useCallback(
    async (offset = 1) => {
      if (!tc?.tool_output_ref) return;
      setSpillLoading(true);
      setSpillError(null);
      try {
        const res = await fetchToolOutput(tc.tool_output_ref, { offset, limit: 500 });
        const chunk = res.content ?? "";
        setSpillContent((prev) => (offset <= 1 || !prev ? chunk : prev + chunk));
        setSpillNext(
          res.next_offset != null
            ? res.next_offset
            : res.has_more
              ? (res.offset ?? offset) + (res.limit ?? 500)
              : null,
        );
      } catch (err) {
        setSpillError((err as Error).message || "加载失败");
      } finally {
        setSpillLoading(false);
      }
    },
    [tc?.tool_output_ref],
  );

  if (!tc) {
    return (
      <PageShell>
        <ErrorState
          message="未找到该工具调用（可能已离开当前窗口）"
          action={
            <Button size="small" onClick={() => navigate(-1)}>
              返回
            </Button>
          }
        />
      </PageShell>
    );
  }

  const statusLabel = !tc.done
    ? "运行中"
    : tc.ok === false || tc.is_error
      ? "失败"
      : "成功";
  const statusIcon = !tc.done ? (
    <LoadingOutlined style={{ color: "var(--coara-accent)" }} />
  ) : tc.ok === false || tc.is_error ? (
    <CloseCircleOutlined style={{ color: "var(--coara-error)" }} />
  ) : (
    <CheckCircleOutlined style={{ color: "var(--coara-success)" }} />
  );

  const argsText = formatToolArgs(tc.args);
  const outputText = spillContent ?? tc.tool_output ?? "";
  const hasDiff = Boolean(tc.diff_lines);
  const hasOutput = Boolean(outputText);
  const hasSummary = Boolean(tc.summary && tc.summary.trim());

  const header = (
    <PageHeader
      onBack={() => navigate(-1)}
      title={tc.tool}
      meta={
        <>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 12 }}>
            {statusIcon}
            <Text type="secondary">{statusLabel}</Text>
          </span>
          {typeof tc.duration_ms === "number" ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {tc.duration_ms >= 1000
                ? `${(tc.duration_ms / 1000).toFixed(1)}s`
                : `${Math.round(tc.duration_ms)}ms`}
            </Text>
          ) : null}
        </>
      }
      subline={
        <Text type="secondary" style={{ fontSize: 12 }}>
          {formatToolTime(tc.timestamp)}
          {tc.call_id ? ` · ${tc.call_id}` : ""}
        </Text>
      }
    />
  );

  return (
    <PageShell header={header} scroll="hidden">
      <div
        style={{
          flex: 1,
          overflow: "auto",
          padding: "16px 20px 28px",
          background: "var(--coara-surface)",
          display: "flex",
          flexDirection: "column",
          gap: 20,
        }}
      >
        {hasSummary ? (
          <SectionCard title="摘要">
            <Text style={{ fontSize: 13, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
              {tc.summary}
            </Text>
          </SectionCard>
        ) : null}

        {argsText ? (
          <SectionCard title="参数">
            <CodeBlock content={argsText} language="json" />
          </SectionCard>
        ) : null}

        {hasDiff ? (
          <SectionCard title="变更">
            <DiffBlocksView diff={tc.diff_lines!} />
          </SectionCard>
        ) : null}

        {hasOutput ? (
          <SectionCard title="输出">
            {(tc.tool_output_truncated || Boolean(tc.tool_output_ref)) && (
              <Text type="warning" style={{ display: "block", marginBottom: 8, fontSize: 12 }}>
                {tc.tool_output_truncated
                  ? "输出较长，以下为截断预览"
                  : "完整输出已落盘"}
                {tc.tool_output_ref && !spillContent ? (
                  <>
                    {" · "}
                    <Button
                      type="link"
                      size="small"
                      style={{ padding: 0, height: "auto", fontSize: 12 }}
                      loading={spillLoading}
                      onClick={() => void loadSpill(1)}
                    >
                      加载全文
                    </Button>
                  </>
                ) : null}
              </Text>
            )}
            <CodeBlock content={outputText} />
            {spillNext != null ? (
              <div style={{ textAlign: "center", marginTop: 12 }}>
                <Button size="small" loading={spillLoading} onClick={() => void loadSpill(spillNext)}>
                  加载更多
                </Button>
              </div>
            ) : null}
            {spillError ? (
              <Text type="danger" style={{ display: "block", marginTop: 8, fontSize: 12 }}>
                {spillError}
              </Text>
            ) : null}
          </SectionCard>
        ) : tc.tool_output_ref ? (
          <SectionCard title="输出">
            <Button size="small" loading={spillLoading} onClick={() => void loadSpill(1)}>
              加载完整输出
            </Button>
            {spillError ? (
              <Text type="danger" style={{ display: "block", marginTop: 8, fontSize: 12 }}>
                {spillError}
              </Text>
            ) : null}
          </SectionCard>
        ) : !hasDiff && !hasSummary && !argsText ? (
          <Text type="secondary">暂无更多内容（工具可能仍在运行，或输出未写入）</Text>
        ) : null}
      </div>
    </PageShell>
  );
}

function formatToolArgs(args: ToolActivity["args"]): string | null {
  if (!args || typeof args !== "object") return null;
  const keys = Object.keys(args);
  if (keys.length === 0) return null;
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args);
  }
}

function formatToolTime(ts: string): string {
  return formatDateTime(ts);
}
