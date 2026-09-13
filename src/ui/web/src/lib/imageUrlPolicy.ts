/**
 * Markdown 图片 URL 白名单判定（Web 端）
 *
 * LLM 输出可被提示词注入操纵，图片 URL 的 path/query 是渲染即触发的
 * 数据外带/内网探测通道，因此默认只放行与页面（即 API 服务，api.ts 的
 * API_BASE 为空、走同源相对路径）同源的地址与显式受信域名，其余一律
 * 不加载（UI 显示「已拦截外部图片」占位，见 MessageList）
 *
 * 与 Android core/markdown/ImageUrlPolicy 同口径，防两端策略分叉
 */

/**
 * 额外受信的图片域名（小写，不含 scheme/端口，精确或子域匹配）
 * 常量配置，需要放行受信 CDN 时在此追加
 */
export const EXTRA_TRUSTED_HOSTS: readonly string[] = [];

/**
 * 判定 markdown 图片 URL 是否允许加载
 * @param url 图片地址（允许相对路径——相对路径解析后必然落在页面同源）
 * @param pageOrigin 页面 origin（如 window.location.origin），仅 http/https
 * @param extraHosts 额外受信域名，默认取 EXTRA_TRUSTED_HOSTS
 */
export function isTrustedImageUrl(
  url: string,
  pageOrigin: string,
  extraHosts: readonly string[] = EXTRA_TRUSTED_HOSTS,
): boolean {
  const page = parseHttpUrl(pageOrigin.trim());
  if (!page) return false;
  const img = parseHttpUrl(url.trim(), page.href);
  if (!img) return false;
  // 同源比较：URL.origin 已归一化默认端口；userinfo 不影响 host 判定
  if (img.origin === page.origin) return true;
  const host = img.hostname.toLowerCase();
  return extraHosts.some((trusted) => {
    const t = trusted.toLowerCase();
    return host === t || host.endsWith(`.${t}`);
  });
}

/** 解析 http/https URL；可带 base 解析相对路径，其余一律返回 null */
function parseHttpUrl(raw: string, base?: string): URL | null {
  if (!raw) return null;
  try {
    const u = base ? new URL(raw, base) : new URL(raw);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    if (!u.hostname) return null;
    return u;
  } catch {
    return null;
  }
}
