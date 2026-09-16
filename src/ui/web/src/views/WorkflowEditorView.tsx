// WorkflowEditorView — route page for /workflow/editor/:draftId.
//
// This is the entry the WS navigation message (open_workflow_editor) lands on
// when a skill or save_draft asks the browser to present the editor. The
// actual React Flow canvas lives in features/workflow/editor/WorkflowEditor;
// this view just wires route params + draft loading (for the title) + layout.

import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Button, Spin, Tooltip, Typography } from "antd";
import { ArrowLeftOutlined } from "@ant-design/icons";
import { fetchWorkflowDraft, reportActiveWorkflowDraft, type WorkflowDraft } from "../lib/api";
import { PageShell } from "../components/layout/PageShell";
import { PageHeader } from "../components/layout/PageHeader";
import { WorkflowEditor } from "../features/workflow/editor/WorkflowEditor";
import "../features/workflow/editor/editor.css";

export default function WorkflowEditorView() {
  const { draftId } = useParams<{ draftId: string }>();
  const navigate = useNavigate();
  const [draft, setDraft] = useState<WorkflowDraft | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // 上报当前打开的草案：构建对话编排写穿会复用这份草案，而不是新开一份
    void reportActiveWorkflowDraft(draftId ?? null);
  }, [draftId]);

  useEffect(() => {
    let cancelled = false;
    if (!draftId) {
      setError("缺少 draftId");
      setLoading(false);
      return;
    }
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const d = await fetchWorkflowDraft(draftId);
        if (!cancelled) setDraft(d);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [draftId]);

  if (loading) {
    return (
      <div style={{ padding: 24, textAlign: "center" }}>
        <Spin tip="加载工作流草案…" />
      </div>
    );
  }

  if (error || !draftId) {
    return (
      <div style={{ padding: 24 }}>
        <Typography.Paragraph type="danger">
          加载失败：{error || "缺少 draftId"}
        </Typography.Paragraph>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate("/workflow")}>
          返回列表
        </Button>
      </div>
    );
  }

  return (
    <PageShell
      scroll="hidden"
      padded={false}
      header={
        <PageHeader
          title={draft?.name || "未命名工作流"}
          meta={
            <Tooltip title={`草案 ID: ${draftId}`}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                工作流草案
              </Typography.Text>
            </Tooltip>
          }
          onBack={() => navigate("/workflow")}
        />
      }
    >
      <WorkflowEditor draftId={draftId} />
    </PageShell>
  );
}
