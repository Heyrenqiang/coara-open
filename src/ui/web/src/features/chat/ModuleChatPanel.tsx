/**
 * ModuleChatPanel — 模块级独立会话面板（通用高标准对话组件）。
 *
 * 一套对话体验，处处复用：主对话页之外，任何 agentic 模块（工作流/配置/...）
 * 的浮动会话都用它。复用 ChatInput（斜杠命令/附件/拖拽/粘贴）与主对话的
 * markdown 渲染，按 WS subject 隔离独立会话主体与历史。
 */

import { useEffect, useRef } from "react";
import { getWS } from "../../lib/ws";
import { useStore } from "../../lib/store";
import { fetchModuleSessionMessages, uploadRawUrl } from "../../lib/api";
import type { ChatFileAttachment } from "../../lib/store";
import { ChatInput, type Attachment } from "../chat/ChatInput";
import { MarkdownMessage, UserMessage } from "../chat/MessageList";
import "./module-chat.css";

/** 距底部多少像素内视为「在底部」，恢复自动跟随。 */
const STICK_THRESHOLD = 48;

/** 把助手文本拆成 yield 行（✓/✗）与普通段落，便于分样式渲染。 */
function splitAssistantParts(text: string): Array<{ kind: "yield" | "prose"; text: string }> {
  const parts: Array<{ kind: "yield" | "prose"; text: string }> = [];
  let proseBuf: string[] = [];
  const flushProse = () => {
    if (proseBuf.length === 0) return;
    const t = proseBuf.join("\n").trim();
    if (t) parts.push({ kind: "prose", text: t });
    proseBuf = [];
  };
  for (const line of text.split("\n")) {
    const s = line.trimStart();
    if (s.startsWith("✓") || s.startsWith("✗")) {
      flushProse();
      parts.push({ kind: "yield", text: s });
    } else {
      proseBuf.push(line);
    }
  }
  flushProse();
  return parts;
}

interface ModuleChatPanelProps {
  /** WS subject / 历史 source 后缀（"flow" / "config" / ...）。 */
  subject: string;
  /** 空态引导语。 */
  emptyHint?: string;
}

export function ModuleChatPanel({
  subject,
  emptyHint,
}: ModuleChatPanelProps) {
  const session = useStore((s) => s.moduleSessions[subject]);
  const ensureModuleSession = useStore((s) => s.ensureModuleSession);
  const loadModuleHistory = useStore((s) => s.loadModuleHistory);
  const addModuleUserMessage = useStore((s) => s.addModuleUserMessage);
  const bottomRef = useRef<HTMLDivElement>(null);

  const messages = session?.messages ?? [];
  const turnActive = session?.turnActive ?? false;
  const scrollRef = useRef<HTMLDivElement>(null);
  // 跟随底部：向上翻看历史时暂停自动滚底，回到底部附近恢复（与主对话页一致）。
  const stickToBottomRef = useRef(true);

  // 加载历史（subject 变化时重读）。
  useEffect(() => {
    let cancelled = false;
    ensureModuleSession(subject);
    fetchModuleSessionMessages(subject, 500)
      .then((data) => {
        if (!cancelled && data.messages?.length) loadModuleHistory(subject, data.messages);
      })
      .catch((err) => console.error(`Failed to load module session (${subject}):`, err));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subject]);

  const handleScroll = () => {
    const container = scrollRef.current;
    if (!container) return;
    stickToBottomRef.current =
      container.scrollHeight - container.scrollTop - container.clientHeight < STICK_THRESHOLD;
  };

  useEffect(() => {
    if (!stickToBottomRef.current) return;
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  const sendChat = (text: string, imageRefs?: string[], fileRefs?: string[], attachments?: Attachment[]) => {
    if (!text.trim()) return;
    const chatAttachments: ChatFileAttachment[] | undefined = attachments?.map((a) => ({
      file_id: a.ref,
      url: uploadRawUrl(a.ref),
      filename: a.filename,
      mime: "",
      size: a.size,
      is_image: a.kind === "image",
    }));
    addModuleUserMessage(subject, text, chatAttachments);
    getWS().send({ type: "chat", text, subject, image_refs: imageRefs, file_refs: fileRefs });
  };

  const sendCommand = (text: string) => {
    if (!text.trim()) return;
    addModuleUserMessage(subject, text);
    getWS().send({ type: "command", text, subject });
  };

  return (
    <div className="flow-chat-panel">
      <div className="flow-chat-panel-scroll" ref={scrollRef} onScroll={handleScroll}>
        {messages.length === 0 && (
          <div className="flow-chat-empty">
            {emptyHint ?? "输入消息开始对话"}
          </div>
        )}
        {messages.map((m) =>
          m.role === "user" ? (
            <div key={m.id} style={{ marginBottom: 8, display: "flex", justifyContent: "flex-end" }}>
              <div className="chat-bubble-user">
                <UserMessage text={m.text} />
              </div>
            </div>
          ) : (
            <div key={m.id} style={{ marginBottom: 8, display: "flex", justifyContent: "flex-start" }}>
              <div className="chat-bubble-agent">
                {splitAssistantParts(m.text).map((part, i) =>
                  part.kind === "yield" ? (
                    <div
                      key={`${m.id}-y-${i}`}
                      className={`flow-chat-yield${part.text.startsWith("✗") ? " flow-chat-yield-err" : ""}`}
                    >
                      {part.text}
                    </div>
                  ) : (
                    <MarkdownMessage key={`${m.id}-p-${i}`} text={part.text} />
                  ),
                )}
              </div>
            </div>
          ),
        )}
        <div ref={bottomRef} />
      </div>
      <div className="flow-chat-panel-input">
        <ChatInput onSend={sendChat} onCommand={sendCommand} disabled={turnActive} />
      </div>
    </div>
  );
}
