/** ReactMarkdown 的 href 变换：放行文件路径，只拦真正危险的协议。
 *
 *  react-markdown 默认的 urlTransform 把「冒号前有内容、又没有正斜杠」的写法
 *  当成协议做白名单校验——Windows 盘符路径（`D:\...` / `D:/...`）因此被判为
 *  未知协议，整条 href 被清空。到了 MarkdownLink 手上 href 已经是空串，只能
 *  降级成纯文本，「[文件](D:\...)」自然点不动。
 *
 *  这里只做安全兜底（javascript: / data: / vbscript: 一律清空），其余原样透传：
 *  可不可点、点去哪儿由 MarkdownLink 统一判定（http(s) 走新标签页，文件路径走
 *  /file，其余落纯文本）。 */
const BLOCKED_PROTOCOL = /^(javascript|data|vbscript|file):/i;

export function markdownUrlTransform(url: string): string {
  const raw = (url ?? "").trim();
  if (!raw) return "";
  // 判定前把所有控制字符与空白归一：URL 规范会剥掉协议名里的制表符/换行
  // （`java\tscript:` 浏览器仍按脚本执行），先归一才堵得住这类绕过。
  if (BLOCKED_PROTOCOL.test(raw.replace(/[\u0000-\u0020\u007f]+/g, ""))) return "";
  return raw;
}
