/** Inline diff panel for tool activity rows (web).
 *
 * Renders coara-computed ``diff_lines`` (single source of truth) as line-numbered
 * add/del rows with the light-theme palette from src/cli/theme.py (_DIFF_LIGHT).
 * No line budget caps — the browser scrolls. Header carries the edited path
 * and +add/-del counts, matching the CLI header.
 */

import { memo } from "react";
import type { CanonicalDiffLines } from "../lib/ws";

const ADD_BG = "var(--coara-diff-add-bg)";
const DEL_BG = "var(--coara-diff-del-bg)";

const MONO: React.CSSProperties = {
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
};

// 内核 _build_canonical_diff_lines 序列化 DiffLineKind.name.lower()：
// "add" / "delete" / "context"。前端据此归一到三类。
type DiffLineKind = "add" | "delete" | "context";

function lineColors(kind: DiffLineKind): { bg?: string; num: string; sign: string; signColor: string } {
  if (kind === "add") return { bg: ADD_BG, num: "var(--coara-diff-add-fg)", sign: "+", signColor: "var(--coara-diff-add-fg)" };
  if (kind === "delete") return { bg: DEL_BG, num: "var(--coara-diff-del-fg)", sign: "-", signColor: "var(--coara-diff-del-fg)" };
  return { num: "var(--coara-text-tertiary)", sign: " ", signColor: "transparent" };
}

export const DiffBlocksView = memo(function DiffBlocksView({ diff }: { diff: CanonicalDiffLines | null }) {
  if (!diff) return null;

  const maxLn = Math.max(
    0,
    ...diff.hunks.flat().map((l) => Math.max(l.oldNum, l.newNum)),
  );
  const numWidth = Math.max(String(maxLn).length, 2);

  return (
    <div
      style={{
        // 上下间距一律由聊天流的消息间距决定（工具行/正文/卡片共用一套）：
        // 卡片自己再加 marginTop，会让「工具行 → diff」比「diff → 工具行」
        // 多出这一段，上下不匀。
        border: "1px solid var(--coara-border-faint)",
        borderRadius: 6,
        overflow: "hidden",
        background: "var(--coara-surface)",
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div
        style={{
          padding: "4px 8px",
          fontSize: 11,
          borderBottom: "1px solid var(--coara-bg-subtle)",
          display: "flex",
          gap: 6,
          alignItems: "baseline",
          ...MONO,
        }}
      >
        <span
          style={{
            color: "var(--coara-accent)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            flex: 1,
          }}
          title={diff.path}
        >
          {diff.path}
        </span>
        {diff.added > 0 && (
          <span style={{ color: "var(--coara-success)", fontWeight: 600, flexShrink: 0 }}>+{diff.added}</span>
        )}
        {diff.removed > 0 && (
          <span style={{ color: "var(--coara-danger)", fontWeight: 600, flexShrink: 0 }}>-{diff.removed}</span>
        )}
      </div>
      <div style={{ overflowX: "auto" }}>
        {diff.hunks.map((hunk, hi) => (
          <div key={hi}>
            {hi > 0 && (
              <div
                style={{
                  padding: "1px 8px",
                  fontSize: 10,
                  color: "var(--coara-text-tertiary)",
                  background: "var(--coara-bg-subtle)",
                  ...MONO,
                }}
              >
                ⋯
              </div>
            )}
            {hunk.map((line, li) => {
              const c = lineColors(line.kind);
              const num = line.kind === "delete" ? line.oldNum : line.newNum;
              return (
                <div
                  key={li}
                  style={{
                    display: "flex",
                    fontSize: 11,
                    lineHeight: "18px",
                    background: c.bg,
                    whiteSpace: "pre",
                    paddingLeft: 4,
                    paddingRight: 12,
                    ...MONO,
                  }}
                >
                  <span
                    style={{
                      minWidth: `${numWidth + 1}ch`,
                      textAlign: "right",
                      paddingRight: "1ch",
                      color: c.num,
                      flexShrink: 0,
                      userSelect: "none",
                    }}
                  >
                    {num || ""}
                  </span>
                  <span style={{ width: "2ch", color: c.signColor, fontWeight: 700, flexShrink: 0, userSelect: "none" }}>
                    {c.sign}
                  </span>
                  <span style={{ color: "var(--coara-text)" }}>{line.code}</span>
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
});
