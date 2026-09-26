// WorkflowView — WDL 工作台主视图：左侧 WDL 文件列表 + 右侧画布编辑器。
//
// 迁移口径（自 coara Web 工作台）：
// - 草案列表 = WDL 文件列表（/api/files），新建/删除/选择即文件操作；
// - 运行态来自 `wdl serve` 的 WS 引擎事件（node_started/completed/failed
//   投影为节点徽章），不再订阅 coara 的 FlowRoot flow_snapshot；
// - 去掉构建对话（FlowChatFloat）与节点模型设置（跟随 providers.yaml）。

import { useCallback, useEffect, useState } from "react";
import { Button, Modal, Spin, Typography, message } from "antd";
import {
  DeleteOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import { WorkflowEditor } from "../features/workflow/editor/WorkflowEditor";
import {
  createWdlFile,
  deleteWorkflowDraft,
  fetchWdlFiles,
  type WdlFileRow,
} from "../lib/api";
import { useStore } from "../lib/store";
import { getWS, type FlowLiveNode } from "../lib/ws";
import "../features/workflow/editor/editor.css";
import "../features/workflow/flow-live/flow-live.css";

const { Text } = Typography;

const LAST_FILE_KEY = "wdl.workbench.lastFile";

export function WorkflowView() {
  const runLiveNodes = useStore((s) => s.runLiveNodes);
  const mergeRunNode = useStore((s) => s.mergeRunNode);
  const setRunStatus = useStore((s) => s.setRunStatus);

  const [files, setFiles] = useState<WdlFileRow[]>([]);
  const [fileName, setFileName] = useState<string | null>(null);
  const [runInstanceId, setRunInstanceId] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [listCollapsed, setListCollapsed] = useState(false);

  const reloadFiles = useCallback(async () => {
    try {
      const data = await fetchWdlFiles();
      setFiles(data.files || []);
      return data.files || [];
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载 WDL 文件列表失败");
      return [];
    }
  }, []);

  // 首载：恢复上次打开的文件，否则打开列表第一个，空目录则新建一个
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const list = await reloadFiles();
      if (cancelled) return;
      const last = localStorage.getItem(LAST_FILE_KEY);
      const pick =
        (last && list.some((f) => f.name === last) && last) ||
        list[0]?.name ||
        null;
      if (pick) {
        setFileName(pick);
        return;
      }
      try {
        const created = await createWdlFile("untitled");
        if (cancelled) return;
        await reloadFiles();
        setFileName(created.name);
      } catch (err) {
        if (!cancelled) setLoadError(String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadFiles]);

  // 订阅引擎事件 → 节点实时状态表（按实例归档，编辑器按 runInstanceId 消费）
  useEffect(() => {
    const ws = getWS();
    return ws.onMessage((msg) => {
      const instanceId = msg.instance_id;
      if (!instanceId) return;
      const stepId = String(msg.payload?.step_id || "");
      if (msg.type === "node_started" && stepId) {
        mergeRunNode(instanceId, { id: stepId, status: "running", task: "", result: "" });
      } else if (msg.type === "node_completed" && stepId) {
        mergeRunNode(instanceId, { id: stepId, status: "done", task: "", result: "" });
      } else if (msg.type === "node_failed" && stepId) {
        mergeRunNode(instanceId, {
          id: stepId,
          status: "failed",
          task: "",
          result: "",
          error: String(msg.payload?.error || ""),
        });
      } else if (
        msg.type === "workflow_started" ||
        msg.type === "workflow_completed" ||
        msg.type === "workflow_failed" ||
        msg.type === "workflow_cancelled"
      ) {
        const status =
          msg.type === "workflow_started"
            ? "running"
            : msg.type === "workflow_failed"
              ? "failed"
              : msg.type === "workflow_cancelled"
                ? "cancelled"
                : "completed";
        setRunStatus(instanceId, status);
        if (instanceId === runInstanceId) void reloadFiles();
      }
    });
  }, [mergeRunNode, setRunStatus, runInstanceId, reloadFiles]);

  const selectFile = (name: string) => {
    setFileName(name);
    localStorage.setItem(LAST_FILE_KEY, name);
  };

  const createNew = async () => {
    try {
      const created = await createWdlFile(`flow-${Date.now().toString(36).slice(-4)}`);
      await reloadFiles();
      selectFile(created.name);
      message.success("已新建工作流");
    } catch (err) {
      message.error(String(err));
    }
  };

  const confirmDelete = (name: string) => {
    Modal.confirm({
      title: "删除工作流",
      content: `确定删除「${name}」？该操作不可撤销。`,
      okText: "删除",
      okButtonProps: { danger: true },
      onOk: async () => {
        await deleteWorkflowDraft(name);
        message.success("已删除");
        const list = await reloadFiles();
        if (fileName === name) {
          const next = list[0]?.name ?? null;
          if (next) {
            selectFile(next);
          } else {
            const created = await createWdlFile("untitled");
            await reloadFiles();
            selectFile(created.name);
          }
        }
      },
    });
  };

  const liveNodes: Record<string, FlowLiveNode> | null =
    runInstanceId && runLiveNodes[runInstanceId]
      ? runLiveNodes[runInstanceId].nodes
      : null;
  const activeLabel = fileName?.replace(/\.wdl$/, "") || "工作流";

  return (
    <div className="flow-workbench">
      <header className="flow-workbench-bar">
        <div style={{ display: "flex", alignItems: "center", gap: 4, flex: 1, minWidth: 0 }}>
          <Text strong style={{ fontSize: 14 }}>
            WDL 工作台
          </Text>
        </div>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", minWidth: 0 }}>
          <Text
            style={{
              fontWeight: 600,
              maxWidth: 240,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {activeLabel}
          </Text>
        </div>
        <div style={{ flex: 1 }} />
      </header>

      <div className="flow-workbench-body">
        <aside className={`flow-workbench-drafts${listCollapsed ? " collapsed" : ""}`}>
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
              onClick={() => setListCollapsed(true)}
            />
          </div>
          {!listCollapsed && (
            <div className="flow-workbench-drafts-list">
              {files.map((f) => (
                <div
                  key={f.name}
                  className={`flow-workbench-draft-item${f.name === fileName ? " active" : ""}`}
                  onClick={() => selectFile(f.name)}
                >
                  <span className="flow-workbench-draft-name" title={f.name}>
                    {f.name.replace(/\.wdl$/, "")}
                  </span>
                  <DeleteOutlined
                    className="flow-workbench-draft-del"
                    onClick={(e) => {
                      e.stopPropagation();
                      confirmDelete(f.name);
                    }}
                  />
                </div>
              ))}
            </div>
          )}
          {listCollapsed && (
            <button
              className="flow-workbench-drafts-expand"
              onClick={() => setListCollapsed(false)}
              title="展开工作流列表"
            >
              <MenuUnfoldOutlined />
            </button>
          )}
        </aside>
        <main className="flow-workbench-canvas flow-workbench-editor">
          {loadError ? (
            <div className="flow-live-empty">
              <span style={{ color: "#cf1322" }}>无法打开：{loadError}</span>
            </div>
          ) : !fileName ? (
            <div className="flow-live-empty">
              <Spin tip="准备编辑器…" />
            </div>
          ) : (
            <WorkflowEditor
              draftId={fileName}
              liveNodes={liveNodes}
              onRunInstance={setRunInstanceId}
            />
          )}
        </main>
      </div>
    </div>
  );
}
