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

/** KaTeX 未定义宏 → 可用宏的替换表（仅 fenced code block 外生效）。
 *  \slashed 是物理文献高频命令（费曼斜杠记号），KaTeX 未内置；\cancel 视觉等价且已支持。
 *  未识别命令在 rehype-katex strict:false 下仍整段报红色源码——宏替换是唯一根治路径。 */
const MACRO_REPLACEMENTS: ReadonlyArray<readonly [RegExp, string]> = [
  [/\\slashed\{([^{}]+)\}/g, "\\cancel{$1}"],
];

/** 归一化整段消息文本；无 `\[` / `\(` / 待替换宏时原样返回（零开销）。 */
export function normalizeMathDelimiters(text: string): string {
  const needsMacros = MACRO_REPLACEMENTS.some(([re]) => {
    re.lastIndex = 0;
    return re.test(text);
  });
  if (!text.includes("\\[") && !text.includes("\\(") && !needsMacros) return text;
  // 按 ``` 围栏切分：偶数段在围栏外，奇数段是围栏代码块（原样保留）
  const parts = text.split(/(```[\s\S]*?(?:```|$))/g);
  return parts
    .map((part, i) => {
      if (i % 2 === 1) return part;
      let out = convertOutsideFences(part);
      for (const [re, replacement] of MACRO_REPLACEMENTS) {
        re.lastIndex = 0;
        out = out.replace(re, replacement);
      }
      return out;
    })
    .join("");
}
