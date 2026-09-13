import { useCallback, useEffect, useMemo, useState, Suspense, lazy } from "react";
import {
  Button,
  Input,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Tag,
  Typography,
  message,
} from "antd";
import { ReloadOutlined, SendOutlined } from "@ant-design/icons";
import { ExperimentalBadge } from "../components/ExperimentalBadge";
import { PageShell } from "../components/layout/PageShell";
import { PageHeader } from "../components/layout/PageHeader";
import { EmptyState, LoadingState } from "../components/states/States";
import { useNavigate } from "react-router-dom";
import {
  archiveUpdate,
  fetchUpdatesList,
  fetchUpdatesSummary,
  markUpdateRead,
  reviewUpdate,
  switchWorkspace,
  type UpdateItem,
  type UpdateSalience,
  type UpdateStatus,
} from "../lib/api";
import { useStore } from "../lib/store";
import { formatDateTime } from "../lib/format";

const { Text, Paragraph } = Typography;

const TYPE_LABELS: Record<string, string> = {
  reminder: "提醒",
  file_change: "文件变动",
  webhook: "Webhook",
  workflow: "工作流",
  note: "动态",
};

const DISPOSITION_LABELS: Record<string, string> = {
  pending: "待过目",
  elevated: "已呈阅",
  resolved: "已处置",
  dismissed: "已勾掉",
};

const DISPOSITION_COLORS: Record<string, string> = {
  pending: "var(--coara-text-tertiary)",
  elevated: "var(--coara-accent)",
  resolved: "var(--coara-success)",
  dismissed: "var(--coara-border)",
};

const SALIENCE_COLORS: Record<UpdateSalience, string> = {
  high: "var(--coara-warning)",
  normal: "var(--coara-text-tertiary)",
  low: "var(--coara-border)",
};

/** 与消息空间对话浮窗：仅当 web 视图已切到消息空间（侧边栏点「消息」触发）时挂载。 */
const ModuleChatFloat = lazy(() =>
  import("../features/chat/ModuleChatFloat").then((m) => ({ default: m.ModuleChatFloat })),
);

/** 消息空间的展示名（侧边栏 home: 分支即按它找 home_view）。 */
const REVIEW_SPACE_NAME = "消息";

function typeLabel(item: UpdateItem): string {
  return TYPE_LABELS[item.type] || TYPE_LABELS[item.event_type] || item.type || "动态";
}

export function ReviewView() {
  const navigate = useNavigate();
  const workspaces = useStore((s) => s.workspaces);
  const activeName = useStore((s) => s.activeName);
  const reviewViewActive = activeName === REVIEW_SPACE_NAME;
  const setUpdatesPending = useStore((s) => s.setUpdatesPending);
  const resetForWorkspaceSwitch = useStore((s) => s.resetForWorkspaceSwitch);
  const hydrateToolActivity = useStore((s) => s.hydrateToolActivity);

  const [items, setItems] = useState<UpdateItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [salienceFilter, setSalienceFilter] = useState<"all" | "high">("all");
  const [workspaceFilter, setWorkspaceFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<UpdateStatus | "all">("unread");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [reviewOpenId, setReviewOpenId] = useState<string | null>(null);
  const [reviewText, setReviewText] = useState("");
  const [acting, setActing] = useState<string | null>(null);

  const refreshSummary = useCallback(() => {
    fetchUpdatesSummary()
      .then((s) => setUpdatesPending(s.pending))
      .catch(() => undefined);
  }, [setUpdatesPending]);

  const load = useCallback(() => {
    setLoading(true);
    fetchUpdatesList({
      workspace: workspaceFilter || undefined,
      status: statusFilter,
      salience: salienceFilter === "high" ? "high" : undefined,
      limit: 100,
    })
      .then((data) => setItems(data.items))
      .catch((err) => message.error(`加载失败: ${err instanceof Error ? err.message : err}`))
      .finally(() => setLoading(false));
  }, [workspaceFilter, statusFilter, salienceFilter]);

  useEffect(() => {
    load();
  }, [load]);

  // 侧栏角标首次填充
  useEffect(() => {
    refreshSummary();
  }, [refreshSummary]);

  const removeItem = (id: string) => {
    setItems((prev) => prev.filter((it) => it.message_id !== id));
    refreshSummary();
  };

  const handleRead = async (item: UpdateItem) => {
    setActing(item.message_id);
    try {
      await markUpdateRead(item.message_id);
      if (statusFilter === "unread") removeItem(item.message_id);
      else load();
      refreshSummary();
    } catch (err) {
      message.error(`操作失败: ${err instanceof Error ? err.message : err}`);
    } finally {
      setActing(null);
    }
  };

  const handleArchive = async (item: UpdateItem) => {
    setActing(item.message_id);
    try {
      await archiveUpdate(item.message_id);
      removeItem(item.message_id);
    } catch (err) {
      message.error(`操作失败: ${err instanceof Error ? err.message : err}`);
    } finally {
      setActing(null);
    }
  };

  const handleReview = async (item: UpdateItem) => {
    const text = reviewText.trim();
    if (!text) return;
    setActing(item.message_id);
    try {
      const result = await reviewUpdate(item.message_id, text);
      message.success(`批示已发往 @${result.workspace}`);
      setReviewOpenId(null);
      setReviewText("");
      removeItem(item.message_id);
    } catch (err) {
      message.error(`批示失败: ${err instanceof Error ? err.message : err}`);
    } finally {
      setActing(null);
    }
  };

  const handleEnter = async (item: UpdateItem) => {
    try {
      const result = await switchWorkspace(item.workspace);
      const dir = String(result?.workspace_dir || "").trim();
      if (!dir) {
        message.error("切换成功但未收到空间标识，请刷新页面");
        return;
      }
      // 响应自带目标空间快照：就地一次提交上屏（内容 + runtime + 折叠区 + 线身份
      // 同批落定），不必再等下一次 hydrate。旧服务端没有 snapshot 时退回原流程。
      resetForWorkspaceSwitch(dir, result.snapshot ?? null);
      void hydrateToolActivity();
      navigate("/chat");
    } catch (err) {
      message.error(`切换失败: ${err instanceof Error ? err.message : err}`);
    }
  };

  const toggleExpanded = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const workspaceOptions = useMemo(
    () => [
      { value: "", label: "全部工作空间" },
      ...workspaces.map((ws) => ({ value: ws.name, label: ws.name })),
    ],
    [workspaces]
  );

  const header = (
    <PageHeader
      title={
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          消息
          <ExperimentalBadge />
        </span>
      }
      actions={
        <Space size={8} wrap>
          <Segmented
            size="small"
            value={salienceFilter}
            onChange={(v) => setSalienceFilter(v as "all" | "high")}
            options={[
              { value: "all", label: "全部" },
              { value: "high", label: "仅高显著" },
            ]}
          />
          <Select
            size="small"
            value={workspaceFilter}
            onChange={setWorkspaceFilter}
            options={workspaceOptions}
            style={{ minWidth: 140 }}
          />
          <Select
            size="small"
            value={statusFilter}
            onChange={(v) => setStatusFilter(v as UpdateStatus | "all")}
            options={[
              { value: "unread", label: "未读" },
              { value: "read", label: "已读" },
              { value: "archived", label: "已归档" },
              { value: "all", label: "全部状态" },
            ]}
            style={{ width: 96 }}
          />
          <Button size="small" icon={<ReloadOutlined />} onClick={load} />
        </Space>
      }
    />
  );

  return (
    <PageShell header={header} surface="subtle" padded={false}>
      <div style={{ padding: "20px 28px" }}>
      <div style={{ maxWidth: 860, margin: "0 auto" }}>
        {loading ? (
          <LoadingState fill={false} tip="加载中…" />
        ) : items.length === 0 ? (
          <EmptyState
            fill={false}
            description={statusFilter === "unread" ? "没有新消息" : "全部消息已读完"}
          />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {items.map((item) => {
              const isExpanded = expanded.has(item.message_id);
              const summary = (item.display_text || item.text || "").trim();
              const isReviewOpen = reviewOpenId === item.message_id;
              const busy = acting === item.message_id;
              return (
                <div
                  key={item.message_id}
                  style={{
                    background: "var(--coara-surface)",
                    border: "1px solid var(--coara-border-faint)",
                    borderRadius: 12,
                    padding: "14px 16px",
                    transition: "box-shadow 200ms ease, border-color 200ms ease",
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.borderColor = "var(--coara-border-muted)";
                    e.currentTarget.style.boxShadow = "var(--coara-shadow-hover)";
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.borderColor = "var(--coara-border-faint)";
                    e.currentTarget.style.boxShadow = "none";
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span
                      style={{
                        width: 8,
                        height: 8,
                        borderRadius: "50%",
                        flexShrink: 0,
                        background: SALIENCE_COLORS[item.salience] || SALIENCE_COLORS.normal,
                      }}
                      title={`显著性 ${item.salience}`}
                    />
                    <Text strong style={{ fontSize: 14, flex: 1, minWidth: 0 }} ellipsis>
                      {item.title || "（无标题）"}
                    </Text>
                    <Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>
                      {formatDateTime(item.created_at)}
                    </Text>
                  </div>
                  <div style={{ marginTop: 4, marginLeft: 16 }}>
                    <Space size={6}>
                      <Tag style={{ marginInlineEnd: 0 }}>{item.workspace}</Tag>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {typeLabel(item)}
                      </Text>
                    </Space>
                  </div>
                  {summary && (
                    <Paragraph
                      type="secondary"
                      style={{ fontSize: 13, marginTop: 6, marginBottom: 0, marginLeft: 16 }}
                      ellipsis={
                        isExpanded
                          ? false
                          : { rows: 2, expandable: true, symbol: "展开", onExpand: () => toggleExpanded(item.message_id) }
                      }
                    >
                      {summary}
                    </Paragraph>
                  )}
                  {isExpanded && (
                    <Button type="link" size="small" onClick={() => toggleExpanded(item.message_id)}>
                      收起
                    </Button>
                  )}
                  {(() => {
                    const history = item.review_history || [];
                    const hasReview =
                      history.length > 0 || (item.disposition && item.disposition !== "pending");
                    if (!hasReview) return null;
                    return (
                      <div
                        style={{
                          marginTop: 10,
                          marginLeft: 16,
                          padding: "8px 12px",
                          background: "var(--coara-bg-subtle)",
                          borderRadius: 8,
                          border: "1px solid var(--coara-border-faint)",
                        }}
                      >
                        <Space size={6} style={{ marginBottom: 4 }}>
                          <Text strong style={{ fontSize: 12 }}>
                            处置轨迹
                          </Text>
                          {item.disposition && (
                            <Tag
                              style={{ marginInlineEnd: 0, fontSize: 11 }}
                              color={DISPOSITION_COLORS[item.disposition] || undefined}
                            >
                              {DISPOSITION_LABELS[item.disposition] || item.disposition}
                            </Tag>
                          )}
                        </Space>
                        {history.length > 0 ? (
                          history.map((entry, idx) => (
                            <div key={idx} style={{ fontSize: 12, color: "var(--coara-text-secondary)", marginTop: 2 }}>
                              {DISPOSITION_LABELS[entry.action] || entry.action} · {entry.by} ·{" "}
                              {formatDateTime(entry.at)}
                              {entry.note ? ` — ${entry.note}` : ""}
                            </div>
                          ))
                        ) : (
                          <div style={{ fontSize: 12, color: "var(--coara-text-secondary)", marginTop: 2 }}>
                            {DISPOSITION_LABELS[item.disposition || ""] || item.disposition} ·{" "}
                            {item.reviewed_by || "—"} · {item.reviewed_at ? formatDateTime(item.reviewed_at) : "—"}
                            {item.review_note ? ` — ${item.review_note}` : ""}
                          </div>
                        )}
                      </div>
                    );
                  })()}
                  <div style={{ marginTop: 8, marginLeft: 8 }}>
                    <Space size={4} wrap>
                      <Button size="small" type="text" disabled={busy} onClick={() => void handleRead(item)}>
                        知道了
                      </Button>
                      <Popconfirm
                        title="忽略这条内容？"
                        description="归档后不再出现在列表里"
                        onConfirm={() => void handleArchive(item)}
                        okText="忽略"
                        cancelText="取消"
                      >
                        <Button size="small" type="text" danger disabled={busy}>
                          忽略
                        </Button>
                      </Popconfirm>
                      <Button
                        size="small"
                        type="text"
                        style={{ color: "var(--coara-accent)" }}
                        disabled={busy}
                        onClick={() => {
                          setReviewOpenId(isReviewOpen ? null : item.message_id);
                          setReviewText("");
                        }}
                      >
                        批示
                      </Button>
                      <Button size="small" type="text" disabled={busy} onClick={() => void handleEnter(item)}>
                        切入处理 →
                      </Button>
                    </Space>
                  </div>
                  {isReviewOpen && (
                    <div style={{ marginTop: 8, marginLeft: 16, display: "flex", gap: 8 }}>
                      <Input.TextArea
                        autoSize={{ minRows: 1, maxRows: 4 }}
                        placeholder={`批示发往 @${item.workspace}，该空间会话按指示处理`}
                        value={reviewText}
                        onChange={(e) => setReviewText(e.target.value)}
                        onPressEnter={(e) => {
                          if (!e.shiftKey) {
                            e.preventDefault();
                            void handleReview(item);
                          }
                        }}
                        autoFocus
                      />
                      <Button
                        type="primary"
                        icon={<SendOutlined />}
                        loading={busy}
                        disabled={!reviewText.trim()}
                        onClick={() => void handleReview(item)}
                      />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        </div>
      </div>
      {reviewViewActive && (
        <Suspense fallback={null}>
          <ModuleChatFloat
            subject="root"
            title="与消息空间对话"
            emptyHint="问消息：今天有哪些待过目动态、帮我汇总高优先级"
          />
        </Suspense>
      )}
    </PageShell>
  );
}
