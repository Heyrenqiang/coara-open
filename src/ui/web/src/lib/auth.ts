/**
 * Token 鉴权统一管理。
 *
 * 首次加载时从 URL ?token=xxx 读取并持久化到 sessionStorage，
 * 之后所有 API/WS 调用统一从 getAuthToken() 取值。
 * 这样 react-router 路由切换、页面刷新、用户手动访问根路径都不会丢 token。
 *
 * 选用 sessionStorage 而非 localStorage：token 仅限当前浏览器会话，
 * 关闭标签页即失效，避免长期残留。
 */

const STORAGE_KEY = "coara.auth.token";

/**
 * 初始化 token：若 URL 带 token 则写入 sessionStorage；
 * 若 URL 不带但 sessionStorage 已有，则保持不变；
 * 否则留空（后续请求会 401，由调用方处理）。
 */
export function initAuthToken(): void {
  if (typeof window === "undefined") return;
  const fromUrl = new URLSearchParams(window.location.search).get("token");
  if (fromUrl) {
    sessionStorage.setItem(STORAGE_KEY, fromUrl);
    // 清掉 URL 中的 token，避免泄露到 referrer / 历史记录
    const cleanUrl = window.location.pathname + window.location.hash;
    window.history.replaceState(null, "", cleanUrl);
    return;
  }
  // URL 不带 token：保留 sessionStorage 已有值（若有）
}

/** 读取当前 token（优先 sessionStorage，回退 URL）。 */
export function getAuthToken(): string {
  if (typeof window === "undefined") return "";
  const stored = sessionStorage.getItem(STORAGE_KEY);
  if (stored) return stored;
  return new URLSearchParams(window.location.search).get("token") || "";
}

/** 清除本地 token（认证失败或重新打开 Web 界面前）。 */
export function clearAuthToken(): void {
  if (typeof window === "undefined") return;
  sessionStorage.removeItem(STORAGE_KEY);
}

/** 返回附带 token 的 query string 前缀，例如 "?token=xxx" 或空串。 */
export function tokenQuery(): string {
  const t = getAuthToken();
  return t ? `?token=${encodeURIComponent(t)}` : "";
}

/** 返回附带 token 的 query string 片段（无前导 ?），用于拼接已有 query。例如 "token=xxx"。 */
export function tokenQueryFragment(): string {
  const t = getAuthToken();
  return t ? `token=${encodeURIComponent(t)}` : "";
}
