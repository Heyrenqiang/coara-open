// WDL 工作台 API 层 — 对接 `wdl serve` 内嵌服务（wdl/server.py）。
//
// 数据源迁移口径：
// - coara 的 workflow-drafts（draft_store）→ 本服务的 /api/files（WDL 文件即草案）
// - /api/workflow-wdl/{parse,emit} → /api/wdl/{parse,emit}（同 core.serde）
// - /api/workflow-instances/* → /api/instances/*（run 改为 POST /api/instances/run，
//   传 wdl 文本或 file 文件名，不再走 draft_id）

const API_BASE = "";

async function _extractError(res: Response, fallback: string): Promise<Error> {
  try {
    const body = await res.json();
    if (body?.error) return new Error(String(body.error));
  } catch {
    /* ignore JSON parse failures */
  }
  return new Error(fallback);
}

// ---- WDL 文件 CRUD ----

export interface WdlFileRow {
  name: string;
  size: number;
  updated_at: string;
}

export async function fetchWdlFiles(): Promise<{ files: WdlFileRow[]; root: string }> {
  const res = await fetch(`${API_BASE}/api/files`);
  if (!res.ok) throw await _extractError(res, `Failed to list files: ${res.statusText}`);
  return res.json();
}

export async function createWdlFile(name: string): Promise<{ name: string; wdl: string }> {
  const res = await fetch(`${API_BASE}/api/files`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) throw await _extractError(res, `Failed to create file: ${res.statusText}`);
  return res.json();
}

/** 编辑器视角的「草案」= 一个 WDL 文件；draft_id 沿用旧字段名以减少画布改动。 */
export interface WorkflowDraft {
  draft_id: string;
  name: string;
  wdl: string;
  updated_at?: string;
  [key: string]: unknown;
}

export async function fetchWorkflowDraft(name: string): Promise<WorkflowDraft> {
  const res = await fetch(`${API_BASE}/api/files/${encodeURIComponent(name)}`);
  if (!res.ok) throw await _extractError(res, `Failed to fetch file: ${res.statusText}`);
  const body = (await res.json()) as { name: string; wdl: string };
  return { draft_id: body.name, name: body.name.replace(/\.wdl$/, ""), wdl: body.wdl };
}

export async function saveWorkflowDraft(
  name: string,
  wdl: string,
  _baseUpdatedAt?: string,
): Promise<WorkflowDraft> {
  const res = await fetch(`${API_BASE}/api/files/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wdl }),
  });
  if (!res.ok) throw await _extractError(res, `Failed to save file: ${res.statusText}`);
  const body = (await res.json()) as { name: string; updated_at?: string };
  return {
    draft_id: body.name,
    name: body.name.replace(/\.wdl$/, ""),
    wdl,
    updated_at: body.updated_at,
  };
}

/** 文件级保存无共同编辑冲突通道；保留类型以兼容编辑器错误分支。 */
export class DraftConflictError extends Error {
  constructor(
    message: string,
    public readonly updatedAt: string,
  ) {
    super(message);
    this.name = "DraftConflictError";
  }
}

export async function deleteWorkflowDraft(name: string): Promise<{ deleted: boolean }> {
  const res = await fetch(`${API_BASE}/api/files/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw await _extractError(res, `Failed to delete file: ${res.statusText}`);
  return res.json();
}

// ---- WDL parse / emit ----

/** 内核图（kernel projection）节点：唯一节点类型是智能体。
 *  与后端 /api/wdl/parse 返回的 document.nodes[id] 对齐（core/model.py Node）。 */
export interface KernelNodeSpec {
  task: string;
  input: string;
  routes: "all" | "one";
  /** 节点级激活上限（覆写全局）；缺省 = 继承全局 */
  max_activations?: number;
  /** 节点级 LLM 覆盖；缺省 = 跟随图级/providers.yaml 默认 */
  provider?: string;
  model?: string;
}

/** 内核图边：from → to；on 缺省 success，error 表示失败时投递（重试/兜底路由）。 */
export interface KernelEdgeSpec {
  from: string;
  to: string;
  on?: "success" | "error";
}

/** 内核图文档：POST /api/wdl/parse 的 document 与 /emit 的入参同构。 */
export interface KernelDocument {
  name?: string;
  description?: string;
  schedule?: Record<string, unknown>;
  /** 全局激活上限（缺省 100） */
  max_activations?: number;
  nodes: Record<string, KernelNodeSpec>;
  edges: KernelEdgeSpec[];
}

export interface WdlParseResult {
  document: KernelDocument;
  warnings?: string[];
}

export async function parseWdl(wdl: string): Promise<WdlParseResult> {
  const res = await fetch(`${API_BASE}/api/wdl/parse`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wdl }),
  });
  if (!res.ok) throw await _extractError(res, `Failed to parse WDL: ${res.statusText}`);
  return res.json();
}

export interface WdlEmitResult {
  wdl: string;
}

export async function emitWdl(document: KernelDocument): Promise<WdlEmitResult> {
  const res = await fetch(`${API_BASE}/api/wdl/emit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ document }),
  });
  if (!res.ok) throw await _extractError(res, `Failed to emit WDL: ${res.statusText}`);
  return res.json();
}

// ---- 实例 ----

/** Progress payload embedded in the run view (derived server-side). */
export interface WorkflowRunProgress {
  completed: number;
  total: number;
  percentage: number;
}

/** Instance summary row returned by GET /api/instances.
 *  Mirrors WorkflowPersistence.list_instances() (datetime → ISO strings). */
export interface WorkflowInstanceSummary {
  instance_id: string;
  name: string;
  status: string;
  current_step_id?: string | null;
  error?: string | null;
  wait_event?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export async function listWorkflowInstances(
  status?: string,
): Promise<{ instances: WorkflowInstanceSummary[] }> {
  const url =
    status && status !== "all"
      ? `${API_BASE}/api/instances?status=${encodeURIComponent(status)}`
      : `${API_BASE}/api/instances`;
  const res = await fetch(url);
  if (!res.ok) throw await _extractError(res, `Failed to list instances: ${res.statusText}`);
  return res.json();
}

export interface WorkflowResultDoc {
  instance_id?: string;
  name?: string;
  status?: string;
  final?: { summary?: string; outputs?: Record<string, unknown> };
  nodes?: Array<{ step_id: string; status?: string; output?: unknown; error?: string }>;
  error?: string | null;
}

/** Live run view returned by GET /api/instances/{id}.
 *  Mirrors WorkflowPersistence.get_instance_run_view(). */
export interface WorkflowRunView {
  instance_id: string;
  status: string;
  current_step_id?: string | null;
  completed_steps?: string[];
  error?: string | null;
  wait_event: string | null;
  updated_at?: string;
  progress?: WorkflowRunProgress;
  result?: WorkflowResultDoc | null;
  [key: string]: unknown;
}

export async function getWorkflowInstance(instanceId: string): Promise<WorkflowRunView> {
  const res = await fetch(`${API_BASE}/api/instances/${encodeURIComponent(instanceId)}`);
  if (!res.ok) throw await _extractError(res, `Failed to fetch instance: ${res.statusText}`);
  return res.json();
}

export interface WorkflowRunResult {
  instance_id: string;
  status: string;
}

/** 按 WDL 文件运行：服务端读文件 → 引擎执行。 */
export async function runWorkflowInstance(
  file: string,
  inputs?: Record<string, unknown>,
): Promise<WorkflowRunResult> {
  const res = await fetch(`${API_BASE}/api/instances/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file, inputs: inputs ?? {} }),
  });
  if (!res.ok) throw await _extractError(res, `Failed to run workflow: ${res.statusText}`);
  return res.json();
}

export interface WorkflowCancelResult {
  cancelled: boolean;
  instance_id: string;
}

export async function cancelWorkflowInstance(
  instanceId: string,
): Promise<WorkflowCancelResult> {
  const res = await fetch(
    `${API_BASE}/api/instances/${encodeURIComponent(instanceId)}/cancel`,
    { method: "POST" },
  );
  if (!res.ok) throw await _extractError(res, `Failed to cancel workflow: ${res.statusText}`);
  return res.json();
}

export interface WorkflowResumeResult {
  resumed: boolean;
  instance_id?: string;
  [key: string]: unknown;
}

/** Resume an interrupted workflow instance (409 = not resumable). */
export async function resumeWorkflowInstance(
  instanceId: string,
): Promise<WorkflowResumeResult> {
  const res = await fetch(
    `${API_BASE}/api/instances/${encodeURIComponent(instanceId)}/resume`,
    { method: "POST" },
  );
  if (!res.ok) throw await _extractError(res, `Failed to resume workflow: ${res.statusText}`);
  return res.json();
}
