/**
 * LaTeX 分隔符归一化：把 LLM 常用的 `\[...\]`（块级）和 `\(...\)`（行内）
 * 统一转换为 remark-math 支持的 `$$...$$` / `$...$`。
 * 仅处理 fenced code block 之外的内容；含 GFM 表格分隔行的 `\[\]` 视为真表格不转换。
 */

const BRACKET_BLOCK_RE = /\\\[([\s\S]+?)\\\]/g;
const BRACKET_INLINE_RE = /\\\(([\s\S]+?)\\\)/g;

/** GFM 表格分隔行（如 `| --- | :---: |`）——区分真表格与含绝对值 `|` 的公式。 */
function isGfmTableDelimiterRow(line: string): boolean {
  const t = line.trim();
  if (!t.startsWith("|")) return false;
  const cells = t
    .replace(/^\|+|\|+$/g, "")
    .split("|")
    .map((c) => c.trim());
  return cells.length >= 2 && cells.every((c) => /^:?-+:?$/.test(c));
}

function convertOutsideFences(part: string): string {
  let out = part.replace(BRACKET_BLOCK_RE, (match, body: string) => {
    if (body.split("\n").some(isGfmTableDelimiterRow)) return match;
    const latex = body.trim();
    return latex ? `\n\n$$${latex}$$\n\n` : match;
  });
  out = out.replace(BRACKET_INLINE_RE, (match, body: string) => {
    const latex = body.trim();
    return latex ? `$${latex}$` : match;
  });
  return out;
}

/** 归一化整段消息文本；无 `\[` / `\(` 时原样返回（零开销）。 */
export function normalizeMathDelimiters(text: string): string {
  if (!text.includes("\\[") && !text.includes("\\(")) return text;
  // 按 ``` 围栏切分：偶数段在围栏外，奇数段是围栏代码块（原样保留）
  const parts = text.split(/(```[\s\S]*?(?:```|$))/g);
  return parts
    .map((part, i) => (i % 2 === 1 ? part : convertOutsideFences(part)))
    .join("");
}
