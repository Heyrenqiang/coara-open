import { memo, useState, useEffect, useRef, useMemo, type KeyboardEvent, type ClipboardEvent, type DragEvent } from "react";
import { Image, message } from "antd";
import {
  FileImageOutlined,
  FilePdfOutlined,
  FileTextOutlined,
  FileWordOutlined,
  FolderOutlined,
  LoadingOutlined,
  PaperClipOutlined,
} from "@ant-design/icons";
import {
  fetchClipboardPaths,
  fetchCommandList,
  uploadFile,
  uploadRawUrl,
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

/** 附件语义类别：决定后端如何内联（文本/Office/PDF 提取文本，二进制给路径）。 */
type AttachmentKind = "image" | "text" | "office" | "pdf" | "binary";

/** Pending attachment shown as a chip above the textarea. */
export interface Attachment {
  ref: string;
  filename: string;
  kind: AttachmentKind;
  size: number;
  /** 图片附件的本地预览地址（blob:，粘贴/拖拽/选择即刻生成，无需等上传落地）。 */
  previewUrl?: string;
  /** 长文本粘贴缩略：首行预览（芯片上展示）。 */
  previewText?: string;
}

/** 长文本粘贴阈值：达到则收成附件芯片，不灌进输入框。 */
const LONG_TEXT_CHARS = 800;
const LONG_TEXT_LINES = 12;

function isLongPasteText(text: string): boolean {
  if (text.length >= LONG_TEXT_CHARS) return true;
  return text.split(/\r\n|\n|\r/).length >= LONG_TEXT_LINES;
}

function longTextPreview(text: string): string {
  const line = text.split(/\r\n|\n|\r/).find((l) => l.trim()) ?? text;
  const one = line.trim().replace(/\s+/g, " ");
  return one.length > 48 ? `${one.slice(0, 47)}…` : one;
}

function namePastedLongText(text: string): File {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return new File([text], `粘贴文本-${stamp}.txt`, {
    type: "text/plain",
    lastModified: Date.now(),
  });
}

/** 截图/位图粘贴：浏览器给的伪文件名，无 CF_HDROP，不应去打路径接口。 */
function looksLikeClipboardBitmap(file: File): boolean {
  const name = (file.name || "").trim().toLowerCase();
  const stem = name.replace(/\.[a-z0-9]+$/i, "");
  if (/^(image|blob|clipboard)([-_ ]?\d+)?$/i.test(stem) || stem === "") {
    return true;
  }
  // 部分浏览器贴图带 image/png 且无可靠文件名
  if (file.type.startsWith("image/") && !name.includes("/") && !name.includes("\\")) {
    if (/^img[-_]?\d*\.(png|jpe?g|gif|webp)$/i.test(name)) return true;
  }
  return false;
}

/** 拖入/粘贴目录时跳过的常见噪音目录。 */
const DROP_SKIP_DIRS = new Set([
  ".git",
  "node_modules",
  "__pycache__",
  ".venv",
  "venv",
  "dist",
  ".next",
  ".cache",
  ".turbo",
  "target",
]);
/** 目录清单最多写多少行（只进一个文本附件）。 */
const DROP_LISTING_MAX_LINES = 400;
/** 目录展开最大深度。 */
const DROP_MAX_DEPTH = 8;

type DataTransferItemEntry = DataTransferItem & {
  webkitGetAsEntry?: () => FileSystemEntry | null;
};

function readFileEntry(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => {
    entry.file(resolve, reject);
  });
}

function readAllDirectoryEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => {
    const all: FileSystemEntry[] = [];
    const pump = () => {
      reader.readEntries((batch) => {
        if (batch.length === 0) {
          resolve(all);
          return;
        }
        all.push(...batch);
        pump();
      }, reject);
    };
    pump();
  });
}

async function walkDirectoryListing(
  entry: FileSystemDirectoryEntry,
  prefix: string,
  lines: string[],
  state: { truncated: boolean },
  depth: number,
): Promise<void> {
  if (lines.length >= DROP_LISTING_MAX_LINES) {
    state.truncated = true;
    return;
  }
  if (depth > DROP_MAX_DEPTH) {
    state.truncated = true;
    if (lines.length < DROP_LISTING_MAX_LINES) lines.push(`${prefix}/…`);
    return;
  }
  const children = await readAllDirectoryEntries(entry.createReader());
  for (const child of children) {
    if (lines.length >= DROP_LISTING_MAX_LINES) {
      state.truncated = true;
      break;
    }
    const rel = `${prefix}/${child.name}`;
    if (child.isDirectory) {
      if (DROP_SKIP_DIRS.has(child.name)) continue;
      lines.push(`${rel}/`);
      await walkDirectoryListing(
        child as FileSystemDirectoryEntry,
        rel,
        lines,
        state,
        depth + 1,
      );
    } else if (child.isFile) {
      lines.push(rel);
    }
  }
}

/** 目录 → 单个文本附件（清单）；芯片上只显示目录名。 */
async function directoryEntryToListingFile(entry: FileSystemDirectoryEntry): Promise<{
  file: File;
  body: string;
  preview: string;
}> {
  const dirName = (entry.name || "folder").trim() || "folder";
  const lines: string[] = [`${dirName}/`];
  const state = { truncated: false };
  await walkDirectoryListing(entry, dirName, lines, state, 1);
  let body =
    `目录：${dirName}\n` +
    `（浏览器拖入无法提供绝对路径；需要本机路径请在资源管理器复制后粘贴。）\n\n` +
    lines.join("\n");
  if (state.truncated) {
    body += `\n…（清单已截断，最多 ${DROP_LISTING_MAX_LINES} 条）`;
  }
  const file = new File([body], `目录-${dirName}.txt`, {
    type: "text/plain",
    lastModified: Date.now(),
  });
  return { file, body, preview: dirName };
}

function isDirectoryListingAttachment(att: Attachment): boolean {
  return att.kind === "text" && att.filename.startsWith("目录-");
}

/**
 * 同步摘下 DataTransfer 的 FileSystemEntry（必须在 paste/drop 同步阶段调用）。
 */
function snapshotTransferEntries(data: DataTransfer | null): {
  directories: FileSystemDirectoryEntry[];
  files: FileSystemFileEntry[];
  fallbackFiles: File[];
} {
  const directories: FileSystemDirectoryEntry[] = [];
  const files: FileSystemFileEntry[] = [];
  const items = data?.items;
  if (items) {
    for (let i = 0; i < items.length; i++) {
      const entry = (items[i] as DataTransferItemEntry).webkitGetAsEntry?.() ?? null;
      if (!entry) continue;
      if (entry.isDirectory) directories.push(entry as FileSystemDirectoryEntry);
      else if (entry.isFile) files.push(entry as FileSystemFileEntry);
    }
  }
  const fallbackFiles = Array.from(data?.files ?? []).filter((f) => Boolean(f.name));
  return { directories, files, fallbackFiles };
}

const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"]);

/** 输入草稿持久化键：切模块/刷新后恢复未发送内容（对话页输入体验）。 */
const CHAT_DRAFT_KEY = "coara.chat.draft";
/** 附件草稿（已落盘 uploads 的元数据；不含 blob 预览）。 */
const CHAT_DRAFT_ATTACH_KEY = "coara.chat.draft.attachments";
/** 草稿落 localStorage 的防抖窗口。 */
const DRAFT_WRITE_DEBOUNCE_MS = 300;

function writeDraft(text: string): void {
  try {
    localStorage.setItem(CHAT_DRAFT_KEY, text);
  } catch {
    /* localStorage 不可用时静默降级为内存态 */
  }
}

function readDraftAttachments(): Attachment[] {
  try {
    const raw = localStorage.getItem(CHAT_DRAFT_ATTACH_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    const out: Attachment[] = [];
    for (const row of parsed) {
      if (!row || typeof row !== "object") continue;
      const rec = row as Record<string, unknown>;
      const ref = String(rec.ref || "").trim();
      const filename = String(rec.filename || "").trim();
      const kind = String(rec.kind || "").trim() as AttachmentKind;
      if (!ref || !filename || ref.startsWith("pending-")) continue;
      if (!["image", "text", "office", "pdf", "binary"].includes(kind)) continue;
      const size = Number(rec.size) || 0;
      const previewText = typeof rec.previewText === "string" ? rec.previewText : undefined;
      out.push({ ref, filename, kind, size, previewText });
    }
    return out;
  } catch {
    return [];
  }
}

function writeDraftAttachments(atts: Attachment[]): void {
  try {
    const rows = atts
      .filter((a) => a.ref && !a.ref.startsWith("pending-"))
      .map((a) => ({
        ref: a.ref,
        filename: a.filename,
        kind: a.kind,
        size: a.size,
        ...(a.previewText ? { previewText: a.previewText } : {}),
      }));
    if (rows.length === 0) {
      localStorage.removeItem(CHAT_DRAFT_ATTACH_KEY);
      return;
    }
    localStorage.setItem(CHAT_DRAFT_ATTACH_KEY, JSON.stringify(rows));
  } catch {
    /* ignore */
  }
}

function clearDraftAttachments(): void {
  try {
    localStorage.removeItem(CHAT_DRAFT_ATTACH_KEY);
  } catch {
    /* ignore */
  }
}
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

function loadDraftAttachments(): Attachment[] {
  return readDraftAttachments().map((a) =>
    a.kind === "image" ? { ...a, previewUrl: uploadRawUrl(a.ref) } : a,
  );
}

export const ChatInput = memo(function ChatInput({ onSend, onCommand, disabled }: ChatInputProps) {
  const [text, setText] = useState(() => {
    try {
      return localStorage.getItem(CHAT_DRAFT_KEY) ?? "";
    } catch {
      return "";
    }
  });
  // 与文本草稿同寿：刷新/切模块后恢复已落盘 uploads 附件（pending 不入盘）。
  const initialAttsRef = useRef<Attachment[] | null>(null);
  if (initialAttsRef.current === null) {
    initialAttsRef.current = loadDraftAttachments();
  }
  const [attachments, setAttachments] = useState<Attachment[]>(() => initialAttsRef.current!);
  // state 异步——发送路径 await 上传后不能信闭包里的 attachments，
  // 一律经 attachmentsRef 拿最新值；所有变更点必须走 applyAttachments。
  const attachmentsRef = useRef<Attachment[]>(initialAttsRef.current!);
  const applyAttachments = (updater: (prev: Attachment[]) => Attachment[]) => {
    const next = updater(attachmentsRef.current);
    attachmentsRef.current = next;
    setAttachments(next);
    writeDraftAttachments(next);
  };
  const [commands, setCommands] = useState<CommandInfo[]>([]);
  const [pickers, setPickers] = useState<Record<string, SlashPickerOptionInfo[]>>({});
  const [pickerCommands, setPickerCommands] = useState<string[]>([]);
  const [completionIdx, setCompletionIdx] = useState(0);
  const [menuOpen, setMenuOpen] = useState(true);
  const [dragActive, setDragActive] = useState(false);
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const dragDepthRef = useRef(0);
  /** 草稿防抖写：timer 与最新文本（卸载时补写用）。 */
  const draftTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const textRef = useRef(text);
  textRef.current = text;
  /** 进行中的上传 Promise；发送时 await 它，避免图片/文件漏带到下一轮。 */
  const pendingUploadRef = useRef<Promise<void> | null>(null);
  /** 长文本正文：乐观 id / 落盘 ref → 全文（展开到输入框用，免再拉网）。 */
  const pendingTextBodiesRef = useRef(new Map<string, string>());
  /** 用户已展开/删掉的乐观芯片：上传完成时勿再塞回附件栏。 */
  const skippedOptimisticRef = useRef(new Set<string>());

  // 草稿持久化：防抖写（每次击键都同步落 localStorage 会与流式渲染抢主线程），
  // 卸载（切模块）时把未落盘的那份补写出去，草稿不丢。
  useEffect(() => {
    if (draftTimerRef.current !== null) clearTimeout(draftTimerRef.current);
    draftTimerRef.current = setTimeout(() => {
      draftTimerRef.current = null;
      writeDraft(textRef.current);
    }, DRAFT_WRITE_DEBOUNCE_MS);
    return () => {
      if (draftTimerRef.current !== null) {
        clearTimeout(draftTimerRef.current);
        draftTimerRef.current = null;
      }
    };
  }, [text]);

  useEffect(
    () => () => {
      if (draftTimerRef.current !== null) {
        clearTimeout(draftTimerRef.current);
        draftTimerRef.current = null;
      }
      writeDraft(textRef.current);
    },
    [],
  );

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
    writeDraft("");
    applyAttachments((prev) => {
      prev.forEach((a) => {
        if (a.previewUrl?.startsWith("blob:")) URL.revokeObjectURL(a.previewUrl);
      });
      return [];
    });
    clearDraftAttachments();
    pendingTextBodiesRef.current.clear();
    skippedOptimisticRef.current.clear();
    setCompletionIdx(0);
    setMenuOpen(true);
    pendingUploadRef.current = null;
  };

  /** 长文本芯片 → 灌回输入框并卸芯片（可先改再发）。 */
  const expandTextAttachment = async (index: number) => {
    const att = attachmentsRef.current[index];
    if (!att || att.kind !== "text") return;
    let body = pendingTextBodiesRef.current.get(att.ref);
    if (!body && !att.ref.startsWith("pending-")) {
      try {
        const res = await fetch(uploadRawUrl(att.ref));
        if (res.ok) body = await res.text();
      } catch {
        /* fall through */
      }
    }
    if (body == null || body === "") {
      message.warning("无法展开该文本");
      return;
    }
    if (att.ref.startsWith("pending-")) {
      skippedOptimisticRef.current.add(att.ref);
    }
    pendingTextBodiesRef.current.delete(att.ref);
    const current = textRef.current;
    const next = current.trim() ? `${current.replace(/\s+$/, "")}\n\n${body}` : body;
    setText(next);
    applyAttachments((prev) => prev.filter((_, idx) => idx !== index));
    requestAnimationFrame(() => {
      const node = inputRef.current;
      if (!node) return;
      node.focus();
      const caret = next.length;
      node.setSelectionRange(caret, caret);
    });
  };

  const removeAttachmentAt = (index: number) => {
    applyAttachments((prev) => {
      const target = prev[index];
      if (target?.previewUrl?.startsWith("blob:")) URL.revokeObjectURL(target.previewUrl);
      if (target?.ref) {
        pendingTextBodiesRef.current.delete(target.ref);
        if (target.ref.startsWith("pending-")) {
          skippedOptimisticRef.current.add(target.ref);
        }
      }
      return prev.filter((_, idx) => idx !== index);
    });
  };

  const handleSend = async () => {
    const trimmed = text.trim();
    // 发送前等未完成的上传落地，避免图片/文件被异步上传拖到下一轮。
    // state 异步——await 后必须经 attachmentsRef 拿最新附件，闭包里的
    const pending = pendingUploadRef.current;
    if (pending) {
      try {
        await pending;
      } catch {
        /* 上传失败由 uploadFiles 内部提示 */
      }
    }
    const current = attachmentsRef.current;
    if (!trimmed && current.length === 0) return;
    if (trimmed.startsWith("/")) {
      onCommand(trimmed);
    } else {
      const imageRefs = current.filter((a) => a.kind === "image").map((a) => a.ref);
      const fileRefs = current.filter((a) => a.kind !== "image").map((a) => a.ref);
      onSend(
        trimmed,
        imageRefs.length > 0 ? imageRefs : undefined,
        fileRefs.length > 0 ? fileRefs : undefined,
        current.length > 0 ? current : undefined,
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

  const uploadFiles = async (
    files: File[],
    opts?: { previewText?: string; optimisticId?: string },
  ) => {
    if (files.length === 0) return;
    setUploading(true);
    let failed = 0;
    let rejected = 0;
    try {
      for (const file of files) {
        const kind = attachmentKind(file);
        if (!kind) {
          rejected += 1;
          if (opts?.optimisticId) {
            applyAttachments((prev) => prev.filter((a) => a.ref !== opts.optimisticId));
            pendingTextBodiesRef.current.delete(opts.optimisticId);
          }
          continue;
        }
        try {
          const result = await uploadFile(file);
          if (result.files.length > 0) {
            const uploaded = result.files[0];
            if (opts?.optimisticId && skippedOptimisticRef.current.has(opts.optimisticId)) {
              skippedOptimisticRef.current.delete(opts.optimisticId);
              pendingTextBodiesRef.current.delete(opts.optimisticId);
              continue;
            }
            if (opts?.optimisticId) {
              const body = pendingTextBodiesRef.current.get(opts.optimisticId);
              if (body != null) {
                pendingTextBodiesRef.current.delete(opts.optimisticId);
                pendingTextBodiesRef.current.set(uploaded.ref, body);
              }
            }
            // 图片粘贴/拖拽/选择即给本地预览（不等上传落地，与手机端草稿图预览同效）。
            const previewUrl = kind === "image" ? URL.createObjectURL(file) : undefined;
            const previewText =
              kind === "text" && opts?.previewText ? opts.previewText : undefined;
            applyAttachments((prev) => {
              const withoutOptimistic = opts?.optimisticId
                ? prev.filter((a) => a.ref !== opts.optimisticId)
                : prev;
              return [
                ...withoutOptimistic,
                {
                  ref: uploaded.ref,
                  filename: uploaded.filename,
                  kind,
                  size: uploaded.size,
                  previewUrl,
                  previewText,
                },
              ];
            });
          } else if (opts?.optimisticId) {
            applyAttachments((prev) => prev.filter((a) => a.ref !== opts.optimisticId));
            pendingTextBodiesRef.current.delete(opts.optimisticId);
            failed += 1;
          }
        } catch (err) {
          failed += 1;
          if (opts?.optimisticId) {
            applyAttachments((prev) => prev.filter((a) => a.ref !== opts.optimisticId));
            pendingTextBodiesRef.current.delete(opts.optimisticId);
          }
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

  /** 把绝对路径插到光标处；必须读 textRef，避免 await 后盖掉用户新输入。 */
  const insertPathsAtSelection = (pathText: string, start: number, end: number) => {
    const current = textRef.current;
    const next = `${current.slice(0, start)}${pathText}${current.slice(end)}`;
    setText(next);
    setMenuOpen(true);
    requestAnimationFrame(() => {
      const node = inputRef.current;
      if (!node) return;
      const caret = start + pathText.length;
      node.focus();
      node.setSelectionRange(caret, caret);
    });
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

  /** 目录 → 单个「目录」芯片（清单文本），不炸成一堆文件芯片。 */
  const attachDirectoryListing = async (entry: FileSystemDirectoryEntry) => {
    const { file, body, preview } = await directoryEntryToListingFile(entry);
    const optimisticId = `pending-dir-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    pendingTextBodiesRef.current.set(optimisticId, body);
    applyAttachments((prev) => [
      ...prev,
      {
        ref: optimisticId,
        filename: file.name,
        kind: "text",
        size: file.size,
        previewText: preview,
      },
    ]);
    const p = uploadFiles([file], { previewText: preview, optimisticId });
    pendingUploadRef.current = p;
    await p;
  };

  const handlePaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    // 目录必须同步摘 entry（await 后 DataTransfer 失效）
    const { directories } = snapshotTransferEntries(e.clipboardData);
    const items = Array.from(e.clipboardData?.items ?? []);
    const files = items
      .filter((item) => item.kind === "file")
      .map((item) => item.getAsFile())
      .filter((f): f is File => f !== null)
      .map((f, i) => namePastedFile(f, i));

    if (directories.length > 0) {
      e.preventDefault();
      const el = inputRef.current;
      const start = el?.selectionStart ?? textRef.current.length;
      const end = el?.selectionEnd ?? start;
      void (async () => {
        // 有 CF_HDROP 绝对路径时只插路径（一条目录路径），绝不拆成文件芯片
        try {
          const { text: pathText, paths } = await fetchClipboardPaths();
          if (paths.length > 0 && pathText) {
            insertPathsAtSelection(pathText, start, end);
            return;
          }
        } catch {
          /* fall through */
        }
        for (const dir of directories) {
          await attachDirectoryListing(dir);
        }
      })();
      return;
    }

    if (files.length > 0) {
      e.preventDefault();
      const el = inputRef.current;
      const start = el?.selectionStart ?? textRef.current.length;
      const end = el?.selectionEnd ?? start;
      // 截图/位图：无 CF_HDROP，直接上传，别空跑路径接口
      const allBitmaps = files.every(looksLikeClipboardBitmap);
      void (async () => {
        if (!allBitmaps) {
          try {
            const { text: pathText, paths } = await fetchClipboardPaths();
            if (paths.length > 0 && pathText) {
              insertPathsAtSelection(pathText, start, end);
              return;
            }
          } catch {
            /* 非本机 / 接口失败 → 上传 */
          }
        }
        const p = uploadFiles(files);
        pendingUploadRef.current = p;
        await p;
      })();
      return;
    }
    // 纯文本长粘贴：先乐观出芯片，再后台上传（像贴图秒显）
    const plain = e.clipboardData?.getData("text/plain") ?? "";
    if (!isLongPasteText(plain)) return;
    e.preventDefault();
    const file = namePastedLongText(plain);
    const preview = longTextPreview(plain);
    const optimisticId = `pending-text-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    pendingTextBodiesRef.current.set(optimisticId, plain);
    applyAttachments((prev) => [
      ...prev,
      {
        ref: optimisticId,
        filename: file.name,
        kind: "text",
        size: file.size,
        previewText: preview,
      },
    ]);
    const p = uploadFiles([file], { previewText: preview, optimisticId });
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
    // 必须同步摘 entry：目录 → 单个芯片；文件 → 上传。
    const { directories, files: fileEntries, fallbackFiles } = snapshotTransferEntries(e.dataTransfer);
    void (async () => {
      try {
        if (directories.length > 0) {
          for (const dir of directories) {
            await attachDirectoryListing(dir);
          }
        }
        const plainFiles: File[] = [];
        if (fileEntries.length > 0) {
          for (const fe of fileEntries) {
            plainFiles.push(await readFileEntry(fe));
          }
        } else if (directories.length === 0) {
          plainFiles.push(...fallbackFiles);
        }
        if (plainFiles.length > 0) {
          const p = uploadFiles(plainFiles);
          pendingUploadRef.current = p;
          await p;
        } else if (directories.length === 0) {
          message.warning("没有可上传的文件");
        }
      } catch (err) {
        console.error("读取拖入内容失败:", err);
        message.error("读取拖入内容失败");
      }
    })();
  };

  return (
    <div
      style={{
        borderTop: "1px solid var(--coara-border-faint)",
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
            border: dragActive ? "2px dashed var(--coara-accent)" : "1px solid var(--coara-border-soft)",
            borderRadius: 12,
            background: dragActive ? "var(--coara-accent-subtle)" : "var(--coara-surface)",
            padding: 12,
            display: "flex",
            flexDirection: "column",
            gap: 8,
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
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {attachments.map((att, i) =>
                att.kind === "image" && att.previewUrl ? (
                  // 图片附件：缩略图预览（本地 blob，发送前所见即所得）+ 点击放大 + 右上角移除
                  <span
                    key={att.ref || i}
                    style={{
                      position: "relative",
                      display: "inline-block",
                      borderRadius: 8,
                      overflow: "hidden",
                      border: "1px solid var(--coara-border-muted)",
                      background: "var(--coara-bg-subtle)",
                    }}
                  >
                    <Image
                      src={att.previewUrl}
                      alt={att.filename}
                      title={att.filename}
                      height={48}
                      style={{ display: "block", maxWidth: 96, objectFit: "cover", cursor: "pointer" }}
                    />
                    <button
                      type="button"
                      onClick={() => removeAttachmentAt(i)}
                      style={{
                        position: "absolute",
                        top: 2,
                        right: 2,
                        width: 18,
                        height: 18,
                        borderRadius: "50%",
                        border: "none",
                        // 半透明墨底 = 文字主色加弱透明，深浅色主题下都可辨认
                        background: "var(--coara-text)",
                        opacity: 0.72,
                        color: "var(--coara-bg)",
                        cursor: "pointer",
                        fontSize: 12,
                        lineHeight: 1,
                        padding: 0,
                      }}
                    >
                      ×
                    </button>
                  </span>
                ) : isDirectoryListingAttachment(att) ? (
                  <span
                    key={att.ref || i}
                    role="button"
                    tabIndex={0}
                    title="点击展开目录清单到输入框"
                    onClick={() => void expandTextAttachment(i)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        void expandTextAttachment(i);
                      }
                    }}
                    style={{
                      padding: "4px 10px",
                      paddingRight: 26,
                      background: "var(--coara-bg-subtle)",
                      border: "1px solid var(--coara-border-muted)",
                      borderRadius: 6,
                      fontSize: 12,
                      color: "var(--coara-text-strong)",
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 4,
                      maxWidth: 220,
                      position: "relative",
                      cursor: "pointer",
                      boxSizing: "border-box",
                    }}
                  >
                    <FolderOutlined />
                    <span
                      style={{
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {att.previewText || att.filename}
                    </span>
                    <button
                      type="button"
                      title="移除"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeAttachmentAt(i);
                      }}
                      style={{
                        position: "absolute",
                        top: "50%",
                        right: 4,
                        transform: "translateY(-50%)",
                        width: 18,
                        height: 18,
                        borderRadius: "50%",
                        border: "none",
                        background: "none",
                        color: "var(--coara-text-tertiary)",
                        cursor: "pointer",
                        fontSize: 14,
                        lineHeight: 1,
                        padding: 0,
                      }}
                    >
                      ×
                    </button>
                  </span>
                ) : att.kind === "text" && att.previewText ? (
                  <span
                    key={att.ref || i}
                    role="button"
                    tabIndex={0}
                    title="点击展开到输入框"
                    onClick={() => void expandTextAttachment(i)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        void expandTextAttachment(i);
                      }
                    }}
                    style={{
                      position: "relative",
                      display: "inline-flex",
                      flexDirection: "column",
                      justifyContent: "center",
                      gap: 2,
                      minWidth: 120,
                      maxWidth: 220,
                      height: 48,
                      padding: "6px 28px 6px 10px",
                      borderRadius: 8,
                      border: "1px solid var(--coara-border-muted)",
                      background: "var(--coara-bg-subtle)",
                      boxSizing: "border-box",
                      cursor: "pointer",
                    }}
                  >
                    <span
                      style={{
                        fontSize: 10,
                        color: "var(--coara-text-tertiary)",
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 4,
                      }}
                    >
                      <FileTextOutlined />
                      粘贴文本
                    </span>
                    <span
                      style={{
                        fontSize: 12,
                        color: "var(--coara-text-strong)",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {att.previewText}
                    </span>
                    <button
                      type="button"
                      title="移除"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeAttachmentAt(i);
                      }}
                      style={{
                        position: "absolute",
                        top: 2,
                        right: 2,
                        width: 18,
                        height: 18,
                        borderRadius: "50%",
                        border: "none",
                        background: "var(--coara-text)",
                        opacity: 0.72,
                        color: "var(--coara-bg)",
                        cursor: "pointer",
                        fontSize: 12,
                        lineHeight: 1,
                        padding: 0,
                      }}
                    >
                      ×
                    </button>
                  </span>
                ) : (
                  <span
                    key={att.ref || i}
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
                    ) : att.kind === "text" ? (
                      <FileTextOutlined />
                    ) : (
                      <PaperClipOutlined />
                    )}{" "}
                    {att.filename}
                    <button
                      type="button"
                      onClick={() => removeAttachmentAt(i)}
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
                ),
              )}
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
                : "输入消息…（Shift+Enter 换行；复制粘贴本机文件/文件夹→路径；拖拽文件/文件夹→上传；/ 命令 ↑↓）"
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
              transition: "color var(--coara-transition)",
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
                  width: 28,
                  height: 28,
                  borderRadius: "50%",
                  background: "transparent",
                  color: "var(--coara-text-tertiary)",
                  cursor: "pointer",
                  transition: "background var(--coara-transition), color var(--coara-transition)",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "var(--coara-row-hover)";
                  e.currentTarget.style.color = "var(--coara-text-secondary)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent";
                  e.currentTarget.style.color = "var(--coara-text-tertiary)";
                }}
              >
                <input
                  type="file"
                  accept="image/*,.pdf,.docx,.xlsx,.pptx,.zip,.txt,.md,.py,.js,.ts,.tsx,.jsx,.json,.yaml,.yml,.toml,.csv,.xml,.html,.css,.rs,.go,.java,.c,.h,.cpp,.sh,.sql,.log"
                  onChange={handleUpload}
                  style={{ display: "none" }}
                />
                {uploading ? (
                  <LoadingOutlined
                    style={{ fontSize: 14, color: "var(--coara-text-secondary)" }}
                    spin
                  />
                ) : (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
                  </svg>
                )}
              </label>
            </div>

            <button
              onClick={handleSend}
              disabled={!text.trim() && attachments.length === 0}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                width: 36,
                height: 36,
                borderRadius: "50%",
                background:
                  text.trim() || attachments.length > 0 ? "var(--coara-text)" : "var(--coara-border-muted)",
                border: "none",
                cursor: text.trim() || attachments.length > 0 ? "pointer" : "not-allowed",
                transition: "background var(--coara-transition), box-shadow var(--coara-transition)",
                boxShadow:
                  text.trim() || attachments.length > 0 ? "var(--coara-shadow-primary)" : "none",
              }}
              onMouseEnter={(e) => {
                if (text.trim() || attachments.length > 0) {
                  e.currentTarget.style.background = "var(--coara-text-active)";
                  e.currentTarget.style.boxShadow = "var(--coara-shadow-primary-hover)";
                }
              }}
              onMouseLeave={(e) => {
                if (text.trim() || attachments.length > 0) {
                  e.currentTarget.style.background = "var(--coara-text)";
                  e.currentTarget.style.boxShadow = "var(--coara-shadow-primary)";
                }
              }}
            >
              <svg
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                stroke={
                  text.trim() || attachments.length > 0 ? "var(--coara-surface)" : "var(--coara-text-tertiary)"
                }
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
});
