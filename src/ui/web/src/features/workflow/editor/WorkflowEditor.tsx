/**
 * WorkflowEditor — main workflow visual editor component (kernel graph).
 *
 * 2026-08-16 内核化：草案文本是内核投影 YAML（nodes + edges）。加载链路
 * draft.wdl → parseWdl → KernelDocument → documentToReactFlow 渲染；编辑后
 * reactFlowToDocument → emitWdl 得 canonical 文本 → AutosaveScheduler 落盘
 * 并与 WDL 文本框双向同步。布局是拓扑的纯函数（同图必同布局），节点拖动
 * 为会话内排版，不持久化。
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from 'react';
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  MarkerType,
  type Edge,
  type Connection,
  type ReactFlowInstance,
  type OnEdgesDelete,
  type OnNodesDelete,
  type IsValidConnection,
  type NodeMouseHandler,
} from '@xyflow/react';
import { Button, ConfigProvider, Modal, Space, Spin, Tag, Tooltip } from 'antd';
import {
  AppstoreOutlined,
  AuditOutlined,
  CloseOutlined,
  FileTextOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  QuestionCircleOutlined,
  SettingOutlined,
  ThunderboltOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons';
import {
  documentToReactFlow,
  reactFlowToDocument,
  kernelNodeData,
  taskSummary,
  validateDocumentConsistency,
  wdlDocumentToFlowEdges,
  kernelEdgeId,
  edgeIdOccupied,
} from './wdl-graph-adapter';
import { AutosaveScheduler } from './draft-autosave';
import {
  applyBindingsToNodes,
  buildDataEdge,
  buildExecEdge,
  stripTemplateFromInput,
  templateFromSourceHandle,
} from './binding-sync';
import type { FlowLiveNode } from '../../../lib/ws';
import { useStore } from '../../../lib/store';
import { FlowNodeStatusPanel } from '../FlowNodeStatusPanel';
import {
  isDataSourceHandle,
  isDataTargetHandle,
  isExecSourceHandle,
  isExecTargetHandle,
} from './node-port-schema';
import { CanvasNode, NodeCallbacksContext, type FlowNode } from './CanvasNode';
import { RoutableEdge } from './edges/RoutableEdge';
import { EdgeRouteProvider } from './edges/EdgeRouteProvider';
import { EdgeEditProvider } from './edges/EdgeEditContext';
import type { Waypoint } from './edges/edge-types';
import { NodeConfigPanel } from './NodeConfigPanel';
import { Palette, type PaletteProps } from './Palette';
import { injectTheme, token } from './theme';
import type { ScheduleValue } from './SchedulePanel';
import {
  fetchWorkflowDraft,
  saveWorkflowDraft,
  DraftConflictError,
  parseWdl,
  emitWdl,
  type KernelDocument,
} from '../../../lib/api';
import type { ReactFlowNode, ReactFlowEdge, NodeData } from './types';

const LEFT_PANEL_MIN = 140;
const LEFT_PANEL_MAX = 360;
const RIGHT_PANEL_MIN = 260;
const RIGHT_PANEL_MAX = 720;

const nodeTypes = { custom: CanvasNode };
const edgeTypes = { routable: RoutableEdge };

/* 模块加载即注入 --wf-* / --xy-* CSS 变量（幂等）。 */
injectTheme();

/** 新建空草案：最小内核图（一个空智能体节点）。 */
function emptyDocument(): KernelDocument {
  return {
    name: '',
    description: '',
    schedule: { kind: 'manual' },
    nodes: { 节点1: { task: '', input: '', routes: 'all' } },
    edges: [],
  };
}

/** 转义正则特殊字符（删除节点/输入清理 {{steps.<id>}} 用）。 */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** 生成不冲突的智能体节点 id：节点1、节点2 … */
function nextNodeId(nodes: { id: string }[]): string {
  const ids = new Set(nodes.map((n) => n.id));
  let i = 1;
  while (ids.has(`节点${i}`)) i += 1;
  return `节点${i}`;
}

function isDataEdge(e: Edge | ReactFlowEdge): boolean {
  return isDataSourceHandle(e.sourceHandle) && isDataTargetHandle(e.targetHandle);
}

/* ──────────────────────────────────────────────────────────────────────
 * Layout helpers
 * ────────────────────────────────────────────────────────────────────── */

function startColDrag(onMove: (clientX: number) => void, onEnd?: () => void) {
  const move = (e: PointerEvent) => {
    e.preventDefault();
    onMove(e.clientX);
  };
  const up = () => {
    document.removeEventListener('pointermove', move);
    document.removeEventListener('pointerup', up);
    document.removeEventListener('pointercancel', up);
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    onEnd?.();
  };
  document.body.style.cursor = 'col-resize';
  document.body.style.userSelect = 'none';
  document.addEventListener('pointermove', move);
  document.addEventListener('pointerup', up);
  document.addEventListener('pointercancel', up);
}

/** 右侧面板左边缘 / 左侧面板右边缘的 edge-resize */
function PanelEdgeResize({
  side,
  onPointerDown,
}: {
  side: 'w' | 'e';
  onPointerDown: (e: React.PointerEvent) => void;
}) {
  return (
    <div
      className={`panel-edge-resize panel-edge-resize-${side}`}
      title="拖动边缘调整宽度"
      role="separator"
      aria-orientation="vertical"
      onPointerDown={(e) => {
        e.preventDefault();
        e.stopPropagation();
        (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
        onPointerDown(e);
      }}
    />
  );
}

function beginRightPanelResize(
  e: React.PointerEvent,
  width: number,
  setWidth: (w: number) => void,
) {
  const startX = e.clientX;
  const startW = width;
  startColDrag((clientX) => {
    const next = Math.max(
      RIGHT_PANEL_MIN,
      Math.min(RIGHT_PANEL_MAX, startW - (clientX - startX)),
    );
    setWidth(next);
  });
}

function beginLeftPanelResize(
  e: React.PointerEvent,
  width: number,
  setWidth: (w: number) => void,
) {
  const startX = e.clientX;
  const startW = width;
  startColDrag((clientX) => {
    const next = Math.max(
      LEFT_PANEL_MIN,
      Math.min(LEFT_PANEL_MAX, startW + (clientX - startX)),
    );
    setWidth(next);
  });
}

/* ──────────────────────────────────────────────────────────────────────
 * WorkflowEditor
 * ────────────────────────────────────────────────────────────────────── */

interface WorkflowEditorProps {
  draftId: string;
  /**
   * FlowRoot 实时图的 canonical WDL。变化时灌入画布（与构建对话同源），
   * 并走既有 autosave 落盘到当前草案。
   */
  liveWdl?: string | null;
  /** FlowRoot 会话内节点状态（画布徽章 + 运转侧栏）。 */
  liveNodes?: Record<string, FlowLiveNode> | null;
  /** Flow 推进 hop 计数。 */
  liveHops?: number;
}

function mapLiveStatusToBadge(status: string | undefined): string {
  if (status === 'done') return 'success';
  if (status === 'failed') return 'failed';
  if (status === 'running') return 'running';
  if (status === 'pending') return 'pending';
  return status || 'pending';
}

export function WorkflowEditor({
  draftId,
  liveWdl = null,
  liveNodes = null,
  liveHops = 0,
}: WorkflowEditorProps) {
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');

  const [wdlDocument, setWdlDocument] = useState<KernelDocument>(() => emptyDocument());
  const wdlDocumentRef = useRef(wdlDocument);
  useEffect(() => {
    wdlDocumentRef.current = wdlDocument;
  }, [wdlDocument]);

  const initialFlow = useMemo(() => documentToReactFlow(emptyDocument()), []);
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>(initialFlow.nodes as FlowNode[]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(initialFlow.edges as Edge[]);
  const [selectedNode, setSelectedNode] = useState<FlowNode | null>(null);
  const selectedNodeRef = useRef<FlowNode | null>(selectedNode);
  useEffect(() => {
    selectedNodeRef.current = selectedNode;
  }, [selectedNode]);
  /** 选中的边（on=error 切换面板用）；按 id 记录，边重建后仍可寻回 */
  const [selectedEdgeId, setSelectedEdgeId] = useState('');
  const selectedEdgeIdRef = useRef(selectedEdgeId);
  useEffect(() => {
    selectedEdgeIdRef.current = selectedEdgeId;
  }, [selectedEdgeId]);

  const reactFlowWrapper = useRef<HTMLDivElement | null>(null);
  const [reactFlowInstance, setReactFlowInstance] =
    useState<ReactFlowInstance<FlowNode, Edge> | null>(null);
  const layoutTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wdlTextRef = useRef<HTMLTextAreaElement | null>(null);
  const wdlParseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** 文本↔画布双向同步的自增序号：切草稿或新编辑时递增，陈旧响应落地前校验丢弃 */
  const syncEpochRef = useRef(0);
  const [wdlText, setWdlText] = useState('');
  const [wdlError, setWdlError] = useState('');
  /** parseWdl 返回的非致命提示（WDL 面板展示） */
  const [wdlWarnings, setWdlWarnings] = useState<string[]>([]);
  /** 连线 on 切换被拒绝时的提示（目标边 id 已占用） */
  const [edgeOnError, setEdgeOnError] = useState('');

  const nodesRef = useRef(nodes);
  useEffect(() => {
    nodesRef.current = nodes;
  }, [nodes]);
  const edgesRef = useRef(edges);
  useEffect(() => {
    edgesRef.current = edges;
  }, [edges]);

  const [paletteOpen, setPaletteOpen] = useState(true);
  const [paletteWidth, setPaletteWidth] = useState(200);
  const [configOpen, setConfigOpen] = useState(false);
  const [wdlOpen, setWdlOpen] = useState(true);
  const [helpOpen, setHelpOpen] = useState(false);
  const [rightPanelWidth, setRightPanelWidth] = useState(320);

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [inspectorOpen, setInspectorOpen] = useState(false);

  /* ---- 全屏切换：Fullscreen API 占满整个物理屏幕（非扩大 HTML 区域） ---- */
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const onFullscreenChange = () => {
      setIsFullscreen(Boolean(document.fullscreenElement));
      // 全屏切换后容器尺寸突变，延迟一帧触发 fitView 让画布重新适配
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        reactFlowInstance?.fitView({ padding: 0.25, duration: 200 });
      }, 50);
    };
    document.addEventListener('fullscreenchange', onFullscreenChange);
    return () => {
      document.removeEventListener('fullscreenchange', onFullscreenChange);
      if (timer) clearTimeout(timer);
    };
  }, [reactFlowInstance]);

  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) {
      void document.exitFullscreen();
    } else {
      void containerRef.current?.requestFullscreen();
    }
  }, []);

  /* ---- 卸载时清理所有定时器，避免泄漏 ---- */
  useEffect(() => {
    return () => {
      if (layoutTimerRef.current) clearTimeout(layoutTimerRef.current);
      if (wdlParseTimerRef.current) clearTimeout(wdlParseTimerRef.current);
    };
  }, []);

  /* ---- 自动保存调度器：dirty 比对 + 保存串行化（台账 台账216），每个草稿一个实例 ---- */
  const autosaveRef = useRef<AutosaveScheduler<KernelDocument> | null>(null);
  /** 读取/保存基线的草案版本号：共同编辑冲突检测（409）用 */
  const savedUpdatedAtRef = useRef<string>('');

  useEffect(() => {
    // 闭包捕获本实例的 draftId：切换草稿后旧实例的在途保存仍写回旧草稿，不会串稿
    const scheduler = new AutosaveScheduler<KernelDocument>({
      save: async (doc) => {
        const emit = await emitWdl(doc);
        // 切换草稿后旧调度器的迟到回调只完成网络请求，不碰新草稿的 UI state（台账 台账341）；
        // 用户正在编辑 WDL 文本框时也不要覆盖其输入
        if (
          autosaveRef.current === scheduler &&
          document.activeElement !== wdlTextRef.current
        ) {
          setWdlText(emit.wdl);
        }
        try {
          const resp = await saveWorkflowDraft(
            draftId,
            emit.wdl,
            savedUpdatedAtRef.current || undefined,
          );
          savedUpdatedAtRef.current = resp.updated_at || savedUpdatedAtRef.current;
        } catch (e) {
          // 共同编辑冲突：会话侧已推进草案——刷新画布到新基线，
          // 本窗口未落盘的本地修改让位（不静默覆盖会话成果）
          if (e instanceof DraftConflictError && autosaveRef.current === scheduler) {
            setSaveError('会话侧已更新，画布已刷新为最新版本');
            void loadDraft();
          }
          throw e;
        }
      },
      onSavingChange: (next) => {
        if (autosaveRef.current === scheduler) setSaving(next);
      },
      onError: (message) => {
        if (autosaveRef.current === scheduler) setSaveError(message || '');
      },
    });
    autosaveRef.current = scheduler;
    setSaving(false);
    setSaveError('');
    return () => {
      scheduler.dispose();
      if (autosaveRef.current === scheduler) autosaveRef.current = null;
    };
  }, [draftId]);

  /* ---- Load draft + parse WDL on mount / draftId change / manual retry ---- */
  const loadDraft = useCallback(
    async (isStale: () => boolean = () => false) => {
      // 清理上一草稿遗留的防抖定时器，并递增 epoch 使上一草稿在途的
      // setWdlTextFromDocument / syncCanvasFromWdlText 响应落地时被丢弃（防串稿）
      if (wdlParseTimerRef.current) {
        clearTimeout(wdlParseTimerRef.current);
        wdlParseTimerRef.current = null;
      }
      syncEpochRef.current += 1;
      setLoading(true);
      setLoadError('');
      try {
        const draft = await fetchWorkflowDraft(draftId);
        if (isStale()) return;
        savedUpdatedAtRef.current = draft.updated_at || '';
        const wdl = draft.wdl || '';
        let doc: KernelDocument;
        let warnings: string[] = [];
        if (wdl.trim()) {
          const parsed = await parseWdl(wdl);
          if (isStale()) return;
          doc = parsed.document;
          warnings = parsed.warnings || [];
        } else {
          doc = { ...emptyDocument(), name: draft.name || '' };
        }
        if (isStale()) return;
        setWdlWarnings(warnings);
        // 登记保存基线：打开草稿本身不触发自动保存
        autosaveRef.current?.markSaved(doc);
        wdlDocumentRef.current = doc;
        setWdlDocument(doc);
        const flow = documentToReactFlow(doc);
        setNodes(flow.nodes as FlowNode[]);
        setEdges(flow.edges as Edge[]);
        setWdlText(wdl);
        setWdlError('');
      } catch (e) {
        if (!isStale()) {
          setLoadError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        if (!isStale()) setLoading(false);
      }
    },
    [draftId, setNodes, setEdges],
  );

  useEffect(() => {
    let cancelled = false;
    loadDraft(() => cancelled);
    return () => {
      cancelled = true;
    };
  }, [loadDraft]);

  /* ---- 编排写穿：会话侧改图 → workflow_draft_updated → revision 刷新 ----
   * 本地有未落盘编辑时让路（autosave 落盘后下一次推送再校准），不盖用户输入 */
  const draftRevision = useStore((s) => s.workflowDraftRevisions[draftId] ?? 0);
  const lastRevisionRef = useRef(draftRevision);
  useEffect(() => {
    if (draftRevision === lastRevisionRef.current) return;
    lastRevisionRef.current = draftRevision;
    if (loading) return;
    if (autosaveRef.current?.hasPending()) return;
    void loadDraft();
  }, [draftRevision, loading, loadDraft]);

  /* ---- FlowRoot live WDL → 同一块编辑画布 ---- */
  const lastLiveWdlRef = useRef<string | null>(null);
  useEffect(() => {
    lastLiveWdlRef.current = null;
  }, [draftId]);

  useEffect(() => {
    if (loading) return;
    const text = typeof liveWdl === 'string' ? liveWdl.trim() : '';
    if (!text || text === lastLiveWdlRef.current) return;
    lastLiveWdlRef.current = text;
    let cancelled = false;
    const epoch = ++syncEpochRef.current;
    (async () => {
      try {
        const parsed = await parseWdl(text);
        if (cancelled || epoch !== syncEpochRef.current) return;
        const doc = parsed.document;
        setWdlWarnings(parsed.warnings || []);
        wdlDocumentRef.current = doc;
        setWdlDocument(doc);
        const flow = documentToReactFlow(doc);
        setNodes(flow.nodes as FlowNode[]);
        setEdges(flow.edges as Edge[]);
        if (document.activeElement !== wdlTextRef.current) {
          setWdlText(text);
        }
        setWdlError('');
      } catch (e) {
        if (!cancelled) {
          setWdlError(e instanceof Error ? e.message : String(e));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [liveWdl, loading, setNodes, setEdges]);

  /* ---- Auto-save draft when wdlDocument changes ----
   * dirty 比对 / 打开不触发 / 串行化全部收敛在 AutosaveScheduler 内 */
  useEffect(() => {
    if (loading) return;
    autosaveRef.current?.request(wdlDocument);
  }, [wdlDocument, loading]);

  /* ---- FlowRoot live node status → canvas badges ---- */
  useEffect(() => {
    if (!liveNodes || Object.keys(liveNodes).length === 0) return;
    setNodes((nds) =>
      nds.map((n) => {
        const stepId = String(n.data?.stepId || n.id);
        const live = liveNodes[stepId];
        if (!live) return n;
        const status = mapLiveStatusToBadge(live.status);
        const error = live.error || (live.status === 'failed' ? live.result : undefined);
        if (n.data?.status === status && n.data?.error === error) return n;
        return { ...n, data: { ...n.data, status, error } };
      }),
    );
  }, [liveNodes, setNodes]);

  const liveProgress = useMemo(() => {
    if (!liveNodes) return null;
    const vals = Object.values(liveNodes);
    if (vals.length === 0) return null;
    let pending = 0;
    let running = 0;
    let done = 0;
    let failed = 0;
    for (const n of vals) {
      if (n.status === 'running') running += 1;
      else if (n.status === 'done') done += 1;
      else if (n.status === 'failed') failed += 1;
      else pending += 1;
    }
    return { pending, running, done, failed, total: vals.length, hops: liveHops };
  }, [liveNodes, liveHops]);

  function updateDocument(next: KernelDocument) {
    wdlDocumentRef.current = next;
    setWdlDocument(next);
  }

  function setWdlTextFromDocument(doc: KernelDocument) {
    if (wdlParseTimerRef.current) clearTimeout(wdlParseTimerRef.current);
    // 画布编辑使在途的文本解析（syncCanvasFromWdlText）失效，避免旧响应回退画布
    syncEpochRef.current += 1;
    const epoch = syncEpochRef.current;
    wdlParseTimerRef.current = setTimeout(async () => {
      // 用户正在编辑 WDL 文本框时不要覆盖其输入
      if (document.activeElement === wdlTextRef.current) return;
      try {
        const emit = await emitWdl(doc);
        // 期间有更新（切草稿 / 新编辑），丢弃陈旧结果
        if (epoch !== syncEpochRef.current) return;
        setWdlText(emit.wdl);
        const issues = validateDocumentConsistency(doc);
        setWdlError(issues.length ? issues.join('；') : '');
        setWdlWarnings([]);
      } catch (err) {
        if (epoch !== syncEpochRef.current) return;
        setWdlError(err instanceof Error ? err.message : String(err));
      }
    }, 100);
  }

  /** 从文档重建画布边；保留旧边上会话内的拖拽拐点（按边 id 匹配）。 */
  const rebuildEdgesFromDoc = useCallback(
    (doc: KernelDocument, prevEdges: Edge[] | null): Edge[] => {
      const rebuilt = wdlDocumentToFlowEdges(doc) as Edge[];
      if (!prevEdges?.length) return rebuilt;
      const wps = new Map<string, Waypoint[]>();
      for (const e of prevEdges) {
        const w = (e.data as { waypoints?: Waypoint[] } | undefined)?.waypoints;
        if (Array.isArray(w) && w.length) wps.set(e.id, w);
      }
      if (!wps.size) return rebuilt;
      return rebuilt.map((e) =>
        wps.has(e.id)
          ? { ...e, data: { ...(e.data || {}), waypoints: wps.get(e.id) } }
          : e,
      );
    },
    [],
  );

  async function syncCanvasFromWdlText(text: string) {
    // 捕获调用时的序号：onWdlTextChange 已递增；切草稿 / 画布编辑也会递增，
    // 响应落地前校验序号仍最新，陈旧响应直接丢弃（防乱序回退）
    const epoch = syncEpochRef.current;
    try {
      const result = await parseWdl(text);
      if (epoch !== syncEpochRef.current) return;
      const doc = result.document;
      setWdlDocument(doc);
      wdlDocumentRef.current = doc;
      const { nodes: nextNodes } = documentToReactFlow(doc);
      // 保留用户本会话内拖出的节点位置
      const currentPositions = new Map(nodesRef.current.map((n) => [n.id, n.position]));
      const mergedNodes = (nextNodes as FlowNode[]).map((n) =>
        currentPositions.has(n.id) ? { ...n, position: currentPositions.get(n.id)! } : n,
      );
      setNodes(mergedNodes);
      setEdges(rebuildEdgesFromDoc(doc, edgesRef.current));
      setWdlError('');
      setWdlWarnings(result.warnings || []);
    } catch (err) {
      if (epoch !== syncEpochRef.current) return;
      setWdlError(err instanceof Error ? err.message : String(err));
      setWdlWarnings([]);
    }
  }

  function onWdlTextChange(text: string) {
    setWdlText(text);
    // 新输入递增序号：在途的 setWdlTextFromDocument / syncCanvasFromWdlText
    // 陈旧响应落地时被丢弃，保证最新输入不被旧结果覆盖
    syncEpochRef.current += 1;
    if (wdlParseTimerRef.current) clearTimeout(wdlParseTimerRef.current);
    wdlParseTimerRef.current = setTimeout(() => syncCanvasFromWdlText(text), 600);
  }

  /** 画布 → 文档：绑定物化 → 文档重建 → 边重建 → 文本同步。 */
  function pushToDocument(nds: FlowNode[], eds: Edge[]) {
    const boundNodes = applyBindingsToNodes(
      nds as unknown as ReactFlowNode[],
      eds as unknown as ReactFlowEdge[],
    ) as unknown as FlowNode[];
    const next = reactFlowToDocument(
      wdlDocumentRef.current,
      boundNodes as unknown as ReactFlowNode[],
      eds as unknown as ReactFlowEdge[],
    );
    updateDocument(next);
    setNodes(boundNodes);
    setEdges(rebuildEdgesFromDoc(next, eds));
    setWdlTextFromDocument(next);
  }

  /** 只重算文档（不动节点 data，如边 on 切换后）。 */
  const persistDocument = useCallback(() => {
    const next = reactFlowToDocument(
      wdlDocumentRef.current,
      nodesRef.current as unknown as ReactFlowNode[],
      edgesRef.current as unknown as ReactFlowEdge[],
    );
    updateDocument(next);
    setWdlTextFromDocument(next);
  }, []);

  /* ---- 边拐点：会话内交互，不写入文档（内核图无 ui 字段） ---- */
  const edgeEditApi = useMemo(
    () => ({
      updateWaypoints: (edgeId: string, waypoints: Waypoint[]) => {
        setEdges((prev) =>
          prev.map((e) =>
            e.id === edgeId ? { ...e, data: { ...(e.data || {}), waypoints } } : e,
          ),
        );
      },
      commitWaypoints: (edgeId: string, _waypoints: Waypoint[]) => {
        void edgeId;
        void _waypoints;
      },
    }),
    [setEdges],
  );

  /* ---- Connection validation ---- */
  const isValidConnection = useCallback<IsValidConnection<Edge>>(
    (connection) => {
      const sh = connection.sourceHandle || '';
      const th = connection.targetHandle || '';
      if (isDataSourceHandle(sh) && isDataTargetHandle(th)) {
        // 数据绑定：同一 (源, 出口, 目标) 不重复
        return !edgesRef.current.some(
          (e) =>
            e.source === connection.source &&
            e.sourceHandle === sh &&
            e.target === connection.target,
        );
      }
      if (isExecSourceHandle(sh) && isExecTargetHandle(th)) {
        // 拓扑边：默认 success；回边/自环合法（循环=回边，重试=error 边），
        // 仅拦截重复边。内核允许 (a,b,success) 与 (a,b,error) 并存。
        return !edgesRef.current.some(
          (e) =>
            isExecSourceHandle(e.sourceHandle) &&
            isExecTargetHandle(e.targetHandle) &&
            e.source === connection.source &&
            e.target === connection.target &&
            (e.data as { on?: string } | undefined)?.on !== 'error',
        );
      }
      return false;
    },
    [],
  );

  /* ---- onConnect: 拓扑边（默认 success）或数据绑定边 ---- */
  const onConnect = useCallback(
    (params: Connection) => {
      if (!isValidConnection(params)) return;
      const sh = params.sourceHandle || '';
      const th = params.targetHandle || '';
      const isData = isDataSourceHandle(sh) && isDataTargetHandle(th);
      const sourceNode = nodesRef.current.find((n) => n.id === params.source);
      const template =
        isData
          ? templateFromSourceHandle(sourceNode as unknown as ReactFlowNode, sh) ?? undefined
          : undefined;

      let edgePayload: Edge;
      if (isData) {
        edgePayload = buildDataEdge(
          {
            id: `dataflow_${params.source}_${params.target}_${Date.now()}`,
            source: params.source,
            target: params.target,
            sourceHandle: sh,
            targetHandle: th,
          },
          template,
        ) as Edge;
      } else {
        edgePayload = buildExecEdge({
          id: kernelEdgeId(params.source, params.target, 'success'),
          source: params.source,
          target: params.target,
          sourceHandle: 'exec-out',
          targetHandle: 'exec-in',
          data: { on: 'success' },
        }) as Edge;
      }
      const nextEdges = [...edgesRef.current, edgePayload];
      pushToDocument(nodesRef.current, nextEdges);
    },
    [isValidConnection],
  );

  /* ---- Drag new node from palette ---- */
  const onDragStart: PaletteProps['onDragStart'] = (event, nodeType) => {
    event.dataTransfer.setData('application/reactflow', JSON.stringify(nodeType));
    event.dataTransfer.effectAllowed = 'move';
  };

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      if (!reactFlowInstance) return;
      if (!event.dataTransfer.getData('application/reactflow')) return;
      const dropPos = reactFlowInstance.screenToFlowPosition({
        x: event.clientX,
        y: event.clientY,
      });
      const stepId = nextNodeId(nodesRef.current);
      const newNode: FlowNode = {
        id: stepId,
        type: 'custom',
        position: dropPos,
        data: kernelNodeData(stepId, { task: '', input: '', routes: 'all' }),
      };
      pushToDocument([...nodesRef.current, newNode], edgesRef.current);
    },
    [reactFlowInstance],
  );

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const onNodeClick = useCallback<NodeMouseHandler<FlowNode>>((_, node) => {
    setSelectedNode(node as FlowNode);
    setSelectedEdgeId('');
    setEdgeOnError('');
    setConfigOpen(true);
    setWdlOpen(false);
    setHelpOpen(false);
    // 单击 = 节点配置；会话状态面板只经「会话状态」按钮打开，不与配置面板争抢
    setInspectorOpen(false);
  }, []);
  const onPaneClick = useCallback(() => {
    setSelectedNode(null);
    setSelectedEdgeId('');
    setEdgeOnError('');
    setInspectorOpen(false);
  }, []);
  const onEdgeClick = useCallback((_: unknown, edge: Edge) => {
    setSelectedNode(null);
    setSelectedEdgeId(edge.id);
    setEdgeOnError('');
    setConfigOpen(true);
    setWdlOpen(false);
    setHelpOpen(false);
    setInspectorOpen(false);
  }, []);

  /* ---- Update selected node field ----
   * 副作用（setNodes / pushToDocument）必须放在 state updater 之外，
   * 否则 StrictMode 双调用会导致重复执行。 */
  const updateSelectedNode = useCallback(
    (field: string, value: unknown) => {
      const prevSelected = selectedNodeRef.current;
      if (!prevSelected) return;
      const applyField = (data: NodeData): NodeData => {
        const nextData: NodeData = { ...data, [field]: value };
        if (field === 'task') nextData.desc = taskSummary(String(value ?? ''));
        return nextData;
      };
      const next = nodesRef.current.map((n) =>
        n.id === prevSelected.id ? { ...n, data: applyField(n.data) } : n,
      );
      setNodes(next);
      setSelectedNode({ ...prevSelected, data: applyField(prevSelected.data) });
      pushToDocument(next, edgesRef.current);
    },
    [],
  );

  const updateDocumentSchedule = useCallback((schedule: ScheduleValue | null) => {
    const prev = wdlDocumentRef.current;
    const next: KernelDocument = { ...prev, schedule: schedule || { kind: 'manual' } };
    updateDocument(next);
    setWdlTextFromDocument(next);
  }, []);

  const updateDocumentMeta = useCallback(
    (patch: { description?: string; max_activations?: number | null }) => {
      const prev = wdlDocumentRef.current;
      const next: KernelDocument = { ...prev };
      if (patch.description !== undefined) next.description = patch.description;
      if (patch.max_activations !== undefined) {
        if (patch.max_activations == null) delete next.max_activations;
        else next.max_activations = patch.max_activations;
      }
      updateDocument(next);
      setWdlTextFromDocument(next);
    },
    [],
  );

  /** 选中的拓扑边切换 on=success|error（重试/兜底路由）。 */
  const updateEdgeOn = useCallback(
    (edgeId: string, on: 'success' | 'error') => {
      const target = edgesRef.current.find((e) => e.id === edgeId);
      if (!target || isDataEdge(target)) return;
      const newId = kernelEdgeId(target.source, target.target, on);
      // 目标 id 已被其它边占用（如 a→b success 与 error 并存时把 error 切回
      // success）：拒绝切换并提示，避免 React Flow 渲染歧义（review P2）
      if (edgeIdOccupied(edgesRef.current, newId, edgeId)) {
        setEdgeOnError(
          `已有 ${on === 'error' ? 'error' : 'success'} 边 ${target.source} → ${target.target}，请先删除该边再切换`,
        );
        return;
      }
      setEdgeOnError('');
      const rebuilt = buildExecEdge({
        id: newId,
        source: target.source,
        target: target.target,
        sourceHandle: 'exec-out',
        targetHandle: 'exec-in',
        data: { on },
      }) as Edge;
      const nextEdges = edgesRef.current.map((e) => (e.id === edgeId ? rebuilt : e));
      edgesRef.current = nextEdges;
      setEdges(nextEdges);
      setSelectedEdgeId(rebuilt.id);
      persistDocument();
    },
    [setEdges, persistDocument],
  );

  const autoLayout = useCallback(() => {
    if (layoutTimerRef.current) clearTimeout(layoutTimerRef.current);
    // 布局是拓扑的纯函数：重算分层并回到画布中心
    const { nodes: nextNodes } = documentToReactFlow(wdlDocumentRef.current);
    setNodes(nextNodes as FlowNode[]);
    setEdges(rebuildEdgesFromDoc(wdlDocumentRef.current, edgesRef.current));
    layoutTimerRef.current = setTimeout(() => {
      reactFlowInstance?.fitView({ padding: 0.3, duration: 300 });
    }, 50);
  }, [reactFlowInstance, rebuildEdgesFromDoc]);

  /* ---- Edge delete: 数据绑定边剥除对应模板 ---- */
  const onEdgesDelete = useCallback<OnEdgesDelete<Edge>>((deleted) => {
    const removedTemplates: { target: string; template: string | null }[] = [];
    for (const e of deleted) {
      if (isDataEdge(e)) {
        // 连线建的绑定带 data.template；从模板推导的边只有 data.ref
        const ref = e.data?.ref as string | undefined;
        removedTemplates.push({
          target: e.target,
          template:
            (e.data?.template as string | undefined) ||
            (ref ? `{{${ref}}}` : null) ||
            (e.label as string) ||
            null,
        });
      }
    }
    const remaining = edgesRef.current.filter(
      (e) => !deleted.some((d) => d.id === e.id),
    );
    const nextNodes = stripTemplateFromInput(
      nodesRef.current as unknown as ReactFlowNode[],
      removedTemplates,
    ) as unknown as FlowNode[];
    pushToDocument(nextNodes, remaining);
    if (deleted.some((d) => d.id === selectedEdgeIdRef.current)) {
      setSelectedEdgeId('');
    }
  }, []);

  /* ---- Node delete: scrub dangling {{steps.deletedId.*}} refs ---- */
  const onNodesDelete = useCallback<OnNodesDelete<FlowNode>>((deleted) => {
    const deletedIds = new Set(deleted.map((n) => n.id));
    const remainingNodes = nodesRef.current.filter((n) => !deletedIds.has(n.id));
    const remainingEdges = edgesRef.current.filter(
      (e) => !deletedIds.has(e.source) && !deletedIds.has(e.target),
    );
    const remainingNodesCleaned = remainingNodes.map((node) => {
      const input = String(node.data?.input ?? '');
      let nextInput = input;
      for (const id of deletedIds) {
        const re = new RegExp(`\\{\\{\\s*steps\\.${escapeRegExp(id)}(?:\\.[^}]*)?\\s*\\}\\}`, 'g');
        nextInput = nextInput.replace(re, '');
      }
      nextInput = nextInput.replace(/\s{2,}/g, ' ').trim();
      if (nextInput === input) return node;
      return { ...node, data: { ...node.data, input: nextInput } };
    });
    pushToDocument(remainingNodesCleaned, remainingEdges);
    const curSelected = selectedNodeRef.current;
    if (curSelected && deletedIds.has(curSelected.id)) {
      setSelectedNode(null);
      setConfigOpen(false);
    }
  }, []);

  const onFlowInit = useCallback(
    (instance: ReactFlowInstance<FlowNode, Edge>) => {
      setReactFlowInstance(instance);
      const fit = () => instance.fitView({ padding: 0.25, duration: 200 });
      requestAnimationFrame(() => requestAnimationFrame(fit));
    },
    [],
  );

  /* ---- onInfo / onDelete callbacks：经 context 提供给节点 ---- */
  const [sourcePreview, setSourcePreview] = useState<{
    nodeId: string;
    summary: string;
  } | null>(null);

  const handleNodeDelete = useCallback(
    (nodeId: string) => {
      const target = nodesRef.current.find((n) => n.id === nodeId);
      if (!target) return;
      onNodesDelete([target]);
    },
    [onNodesDelete],
  );

  const handleNodeInfo = useCallback((nodeId: string) => {
    const target = nodesRef.current.find((n) => n.id === nodeId);
    if (!target) return;
    const spec = {
      task: target.data?.task,
      input: target.data?.input,
      routes: target.data?.routes,
      ...(target.data?.max_activations
        ? { max_activations: target.data.max_activations }
        : {}),
      ...(target.data?.provider ? { provider: target.data.provider } : {}),
      ...(target.data?.model ? { model: target.data.model } : {}),
    };
    setSourcePreview({ nodeId, summary: JSON.stringify(spec, null, 2) });
  }, []);

  const nodeCallbacks = useMemo(
    () => ({
      onInfo: handleNodeInfo,
      onDelete: handleNodeDelete,
    }),
    [handleNodeInfo, handleNodeDelete],
  );

  /* ---- Loading / error gates ---- */
  if (loading) {
    return (
      <div style={{ padding: 24, textAlign: 'center' }}>
        <Spin tip="加载工作流编辑器…" />
      </div>
    );
  }

  if (loadError) {
    return (
      <div style={{ padding: 24 }}>
        <div style={{ color: 'var(--wf-color-error)', marginBottom: 8 }}>加载失败：{loadError}</div>
        <Button onClick={() => loadDraft()}>重试</Button>
      </div>
    );
  }

  const selectedEdge = selectedEdgeId ? edges.find((e) => e.id === selectedEdgeId) || null : null;
  const kernelEdgeCount = edges.filter(
    (e) => isExecSourceHandle(e.sourceHandle) && isExecTargetHandle(e.targetHandle),
  ).length;

  /* ---- Render ---- */
  const connectionLineStyle: CSSProperties = {
    stroke: 'var(--wf-color-text-tertiary)',
    strokeWidth: 1.5,
  };

  return (
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: token('color.brand') as string,
          borderRadius: 8,
        },
      }}
      /* 全屏时 body 不在全屏元素内，弹层必须挂到编辑器根容器内才可见 */
      getPopupContainer={() => containerRef.current ?? document.body}
    >
    <div
      ref={containerRef}
      className="workflow-editor-root"
      style={{ height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}
    >
      <div className="editor-inner-toolbar">
        <Space size={8} wrap>
          <Button
            type="default"
            size="small"
            icon={<ThunderboltOutlined />}
            onClick={autoLayout}
          >
            自动布局
          </Button>
          {/* 执行层已剥离为独立 wdl 软件：画布不提供运行/停止/恢复 */}
        </Space>
        <span className="spacer" />
        <Space size={8} wrap>
          {liveProgress && (
            <Tag color={liveProgress.running > 0 ? 'processing' : 'default'} style={{ marginInlineEnd: 0 }}>
              {`会话推进 hops ${liveProgress.hops} · 待${liveProgress.pending}/跑${liveProgress.running}/成${liveProgress.done}/败${liveProgress.failed}`}
            </Tag>
          )}
          {selectedNode && (
            <Button
              type="default"
              size="small"
              icon={<UnorderedListOutlined />}
              onClick={() => {
                setInspectorOpen(true);
                setConfigOpen(false);
                setWdlOpen(false);
                setHelpOpen(false);
              }}
            >
              会话状态
            </Button>
          )}
          {saving && <Tag style={{ marginInlineEnd: 0 }}>保存中…</Tag>}
          {saveError && (
            <Tooltip title={saveError}>
              <Tag color="error" className="toolbar-tag-ellipsis" style={{ marginInlineEnd: 0 }}>
                {`保存失败: ${saveError}`}
              </Tag>
            </Tooltip>
          )}
          <Tag style={{ marginInlineEnd: 0 }}>{`节点 ${nodes.length} · 连线 ${kernelEdgeCount}`}</Tag>
          <Button
            type="text"
            size="small"
            icon={isFullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
            onClick={toggleFullscreen}
          />
        </Space>
      </div>

      <div className="app-body" style={{ position: 'relative', flex: 1 }}>
        {/* Canvas */}
        <div className="canvas-area" ref={reactFlowWrapper}>
          <NodeCallbacksContext.Provider value={nodeCallbacks}>
          <ReactFlowProvider>
            <EdgeEditProvider value={edgeEditApi}>
            <EdgeRouteProvider>
            <ReactFlow<FlowNode, Edge>
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onEdgesDelete={onEdgesDelete}
              onNodesDelete={onNodesDelete}
              onInit={onFlowInit}
              onDrop={onDrop}
              onDragOver={onDragOver}
              onNodeClick={onNodeClick}
              onPaneClick={onPaneClick}
              onEdgeClick={onEdgeClick}
              nodeTypes={nodeTypes}
              edgeTypes={edgeTypes}
              isValidConnection={isValidConnection}
              deleteKeyCode={['Backspace', 'Delete']}
              elevateEdgesOnSelect
              defaultEdgeOptions={{
                type: 'default',
                className: 'edge-exec',
                style: {
                  stroke: 'var(--wf-color-text-tertiary)',
                  strokeWidth: 1.5,
                },
                markerEnd: {
                  type: MarkerType.ArrowClosed,
                  color: token('color.text-tertiary') as string,
                  width: 20,
                  height: 20,
                },
              }}
              connectionLineStyle={connectionLineStyle}
              onlyRenderVisibleElements
              minZoom={0.1}
              maxZoom={2.5}
              zoomOnScroll
              zoomOnPinch
              panOnScroll={false}
              panOnDrag={[1, 2]}
              selectionOnDrag={false}
              style={{ width: '100%', height: '100%' }}
              attributionPosition="bottom-left"
            >
              <Background
                color={token('color.border-strong') as string}
                gap={24}
                size={1}
              />
              <Controls showInteractive />
              <MiniMap
                position="bottom-right"
                style={{
                  background: 'var(--wf-color-bg)',
                  width: 100,
                  height: 72,
                }}
                maskColor="var(--wf-color-node-minimap-mask)"
                pannable
                zoomable
              />
            </ReactFlow>
            </EdgeRouteProvider>
            </EdgeEditProvider>
          </ReactFlowProvider>
          </NodeCallbacksContext.Provider>
        </div>

        {/* Activity bar */}
        <div className="activity-bar">
          <button
            type="button"
            className={`activity-item${paletteOpen ? ' active' : ''}`}
            title="节点库"
            onClick={() => setPaletteOpen((v) => !v)}
          >
            <AppstoreOutlined />
          </button>
          <button
            type="button"
            className={`activity-item${configOpen ? ' active' : ''}`}
            title="配置"
            onClick={() => {
              setConfigOpen((v) => !v);
              if (!configOpen) { setWdlOpen(false); setHelpOpen(false); setInspectorOpen(false); }
            }}
          >
            <SettingOutlined />
          </button>
          <button
            type="button"
            className={`activity-item${inspectorOpen ? ' active' : ''}`}
            title="节点状态"
            onClick={() => {
              setInspectorOpen((v) => !v);
              if (!inspectorOpen) { setConfigOpen(false); setWdlOpen(false); setHelpOpen(false); }
            }}
          >
            <AuditOutlined />
          </button>
          <button
            type="button"
            className={`activity-item${wdlOpen ? ' active' : ''}`}
            title="WDL 文本"
            onClick={() => {
              setWdlOpen((v) => !v);
              if (!wdlOpen) { setConfigOpen(false); setHelpOpen(false); setInspectorOpen(false); }
            }}
          >
            <FileTextOutlined />
          </button>
          <button
            type="button"
            className={`activity-item${helpOpen ? ' active' : ''}`}
            title="帮助"
            onClick={() => {
              setHelpOpen((v) => !v);
              if (!helpOpen) { setConfigOpen(false); setWdlOpen(false); setInspectorOpen(false); }
            }}
          >
            <QuestionCircleOutlined />
          </button>
        </div>

        {/* Palette panel */}
        {paletteOpen && (
          <Palette
            width={paletteWidth}
            onClose={() => setPaletteOpen(false)}
            onDragStart={onDragStart}
            onResizePointerDown={(e) => beginLeftPanelResize(e, paletteWidth, setPaletteWidth)}
          />
        )}

        {/* Config panel */}
        {configOpen && (
          <div
            className="side-panel-floating side-right config-panel-container"
            style={{ width: rightPanelWidth }}
          >
            <PanelEdgeResize
              side="w"
              onPointerDown={(e) => beginRightPanelResize(e, rightPanelWidth, setRightPanelWidth)}
            />
            <div className="panel-header">
              <span>{selectedNode ? '节点配置' : selectedEdge ? '连线配置' : '工作流配置'}</span>
              <Button
                type="text"
                size="small"
                className="btn-ghost btn-small"
                title="关闭配置"
                icon={<CloseOutlined />}
                onClick={() => setConfigOpen(false)}
              />
            </div>
            <div className="panel-scroll">
              {selectedNode && (
                <div className="form-group form-group-static config-step-id">
                  {selectedNode.id}
                </div>
              )}
              {selectedNode ? (
                <NodeConfigPanel
                  selectedNode={selectedNode as unknown as ReactFlowNode}
                  wdlDocument={wdlDocument}
                  nodes={nodes as unknown as ReactFlowNode[]}
                  edges={edges as unknown as ReactFlowEdge[]}
                  updateSelectedNode={updateSelectedNode}
                  updateDocumentSchedule={updateDocumentSchedule}
                />
              ) : selectedEdge ? (
                isDataEdge(selectedEdge) ? (
                  <div className="node-config-panel">
                    <div className="form-hint">数据绑定：把上游数据写进目标节点的 input 模板</div>
                    <div className="form-group form-group-static" style={{ marginTop: 8 }}>
                      {`${selectedEdge.data?.ref || ''}`}
                    </div>
                    <div className="form-hint">
                      选中后按 Delete 删除绑定（模板同步剥除）
                    </div>
                  </div>
                ) : (
                  <div className="node-config-panel">
                    <div className="form-group form-group-static">
                      {`${selectedEdge.source} → ${selectedEdge.target}`}
                    </div>
                    <div className="form-group">
                      <label>触发条件 (on)</label>
                      <select
                        className="config-input"
                        value={(selectedEdge.data as { on?: string } | undefined)?.on === 'error' ? 'error' : 'success'}
                        onChange={(e) => updateEdgeOn(selectedEdge.id, e.target.value === 'error' ? 'error' : 'success')}
                      >
                        <option value="success">success · 正常完成时投递</option>
                        <option value="error">error · 失败时投递（重试/兜底）</option>
                      </select>
                    </div>
                    <div className="form-hint">
                      error 边是 kick 边：不参与就绪判定，上游失败时机会性触发目标节点
                    </div>
                    {edgeOnError && (
                      <div className="form-hint" style={{ color: 'var(--wf-color-error)' }}>
                        {edgeOnError}
                      </div>
                    )}
                  </div>
                )
              ) : (
                <NodeConfigPanel
                  selectedNode={null}
                  wdlDocument={wdlDocument}
                  nodes={nodes as unknown as ReactFlowNode[]}
                  edges={edges as unknown as ReactFlowEdge[]}
                  updateSelectedNode={updateSelectedNode}
                  updateDocumentSchedule={updateDocumentSchedule}
                  updateDocumentMeta={updateDocumentMeta}
                />
              )}
            </div>
          </div>
        )}

        {/* WDL panel */}
        {wdlOpen && (
          <div
            className="side-panel-floating side-right wdl-panel"
            style={{ width: rightPanelWidth }}
          >
            <PanelEdgeResize
              side="w"
              onPointerDown={(e) => beginRightPanelResize(e, rightPanelWidth, setRightPanelWidth)}
            />
            <div className="panel-header">
              <span>WDL 文本（内核投影）</span>
              <Button
                type="text"
                size="small"
                className="btn-ghost btn-small"
                title="关闭 WDL"
                icon={<CloseOutlined />}
                onClick={() => setWdlOpen(false)}
              />
            </div>
            <div
              className="panel-scroll"
              style={{ display: 'flex', flexDirection: 'column', padding: 0 }}
            >
              <div
                className={`wdl-status-bar${
                  wdlError ? ' wdl-status-error' : wdlWarnings.length ? ' wdl-status-warning' : ' wdl-status-ok'
                }`}
              >
                {wdlError
                  ? `WDL 错误: ${wdlError}`
                  : wdlWarnings.length
                    ? `WDL 提示: ${wdlWarnings.join('；')}`
                    : 'WDL 已同步'}
              </div>
              <textarea
                ref={wdlTextRef}
                className="wdl-textarea"
                spellCheck={false}
                value={wdlText}
                onChange={(e) => onWdlTextChange(e.target.value)}
                placeholder={'name: my_flow\nnodes:\n  节点1:\n    task: 做某事\nedges: []'}
              />
            </div>
          </div>
        )}

        {/* Help panel */}
        {helpOpen && (
          <div
            className="side-panel-floating side-right help-panel"
            style={{ width: rightPanelWidth }}
          >
            <PanelEdgeResize
              side="w"
              onPointerDown={(e) => beginRightPanelResize(e, rightPanelWidth, setRightPanelWidth)}
            />
            <div className="panel-header">
              <span>帮助</span>
              <Button
                type="text"
                size="small"
                className="btn-ghost btn-small"
                title="关闭帮助"
                icon={<CloseOutlined />}
                onClick={() => setHelpOpen(false)}
              />
            </div>
            <div className="panel-scroll">
              <h3>内核图</h3>
              <div className="form-hint">
                <div>• 节点只有智能体一种：标题即节点 id，正文是 task</div>
                <div>• 并行 = 一个节点连多条出边；汇聚 = 多条入边</div>
                <div>• 循环 = 回边（允许成环，激活上限保证终止）</div>
                <div>• 重试/兜底 = 选中连线切 on=error（红色虚线）</div>
                <div>• routes=one 时节点每次激活只选一条出边</div>
              </div>
              <h3>数据流</h3>
              <div className="form-hint">
                <div>• 数据写进节点的 input 模板：{'{{steps.y.text}}'} 引用上游，或直接写字面量种子</div>
                <div>• 从上游「text」桩拖线到节点「输入」桩自动写入模板（蓝色虚线为绑定可视化）</div>
              </div>
              <h3>画布操作</h3>
              <div className="form-hint">
                <div>• 拖节点库到画布新增智能体；Delete 删除选中</div>
                <div>• 布局按拓扑自动分层；节点拖动仅本会话内生效</div>
                <div>• 「自动布局」重算全图分层并回到视图中心</div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 节点源码预览（只读） */}
      <Modal
        open={!!sourcePreview}
        title={sourcePreview ? `节点 ${sourcePreview.nodeId} 的定义` : ''}
        footer={null}
        onCancel={() => setSourcePreview(null)}
      >
        <pre className="wdl-source-preview">{sourcePreview?.summary}</pre>
      </Modal>

      {/* 会话内节点状态：右侧面板的一种视图（与配置/WDL/帮助互斥） */}
      {inspectorOpen && (
        <div
          className="side-panel-floating side-right config-panel-container"
          style={{ width: rightPanelWidth }}
        >
          <PanelEdgeResize
            side="w"
            onPointerDown={(e) => beginRightPanelResize(e, rightPanelWidth, setRightPanelWidth)}
          />
          <div className="panel-header">
            <span>
              节点状态{selectedNode ? ` · ${String(selectedNode.data?.stepId || selectedNode.id)}` : ''}
            </span>
            <Button
              type="text"
              size="small"
              className="btn-ghost btn-small"
              title="关闭状态"
              icon={<CloseOutlined />}
              onClick={() => setInspectorOpen(false)}
            />
          </div>
          <div className="panel-scroll">
            <FlowNodeStatusPanel
              nodeId={selectedNode ? String(selectedNode.data?.stepId || selectedNode.id) : null}
              liveNode={
                selectedNode && liveNodes
                  ? liveNodes[String(selectedNode.data?.stepId || selectedNode.id)] || null
                  : null
              }
            />
          </div>
        </div>
      )}
    </div>
    </ConfigProvider>
  );
}
