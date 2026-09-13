/**
 * SingleTabGuard — WebUI 单标签守卫（静默模式）。
 *
 * 判定本标签为「后来者」（已有同源标签存活）时，静默自我退出：
 * 不渲染任何内容 + 尝试 window.close()。浏览器可能拒绝脚本关闭
 * 非脚本打开的标签，此时标签保持空白，由用户手动关闭即可。
 *
 * 主路径由服务端负责：有活跃 WS 则只 focus、不 webbrowser.open；
 * 本组件只兜底（竞态新开了标签），退出前经 BroadcastChannel 请主标签置前。
 */

import { useEffect, useState, type ReactNode } from "react";
import { createSingleTabArbiter } from "../lib/singleTab";

export function SingleTabGuard({ children }: { children: ReactNode }) {
  const [duplicate, setDuplicate] = useState(false);

  useEffect(() => createSingleTabArbiter(() => setDuplicate(true)), []);

  useEffect(() => {
    if (!duplicate) return;
    const t = setTimeout(() => {
      try {
        window.close();
      } catch {
        /* ignore */
      }
    }, 300);
    return () => clearTimeout(t);
  }, [duplicate]);

  if (!duplicate) return <>{children}</>;
  return null;
}
