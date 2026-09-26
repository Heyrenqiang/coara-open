import type { ReactNode } from "react";
import { FilePathLink } from "./FilePathLink";
import { looksLikeFsPath } from "../lib/pathDetect";

interface MarkdownLinkProps {
  href?: string;
  children?: ReactNode;
}

/** href 归一：micromark 会把链接目标里的反斜杠做百分号转义，Windows 路径于是
 *  变成 `D:%5Ccode_ws%5C...`，路径形态判定一个都不匹配、链接退化成纯文本。
 *  这里先解码回原样再判形态；只处理「路径形态」的目标——http(s) 的百分号转义
 *  是 URL 语义的一部分（`%2F`、`%3F`、`%23` 等），解回原义会改变链接目标。 */
function normalizeHref(raw: string): string {
  const url = (raw ?? "").trim();
  if (/^https?:\/\//i.test(url)) return url;
  if (!/%[0-9a-f]{2}/i.test(url)) return url;
  try {
    return decodeURIComponent(url);
  } catch {
    return url;
  }
}

/**
 * 聊天气泡 Markdown 链接：只对三类目标做可点渲染。
 *
 * 1. 网页 — `http://` / `https://` → 新标签页
 * 2. 文件 / 3. 文件夹 — 路径形态（见 looksLikeFsPath）→ /file
 *
 * 其它 href（普通词、工具名、模糊引用、# 锚点以外的杂项）一律不可点，只显示文案。
 * 页内锚点 / mailto 保留原生行为（非「展示系统」三类，但不做文件链）。
 */
export function MarkdownLink({ href, children }: MarkdownLinkProps) {
  const url = normalizeHref(href ?? "");
  if (/^https?:\/\//i.test(url)) {
    return (
      <a href={url} target="_blank" rel="noreferrer">
        {children}
      </a>
    );
  }
  if (url.startsWith("#") || url.startsWith("mailto:")) {
    return <a href={url}>{children}</a>;
  }
  if (url && looksLikeFsPath(url)) {
    return <FilePathLink path={url}>{children}</FilePathLink>;
  }
  return <span>{children}</span>;
}
