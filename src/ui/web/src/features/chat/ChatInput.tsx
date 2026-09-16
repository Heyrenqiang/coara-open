import { useState, useEffect, useRef, useMemo, type KeyboardEvent, type ClipboardEvent, type DragEvent } from "react";
import { message } from "antd";
import {
  FileImageOutlined,
  FilePdfOutlined,
  FileWordOutlined,
  LoadingOutlined,
  PaperClipOutlined,
} from "@ant-design/icons";
import {
  fetchCommandList,
  uploadFile,
  type CommandInfo,
  type SlashPickerOptionInfo,
} from "../../lib/api";
import {
  buildSlashCompletions,
  slashCancelStem,
  type SlashCompletionRow,
} from "./slashCompletions";

interface ChatInputProps {
  onSend: (text: string, imageRefs?: string[], fileRefs?: string[], attachments?: Attachment[]) => void;
  onCommand: (text: string) => void;
  disabled: boolean;
}

/** Pending attachment shown as a chip above the textarea. */
export interface Attachment {
  ref: string;
  filename: string;
  kind: AttachmentKind;
  size: number;
}

const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"]);

/** 输入草稿持久化键：切模块/刷新后恢复未发送内容（对话页输入体验）。 */
const CHAT_DRAFT_KEY = "coara.chat.draft";
const TEXT_EXTS = new Set([
  "txt",
  "md",
  "markdown",
  "py",
  "js",
  "ts",
  "tsx",
  "jsx",
  "json",
  "yaml",
  "yml",
  "toml",
  "csv",
  "xml",
  "html",
  "css",
  "rs",
  "go",
  "java",
  "c",
  "h",
  "cpp",
  "hpp",
  "sh",
  "bat",
  "ps1",
  "sql",
  "log",
]);

const OFFICE_EXTS = new Set(["docx", "xlsx", "pptx"]);
const PDF_EXTS = new Set(["pdf"]);
/** 模型无法直接读文本的纯二进制（压缩/可执行/数据库等），上传后给路径提示。 */
const BINARY_EXTS = new Set([
  "zip", "rar", "7z", "tar", "gz",
  "exe", "dll", "so", "db", "sqlite",
  "mp4", "webm", "mov", "mp3", "wav", "ogg", "m4a",
]);

function fileExt(name: string): string {
  return name.split(".").pop()?.toLowerCase() ?? "";
}

function isImageFile(file: File): boolean {
  if (file.type.startsWith("image/")) return true;
  return IMAGE_EXTS.has(fileExt(file.name));
}

function isTextFile(file: File): boolean {
  if (file.type.startsWith("text/")) return true;
  if (file.type === "application/json" || file.type === "application/xml") return true;
  return TEXT_EXTS.has(fileExt(file.name));
}

/** 附件语义类别：决定后端如何内联（文本/Office/PDF 提取文本，二进制给路径）。 */
type AttachmentKind = "image" | "text" | "office" | "pdf" | "binary";

function attachmentKind(file: File): AttachmentKind | null {
  const ext = fileExt(file.name);
  if (isImageFile(file)) return "image";
  if (PDF_EXTS.has(ext) || file.type === "application/pdf") return "pdf";
  if (OFFICE_EXTS.has(ext)) return "office";
  if (BINARY_EXTS.has(ext)) return "binary";
  if (isTextFile(file)) return "text";
  // 未识别的按文本尝试（服务端解码失败会降级为路径提示）
  return "text";
}

/** Files dragged from the OS file manager (not in-page drags like text). */
function hasDraggedFiles(e: DragEvent): boolean {
  return Array.from(e.dataTransfer.types).includes("Files");
}

export function ChatInput({ onSend, onCommand, disabled }: ChatInputProps) {
  const [text, setText] = useState(() => {
    try {
      return localStorage.getItem(CHAT_DRAFT_KEY) ?? "";
    } catch {
      return "";
    }
  });
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [commands, setCommands] = useState<CommandInfo[]>([]);
  const [pickers, setPickers] = useState<Record<string, SlashPickerOptionInfo[]>>({});
  const [pickerCommands, setPickerCommands] = useState<string[]>([]);
  const [completionIdx, setCompletionIdx] = useState(0);
  const [menuOpen, setMenuOpen] = useState(true);
  const [dragActive, setDragActive] = useState(false);
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const dragDepthRef = useRef(0);
  /** 进行中的上传 Promise；发送时 await 它，避免图片/文件漏带到下一轮。 */
  const pendingUploadRef = useRef<Promise<void> | null>(null);

  // 草稿持久化：组件卸载（切模块）或刷新后恢复输入内容。
  useEffect(() => {
    try {
      localStorage.setItem(CHAT_DRAFT_KEY, text);
    } catch {
      /* localStorage 不可用时静默降级为内存态 */
    }
  }, [text]);

  const refreshCommands = () => {
    fetchCommandList()
      .then((data) => {
        setCommands(data.commands || []);
        setPickers(data.pickers || {});
        setPickerCommands(data.picker_commands || Object.keys(data.pickers || {}));
      })
      .catch(() => {});
  };

  useEffect(() => {
    refreshCommands();
  }, []);

  const completionMatches = useMemo<SlashCompletionRow[]>(
    () => buildSlashCompletions(text, commands, pickers, pickerCommands),
    [text, commands, pickers, pickerCommands],
  );

  const showCompletion = menuOpen && completionMatches.length > 0 && completionMatches.length < 80;

  // Keep highlight in range; default to first row when the menu reshapes (CLI-like).
  useEffect(() => {
    if (!showCompletion) return;
    setCompletionIdx((i) => {
      if (completionMatches.length === 0) return 0;
      if (i < 0 || i >= completionMatches.length) return 0;
      return i;
    });
  }, [showCompletion, completionMatches.length, text]);

  const clearInput = () => {
    setText("");
    setAttachments([]);
    setCompletionIdx(0);
    setMenuOpen(true);
    pendingUploadRef.current = null;
  };

  const handleSend = async () => {
    const trimmed = text.trim();
    if (!trimmed) return;
    // 发送前等未完成的上传落地，避免图片/文件被异步上传拖到下一轮。
    // uploadFiles 只 setAttachments（state 异步），而 handleSend 立即读
    // attachments 会拿到过期空值——await pending 后 state 已更新。
    const pending = pendingUploadRef.current;
    if (pending) {
      try {
        await pending;
      } catch {
        /* 上传失败由 uploadFiles 内部提示 */
      }
    }
    if (trimmed.startsWith("/")) {
      onCommand(trimmed);
    } else {
      const imageRefs = attachments.filter((a) => a.kind === "image").map((a) => a.ref);
      const fileRefs = attachments.filter((a) => a.kind !== "image").map((a) => a.ref);
      onSend(
        trimmed,
        imageRefs.length > 0 ? imageRefs : undefined,
        fileRefs.length > 0 ? fileRefs : undefined,
        attachments.length > 0 ? attachments : undefined,
      );
    }
    clearInput();
  };

  const fillCompletion = (idx: number) => {
    const match = completionMatches[idx];
    if (!match) return;
    // Tab 只补全到输入端，不触发命令（命令需 Enter 发送后才执行）。
    setText(`${match.insert} `);
    setCompletionIdx(0);
    setMenuOpen(true);
    inputRef.current?.focus();
  };

  const acceptCompletion = (idx: number) => {
    const match = completionMatches[idx];
    if (!match) return;
    if (match.submit) {
      onCommand(match.insert);
      clearInput();
      refreshCommands();
      return;
    }
    fillCompletion(idx);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (showCompletion) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setCompletionIdx((i) => (i + 1) % completionMatches.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setCompletionIdx((i) => (i - 1 + completionMatches.length) % completionMatches.length);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        const stem = slashCancelStem(text, pickerCommands);
        if (stem) {
          setText(stem);
          setCompletionIdx(0);
          return;
        }
        setMenuOpen(false);
        return;
      }
      if (e.key === "Tab") {
        e.preventDefault();
        fillCompletion(completionIdx >= 0 ? completionIdx : 0);
        return;
      }
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        // CLI: apply highlighted row; submit when the line is a picker action.
        acceptCompletion(completionIdx >= 0 ? completionIdx : 0);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const uploadFiles = async (files: File[]) => {
    if (files.length === 0) return;
    setUploading(true);
    let failed = 0;
    let rejected = 0;
    try {
      for (const file of files) {
        const kind = attachmentKind(file);
        if (!kind) {
          rejected += 1;
          continue;
        }
        try {
          const result = await uploadFile(file);
          if (result.files.length > 0) {
            const uploaded = result.files[0];
            setAttachments((prev) => [
              ...prev,
              { ref: uploaded.ref, filename: uploaded.filename, kind, size: uploaded.size },
            ]);
          }
        } catch (err) {
          failed += 1;
          console.error(`上传失败 (${file.name}):`, err);
        }
      }
    } finally {
      setUploading(false);
    }
    if (rejected > 0) {
      message.warning(`${rejected} 个文件类型不支持`);
    }
    if (failed > 0) {
      message.error(`${failed} 个文件上传失败`);
    }
  };

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    const p = uploadFiles(files);
    pendingUploadRef.current = p;
    await p;
  };

  /** 剪贴板里的位图没有文件名，浏览器统一给 image.png / blob，连贴几张就成了
   *  image_1.png、image_2.png 这种无信息量的递增名（后端避碰撞加的后缀）。
   *  这里换成带时间戳的名字：既唯一，又能和「什么时候贴的」对上。
   *  从资源管理器复制的文件带真实文件名，原样保留。 */
  const namePastedFile = (file: File, seq: number): File => {
    const name = file.name || "";
    const ext = (name.match(/\.([a-z0-9]+)$/i)?.[1] ?? "").toLowerCase();
    const stem = name.replace(/\.[a-z0-9]+$/i, "");
    if (name && !/^(image|blob|clipboard)([-_ ]?\d+)?$/i.test(stem)) return file;
    const now = new Date();
    const pad = (n: number) => String(n).padStart(2, "0");
    const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
    const fromType = (file.type.split("/")[1] ?? "").toLowerCase();
    const suffix = IMAGE_EXTS.has(ext) ? ext : fromType === "jpeg" ? "jpg" : fromType || "png";
    // 时间戳只到秒：同一秒里连贴几张会撞名，再加序号保证唯一。
    return new File([file], `粘贴图片-${stamp}-${seq + 1}.${suffix}`, {
      type: file.type,
      lastModified: file.lastModified,
    });
  };

  const handlePaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const items = Array.from(e.clipboardData?.items ?? []);
    const files = items
      .filter((item) => item.kind === "file")
      .map((item) => item.getAsFile())
      .filter((f): f is File => f !== null)
      .map((f, i) => namePastedFile(f, i));
    if (files.length === 0) return;
    e.preventDefault();
    const p = uploadFiles(files);
    pendingUploadRef.current = p;
    void p;
  };

  const handleDragEnter = (e: DragEvent<HTMLDivElement>) => {
    if (!hasDraggedFiles(e)) return;
    e.preventDefault();
    dragDepthRef.current += 1;
    setDragActive(true);
  };

  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    if (!hasDraggedFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  };

  const handleDragLeave = (e: DragEvent<HTMLDivElement>) => {
    if (!hasDraggedFiles(e)) return;
    dragDepthRef.current -= 1;
    if (dragDepthRef.current <= 0) {
      dragDepthRef.current = 0;
      setDragActive(false);
    }
  };

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    if (!hasDraggedFiles(e)) return;
    e.preventDefault();
    dragDepthRef.current = 0;
    setDragActive(false);
    const files = Array.from(e.dataTransfer.files ?? []);
    const p = uploadFiles(files);
    pendingUploadRef.current = p;
    void p;
  };

  return (
    <div
      style={{
        borderTop: "1px solid var(--coara-border-muted)",
        background: "var(--coara-surface)",
      }}
    >
      <div
        style={{
          maxWidth: 1200,
          margin: "0 auto",
          width: "100%",
          padding: "16px 20px",
        }}
      >
        <div
          onDragEnter={handleDragEnter}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          style={{
            position: "relative",
            border: dragActive ? "2px dashed var(--coara-accent)" : "1px solid var(--coara-border-muted)",
            borderRadius: "12px",
            background: dragActive ? "var(--coara-accent-subtle)" : "var(--coara-bg-subtle)",
            padding: "12px",
            display: "flex",
            flexDirection: "column",
            gap: "8px",
            transition: "all 0.15s ease",
            boxShadow: dragActive ? "var(--coara-focus-ring)" : "var(--coara-shadow-xs)",
          }}
        >
          {showCompletion && (
            <div
              style={{
                position: "absolute",
                bottom: "100%",
                left: 0,
                right: 0,
                marginBottom: 4,
                background: "var(--coara-surface)",
                border: "1px solid var(--coara-border-muted)",
                borderRadius: 10,
                boxShadow: "var(--coara-shadow-pop)",
                maxHeight: 320,
                overflowY: "auto",
                zIndex: 50,
                padding: 4,
              }}
            >
              {completionMatches.map((row, idx) => {
                const active = idx === completionIdx;
                const prev = idx > 0 ? completionMatches[idx - 1] : null;
                const showCategory =
                  row.kind === "command" &&
                  (!prev || prev.kind !== "command" || prev.category !== row.category);
                return (
                  <div key={`${row.kind}:${row.insert}:${row.meta}:${idx}`}>
                    {showCategory && row.kind === "command" && (
                      <div
                        style={{
                          padding: "6px 10px 2px",
                          fontSize: 11,
                          color: "var(--coara-text-tertiary)",
                          letterSpacing: 0.4,
                          textTransform: "uppercase",
                          fontWeight: 600,
                        }}
                      >
                        {row.category}
                      </div>
                    )}
                    <div
                      onMouseDown={(e) => {
                        e.preventDefault();
                        acceptCompletion(idx);
                      }}
                      onMouseEnter={() => setCompletionIdx(idx)}
                      style={{
                        display: "flex",
                        alignItems: "baseline",
                        gap: 10,
                        padding: "7px 10px",
                        borderRadius: 6,
                        cursor: "pointer",
                        background: active ? "var(--coara-accent-subtle)" : "transparent",
                        transition: "background 0.12s ease",
                      }}
                    >
                      <span
                        style={{
                          fontFamily:
                            "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                          fontSize: 13,
                          color: active ? "var(--coara-accent)" : "var(--coara-text-strong)",
                          fontWeight: 600,
                          minWidth: row.kind === "option" ? 100 : 120,
                          maxWidth: row.kind === "option" ? 220 : undefined,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {row.display}
                      </span>
                      <span
                        style={{
                          fontSize: 12.5,
                          color: "var(--coara-text-secondary)",
                          flex: 1,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {row.meta}
                      </span>
                    </div>
                  </div>
                );
              })}
              <div
                style={{
                  padding: "4px 10px 2px",
                  fontSize: 11,
                  color: "var(--coara-text-tertiary)",
                }}
              >
                ↑↓ 选择 · Enter 发送 · Tab 补全 · Esc 取消
              </div>
            </div>
          )}

          {attachments.length > 0 && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {attachments.map((att, i) => (
                <span
                  key={i}
                  style={{
                    padding: "4px 10px",
                    background: "var(--coara-bg-subtle)",
                    border: "1px solid var(--coara-border-muted)",
                    borderRadius: 6,
                    fontSize: 12,
                    color: "var(--coara-text-strong)",
                    display: "flex",
                    alignItems: "center",
                    gap: 4,
                  }}
                >
                  {att.kind === "image" ? (
                    <FileImageOutlined />
                  ) : att.kind === "pdf" ? (
                    <FilePdfOutlined />
                  ) : att.kind === "office" ? (
                    <FileWordOutlined />
                  ) : (
                    <PaperClipOutlined />
                  )}{" "}
                  {att.filename}
                  <button
                    onClick={() => setAttachments((prev) => prev.filter((_, idx) => idx !== i))}
                    style={{
                      background: "none",
                      border: "none",
                      cursor: "pointer",
                      color: "var(--coara-text-tertiary)",
                      padding: 0,
                      fontSize: 14,
                      lineHeight: 1,
                    }}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}

          <textarea
            ref={inputRef}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              setMenuOpen(true);
            }}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            placeholder={
              disabled
                ? attachments.some((att) => att.kind === "image")
                  ? "带图消息将排队到下一回合... (Shift+Enter 换行)"
                  : "回合进行中 发送后将排队或注入当前回合... (Shift+Enter 换行)"
                : "输入消息... (Shift+Enter 换行，/ 命令可 ↑↓ 选择，可拖拽或粘贴文件/图片)"
            }
            style={{
              width: "100%",
              minHeight: "60px",
              maxHeight: "200px",
              border: "none",
              outline: "none",
              resize: "none",
              fontSize: 15,
              lineHeight: 1.6,
              fontFamily: "inherit",
              background: "transparent",
              color: "var(--coara-text)",
              transition: "color 0.15s ease",
            }}
            rows={3}
          />

          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            <div style={{ display: "flex", gap: 8 }}>
              <label
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  width: 32,
                  height: 32,
                  borderRadius: "50%",
                  background: "var(--coara-bg-subtle)",
                  cursor: "pointer",
                  transition: "background 0.2s",
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = "var(--coara-border-muted)")}
                onMouseLeave={(e) => (e.currentTarget.style.background = "var(--coara-bg-subtle)")}
              >
                <input
                  type="file"
                  accept="image/*,.pdf,.docx,.xlsx,.pptx,.zip,.txt,.md,.py,.js,.ts,.tsx,.jsx,.json,.yaml,.yml,.toml,.csv,.xml,.html,.css,.rs,.go,.java,.c,.h,.cpp,.sh,.sql,.log"
                  onChange={handleUpload}
                  style={{ display: "none" }}
                />
                {uploading ? (
                  <LoadingOutlined
                    style={{ fontSize: 15, color: "var(--coara-text-secondary)" }}
                    spin
                  />
                ) : (
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
                  </svg>
                )}
              </label>
            </div>

            <button
              onClick={handleSend}
              disabled={!text.trim()}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                width: 36,
                height: 36,
                borderRadius: "50%",
                background: text.trim() ? "var(--coara-text)" : "var(--coara-border-muted)",
                border: "none",
                cursor: text.trim() ? "pointer" : "not-allowed",
                transition: "all 0.15s ease",
                transform: "scale(1)",
                boxShadow: text.trim() ? "var(--coara-shadow-btn)" : "none",
              }}
              onMouseEnter={(e) => {
                if (text.trim()) {
                  e.currentTarget.style.background = "var(--coara-text-active)";
                  e.currentTarget.style.transform = "scale(1.05)";
                  e.currentTarget.style.boxShadow = "var(--coara-shadow-btn-hover)";
                }
              }}
              onMouseLeave={(e) => {
                if (text.trim()) {
                  e.currentTarget.style.background = "var(--coara-text)";
                  e.currentTarget.style.transform = "scale(1)";
                  e.currentTarget.style.boxShadow = "var(--coara-shadow-btn)";
                }
              }}
            >
              <svg
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                stroke={text.trim() ? "var(--coara-surface)" : "var(--coara-text-tertiary)"}
                strokeWidth="2"
              >
                <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" />
              </svg>
            </button>
          </div>

          {dragActive && (
            <div
              style={{
                position: "absolute",
                inset: 0,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                borderRadius: 10,
                background: "var(--coara-accent-wash)",
                color: "var(--coara-accent)",
                fontSize: 14,
                fontWeight: 600,
                pointerEvents: "none",
                zIndex: 40,
              }}
            >
              松开以上传文件 / 图片
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
