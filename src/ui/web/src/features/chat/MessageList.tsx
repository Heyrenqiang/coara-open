import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Button } from "antd";
import { LoadingOutlined, MessageOutlined } from "@ant-design/icons";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
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
  formatToolDuration,
  toolRunning,
} from "./ToolLineRow";
import { buildToolLineGroups, workRowsOf, type ProcessEntry, type ToolLineGroup } from "../../lib/toolLineGroups";
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

const MAX_RENDERED_MESSAGES = 200;

/** 距底部多少像素内视为「在底部」，恢复自动跟随。 */
const STICK_THRESHOLD = 48;

/** 量一次「此刻停在哪儿」：贴着底＝贴底锚；否则量到视口顶部那条消息 → 内容锚
 *  （见 lib/chatScroll.ts::ScrollAnchor）。只量到第一条可见行为止，不遍历整列。
 *  渲染成空的条目（空流式 / 排队占位）高度为 0，不参与锚定——否则会锚到一条
 *  看不见的行上。 */
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

/** 按锚落位。返回 false＝锚定那条消息此刻还没进 DOM（骨架期 / 内容未到），调用方
 *  留着这个锚等下一次提交；锚定消息已经不在了（被 200 条渲染窗口回收）时按规格
 *  退回贴底。 */
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

/** 贴底：layout 阶段先同步滚一次（DOM 已更新、还没绘制，不会闪），随后一帧 rAF 再
 *  对一次；若这一帧里高度还在变（字体 / 图片 / 长代码块陆续撑高），再补最后一帧。 */
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

/** 分隔线（模型/工作空间切换）的视觉基准留白：线到相邻「视觉内容」的目标距离。
 *  与普通气泡不同，分隔线上下相邻的是气泡盒——无框气泡的透明纵向 padding
 *  会把感知间距放大，必须按相邻气泡是否有视觉边界做补偿，两侧看起来才等距。 */
const DIVIDER_BREATH = 12;

/** 无视觉边界的气泡（assistant 正文）的透明纵向 padding，取自 --coara-bubble-pad-v。 */
function framelessPadding(): number {
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue("--coara-bubble-pad-v")
    .trim();
  const pad = raw ? parseFloat(raw) : NaN;
  return Number.isFinite(pad) ? pad : 8;
}

/** 该消息是否渲染成「无视觉边界」的内容（无底色/边框，与背景同色，靠位置区分）：
 *  assistant 正文气泡、排队占位属此类；命令卡 / diff 卡 / 分隔线各有自己的边界。 */
function isFrameless(m?: ChatMessage): boolean {
  if (!m) return false;
  return (
    m.role === "assistant" && !m.dividerLabel && !m.dividerTimeOnly && !m.isCommandResult && !m.diff && !m.tool
  );
}

/** 与分隔线相邻一侧的净间距：有框侧直接给 DIVIDER_BREATH；无框侧自身含透明
 *  padding，margin 需压缩补差，使「线到视觉内容」两侧一致（clamp 防过小）。 */
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

/** Markdown 组件：可点链接只认 `[文案](目标)` 且目标为网页 / 文件 / 文件夹（MarkdownLink）。
 *  裸路径、inline code、其它链接文案一律不可点。 */
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

/** Inline preview / download for files delivered via send_file. */
const MessageFiles = memo(function MessageFiles({ files }: { files: ChatFileAttachment[] }) {
  if (!files.length) return null;
  return (
    <div className="chat-message-files">
      {files.map((file) => {
        const src = outboundMediaSrc(file.url);
        const sizeLabel = formatFileSize(file.size);
        if (file.is_image) {
          return (
            <figure key={file.file_id} className="chat-file-media">
              <a href={src} target="_blank" rel="noreferrer">
                <img src={src} alt={file.filename} loading="lazy" />
              </a>
              {(file.caption || sizeLabel) && (
                <figcaption>
                  {file.caption || file.filename}
                  {sizeLabel ? ` · ${sizeLabel}` : ""}
                </figcaption>
              )}
            </figure>
          );
        }
        if (file.is_video) {
          return (
            <figure key={file.file_id} className="chat-file-media">
              <video src={src} controls preload="metadata" />
              <figcaption>
                {file.caption || file.filename}
                {sizeLabel ? ` · ${sizeLabel}` : ""}
              </figcaption>
            </figure>
          );
        }
        if (file.is_audio) {
          return (
            <figure key={file.file_id} className="chat-file-media">
              <audio src={src} controls preload="metadata" />
              <figcaption>
                {file.caption || file.filename}
                {sizeLabel ? ` · ${sizeLabel}` : ""}
              </figcaption>
            </figure>
          );
        }
        return (
          <a
            key={file.file_id}
            className="chat-file-download"
            href={src}
            download={file.filename}
            target="_blank"
            rel="noreferrer"
          >
            <span className="chat-file-download-name">{file.filename}</span>
            <span className="chat-file-download-meta">
              {[file.mime, sizeLabel].filter(Boolean).join(" · ")}
            </span>
          </a>
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
        // 上侧净间距由调用方按相邻气泡有无视觉边界算好（补偿无框气泡的透明
        // padding）；下侧不设 margin——交给下一条消息自己的 marginTop，避免
        // 块级 margin 折叠把分隔线上下留白搅乱。
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

/** Memoized markdown message — only re-renders when text changes.
 *  This is the single most important optimization: ReactMarkdown with 4 plugins
 *  is extremely expensive to re-render, and without memo it would re-parse
 *  ALL messages on every streaming chunk. */
/** 工具行的耗时括注、后代行取法、行渲染与展开面板：全部在 ToolLineRow.tsx
 *  （行渲染只写一份，别处复用时不会漂）。 */

/** 代码 diff 块：作为聊天流独立内容块渲染（消息流与 delegate 折叠区共用同一份）。
 *  默认水平方向比正文内收 6px（容器 padding 22px vs 正文 16px）；折叠区里要跟同组
 *  工具行的 ✓ 左缘对齐，所以给一个顶格变体（compact：左右内边距归零），而不是去改
 *  主流那套缩进——两处外观要求不同，用变体而不是复制一份。 */
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

/** 过程组的一帧画得出来吗（与 ProcessEntryList 的渲染分支一一对应）：帧要么是
 *  工具行、要么是 diff，两者都不是的帧在列表里只是一条空条目。把它挡在分组之前，
 *  「项数提示」与「渲染用的条目数组」才是同一份——C3：hint 的项数由
 *  buildToolLineGroups 从它收到的这份 frames 数出（`entries.length`），而渲染用的
 *  就是同一个数组，不再有第二条各滤各的路。 */
function rendersFrame(frame: ChatMessage): boolean {
  return Boolean(frame.tool || frame.diff);
}

/** 过程组的条目列表：落带帧（工具行按行、diff 按块，同一套组件不重画）+ 活动树里
 *  帧还没有的行（在跑的工具：显示 ◌，跑完的先由帧接管、不会重复）。
 *  diff 用顶格变体，与同组工具行左缘对齐；树行保留 depth 缩进（那是树的形状）。 */
function ProcessEntryList({ entries }: { entries: ProcessEntry[] }) {
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
                elapsed={
                  typeof entry.frame.tool.duration_ms === "number" && entry.frame.tool.duration_ms > 0
                    ? formatToolDuration(entry.frame.tool.duration_ms)
                    : ""
                }
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

/** 展开面板的固定上限高度：超出部分在面板内部滚动。子智能体的过程条目与过程输出
 *  可能很长，让它无限撑高会把下面的内容与输入区顶出屏幕。 */
const EXPAND_PANEL_MAX_HEIGHT = 240;

/** 聊天流内的工具行：✓/✗（运行中 ◌）内联一行，插在正文段落之间（CLI 同构）。
 *  delegate 行的展开面板是「任务指令 / 过程 / 最终结果」三组——「过程」组里的
 *  活动树补给行不含子智能体自己那一行（那就是本条工具行本身），重复没有信息量。 */
const ToolLine = memo(function ToolLine({
  tool,
  marginTop,
}: {
  tool: ChatToolLine;
  marginTop: number;
}) {
  const ok = tool.ok !== false;
  const elapsed =
    typeof tool.duration_ms === "number" && tool.duration_ms > 0
      ? formatToolDuration(tool.duration_ms)
      : "";
  const callId = String(tool.tool_call_id ?? "");
  const rows = useStore((s) => s.subagentRows);
  const output = useStore((s) => (callId ? s.subagentOutput[callId] : undefined));
  const [open, setOpen] = useState(false);

  const running = useMemo(() => toolRunning(rows, callId), [rows, callId]);
  const work = useMemo(() => (callId ? workRowsOf(rows, callId) : []), [rows, callId]);
  // 子智能体自己产生的工具行 / diff（服务端按 parent_tool_call_id 归集到本条行）：
  // 它们不进主消息流，只在这条 delegate 行的「过程」组里，与过程正文并列。
  const subFrames = useStore((s) => (callId ? s.subagentDiffs[callId] : undefined));
  // 过程组内按 view_seq 排（store 归集时已排过序，这里再稳一次：展示顺序不能取决于
  // 到达/合并顺序）。无序号帧留在原相对位置。画不出来的帧（既非工具行也非 diff）
  // 在这里就挡掉：项数提示与列表渲染共用这一份数组（见 rendersFrame）。
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
  // 分组（组序/计数/默认展开态/哪组算有内容）在纯模块里判，组件只管画；
  // 空组不出现＝一组都没有就不可展开（与旧面板「全空不可展开」同一判据）。
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
  const expandable = groups.length > 0;

  return (
    <div style={{ marginTop, display: "flex", flexDirection: "column", alignItems: "flex-start" }}>
      <ToolLineRow
        label={tool.label}
        ok={ok}
        running={running}
        elapsed={elapsed}
        expandable={expandable}
        open={open}
        onToggle={() => setOpen((v) => !v)}
      />
      {open && expandable ? (
        <ToolLinePanel>
          <div style={{ maxHeight: EXPAND_PANEL_MAX_HEIGHT, overflowY: "auto" }}>
            <ToolLineAccordion
              groups={groups}
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
          // 输出端不显示 spinner：assistant 首 token 前的空态由状态栏
          // 「运行状态 typing...」承担（MessageBubble 对空文本 streaming 直接不渲染）
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

/** Single message bubble — picks the right memoized sub-component.
 *  块级间距模型（对齐手机端）：与前一条同说话方贴紧（4px），跨说话方拉开（16px）；
 *  分隔线作为时间线界标参与时改用专用净间距（线到两侧视觉内容等距）。 */
const MessageBubble = memo(
  function MessageBubble({ msg, prevMsg }: { msg: ChatMessage; prevMsg?: ChatMessage }) {
    const isUser = msg.role === "user";
    // 前一条是分隔线：不用说话方 4/16 规则，而用分隔线专用净间距
    // （当前消息无视觉边界时其透明 padding 在线下方，同样要压缩补差）。
    const gap = prevMsg
      ? prevMsg.dividerLabel
        ? dividerAdjacentGap(msg)
        : prevMsg.role === msg.role
          ? 4
          : 16
      : 0;
    if (msg.dividerLabel) {
      // 分隔线自身：上侧净间距看前一条有无视觉边界；下侧由后一条接管。
      // 时间标签与主标签融合一行（手机端同款：「新会话  09:12」）。
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
    // 工具行（✓ tool(...)）：聊天流内联一行，插在正文段落之间（CLI 同构）。
    // delegate 行同样在这里原位渲染（它与本轮正文的先后顺序就是真实发生顺序）；
    // 展开看该子智能体的过程（落带帧 + 在跑的行 + 过程输出）。
    // 例外：`delegate wait` 是同步点不是工作（一回合能出现几十次），与活动树同规则
    // 隐掉不画——只跳过渲染，行仍在 store 里参与顺序与对账（见 lib/toolVisibility）。
    if (msg.tool) {
      if (isHiddenToolLine(msg.tool)) return null;
      return <ToolLine tool={msg.tool} marginTop={gap} />;
    }
    // 代码 diff 块：不占正文气泡，作为聊天流独立内容块渲染（与折叠区共用 DiffBlock）。
    if (msg.diff) {
      return <DiffBlock diff={msg.diff} marginTop={gap} />;
    }
    // 被撤回的行（chat_turn_retracted）：只隐藏、不删——删行会让它后面的每一行
    // 上移一格（已显示前缀不变式，见 docs/消息渲染契约.md）。
    if (msg.retracted) return null;
    // 输出端不显示 spinner：assistant 首 token 前的空态由状态栏
    // 「运行状态 typing...」指示，这里直接不渲染气泡。
    // 例外：回合锁排队中（其他前端占着回合）——显示排队占位 给用户明确反馈。
    // 空 assistant 行一律不画（隐藏行不占位、不参与间距）：排队占位在回合结束时
    // 只清标记、不删行，靠这一条从屏上消失。
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
    // Only re-render if the message content or streaming state changed.
    // This is critical: without this, every streaming chunk would re-render
    // ALL messages in the list, not just the one being appended to.
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

/**
 * 消息列表 — 对齐 coara app 设计规范：
 * - 无头像（靠气泡颜色和位置区分）
 * - 助手气泡无视觉边界（与背景同色，占满宽度）
 * - 用户气泡单色灰阶（var(--coara-bubble-user)），靠右
 * - 字号 15/20（正文），统一阅读节奏
 *
 * Performance optimizations:
 * - Each message is wrapped in React.memo so streaming chunks only re-render
 *   the active message, not the entire list.
 * - Only the last MAX_RENDERED_MESSAGES messages are rendered to keep DOM
 *   size bounded for long sessions.
 * - Scroll-to-bottom is throttled during streaming to avoid layout thrashing.
 */
/** 每个空间的滚动锚：本页面生命内「切走时留在哪儿，切回来就停在哪儿」。
 *  记的是「贴底 / 某条消息 + 相对视口顶的偏移」，不是绝对像素（见 ScrollAnchor）：
 *  切回来那一刻按那条消息的实际位置反推 scrollTop，内容长高长矮都停在同一条上。
 *  刻意只放内存（**不落 sessionStorage**）：刷新是新的一次页面生命，几何与内容都
 *  重来过，套用上一次生命留下的位置正是「刷新后停在最上方」的成因；刷新一律贴底
 *  （见 lib/chatScroll.ts::decideScroll）。 */
const spaceScrollAnchors = new Map<string, ScrollAnchor>();

/** 本页面生命是否已经提交过一次权威内容：false＝刷新/首次进入（贴底），
 *  true 之后的空间切换才允许还原锚点。 */
let pageLifeCommitted = false;

/** 骨架占位：权威快照未落定（viewReady=false）且还没有消息时渲染。
 *  为什么不是「空态欢迎卡」：快照未到就上屏等于先给一份可能不对的画面，卡片
 *  闪一下又被真实内容换掉——正是设计体系 0.1 禁止的 A 跳 B。形状照真实消息行
 *  （用户行靠右窄、助手行靠左宽），尺寸接近，落定前后高度变化最小。 */
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

export function MessageList() {
  const messages = useStore((s) => s.messages);
  const workspaceDir = useStore((s) => s.workspaceDir);
  const viewReady = useStore((s) => s.viewReady);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const scrollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scrollRafRef = useRef<number | null>(null);
  // 是否跟随底部：向上翻看历史时暂停自动滚底（新消息不打扰阅读），
  // 手动滚回底部附近后恢复跟随。仅 ref 驱动节流 effect，无需重渲染。
  const stickToBottomRef = useRef(true);

  const handleScroll = () => {
    const container = scrollContainerRef.current;
    if (!container) return;
    const anchor = measureScrollAnchor(container);
    stickToBottomRef.current = anchor.kind === "bottom";
    // 顺手把当前位置记进本空间的锚（只在本页面生命内有效）：切走那一刻读到的
    // 就是「原来的样子」，切回来照它还原。
    if (workspaceDir) {
      spaceScrollAnchors.set(workspaceDir, anchor);
    }
  };

  // 滚动落点：刷新贴底、切空间还原锚点、骨架换真实内容重贴底——判据在
  // lib/chatScroll.ts（纯函数，可断言），这里只管时序：
  //   · 锚点还原：layout 阶段同步落位一次；内容锚再补一帧 rAF（markdown / KaTeX /
  //     图片撑高会改落点）。目标消息还没进 DOM（骨架期）就把锚留着，等它到了再落位。
  //   · bottom：layout 阶段先同步滚一次，随后一帧 rAF 再对一次，高度还在变再补一帧。
  //   · hold：什么都不做（用户在翻历史）。
  // 这里是**唯一**会因「切空间 / 刷新 / 骨架转内容」改滚动位置的地方：实时帧到达
  // 不经过本 effect（deps 只有 workspaceDir 与 viewReady），滚动位置因此不被帧动过。
  const prevDirRef = useRef<string | null>(null);
  const prevReadyRef = useRef(false);
  /** 切空间时挂起的待还原锚：无快照的切空间路径分两次提交（先骨架后内容），第一次
   *  提交时锚定那条消息还不在 DOM 里——锚留到它进 DOM 再落位。也正是它压住第二次
   *  提交的「骨架转内容贴底」，否则翻着历史切回来会被拽到底（旧实现靠 React 批处理
   *  侥幸正确，脆性耦合）。 */
  const pendingAnchorRef = useRef<ScrollAnchor | null>(null);
  useLayoutEffect(() => {
    const dirChanged = prevDirRef.current !== workspaceDir;
    const hadDir = prevDirRef.current !== null;
    const becameReady = viewReady && !prevReadyRef.current;
    prevDirRef.current = workspaceDir;
    prevReadyRef.current = viewReady;
    const el = scrollContainerRef.current;
    if (workspaceDir === null || el === null) return;

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
        // 一行都还量不到：内容没到（骨架期）就留着锚等下一提交；已经就绪却仍没有行
        // ⇒ 这个锚已失效（那条消息不在这条线里了），退回贴底。
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

  // Cap to MAX_RENDERED_MESSAGES to keep DOM size bounded for long sessions.
  // System messages (<系统消息> etc.) are already filtered by the backend
  // in _handle_session_messages, so no frontend filter is needed here.
  const visibleMessages = useMemo(() => {
    if (messages.length > MAX_RENDERED_MESSAGES) {
      return messages.slice(messages.length - MAX_RENDERED_MESSAGES);
    }
    return messages;
  }, [messages]);

  // Throttled scroll-to-bottom: during streaming, chunks arrive at high
  // frequency. Calling scrollIntoView on every chunk causes severe layout
  // thrashing. Instead, batch scroll calls to once per 150ms.
  // 用户在上方阅读历史（内容锚 / stickToBottom=false）时跳过，避免被拉回底部。
  // 「贴底锚继续贴底」不是「改滚动位置」：位置没变（还是底部），新内容照常跟手。
  // 回合状态行在 ChatView 中常驻占位（见 TurnSpinner），不改变本列表几何，
  // 因此无需监听 turnActive——发送/流式一次滚底即稳定到位，无二次跳动。
  useEffect(() => {
    if (!stickToBottomRef.current) return;
    if (scrollTimerRef.current) return;
    scrollTimerRef.current = setTimeout(() => {
      scrollTimerRef.current = null;
      // Re-read scrollHeight after the next paint: markdown/KaTeX rendering
      // can grow the container after the timeout fires, which would
      // otherwise leave the final scroll short of the bottom.
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

  // 本端发送即回底：乐观泡是**本端用户的显式动作**（唯一允许定滚动的动作之一），
  // 即使此前上翻过历史（内容锚）也恢复跟随并滚到底部，确保自己刚发的消息与随后的
  // 回复立即可见。
  //
  // 除此之外，**任何 WS 帧到达都不改滚动位置**：他端落下的分隔线（模型切换 / 新
  // 会话）、后台任务完成提示来了，都只是内容变多——用户正在读的那条（内容锚）一动
  // 不动。旧实现在这里见了分隔线就无条件强制回底，他端一落线就把正在翻历史的用户
  // 拽到底，正是这一条要治的病；「点新会话」本端动作的回底由上面的 viewReady
  // （skeletonToContent）负责，不靠这一条。
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
      onScroll={handleScroll}
      style={{
        height: "100%",
        overflowY: "auto",
        // 底部留白小而有呼吸：回合状态行（TurnSpinner）在列表外常驻占位，
        // 不随回合开始/结束改变本列表几何，滚底一次到位即可。
        padding: "20px 20px 12px",
        maxWidth: 1200,
        margin: "0 auto",
        width: "100%",
        // 顶部渐隐遮罩：滚上去的消息柔和淡出，而不是在窗口上沿被硬截断
        WebkitMaskImage: "linear-gradient(to bottom, transparent 0, black 28px)",
        maskImage: "linear-gradient(to bottom, transparent 0, black 28px)",
      }}
    >
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
        // 隐藏行（delegate wait / send_file）不画、也不参与间距计算：间距必须按
        // 「可见前一条」算，否则「用户泡 → 隐藏工具行 → 助手泡」会按 4px 贴紧。
        .filter((m) => !(m.tool && isHiddenToolLine(m.tool)))
        .map((msg, i, rows) => (
          // 外层只作「锚点量尺」：data-msg-key 让滚动锚按服务端键定位（seq /
          // tool_call_id / client_msg_id）——对账只改内容不换键，锚与 React key
          // 因此在整页生命里都稳定（见 store.ts::chatRowKey）。
          <div key={chatRowKey(msg)} data-msg-key={chatRowKey(msg)}>
            <MessageBubble
              msg={msg}
              prevMsg={i > 0 ? rows[i - 1] : undefined}
            />
          </div>
        ))}
    </div>
  );
}
