import type { ReactNode } from "react";

/**
 * 统一页面骨架（设计契约见 docs/Web设计体系.md §2）。
 *
 * 固定槽位：① 身份头部 / ② 工具条 / ③ 内容区 / ④ 检查器（可选）/ ⑤ 常驻对话入口（可选）。
 * 页面只管往槽位里填内容，不再自造头部、滚动与外边距。
 *
 * **滚动契约**：③ 内容区由 PageShell 统一代管滚动与内边距。
 *   - `scroll="auto"`（默认）：内容超高即滚 —— 列表、详情、表格页都用这个。
 *   - `scroll="hidden"`：内容自己管滚动（内部有独立滚动子区，如左右分栏列表+详情、
 *     或查看器自带上 `flex:1 + overflow:auto` 的容器）。
 *   - `padded={false}` 关掉内边距 —— 给自带 padding 的内容（如表格块、分栏布局）用。
 * 此前每个视图各自复写 `flex:1 + overflow:auto + padding`，收敛到此处。
 */
export interface PageShellProps {
  /** ① 空间身份头部（通常用 PageHeader） */
  header?: ReactNode;
  /** ② 工具条：筛选 / 视图切换 / 动作 */
  toolbar?: ReactNode;
  /** ④ 右侧检查器（可选） */
  inspector?: ReactNode;
  /** ④ 检查器宽度（默认 320） */
  inspectorWidth?: number;
  /** ⑤ 底部固定区：常驻对话入口 / 输入 */
  composer?: ReactNode;
  /** ③ 内容区滚动归属（默认 auto = PageShell 代管） */
  scroll?: "auto" | "hidden";
  /** ③ 内容区内边距（默认 true；自带 padding 的内容传 false） */
  padded?: boolean;
  /** ③ 内容区背景（默认 surface；灰底内容页传 bg-subtle） */
  surface?: "surface" | "subtle";
  /** ③ 内容区 */
  children: ReactNode;
}

export function PageShell({
  header,
  toolbar,
  inspector,
  inspectorWidth = 320,
  composer,
  scroll = "auto",
  padded = true,
  surface = "surface",
  children,
}: PageShellProps) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        minHeight: 0,
        background: "var(--coara-surface)",
      }}
    >
      {header}
      {toolbar}
      <div style={{ flex: 1, minHeight: 0, display: "flex", minWidth: 0 }}>
        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", minHeight: 0 }}>
          {scroll === "auto" ? (
            <div
              style={{
                flex: 1,
                minHeight: 0,
                overflow: "auto",
                padding: padded ? "16px 20px 28px" : 0,
                background:
                  surface === "subtle" ? "var(--coara-bg-subtle)" : "var(--coara-surface)",
              }}
            >
              {children}
            </div>
          ) : (
            children
          )}
        </div>
        {inspector ? (
          <div
            style={{
              width: inspectorWidth,
              flexShrink: 0,
              overflow: "hidden",
              borderLeft: "1px solid var(--coara-border-faint)",
            }}
          >
            {inspector}
          </div>
        ) : null}
      </div>
      {composer}
    </div>
  );
}
