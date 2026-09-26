import type { CSSProperties, MouseEvent, ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { fileViewRoute } from "../lib/fileLink";

interface FilePathLinkProps {
  /** 文件路径（Windows 绝对路径 / POSIX 绝对路径 / 相对路径均可，后端负责解析）。 */
  path: string;
  children?: ReactNode;
  style?: CSSProperties;
  className?: string;
  title?: string;
}

/**
 * 文件路径内部链接——点击跳整页文件显示页 /file。
 * 聊天 markdown 链接、工具行路径等共用；按住 Ctrl/中键时走原生 <a> 行为新开标签页。
 */
export function FilePathLink({ path, children, style, className, title }: FilePathLinkProps) {
  const navigate = useNavigate();
  const route = fileViewRoute(path);
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    navigate(route);
  };
  return (
    <a href={route} onClick={onClick} style={style} className={className} title={title ?? path}>
      {children ?? path}
    </a>
  );
}
