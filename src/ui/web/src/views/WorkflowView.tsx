import { useCallback, useEffect, useState } from "react";
import { Button, Modal, Spin, Typography, message } from "antd";
import {
  DeleteOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import { WorkflowEditor } from "../features/workflow/editor/WorkflowEditor";
import { ModuleChatFloat } from "../features/chat/ModuleChatFloat";
import { ExperimentalBadge } from "../components/ExperimentalBadge";
import { PageShell } from "../components/layout/PageShell";
import { PageHeader } from "../components/layout/PageHeader";
import { useStore, flowGraphKey } from "../lib/store";
import { getWS } from "../lib/ws";
import {
  createWorkflowDraft,
  deleteWorkflowDraft,
  fetchModuleSessionMessages,
  fetchWorkflowDrafts,
  reportActiveWorkflowDraft,
} from "../lib/api";
import "../features/workflow/flow-live/flow-live.css";
import "../features/workflow/editor/editor.css";

const { Text } = Typography;

const WORKBENCH_DRAFT_KEY = "coara.workflow.workbenchDraftId";

interface DraftRow {
  draft_id: string;
  name: string;
  updated_at: string;
}

async function ensureWorkbenchDraft(): Promise<string> {
  const stored = localStorage.getItem(WORKBENCH_DRAFT_KEY);
  if (stored) {
    try {
      const list = await fetchWorkflowDrafts();
      const found = (list.drafts || []).some(
        (d: { draft_id: string }) => d.draft_id === stored,
      );
      if (found) return stored;
    } catch {
      /* fall through */
    }
  }
  const created = await createWorkflowDraft("workbench");
  localStorage.setItem(WORKBENCH_DRAFT_KEY, created.draft_id);
  return created.draft_id;
}

export function WorkflowView() {
  const flowGraphs = useStore((s) => s.flowGraphs);
  const activeName = useStore((s) => s.activeName);

  const liveFlowKeys = Object.keys(flowGraphs)
    .filter((k) => k.startsWith("flow:"))
    .sort((a, b) => flowGraphs[b].updatedAt.localeCompare(flowGraphs[a].updatedAt));
  const liveKey = liveFlowKeys[0] ?? null;
  const liveFlowName = liveKey ? liveKey.slice("flow:".length) : null;

  const [draftId, setDraftId] = useState<string | null>(null);
  const [workbenchId, setWorkbenchId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<DraftRow[]>([]);
  const [draftError, setDraftError] = useState<string | null>(null);
  const [draftsCollapsed, setDraftsCollapsed] = useState(false);

  // 上报当前打开的草案：服务端随之把构建对话切到该草案的专属会话
  // （每草案一个 section），上报完成后用新 section 的历史刷新构建对话面板
  useEffect(() => {
    let cancelled = false;
    void reportActiveWorkflowDraft(draftId).then(async () => {
      if (cancelled) return;
      const { loadModuleHistory } = useStore.getState();
      const resetModuleSession = (subject: string) => {
        const st = useStore.getState();
        useStore.setState({
          moduleSessions: {
            ...st.moduleSessions,
            [subject]: { messages: [], turnActive: false, currentTurnId: null },
          },
        });
      };
      try {
        const data = await fetchModuleSessionMessages("flow", 500);
        if (cancelled) return;
        resetModuleSession("flow");
        if (data.messages?.length) loadModuleHistory("flow", data.messages);
      } catch {
        /* best-effort：刷新失败下条消息仍会进当前 section */
      }
    });
    return () => {
      cancelled = true;
    };
  }, [draftId]);

  /** 当前编辑的是工作台稿时，构建对话的 live WDL 灌进同一块编辑器。 */
  const followingLive = Boolean(draftId && workbenchId && draftId === workbenchId);
  const liveWdl =
    followingLive && liveKey && flowGraphs[liveKey]?.wdl
      ? flowGraphs[liveKey].wdl
      : null;
  const liveNodes =
    followingLive && liveKey && flowGraphs[liveKey]
      ? flowGraphs[liveKey].nodes
      : null;
  const liveHops =
    followingLive && liveKey && flowGraphs[liveKey] ? flowGraphs[liveKey].hops : 0;

  const activeDraft = drafts.find((d) => d.draft_id === draftId);
  const switchLabel = activeDraft?.name || liveFlowName || "工作流";

  const reloadDrafts = useCallback(async () => {
    try {
      const data = await fetchWorkflowDrafts();
      setDrafts(data.drafts || []);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载工作流失败");
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const id = await ensureWorkbenchDraft();
        if (cancelled) return;
        setWorkbenchId(id);
        setDraftId(id);
        await reloadDrafts();
      } catch (err) {
        if (!cancelled) setDraftError(String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadDrafts]);

  // 订阅 FlowRoot 图 → live WDL
  useEffect(() => {
    const ws = getWS();
    const subject = "flow" as const;
    if (!liveFlowName) return;
    const key = flowGraphKey(liveFlowName, subject);
    const requestSnapshot = () => {
      ws.send({ type: "flow_snapshot", flow: liveFlowName, subject });
    };
    if (!useStore.getState().flowGraphs[key]) {
      useStore.setState((s) => ({
        flowGraphs: {
          ...s.flowGraphs,
          [key]: {
            name: liveFlowName,
            hops: 0,
            nodes: {},
            edges: [],
            loaded: false,
            updatedAt: new Date().toISOString(),
          },
        },
      }));
    }
    requestSnapshot();
    const unsub = ws.onConnection((ok) => {
      if (ok) requestSnapshot();
    });
    const timer = setInterval(requestSnapshot, 3000);
    return () => {
      clearInterval(timer);
      unsub();
    };
  }, [liveFlowName]);

  // live 图名变化时刷新列表（save 后名称会进草案）
  useEffect(() => {
    if (!liveFlowName) return;
    void reloadDrafts();
  }, [liveFlowName, reloadDrafts]);

  // 编排写穿：会话侧建/改草案 → revision 递增 → 列表刷新
  const draftRevisions = useStore((s) => s.workflowDraftRevisions);
  useEffect(() => {
    void reloadDrafts();
  }, [draftRevisions, reloadDrafts]);

  const selectDraft = (id: string) => {
    setDraftId(id);
    if (id === workbenchId) {
      localStorage.setItem(WORKBENCH_DRAFT_KEY, id);
    }
  };

  const createNew = async () => {
    try {
      const created = await createWorkflowDraft(`flow-${Date.now().toString(36).slice(-4)}`);
      localStorage.setItem(WORKBENCH_DRAFT_KEY, created.draft_id);
      setWorkbenchId(created.draft_id);
      setDraftId(created.draft_id);
      await reloadDrafts();
      message.success("已新建工作流");
    } catch (err) {
      message.error(String(err));
    }
  };

  const confirmDelete = (id: string, name: string) => {
    Modal.confirm({
      title: "删除工作流",
      content: `确定删除「${name || id}」？该操作不可撤销。`,
      okText: "删除",
      okButtonProps: { danger: true },
      onOk: async () => {
        await deleteWorkflowDraft(id);
        message.success("已删除");
        const data = await fetchWorkflowDrafts();
        const list = data.drafts || [];
        setDrafts(list);
        if (draftId === id) {
          if (list.length > 0) {
            // 切到列表第一个，避免删除后停留在空草案导致转圈
            const next = list[0].draft_id;
            setDraftId(next);
            if (next === workbenchId) localStorage.setItem(WORKBENCH_DRAFT_KEY, next);
          } else {
            // 无草案则新建 workbench 兜底
            const created = await createWorkflowDraft("workbench");
            localStorage.setItem(WORKBENCH_DRAFT_KEY, created.draft_id);
            setWorkbenchId(created.draft_id);
            setDraftId(created.draft_id);
            await reloadDrafts();
          }
        } else {
          await reloadDrafts();
        }
      },
    });
  };

  return (
    <PageShell
      scroll="hidden"
      padded={false}
      header={
        <PageHeader
          title={
            <span style={{ display: "inline-flex", alignItems: "center", gap: 8, maxWidth: 320 }}>
              <span
                style={{
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {switchLabel}
              </span>
              <ExperimentalBadge />
            </span>
          }
          subline={
            activeName ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {activeName}
              </Text>
            ) : undefined
          }
        />
      }
    >
      <div className="flow-workbench-body">
        <aside className={`flow-workbench-drafts${draftsCollapsed ? " collapsed" : ""}`}>
          <div className="flow-workbench-drafts-head">
            <span className="flow-workbench-drafts-title">工作流</span>
            <Button
              size="small"
              type="text"
              icon={<PlusOutlined />}
              title="新建工作流"
              onClick={() => void createNew()}
            />
            <Button
              size="small"
              type="text"
              icon={<MenuFoldOutlined />}
              title="收起列表"
              onClick={() => setDraftsCollapsed(true)}
            />
          </div>
          {!draftsCollapsed && (
            <div className="flow-workbench-drafts-list">
              {drafts.map((d) => (
                <div
                  key={d.draft_id}
                  className={`flow-workbench-draft-item${d.draft_id === draftId ? " active" : ""}`}
                  onClick={() => selectDraft(d.draft_id)}
                >
                  <span className="flow-workbench-draft-name" title={d.name || d.draft_id}>
                    {d.name || d.draft_id}
                  </span>
                  <DeleteOutlined
                    className="flow-workbench-draft-del"
                    onClick={(e) => {
                      e.stopPropagation();
                      confirmDelete(d.draft_id, d.name);
                    }}
                  />
                </div>
              ))}
            </div>
          )}
          {draftsCollapsed && (
            <button
              className="flow-workbench-drafts-expand"
              onClick={() => setDraftsCollapsed(false)}
              title="展开工作流列表"
            >
              <MenuUnfoldOutlined />
            </button>
          )}
        </aside>
        <main className="flow-workbench-canvas flow-workbench-editor">
          {draftError ? (
            <div className="flow-live-empty">
              <span style={{ color: "var(--coara-danger)" }}>无法打开：{draftError}</span>
            </div>
          ) : !draftId ? (
            <div className="flow-live-empty">
              <Spin tip="准备编辑器…" />
            </div>
          ) : (
            <WorkflowEditor
              draftId={draftId}
              liveWdl={liveWdl}
              liveNodes={liveNodes}
              liveHops={liveHops}
            />
          )}
        </main>
      </div>
      <ModuleChatFloat
        subject="flow"
        title="构建对话"
        emptyHint={"说出目标与节点拆分\n画布会随节点实时更新"}
      />
    </PageShell>
  );
}
