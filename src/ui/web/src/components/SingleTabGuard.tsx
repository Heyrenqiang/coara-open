/**
 * SingleTabGuard — WebUI 单标签守卫（静默模式）。
 *
 * 判定本标签为「后来者」（已有同源活标签）时，静默自我退出：
 * 不渲染任何内容 + 尝试 window.close()。浏览器可能拒绝脚本关闭
 * 非脚本打开的标签，此时标签保持空白，由用户手动关闭即可。
 *
 * 入口带 token 新开的标签优先接管；僵死/被挤掉的旧标签让路。
 */

import { useEffect, useState, type ReactNode } from "react";
import { getAuthToken, tookTokenFromUrlThisLoad } from "../lib/auth";
import { createSingleTabArbiter } from "../lib/singleTab";
import { getWS } from "../lib/ws";

function tabIsClaimingLive(): boolean {
  const ws = getWS();
  if (ws.isClosed()) return false;
  if (!getAuthToken()) return false;
  return true;
}

export function SingleTabGuard({ children }: { children: ReactNode }) {
  const [duplicate, setDuplicate] = useState(false);

  // 录像带拖出窗是只读观察标签：不占单标签坑、不参与仲裁（主标签永不挤它，
  // 它也永不挤主标签）。判定只看 URL，与 WS 身份（role=tape）同源。
  const isObserverTab =
    typeof window !== "undefined" &&
    window.location.pathname.startsWith("/tape") &&
    new URLSearchParams(window.location.search).get("popout") === "1";

  useEffect(() => {
    if (isObserverTab) return undefined;
    return createSingleTabArbiter(() => setDuplicate(true), {
      isClaimingLive: tabIsClaimingLive,
      isFreshOpen: tookTokenFromUrlThisLoad,
    });
  }, [isObserverTab]);

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
