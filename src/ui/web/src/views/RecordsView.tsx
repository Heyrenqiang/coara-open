import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Collapse,
  Input,
  Popconfirm,
  Select,
  Space,
  Switch,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import {
  BookOutlined,
  DeleteOutlined,
  DownloadOutlined,
  InboxOutlined,
  ReloadOutlined,
  SearchOutlined,
  StarOutlined,
} from "@ant-design/icons";
import {
  archiveRecord,
  collectionFileUrl,
  deleteRecord,
  fetchCollectionEntry,
  fetchCollectionList,
  fetchRecordEntry,
  fetchRecordList,
  unarchiveRecord,
  type CollectionEntry,
  type RecordEntry,
} from "../lib/api";
import { formatDateGroupKey, formatDateGroupLabel, formatDateTime, formatTimeHm } from "../lib/format";
import { MarkdownMessage } from "../features/chat/MessageList";
import { PageShell } from "../components/layout/PageShell";
import { PageHeader } from "../components/layout/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../components/states/States";
import { RecentFilesList } from "./RecentFilesList";
import { useSearchParams } from "react-router-dom";
import { ClockCircleOutlined } from "@ant-design/icons";
import { useStore } from "../lib/store";
import { lazy, Suspense } from "react";

/** 与 daily 对话浮窗：会话主体是记录空间的 WorkspaceSession（persona=daily）。
 *  仅当 web 视图已切到记录空间（侧边栏点「记录」触发）时挂载——否则浮窗会
 *  对着前台空间说话，违背空间会话语义。 */
const ModuleChatFloat = lazy(() =>
  import("../features/chat/ModuleChatFloat").then((m) => ({ default: m.ModuleChatFloat })),
);

/** 记录空间的展示名（侧边栏 home: 分支即按它找 home_view）。 */
const RECORDS_SPACE_NAME = "记录";

const { Title, Text } = Typography;

type TabKey = "agent" | "user" | "recent";

const TYPE_OPTIONS = [
  { value: "", label: "全部类型" },
  { value: "event", label: "事件" },
  { value: "decision", label: "决策" },
  { value: "fact", label: "事实" },
  { value: "procedure", label: "程序" },
  { value: "reflection", label: "反思" },
  { value: "context", label: "情境" },
  { value: "profile", label: "画像" },
  { value: "preference", label: "偏好" },
];

const TYPE_LABEL: Record<string, string> = Object.fromEntries(
  TYPE_OPTIONS.filter((o) => o.value).map((o) => [o.value, o.label]),
);

const STATUS_COLOR: Record<string, string> = {
  active: "blue",
  archived: "default",
  superseded: "orange",
  deleted: "red",
};

const SOURCE_LABEL: Record<string, string> = {
  link: "链接",
  file: "文件",
  snippet: "摘录",
  note: "笔记",
};

type ListItem = { id: string; created_at: string | null; title?: string };

function groupByDate(entries: ListItem[]): { key: string; label: string; items: ListItem[] }[] {
  const map = new Map<string, ListItem[]>();
  for (const e of entries) {
    const key = formatDateGroupKey(e.created_at) || "_unknown";
    const bucket = map.get(key);
    if (bucket) bucket.push(e);
    else map.set(key, [e]);
  }
  const keys = [...map.keys()].sort((a, b) => {
    if (a === "_unknown") return 1;
    if (b === "_unknown") return -1;
    return b.localeCompare(a);
  });
  return keys.map((key) => ({
    key,
    label: formatDateGroupLabel(key),
    items: (map.get(key) ?? []).sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? "")),
  }));
}

function defaultExpandedKeys(dateGroups: { key: string }[]): string[] {
  const keys: string[] = [];
  const today = formatDateGroupKey(new Date().toISOString());
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  const yesterdayKey = formatDateGroupKey(yesterday.toISOString());
  if (dateGroups.some((g) => g.key === today)) keys.push(today);
  if (dateGroups.some((g) => g.key === yesterdayKey)) keys.push(yesterdayKey);
  if (keys.length === 0 && dateGroups.length > 0) keys.push(dateGroups[0].key);
  return keys;
}

export function RecordsView() {
  const activeName = useStore((s) => s.activeName);
  // web 视图已在记录空间才挂对话浮窗（侧边栏点击触发视图切换，此处只读状态）
  const recordsViewActive = activeName === RECORDS_SPACE_NAME;
  const [searchParams, setSearchParams] = useSearchParams();
  const initialTab = ((): TabKey => {
    const t = searchParams.get("tab");
    return t === "user" || t === "recent" ? t : "agent";
  })();
  const [tab, setTab] = useState<TabKey>(initialTab);
  const [agentEnabled, setAgentEnabled] = useState(true);
  const [agentEntries, setAgentEntries] = useState<RecordEntry[]>([]);
  const [userEntries, setUserEntries] = useState<CollectionEntry[]>([]);
  const [agentCount, setAgentCount] = useState(0);
  const [userCount, setUserCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [agentDetail, setAgentDetail] = useState<RecordEntry | null>(null);
  const [userDetail, setUserDetail] = useState<CollectionEntry | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (tab === "recent") return;
    setLoading(true);
    try {
      if (tab === "agent") {
        const res = await fetchRecordList({
          query: query.trim() || undefined,
          type: typeFilter || undefined,
          includeArchived,
          limit: 100,
        });
        setAgentEnabled(res.enabled);
        setAgentEntries(res.entries);
        setAgentCount(res.count);
        setSelectedId((cur) => {
          if (cur && !res.entries.some((e) => e.id === cur)) {
            setAgentDetail(null);
            return null;
          }
          return cur;
        });
      } else {
        const res = await fetchCollectionList({
          query: query.trim() || undefined,
          limit: 100,
        });
        setUserEntries(res.entries);
        setUserCount(res.count);
        setSelectedId((cur) => {
          if (cur && !res.entries.some((e) => e.id === cur)) {
            setUserDetail(null);
            return null;
          }
          return cur;
        });
      }
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [tab, query, typeFilter, includeArchived]);

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- query via Search button
  }, [tab, typeFilter, includeArchived]);

  useEffect(() => {
    if (!selectedId) {
      setAgentDetail(null);
      setUserDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    const loader =
      tab === "agent"
        ? fetchRecordEntry(selectedId).then((res) => {
            if (!cancelled) {
              setAgentDetail(res.entry);
              setUserDetail(null);
            }
          })
        : fetchCollectionEntry(selectedId).then((res) => {
            if (!cancelled) {
              setUserDetail(res.entry);
              setAgentDetail(null);
            }
          });
    loader
      .catch((err) => {
        if (!cancelled) {
          message.error(err instanceof Error ? err.message : String(err));
          setSelectedId(null);
        }
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, tab]);

  const onTabChange = (key: string) => {
    setTab(key as TabKey);
    setSearchParams(key === "agent" ? {} : { tab: key }, { replace: true });
    setSelectedId(null);
    setAgentDetail(null);
    setUserDetail(null);
    setQuery("");
  };

  const onArchive = async () => {
    if (!selectedId || tab !== "agent") return;
    setBusy(true);
    try {
      await archiveRecord(selectedId);
      message.success("已归档");
      setSelectedId(null);
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const onUnarchive = async () => {
    if (!selectedId || tab !== "agent") return;
    setBusy(true);
    try {
      await unarchiveRecord(selectedId);
      message.success("已恢复为活跃");
      await load();
      setSelectedId(selectedId);
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const onForget = async () => {
    if (!selectedId || tab === "recent") return;
    setBusy(true);
    try {
      await deleteRecord(selectedId, tab);
      message.success(tab === "user" ? "已取消收藏" : "已删除");
      setSelectedId(null);
      setAgentDetail(null);
      setUserDetail(null);
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (error && !agentEntries.length && !userEntries.length && !loading) {
    return (
      <div style={{ padding: 24 }}>
        <Alert type="error" message="加载失败" description={error} showIcon />
      </div>
    );
  }

  const dateGroups = useMemo(
    () => groupByDate(tab === "agent" ? agentEntries : userEntries),
    [tab, agentEntries, userEntries],
  );
  const defaultDateKeys = useMemo(() => defaultExpandedKeys(dateGroups), [dateGroups]);
  const listEmpty = tab === "agent" ? agentEntries.length === 0 : userEntries.length === 0;

  const header = (
    <PageHeader
      divider={false}
      title={
        <>
          <BookOutlined style={{ marginRight: 8 }} />
          记录
        </>
      }
      actions={
        <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
          刷新
        </Button>
      }
    />
  );

  const toolbar = (
    <div
      style={{
        padding: "0 20px 12px",
        borderBottom: "1px solid var(--coara-border-faint)",
        background: "var(--coara-surface)",
        flexShrink: 0,
      }}
    >
        <Tabs
          activeKey={tab}
          onChange={onTabChange}
          style={{ marginTop: 0, marginBottom: 0 }}
          items={[
            {
              key: "agent",
              label: (
                <span>
                  <BookOutlined /> 笔记{agentCount ? ` · ${agentCount}` : ""}
                </span>
              ),
            },
            {
              key: "user",
              label: (
                <span>
                  <StarOutlined /> 收藏{userCount ? ` · ${userCount}` : ""}
                </span>
              ),
            },
            {
              key: "recent",
              label: (
                <span>
                  <ClockCircleOutlined /> 最近
                </span>
              ),
            },
          ]}
        />
        {tab !== "recent" && (
        <Space wrap style={{ width: "100%" }}>
          <Input.Search
            allowClear
            placeholder={tab === "agent" ? "搜索标题 / 正文 / 标签" : "搜索收藏标题 / 摘要"}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onSearch={() => void load()}
            enterButton={<SearchOutlined />}
            style={{ width: 280 }}
          />
          {tab === "agent" && (
            <>
              <Select value={typeFilter} onChange={setTypeFilter} options={TYPE_OPTIONS} style={{ width: 120 }} />
              <Space size={6}>
                <Switch size="small" checked={includeArchived} onChange={setIncludeArchived} />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  含归档
                </Text>
              </Space>
            </>
          )}
          <Text type="secondary" style={{ fontSize: 12 }}>
            {tab === "agent" ? agentCount : userCount} 条
          </Text>
        </Space>
        )}
      </div>
  );

  return (
    <PageShell header={header} toolbar={toolbar} scroll="hidden">
      {tab === "recent" ? (
        <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
          <RecentFilesList />
        </div>
      ) : tab === "agent" && !agentEnabled ? (
        <div style={{ padding: 24, maxWidth: 640 }}>
          <Alert
            type="info"
            showIcon
            message="笔记未启用"
            description="配置里 records.enabled 为 false。请到「配置」页用配置助手打开，并重启 coara。收藏 Tab 仍可用。"
          />
        </div>
      ) : (
        <div style={{ flex: 1, display: "flex", minHeight: 0, overflow: "hidden" }}>
          <div
            style={{
              width: 360,
              borderRight: "1px solid var(--coara-border-faint)",
              overflow: "auto",
              background: "var(--coara-bg-subtle)",
            }}
          >
            {loading && listEmpty ? (
              <LoadingState fill={false} tip="加载中…" />
            ) : listEmpty ? (
              <EmptyState
                fill={false}
                description={
                  tab === "agent"
                    ? "暂无笔记。由 janitor / daily 写入。"
                    : "暂无收藏。可在文件页点「收藏」，或本页右上角添加链接/文本。"
                }
              />
            ) : (
              <Collapse
                ghost
                size="small"
                defaultActiveKey={defaultDateKeys}
                items={dateGroups.map((g) => ({
                  key: g.key,
                  label: (
                    <Space size={8}>
                      <Text strong style={{ fontSize: 13 }}>
                        {g.label}
                      </Text>
                      <Tag style={{ margin: 0 }}>{g.items.length}</Tag>
                    </Space>
                  ),
                  children: (
                    tab === "agent"
                      ? (g.items as RecordEntry[])
                      : (g.items as CollectionEntry[])
                  ).map((e) => {
                    const active = e.id === selectedId;
                    return (
                      <button
                        key={e.id}
                        type="button"
                        onClick={() => setSelectedId(e.id)}
                        style={{
                          display: "block",
                          width: "100%",
                          textAlign: "left",
                          border: "none",
                          borderBottom: "1px solid var(--coara-border-faint)",
                          padding: "5px 10px",
                          cursor: "pointer",
                          background: active ? "var(--coara-accent-subtle)" : "var(--coara-surface)",
                        }}
                      >
                        <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
                          <span style={{ display: "inline-flex", alignItems: "center", gap: 4, flexShrink: 0 }}>
                            {tab === "agent" && "type" in e && e.type && (
                              <Tag
                                bordered={false}
                                style={{ margin: 0, fontSize: 11, lineHeight: "18px", padding: "0 4px" }}
                              >
                                {TYPE_LABEL[e.type] || e.type}
                              </Tag>
                            )}
                            {tab === "user" && "source_type" in e && (
                              <Tag
                                bordered={false}
                                color={e.source_type === "file" ? "gold" : "default"}
                                style={{ margin: 0, fontSize: 11, lineHeight: "18px", padding: "0 4px" }}
                              >
                                {SOURCE_LABEL[e.source_type] || e.source_type || "收藏"}
                              </Tag>
                            )}
                            {tab === "agent" && "status" in e && e.status && e.status !== "active" && (
                              <Tag
                                bordered={false}
                                color={STATUS_COLOR[e.status] || "default"}
                                style={{ margin: 0, fontSize: 11, lineHeight: "18px", padding: "0 4px" }}
                              >
                                {e.status}
                              </Tag>
                            )}
                          </span>
                          <Text strong ellipsis style={{ fontSize: 13, flex: 1, minWidth: 0 }}>
                            {e.title || e.id}
                          </Text>
                          <Text type="secondary" style={{ fontSize: 11, whiteSpace: "nowrap", flexShrink: 0 }}>
                            {formatTimeHm(e.created_at)}
                          </Text>
                        </div>
                      </button>
                    );
                  }),
                }))}
              />
            )}
          </div>

          <div style={{ flex: 1, overflow: "auto", padding: 20, background: "var(--coara-surface)" }}>
            {!selectedId ? (
              <EmptyState
                fill={false}
                description={tab === "agent" ? "选择左侧一条笔记查看详情" : "选择左侧一条收藏查看详情"}
              />
            ) : detailLoading ? (
              <LoadingState fill={false} tip="加载中…" />
            ) : tab === "agent" && agentDetail ? (
              <>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 12,
                    flexWrap: "wrap",
                    marginBottom: 12,
                  }}
                >
                  <div>
                    <Title level={4} style={{ margin: "0 0 8px" }}>
                      {agentDetail.title || agentDetail.id}
                    </Title>
                    <Space wrap size={[4, 4]}>
                      <Tag>{TYPE_LABEL[agentDetail.type] || agentDetail.type}</Tag>
                      <Tag color={STATUS_COLOR[agentDetail.status] || "default"}>{agentDetail.status}</Tag>
                      <Tag>{agentDetail.scope}</Tag>
                      {agentDetail.tags.map((t) => (
                        <Tag key={t}>{t}</Tag>
                      ))}
                    </Space>
                  </div>
                  <Space wrap>
                    {agentDetail.status === "archived" ? (
                      <Button size="small" icon={<InboxOutlined />} loading={busy} onClick={() => void onUnarchive()}>
                        恢复
                      </Button>
                    ) : (
                      <Button size="small" icon={<InboxOutlined />} loading={busy} onClick={() => void onArchive()}>
                        归档
                      </Button>
                    )}
                    <Popconfirm
                      title="确定永久删除这条笔记？"
                      okText="删除"
                      cancelText="取消"
                      okButtonProps={{ danger: true }}
                      onConfirm={() => void onForget()}
                    >
                      <Button size="small" danger icon={<DeleteOutlined />} loading={busy}>
                        忘记
                      </Button>
                    </Popconfirm>
                  </Space>
                </div>
                <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 12 }}>
                  id: {agentDetail.id} · 创建 {formatDateTime(agentDetail.created_at)} · 访问 {agentDetail.access_count}{" "}
                  次 · 来源 {agentDetail.source_type}
                  {agentDetail.path ? ` · ${agentDetail.path}` : ""}
                </Text>
                <div
                  style={{
                    background: "var(--coara-bg-subtle)",
                    border: "1px solid var(--coara-border-faint)",
                    borderRadius: 8,
                    padding: 16,
                    fontSize: 13,
                    lineHeight: 1.6,
                  }}
                >
                  {agentDetail.content ? (
                    <MarkdownMessage text={agentDetail.content} />
                  ) : (
                    <Text type="secondary">（无正文）</Text>
                  )}
                </div>
              </>
            ) : tab === "user" && userDetail ? (
              <>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 12,
                    flexWrap: "wrap",
                    marginBottom: 12,
                  }}
                >
                  <div>
                    <Title level={4} style={{ margin: "0 0 8px" }}>
                      {userDetail.title || userDetail.id}
                    </Title>
                    <Space wrap size={[4, 4]}>
                      <Tag color={userDetail.source_type === "file" ? "gold" : "blue"}>
                        {SOURCE_LABEL[userDetail.source_type] || userDetail.source_type}
                      </Tag>
                      {userDetail.tags.map((t) => (
                        <Tag key={t}>{t}</Tag>
                      ))}
                    </Space>
                  </div>
                  <Space wrap>
                    {userDetail.has_file && (
                      <Button
                        size="small"
                        icon={<DownloadOutlined />}
                        href={collectionFileUrl(userDetail.id)}
                        target="_blank"
                        rel="noreferrer"
                      >
                        下载文件
                      </Button>
                    )}
                    <Popconfirm
                      title="确定取消这条收藏？"
                      okText="取消收藏"
                      cancelText="返回"
                      okButtonProps={{ danger: true }}
                      onConfirm={() => void onForget()}
                    >
                      <Button size="small" danger icon={<DeleteOutlined />} loading={busy}>
                        取消收藏
                      </Button>
                    </Popconfirm>
                  </Space>
                </div>
                <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 12 }}>
                  id: {userDetail.id} · 创建 {formatDateTime(userDetail.created_at)} · 访问 {userDetail.access_count} 次
                  {userDetail.source_url ? ` · ${userDetail.source_url}` : ""}
                  {userDetail.file_name ? ` · ${userDetail.file_name}` : ""}
                </Text>
                {userDetail.summary && (
                  <div style={{ marginBottom: 12 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      摘要
                    </Text>
                    <div style={{ marginTop: 4, fontSize: 13, lineHeight: 1.6 }}>
                      <MarkdownMessage text={userDetail.summary} />
                    </div>
                  </div>
                )}
                {userDetail.note && (
                  <div style={{ marginBottom: 12 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      备注
                    </Text>
                    <div style={{ marginTop: 4, fontSize: 13 }}>{userDetail.note}</div>
                  </div>
                )}
                <div
                  style={{
                    background: "var(--coara-bg-subtle)",
                    border: "1px solid var(--coara-border-faint)",
                    borderRadius: 8,
                    padding: 16,
                    fontSize: 13,
                    lineHeight: 1.6,
                  }}
                >
                  {userDetail.content ? (
                    <MarkdownMessage text={userDetail.content} />
                  ) : (
                    <Text type="secondary">（无正文）</Text>
                  )}
                </div>
              </>
            ) : (
              <ErrorState fill={false} message="详情加载失败" />
            )}
          </div>
        </div>
      )}
      {recordsViewActive && (
        <Suspense fallback={null}>
          <ModuleChatFloat subject="records" title="与记录助手对话" emptyHint="问记录：这条记录哪来的、本周记了什么" />
        </Suspense>
      )}
    </PageShell>
  );
}
