import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "antd";
import { LoadingOutlined, MessageOutlined } from "@ant-design/icons";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { FilePathLink } from "../../components/FilePathLink";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { chatRowKey, useStore, type ChatFileAttachment, type ChatMessage, type ChatToolLine } from "../../lib/store";
import type { CanonicalDiffLines } from "../../lib/ws";
import {
  ToolLineAccordion,
  ToolLinePanel,
  ToolLineRow,
  ToolSubtreeRows,
  toolRunning,
} from "./ToolLineRow";
import { buildToolLineGroups, directChildRows, workRowsOf, type ProcessEntry, type ToolLineGroup } from "../../lib/toolLineGroups";
import { groupFramesByNode } from "../../lib/flowNodeGroups";
import { FlowNodePanel } from "./FlowNodePanel";
import type { TreeRow } from "../../lib/subagentTree";
import { normalizeMathDelimiters } from "../../lib/mathNormalize";
import { isTrustedImageUrl } from "../../lib/imageUrlPolicy";
import { tokenQueryFragment } from "../../lib/auth";
import { MarkdownLink } from "../../components/MarkdownLink";
import { markdownUrlTransform } from "../../lib/markdownUrl";
import { DiffBlocksView } from "../../components/DiffBlocksView";
import { getWS } from "../../lib/ws";
import {
  BOTTOM_ANCHOR,
  anchorAtViewportTop,
  decideScroll,
  type ScrollAnchor,
  type ScrollRow,
} from "../../lib/chatScroll";
import { isHiddenToolLine } from "../../lib/toolVisibility";

/** 渲染窗口行数（固定）：DOM 里始终最多这么多行——看更早内容靠**窗口上移**，
 *  不再靠加深窗口把整表塞进 DOM（那是「消息越多输入越卡」的来源）。 */
const MAX_RENDERED_MESSAGES = 120;

/** 「加载更早消息」一次上移的固定行数。 */
const EARLIER_PAGE_ROWS = 100;

/** 行级跳过渲染：屏外行交给浏览器跳绘（视口外上下各留约一屏余量），
 *  contain-intrinsic-size 的 auto 关键字会记住上次实测高度，减少滚动抖动。
 *  只影响屏外行的布局/绘制，行本身仍照常提交，已显示前缀不变式不受影响。 */
const ROW_CONTAINMENT: React.CSSProperties = {
  contentVisibility: "auto",
  containIntrinsicSize: "auto 72px",
};

/** 距底部多少像素内视为「在底部」，恢复自动跟随。 */
const STICK_THRESHOLD = 48;

/** 量锚：贴底或视口顶首条可见行（空高行跳过）。 */
function measureScrollAnchor(el: HTMLElement): ScrollAnchor {
  if (el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD) return BOTTOM_ANCHOR;
  const containerTop = el.getBoundingClientRect().top;
  const rows: ScrollRow[] = [];
  for (let i = 0; i < el.children.length; i++) {
    const node = el.children[i] as HTMLElement;
    const key = node.dataset.msgKey;
    if (!key) continue;
    const rect = node.getBoundingClientRect();
    if (rect.bottom <= rect.top) continue;
    rows.push({ key, top: rect.top - containerTop, bottom: rect.bottom - containerTop });
    if (rect.bottom - containerTop > 0) break;
  }
  return anchorAtViewportTop(rows);
}

/** 按锚落位；锚不在 DOM → false（骨架）或贴底（已回收）。 */
function applyScrollAnchor(el: HTMLElement, anchor: ScrollAnchor): boolean {
  if (anchor.kind === "bottom") {
    el.scrollTop = el.scrollHeight;
    return true;
  }
  let rows = 0;
  for (let i = 0; i < el.children.length; i++) {
    const node = el.children[i] as HTMLElement;
    const key = node.dataset.msgKey;
    if (!key) continue;
    rows += 1;
    if (key !== anchor.key) continue;
    // 反推 scrollTop：该消息此刻的偏移与记录值之差，就是要补的那一截。
    el.scrollTop += node.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.offset;
    return true;
  }
  if (rows > 0) el.scrollTop = el.scrollHeight; // 有行但没有这一条 ⇒ 它已不在屏上
  return rows > 0;
}

/** 贴底：layout 同步 + rAF 补偿高度变化。 */
function scrollBottomWithCompensation(el: HTMLElement): () => void {
  el.scrollTop = el.scrollHeight;
  let raf2 = 0;
  let heightAfterFirst = -1;
  const raf1 = requestAnimationFrame(() => {
    el.scrollTop = el.scrollHeight;
    heightAfterFirst = el.scrollHeight;
    raf2 = requestAnimationFrame(() => {
      if (el.scrollHeight !== heightAfterFirst) el.scrollTop = el.scrollHeight;
    });
  });
  return () => {
    cancelAnimationFrame(raf1);
    cancelAnimationFrame(raf2);
  };
}

/** 分隔线到视觉内容的目标间距（无框气泡需扣透明 padding）。 */
const DIVIDER_BREATH = 12;

/** 无视觉边界的气泡（assistant 正文）的透明纵向 padding，取自 --coara-bubble-pad-v。
 *  值只在主题切换时可能变，因此按「主题指纹」缓存——render 期反复 getComputedStyle
 *  会强制样式解析，属于每帧都要付的隐性开销。 */
let _padCacheKey = "";
let _padCache = 8;

function framelessPadding(): number {
  const root = document.documentElement;
  const key = `${root.className}\u0000${root.getAttribute("data-theme") ?? ""}`;
  if (key === _padCacheKey) return _padCache;
  const raw = getComputedStyle(root).getPropertyValue("--coara-bubble-pad-v").trim();
  const pad = raw ? parseFloat(raw) : NaN;
  _padCacheKey = key;
  _padCache = Number.isFinite(pad) ? pad : 8;
  return _padCache;
}

/** 无框气泡（assistant 正文 / 排队占位）。 */
function isFrameless(m?: ChatMessage): boolean {
  if (!m) return false;
  return (
    m.role === "assistant" && !m.dividerLabel && !m.dividerTimeOnly && !m.isCommandResult && !m.diff && !m.tool
  );
}

/** 分隔线邻侧净间距（无框侧扣 padding）。 */
function dividerAdjacentGap(adjacent?: ChatMessage): number {
  if (!isFrameless(adjacent)) return DIVIDER_BREATH;
  return Math.max(0, DIVIDER_BREATH - framelessPadding());
}

/** 页面 origin 即 API 服务（api.ts 的 API_BASE 为空、全部走同源相对路径） */
const PAGE_ORIGIN = window.location.origin;

/** 代码块：语言标签 + 一键复制（竞品标配，发布前必补）。 */
function CodeBlock({ children }: { children?: React.ReactNode }) {
  const [copied, setCopied] = useState(false);
  // children 通常是 <code className="language-x">文本</code>
  let lang = "";
  let codeText = "";
  if (
    children &&
    typeof children === "object" &&
    "props" in (children as { props?: { className?: string; children?: unknown } })
  ) {
    const props = (children as { props: { className?: string; children?: unknown } }).props;
    const m = /language-([\w-]+)/.exec(props.className || "");
    if (m) lang = m[1];
    codeText = extractText(props.children);
  } else {
    codeText = extractText(children);
  }
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(codeText);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* 剪贴板不可用时静默 */
    }
  };
  return (
    <div className="codeblock">
      <div className="codeblock-bar">
        <span className="codeblock-lang">{lang || "code"}</span>
        <button type="button" className="codeblock-copy" onClick={() => void onCopy()}>
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre>{children}</pre>
    </div>
  );
}

function extractText(node: unknown): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (typeof node === "object" && "props" in (node as { props?: { children?: unknown } })) {
    return extractText((node as { props: { children?: unknown } }).props.children);
  }
  return "";
}

/** Markdown：仅 MarkdownLink 可点。 */
const markdownComponents: Components = {
  a: MarkdownLink,
  pre({ children }) {
    return <CodeBlock>{children}</CodeBlock>;
  },
  code({ className, children }) {
    return <code className={className}>{children}</code>;
  },
  img({ src, alt }) {
    const url = typeof src === "string" ? src : "";
    if (!isTrustedImageUrl(url, PAGE_ORIGIN)) {
      return (
        <span
          style={{
            display: "inline-block",
            padding: "4px 8px",
            fontSize: 13,
            color: "var(--coara-text-faint)",
            background: "var(--coara-bg-subtle)",
            border: "1px dashed var(--coara-border)",
            borderRadius: 6,
          }}
        >
          {alt ? `已拦截外部图片：${alt}` : "已拦截外部图片"}
        </span>
      );
    }
    return <img src={src} alt={alt ?? ""} loading="lazy" />;
  },
};

function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Build an authenticated media URL for outbound files. */
function outboundMediaSrc(url: string): string {
  const frag = tokenQueryFragment();
  if (!frag) return url;
  return url.includes("?") ? `${url}&${frag}` : `${url}?${frag}`;
}

function _isAbsPath(value: string): boolean {
  return value.includes(":\\") || value.includes(":/") || value.startsWith("/");
}

/** /file 路径；无 path 的出站 uuid → null（走下载）。 */
function attachmentViewPath(file: ChatFileAttachment): string | null {
  const explicit = (file.path || "").trim();
  if (explicit) return explicit;
  const id = (file.file_id || "").trim();
  if (!id) return null;
  if (_isAbsPath(id)) return id;
  if ((file.url || "").includes("/api/outbound-files/")) return null;
  return id.startsWith("uploads/") ? id : `uploads/${id}`;
}

function FileCardLink({
  file,
  viewPath,
  className,
  children,
}: {
  file: ChatFileAttachment;
  viewPath: string | null;
  className?: string;
  children: ReactNode;
}) {
  if (!viewPath) {
    return (
      <a href={outboundMediaSrc(file.url)} className={className} target="_blank" rel="noreferrer">
        {children}
      </a>
    );
  }
  return (
    <FilePathLink path={viewPath} className={className}>
      {children}
    </FilePathLink>
  );
}

/** send_file 附件预览/下载。 */
const MessageFiles = memo(function MessageFiles({ files }: { files: ChatFileAttachment[] }) {
  if (!files.length) return null;
  return (
    <div className="chat-message-files">
      {files.map((file) => {
        const src = outboundMediaSrc(file.url);
        const sizeLabel = formatFileSize(file.size);
        const viewPath = attachmentViewPath(file);
        if (file.is_image) {
          return (
            <figure key={file.file_id} className="chat-file-media">
              <FileCardLink file={file} viewPath={viewPath}>
                <img src={src} alt={file.filename} loading="lazy" />
              </FileCardLink>
              {(file.caption || sizeLabel) && (
                <figcaption>
                  {file.caption || file.filename}
                  {sizeLabel ? ` · ${sizeLabel}` : ""}
                </figcaption>
              )}
            </figure>
          );
        }
        return (
          <FileCardLink
            key={file.file_id}
            file={file}
            viewPath={viewPath}
            className="chat-file-download"
          >
            <span className="chat-file-download-name">{file.filename}</span>
            <span className="chat-file-download-meta">
              {[file.mime, sizeLabel].filter(Boolean).join(" · ")}
            </span>
          </FileCardLink>
        );
      })}
    </div>
  );
});

/** Phone-style timeline divider (model / workspace switch). */
const TimelineDivider = memo(function TimelineDivider({
  label,
  marginTop,
}: {
  label: string;
  marginTop: number;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        margin: `${marginTop}px 0 0`,
        padding: "0 8px",
        gap: 10,
      }}
    >
      <div
        style={{
          flex: 1,
          height: 1,
          background: "var(--coara-border-muted)",
          opacity: 0.9,
        }}
      />
      <span
        style={{
          fontSize: 12,
          lineHeight: "16px",
          color: "var(--coara-text-tertiary)",
          textAlign: "center",
          maxWidth: "72%",
          wordBreak: "break-word",
        }}
      >
        {label}
      </span>
      <div
        style={{
          flex: 1,
          height: 1,
          background: "var(--coara-border-muted)",
          opacity: 0.9,
        }}
      />
    </div>
  );
});

/** Memoized command-result card — never re-renders unless text changes. */
const CommandResultCard = memo(function CommandResultCard({ text }: { text: string }) {
  return (
    <div style={{ marginBottom: 8, display: "flex", justifyContent: "flex-start" }}>
      <div
        style={{
          width: "100%",
          background: "var(--coara-code-canvas)",
          color: "var(--coara-code-text)",
          borderRadius: 10,
          border: "1px solid var(--coara-code-border)",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            padding: "6px 12px",
            borderBottom: "1px solid var(--coara-code-border)",
            background: "var(--coara-code-bar)",
            fontSize: 12,
            color: "var(--coara-code-muted)",
          }}
        >
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--coara-code-dot)" }} />
          <span style={{ letterSpacing: 0.3 }}>命令输出</span>
        </div>
        <pre
          style={{
            margin: 0,
            padding: "12px 14px",
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
            fontSize: 13,
            lineHeight: 1.6,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            color: "var(--coara-code-text)",
          }}
        >
          {text}
        </pre>
      </div>
    </div>
  );
});

/** MarkdownMessage：按 text memo（防整表重解析）。工具行见 ToolLineRow。 */

/** Diff 块；compact＝折叠区内顶格对齐工具行。 */
function DiffBlock({
  diff,
  marginTop,
  compact = false,
}: {
  diff: CanonicalDiffLines;
  marginTop: number;
  compact?: boolean;
}) {
  return (
    <div style={{ marginTop, display: "flex", justifyContent: "flex-start" }}>
      <div
        style={{
          width: "100%",
          paddingLeft: compact ? 0 : 14,
          paddingRight: compact ? 0 : 14,
          boxSizing: "border-box",
        }}
      >
        <DiffBlocksView diff={diff} />
      </div>
    </div>
  );
}

/** 可渲染帧＝有 tool 或 diff（与 hint 计数同源）。 */
function rendersFrame(frame: ChatMessage): boolean {
  return Boolean(frame.tool || frame.diff);
}

/** 过程条目：帧 + 树行补给。 */
export function ProcessEntryList({ entries }: { entries: ProcessEntry[] }) {
  return (
    <>
      {entries.map((entry) =>
        entry.frame ? (
          entry.frame.tool ? (
            <div
              key={chatRowKey(entry.frame)}
              style={{ marginTop: 4, display: "flex", flexDirection: "column", alignItems: "flex-start" }}
            >
              <ToolLineRow
                label={entry.frame.tool.label}
                ok={entry.frame.tool.ok !== false}
                running={false}
                expandable={false}
                open={false}
                onToggle={() => {}}
                paddingLeft={0}
              />
            </div>
          ) : entry.frame.diff ? (
            <DiffBlock key={chatRowKey(entry.frame)} diff={entry.frame.diff} marginTop={4} compact />
          ) : null
        ) : entry.row ? (
          <ToolSubtreeRows key={`work-${entry.row.nodeId}`} rows={[entry.row]} />
        ) : null,
      )}
    </>
  );
}

/** 展开面板限高。 */
const EXPAND_PANEL_MAX_HEIGHT = 240;

/** 空树行常量：选择器里返回同一引用，避免每次新数组引起重渲染。 */
const NO_ROWS: TreeRow[] = [];

/** 聊天流工具行；delegate 展开三组。 */
const ToolLine = memo(function ToolLine({
  tool,
  marginTop,
}: {
  tool: ChatToolLine;
  marginTop: number;
}) {
  const ok = tool.ok !== false;
  const callId = String(tool.tool_call_id ?? "");
  const output = useStore((s) => (callId ? s.subagentOutput[callId] : undefined));
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const isDelegate = tool.tool_name === "delegate";
  const openDetail = callId && !isDelegate
    ? () => navigate("/file?view=tool&call_id=" + encodeURIComponent(callId))
    : undefined;

  // 派生值经选择器直接取（toolLineGroups 内部按树版本缓存）：树没变就返回同一个
  // 引用，zustand 用 Object.is 比较 → 工具行不因「别人动了一下树」而整体重渲染，
  // 也不再每次重算全表。
  const running = useStore((s) => toolRunning(s.subagentRows, callId));
  const work = useStore((s) => (callId ? workRowsOf(s.subagentRows, callId) : NO_ROWS));
  const subFrames = useStore((s) => (callId ? s.subagentDiffs[callId] : undefined));
  const frames = useMemo(
    () =>
      [...(subFrames ?? [])]
        .filter(rendersFrame)
        .sort(
          (a, b) => (a.seq ?? Number.MAX_SAFE_INTEGER) - (b.seq ?? Number.MAX_SAFE_INTEGER),
        ),
    [subFrames],
  );
  // delegate 的任务指令：同样不进正文流，只在这行的「任务指令」组里（默认收起）。
  const brief = useStore((s) => (callId ? s.subagentBriefs[callId] : undefined));
  const body = output?.text ?? "";
  const result = output?.result ?? "";
  const groups = useMemo(
    () =>
      buildToolLineGroups({
        brief: brief ?? "",
        work,
        frames,
        body,
        result,
      }),
    [brief, work, frames, body, result],
  );
  // 编排行：二级是节点清单（活动树里的直接子行），三级是节点自己的内容（帧按节点分好）。
  // 认不出节点的帧与 指令/正文/结果 仍按老形态排在节点清单之后——绝不因为认不出而藏内容。
  const isOrchestrator = tool.tool_name === "orchestrator";
  const nodeRows = useStore((s) =>
    isOrchestrator && callId ? directChildRows(s.subagentRows, callId) : NO_ROWS,
  );
  const nodeGroups = useMemo(
    () =>
      isOrchestrator
        ? groupFramesByNode(
            frames,
            nodeRows.map((row) => ({
              nodeId: row.nodeId,
              subagentId: row.nodeId,
              coaraId: row.coaraId,
            })),
          )
        : null,
    [isOrchestrator, frames, nodeRows],
  );
  const panelNodes = nodeGroups && nodeRows.length > 0 ? nodeRows : NO_ROWS;
  const restGroups = useMemo(
    () =>
      !nodeGroups
        ? groups
        : buildToolLineGroups({
            brief: brief ?? "",
            work: NO_ROWS,
            frames: nodeGroups.ungrouped,
            body,
            result,
          }),
    [nodeGroups, groups, brief, body, result],
  );
  const expandable = restGroups.length > 0 || panelNodes.length > 0;

  return (
    <div
      style={{ marginTop, display: "flex", flexDirection: "column", alignItems: "flex-start" }}
      onDoubleClick={openDetail}
      title={openDetail ? "双击查看工具详情" : undefined}
    >
      <ToolLineRow
        label={tool.label}
        ok={ok}
        running={running}
        expandable={expandable}
        open={open}
        onToggle={() => setOpen((v) => !v)}
      />
      {open && expandable ? (
        <ToolLinePanel>
          <div
            style={{
              maxHeight: EXPAND_PANEL_MAX_HEIGHT,
              overflowY: "auto",
              // 右缘安全边距：内容不贴面板右边框，左右都有边界
              paddingRight: 8,
            }}
          >            {panelNodes.length > 0 && nodeGroups ? (
              <FlowNodePanel nodes={panelNodes} groups={nodeGroups} />
            ) : null}
            {restGroups.length > 0 ? (
            <ToolLineAccordion
              groups={restGroups}
              renderBody={(g: ToolLineGroup) =>
                g.id === "process" ? (
                  <>
                    {/* ① 落带帧（工具行 / diff）→ ② 活动树里帧还没有的行 */}
                    <ProcessEntryList entries={g.entries ?? []} />
                    {/* ③ 过程正文排在这一组最后，无标题、无分隔标签；左缘与上面的
                        工具行 ✓ 齐（都在组体左缘，无额外缩进）。 */}
                    {g.text ? (
                      <div
                        style={{
                          marginTop: (g.entries?.length ?? 0) > 0 ? 6 : 0,
                          whiteSpace: "pre-wrap",
                          overflowWrap: "anywhere",
                        }}
                      >
                        {g.text}
                      </div>
                    ) : null}
                  </>
                ) : (
                  // 任务指令 / 最终结果：原样文本，pre-wrap 保留换行（常是多行清单）
                  <div
                    style={{
                      whiteSpace: "pre-wrap",
                      overflowWrap: "anywhere",
                      color: g.id === "result" ? "var(--coara-text)" : undefined,
                    }}
                  >
                    {g.text}
                  </div>
                )
              }
            />
            ) : null}
          </div>
        </ToolLinePanel>
      ) : null}
    </div>
  );
});

export const MarkdownMessage = memo(
  function MarkdownMessage({ text }: { text: string }) {
    // LLM 公式常用 \[...\] / \(...\) 分隔符，remark-math 只认 $$ / $——先归一化
    const normalized = useMemo(() => normalizeMathDelimiters(text), [text]);
    return (
      <div className="chat-markdown">
        {text ? (
          <ReactMarkdown
            remarkPlugins={[remarkGfm, remarkMath]}
            rehypePlugins={[
              rehypeHighlight,
              // strict 关闭：公式 \text{} 里常混 CJK/破折号，strict 默认会把它们判错致整段不渲染
              [rehypeKatex, { strict: false }],
            ]}
            components={markdownComponents}
            urlTransform={markdownUrlTransform}
          >
            {normalized}
          </ReactMarkdown>
        ) : (
          null
        )}
      </div>
    );
  },
  (prev, next) => prev.text === next.text,
);

/** Memoized user message — never re-renders unless text changes. */
export const UserMessage = memo(function UserMessage({ text }: { text: string }) {
  return (
    <div
      style={{
        whiteSpace: "pre-wrap",
        fontSize: 15,
        lineHeight: "20px",
      }}
    >
      {text}
    </div>
  );
});

/** 气泡：同说话方 4px / 跨方 16px；分隔线用专用间距。 */
const MessageBubble = memo(
  function MessageBubble({ msg, prevMsg }: { msg: ChatMessage; prevMsg?: ChatMessage }) {
    const isUser = msg.role === "user";
    const gap = prevMsg
      ? prevMsg.dividerLabel
        ? dividerAdjacentGap(msg)
        : prevMsg.role === msg.role
          ? 4
          : 16
      : 0;
    if (msg.dividerLabel) {
      const label = msg.dividerTime
        ? `${msg.dividerLabel}  ${msg.dividerTime}`
        : msg.dividerLabel;
      return (
        <TimelineDivider
          label={label}
          marginTop={prevMsg ? dividerAdjacentGap(prevMsg) : 0}
        />
      );
    }
    if (msg.isCommandResult) {
      return <CommandResultCard text={msg.text} />;
    }
    // 工具行（✓ tool(...)）
    if (msg.tool) {
      if (isHiddenToolLine(msg.tool)) return null;
      return <ToolLine tool={msg.tool} marginTop={gap} />;
    }
    // 代码 diff 块：不占正文气泡，作为聊天流独立内容块渲染（与折叠区共用 DiffBlock）。
    if (msg.diff) {
      return <DiffBlock diff={msg.diff} marginTop={gap} />;
    }
    // 被撤回的行（chat_turn_retracted）
    if (msg.retracted) return null;
    // 输出端不显示 spinner
    if (!isUser && !msg.text && !(msg.files && msg.files.length)) {
      if (!msg.queued) return null;
      return (
        <div style={{ marginTop: gap, display: "flex", justifyContent: "flex-start" }}>
          <div
            className="chat-bubble-agent"
            style={{ color: "var(--coara-text-tertiary)", fontSize: 13, fontStyle: "italic" }}
          >
            排队中 · 等待当前回合结束…
          </div>
        </div>
      );
    }
    return (
      <div
        style={{
          marginTop: gap,
          display: "flex",
          alignItems: "center",
          justifyContent: isUser ? "flex-end" : "flex-start",
        }}
      >
        {isUser && msg.pendingInject ? (
          <span
            title="待注入：LLM 尚未读到这句话"
            style={{
              marginRight: 6,
              fontSize: 11,
              color: "var(--coara-text-tertiary)",
              display: "inline-flex",
              alignItems: "center",
              flexShrink: 0,
            }}
          >
            <LoadingOutlined style={{ fontSize: 11 }} />
          </span>
        ) : null}
        {/* 无「中断」徽标：进程被杀的回合由启动恢复注入注记提示，进行中回合不标 */}
        <div className={isUser ? "chat-bubble-user" : "chat-bubble-agent"}>
          {isUser ? (
            <>
              {msg.attachments && msg.attachments.length > 0 ? (
                <MessageFiles files={msg.attachments} />
              ) : null}
              {msg.text ? <UserMessage text={msg.text} /> : null}
              {msg.sendFailed ? (
                <div
                  style={{
                    marginTop: 4,
                    fontSize: 11,
                    color: "var(--coara-danger)",
                  }}
                >
                  未发送 · 服务端没受理这条输入，重发一次即可
                </div>
              ) : null}
            </>
          ) : (
            <>
              <MarkdownMessage text={msg.text} />
              {msg.files && msg.files.length > 0 ? <MessageFiles files={msg.files} /> : null}
            </>
          )}
        </div>
      </div>
    );
  },
  (prev, next) => {
    const pm = prev.msg;
    const nm = next.msg;
    return (
      pm.id === nm.id &&
      pm.text === nm.text &&
      pm.streaming === nm.streaming &&
      pm.isCommandResult === nm.isCommandResult &&
      pm.dividerLabel === nm.dividerLabel &&
      pm.files === nm.files &&
      pm.attachments === nm.attachments &&
      pm.diff === nm.diff &&
      pm.tool === nm.tool &&
      pm.pendingInject === nm.pendingInject &&
      // 失败标记在渲染里有分支：不进比较器会出现「标了但不重绘」
      pm.sendFailed === nm.sendFailed &&
      // 撤回标记同理（渲染里直接不画）
      pm.retracted === nm.retracted &&
      prev.prevMsg === next.prevMsg
    );
  },
);

/** 消息列表 — 对齐 coara app 设计规范 */
/** 记的是「贴底 / 某条消息 + 相对视口顶的偏移」，不是绝对像素（见 ScrollAnchor） */
const spaceScrollAnchors = new Map<string, ScrollAnchor>();

/** 本页面生命是否已经提交过一次权威内容：false＝刷新/首次进入（贴底），
 *  true 之后的空间切换才允许还原锚点。 */
let pageLifeCommitted = false;

/** 为什么不是「空态欢迎卡」：快照未到就上屏等于先给一份可能不对的画面，卡片 闪一下又被真实内容换掉 */
/** 骨架期超过这个时长仍没有权威提交，说明后端没连上：必须给明确文案与重连入口，
 *  不能让用户对着无限骨架（8s 是「本机/局域网后端该连上」的宽松上界）。 */
const SKELETON_STALL_MS = 8000;

function MessageListSkeleton() {
  const skeletonSince = useStore((s) => s.skeletonSince);
  const connected = useStore((s) => s.connected);
  const connError = useStore((s) => s.connError);
  const [, setTick] = useState(0);
  useEffect(() => {
    // 骨架期间每秒重算一次「卡了多久」，到点切成连接态文案；不在骨架期就不刷。
    if (skeletonSince === null) return;
    const timer = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, [skeletonSince]);
  const stalled = skeletonSince !== null && Date.now() - skeletonSince >= SKELETON_STALL_MS;
  const bar = (width: string, key: number) => (
    <div
      key={key}
      style={{
        height: 13,
        width,
        borderRadius: 7,
        background: "var(--coara-border-soft)",
      }}
    />
  );
  const row = (align: "flex-start" | "flex-end", widths: string[], key: string) => (
    <div
      key={key}
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: align,
        gap: 11,
        marginBottom: 26,
      }}
    >
      {widths.map((w, i) => bar(w, i))}
    </div>
  );
  return (
    <div>
      <div aria-hidden style={{ maxWidth: 820, paddingTop: 8 }}>
        {row("flex-end", ["52%", "30%"], "s1")}
        {row("flex-start", ["88%", "82%", "56%"], "s2")}
        {row("flex-end", ["44%"], "s3")}
        {row("flex-start", ["80%", "62%"], "s4")}
      </div>
      {stalled ? (
        <div
          style={{
            maxWidth: 420,
            margin: "4px auto 0",
            textAlign: "center",
            color: "var(--coara-text-muted)",
            fontSize: 13,
            lineHeight: 1.7,
          }}
        >
          <p style={{ marginBottom: 6, color: "var(--coara-text)", fontSize: 14 }}>
            {connected ? "还没等到这个空间的内容" : "正在连接后端…"}
          </p>
          <p style={{ marginBottom: 12 }}>
            {connected
              ? "连接正常，但拿不到这个空间的快照——后端可能刚重启，或正在切换空间。"
              : connError
                ? `连接失败：${connError}`
                : "连接不上后端服务，可能还没启动完成。"}
          </p>
          <Button size="small" onClick={() => getWS().reconnectNow()}>
            重试连接
          </Button>
        </div>
      ) : null}
    </div>
  );
}

export const MessageList = memo(function MessageList() {
  const messages = useStore((s) => s.messages);
  const workspaceDir = useStore((s) => s.workspaceDir);
  const sessionId = useStore((s) => s.sessionId);
  const viewReady = useStore((s) => s.viewReady);
  const hasMoreHistory = useStore((s) => s.hasMoreHistory);
  const earlierLoading = useStore((s) => s.earlierLoading);
  const loadEarlier = useStore((s) => s.loadEarlier);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const scrollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scrollRafRef = useRef<number | null>(null);
  const stickToBottomRef = useRef(true);
  /** 滚动监听只挂一次，空间归属经 ref 读当前值。 */
  const workspaceDirRef = useRef<string | null>(workspaceDir);
  workspaceDirRef.current = workspaceDir;

  // 滚动量几何合并到每帧一次（被动监听 + rAF）：流式输出期滚动事件密集，
  // 逐事件同步量布局会把主线程排在击键后面。
  useEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    let rafId: number | null = null;
    const measure = () => {
      rafId = null;
      const anchor = measureScrollAnchor(container);
      stickToBottomRef.current = anchor.kind === "bottom";
      // 手动滚到底 ＝ 回到最新：窗口重新跟随尾部（幂等：已跟随则返回同引用，不触发重渲）
      if (anchor.kind === "bottom") {
        setWin((w) => (w.end === null ? w : { ...w, end: null }));
      }
      // 顺手把当前位置记进本空间的锚（只在本页面生命内有效）
      const dir = workspaceDirRef.current;
      if (dir) {
        spaceScrollAnchors.set(dir, anchor);
      }
    };
    const onScroll = () => {
      if (rafId !== null) return;
      rafId = requestAnimationFrame(measure);
    };
    container.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      container.removeEventListener("scroll", onScroll);
      if (rafId !== null) cancelAnimationFrame(rafId);
    };
  }, []);

  // 滚动落点
  const prevDirRef = useRef<string | null>(null);
  const prevReadyRef = useRef(false);
  /** prepend 保位：点击「加载更早消息」时记下视口锚，prepend 提交后按它还原——
   *  内容变高但用户在读的那条原地不动；同时压住跟随贴底（stickToBottom 临时置
   *  false 再恢复），否则上翻阅读时点翻页会被节流跟随 effect 拉回底部。 */
  const prependAnchorRef = useRef<ScrollAnchor | null>(null);

  const handleLoadEarlier = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el || earlierLoading) return;
    prependAnchorRef.current = measureScrollAnchor(el);
    stickToBottomRef.current = false;
    // 窗口上移一页（不加深）：右端由「贴底」改为绝对行号，并受最早行约束。
    setWin((w) => {
      const len = useStore.getState().messages.length;
      const end = w.end ?? len;
      const next = Math.max(MAX_RENDERED_MESSAGES, end - EARLIER_PAGE_ROWS);
      return next >= end ? w : { ...w, end: next };
    });
    // 同时向服务端要更早的一页数据；是否上屏由窗口决定
    void loadEarlier();
  }, [earlierLoading, loadEarlier]);

  /** 回到最新：窗口重新跟随尾部，并滚到底。 */
  const handleBackToLatest = useCallback(() => {
    stickToBottomRef.current = true;
    setWin((w) => (w.end === null ? w : { ...w, end: null }));
    const el = scrollContainerRef.current;
    if (el) scrollBottomWithCompensation(el);
  }, []);
  /** 提交时锚定那条消息还不在 DOM 里 */
  const pendingAnchorRef = useRef<ScrollAnchor | null>(null);
  useLayoutEffect(() => {
    const dirChanged = prevDirRef.current !== workspaceDir;
    const hadDir = prevDirRef.current !== null;
    const becameReady = viewReady && !prevReadyRef.current;
    prevDirRef.current = workspaceDir;
    prevReadyRef.current = viewReady;
    const el = scrollContainerRef.current;
    if (workspaceDir === null || el === null) return;

    // prepend 保位优先于一切
    const prependAnchor = prependAnchorRef.current;
    if (prependAnchor) {
      prependAnchorRef.current = null;
      if (!applyScrollAnchor(el, prependAnchor)) {
        stickToBottomRef.current = true;
        return scrollBottomWithCompensation(el);
      }
      stickToBottomRef.current = prependAnchor.kind === "bottom";
      if (prependAnchor.kind === "bottom") return scrollBottomWithCompensation(el);
      const raf = requestAnimationFrame(() => applyScrollAnchor(el, prependAnchor));
      return () => cancelAnimationFrame(raf);
    }

    const decision = decideScroll({
      newPageLife: !pageLifeCommitted,
      userScrolledUp: stickToBottomRef.current === false,
      workspaceSwitched: dirChanged && hadDir,
      hasAnchor: spaceScrollAnchors.has(workspaceDir),
      skeletonToContent: becameReady,
    });
    if (becameReady) pageLifeCommitted = true;
    if (decision === "anchor") {
      pendingAnchorRef.current = spaceScrollAnchors.get(workspaceDir) ?? null;
    }

    const pending = pendingAnchorRef.current;
    if (pending) {
      if (!applyScrollAnchor(el, pending)) {
        // 一行都还量不到
        if (!viewReady) return;
        pendingAnchorRef.current = null;
        stickToBottomRef.current = true;
        return scrollBottomWithCompensation(el);
      }
      pendingAnchorRef.current = null;
      stickToBottomRef.current = pending.kind === "bottom";
      if (pending.kind === "bottom") return scrollBottomWithCompensation(el);
      const raf = requestAnimationFrame(() => applyScrollAnchor(el, pending));
      return () => cancelAnimationFrame(raf);
    }
    if (decision === "hold") return;
    // bottom：刷新首屏 / 骨架转真实内容 / 切到没有锚的空间
    stickToBottomRef.current = true;
    return scrollBottomWithCompensation(el);
  }, [workspaceDir, viewReady]);

  /** 渲染窗口：key ＝ 空间 + 会话边界，切空间 / 新会话回到贴底窗口（不把上一个空间的
   *  阅读位置带过来）。`end` 是窗口右端的**绝对行号**（不含），null ＝ 跟随尾部。
   *  用绝对行号而不是「距尾部偏移」，是为了让用户读历史时新帧不会推动窗口。 */
  const windowKey = `${workspaceDir ?? ""}\u0000${sessionId ?? ""}`;
  const [win, setWin] = useState<{ key: string; end: number | null }>(() => ({ key: windowKey, end: null }));
  if (win.key !== windowKey) setWin({ key: windowKey, end: null });

  const atLatest = win.end === null;
  const windowEnd = win.end ?? messages.length;
  const visibleMessages = useMemo(() => {
    const end = Math.min(windowEnd, messages.length);
    const start = Math.max(0, end - MAX_RENDERED_MESSAGES);
    return messages.slice(start, end);
  }, [messages, windowEnd]);
  const canMoveEarlier = windowEnd > MAX_RENDERED_MESSAGES;

  // Throttled scroll-to-bottom: during streaming, chunks arrive at high frequency. C
  useEffect(() => {
    if (!stickToBottomRef.current) return;
    if (scrollTimerRef.current) return;
    scrollTimerRef.current = setTimeout(() => {
      scrollTimerRef.current = null;
      scrollRafRef.current = requestAnimationFrame(() => {
        scrollRafRef.current = null;
        const container = scrollContainerRef.current;
        if (container) {
          // Use instant scroll during fast streaming to avoid jank.
          container.scrollTop = container.scrollHeight;
        }
      });
    }, 150);
  }, [visibleMessages]);

  // 本端发送即回底
  const lastTailIdRef = useRef<string | null>(null);
  // 上一次做「强制回底」判定时所在的空间：切换后的第一帧不参与回底判定。
  const tailDirRef = useRef<string | null>(null);
  useEffect(() => {
    let tail: ChatMessage | null = null;
    for (let i = visibleMessages.length - 1; i >= 0; i--) {
      const m = visibleMessages[i];
      if (m.optimistic) {
        tail = m;
        break;
      }
    }
    const tailId = tail?.id ?? null;
    const isNewTail = tailId !== lastTailIdRef.current;
    lastTailIdRef.current = tailId;
    // 刚切过空间：位置由锚点效果决定，这里不抢着回底——否则翻着历史切回来会被拽到底。
    if (tailDirRef.current !== workspaceDir) {
      tailDirRef.current = workspaceDir;
      return;
    }
    if (!(isNewTail && tail !== null)) return;
    stickToBottomRef.current = true;
    if (scrollTimerRef.current) {
      clearTimeout(scrollTimerRef.current);
      scrollTimerRef.current = null;
    }
    if (scrollRafRef.current !== null) {
      cancelAnimationFrame(scrollRafRef.current);
      scrollRafRef.current = null;
    }
    scrollRafRef.current = requestAnimationFrame(() => {
      scrollRafRef.current = null;
      const container = scrollContainerRef.current;
      if (container) {
        container.scrollTop = container.scrollHeight;
      }
    });
  }, [visibleMessages]);

  // Cleanup pending scroll timer / animation frame on unmount.
  useEffect(() => {
    return () => {
      if (scrollTimerRef.current) {
        clearTimeout(scrollTimerRef.current);
        scrollTimerRef.current = null;
      }
      if (scrollRafRef.current !== null) {
        cancelAnimationFrame(scrollRafRef.current);
        scrollRafRef.current = null;
      }
    };
  }, []);

  return (
    <div
      ref={scrollContainerRef}
      className="chat-scroll-nobar"
      style={{
        height: "100%",
        overflowY: "auto",
        padding: "20px 20px 12px",
        maxWidth: 1200,
        margin: "0 auto",
        width: "100%",
        // 顶部渐隐遮罩：滚上去的消息柔和淡出，而不是在窗口上沿被硬截断
        WebkitMaskImage: "linear-gradient(to bottom, transparent 0, black 28px)",
        maskImage: "linear-gradient(to bottom, transparent 0, black 28px)",
      }}
    >
      {visibleMessages.length > 0 && (canMoveEarlier || hasMoreHistory) && (
        <div style={{ textAlign: "center", padding: "0 0 12px" }}>
          <Button
            type="link"
            size="small"
            onClick={handleLoadEarlier}
            disabled={earlierLoading || !canMoveEarlier}
            style={{ color: "var(--coara-text-muted)" }}
          >
            {earlierLoading ? (
              <>
                <LoadingOutlined style={{ marginRight: 6 }} />
                加载中…
              </>
            ) : canMoveEarlier ? (
              "加载更早消息"
            ) : (
              "已到最早"
            )}
          </Button>
        </div>
      )}
      {visibleMessages.length === 0 && !viewReady && <MessageListSkeleton />}
      {visibleMessages.length === 0 && viewReady && (
        <div
          style={{
            textAlign: "center",
            color: "var(--coara-text-muted)",
            marginTop: 120,
            fontSize: 15,
          }}
        >
          <div
            style={{
              width: 64,
              height: 64,
              margin: "0 auto 20px",
              borderRadius: "50%",
              background: "linear-gradient(135deg, var(--coara-accent-tint-a), var(--coara-accent-tint-b))",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 28,
              boxShadow: "var(--coara-shadow-accent-sm)",
            }}
          >
            <MessageOutlined />
          </div>
          <p style={{ fontSize: 17, marginBottom: 8, color: "var(--coara-text)", fontWeight: 600 }}>
            coara 已就绪
          </p>
          <p style={{ fontSize: 14, color: "var(--coara-text-muted)", maxWidth: 320, margin: "0 auto", lineHeight: 1.6 }}>
            输入消息开始对话，或输入 /help 查看命令
          </p>
        </div>
      )}
      {visibleMessages
        // 隐藏行（delegate wait / send_file）不画、也不参与间距计算
        .filter((m) => !(m.tool && isHiddenToolLine(m.tool)))
        .map((msg, i, rows) => (
          // 外层只作「锚点量尺」
          <div key={chatRowKey(msg)} data-msg-key={chatRowKey(msg)} style={ROW_CONTAINMENT}>
            <MessageBubble
              msg={msg}
              prevMsg={i > 0 ? rows[i - 1] : undefined}
            />
          </div>
        ))}
      {!atLatest && visibleMessages.length > 0 && (
        <div
          style={{
            position: "sticky",
            bottom: 4,
            display: "flex",
            justifyContent: "center",
            pointerEvents: "none",
            paddingTop: 4,
          }}
        >
          <Button
            size="small"
            shape="round"
            onClick={handleBackToLatest}
            style={{ pointerEvents: "auto", boxShadow: "var(--coara-shadow-float)" }}
          >
            回到最新
          </Button>
        </div>
      )}
    </div>
  );
});
