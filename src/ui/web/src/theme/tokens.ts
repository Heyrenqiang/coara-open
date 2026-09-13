import { theme as antdThemeApi, type ThemeConfig } from "antd";

/**
 * coara Web 设计令牌 —— 单一真源（Single Source of Truth）。
 *
 * 规则（写进 docs/Web设计体系.md，属硬约束）：
 *   1. 任何组件不得出现裸色值（#xxxxxx / rgb() / rgba()），必须引用此处令牌
 *      或对应的 CSS 变量。
 *   2. 变更外观 = 只改本文件。antd 主题与 CSS 变量都由本文件派生，
 *      两处不会再各自漂移。
 *   3. 新增令牌先在此登记，再到组件使用；不允许"先在组件里写死"。
 *
 * 导出三类：
 *   - tokens        语义令牌（JS 侧使用）
 *   - cssVars       CSS 变量表（样式表通过 var(--coara-*) 使用）
 *   - coaraAntdTheme antd ConfigProvider 主题（组件库级）
 */

export const tokens = {
  /* ---- 底色与表面 ---- */
  bg: "#ffffff",
  bgSubtle: "#fafafa",
  surface: "#ffffff",

  /* ---- 文本 ---- */
  text: "#141414",
  textSecondary: "#6b7280",
  textTertiary: "#9ca3af",
  /** 与 textSecondary 同角色，历史遗留两值；合并时以此为准，暂保留以免像素变动 */
  textMuted: "#6b6b6b",
  textFaint: "#888888",
  textStrong: "#374151",

  /* ---- 边框 ---- */
  /** 结构性分隔线：表格、引用块 */
  border: "#d4d4d4",
  /** 组件边框 / 分割线 */
  borderMuted: "#e5e7eb",
  borderSoft: "#eeeeee",
  borderFaint: "#f0f0f0",

  /* ---- 强调与语义 ---- */
  accent: "#3b82f6",
  accentSubtle: "#eff6ff",
  success: "#22c55e",
  error: "#ef4444",
  danger: "#dc2626",
  warning: "#f59e0b",
  link: "#5b9fd4",

  /* ---- 内容块 ---- */
  codeBg: "#f6f8fa",
  tableHead: "#f0f0f1",
  tableBody: "#f8f8f9",

  /* ---- 对话气泡 ---- */
  bubbleUser: "#e8e8e8",
  bubbleAgent: "#ffffff",

  /* ---- 形状 ---- */
  radius: 8,
  radiusLg: 12,
  bubbleRadius: 8,
  bubblePadH: 16,
  bubblePadV: 8,
  itemSpacing: 10,

  /* ---- 阴影 ---- */
  shadowSm: "0 1px 2px rgba(0, 0, 0, 0.04)",
  shadowMd: "0 2px 8px rgba(0, 0, 0, 0.05)",
  shadowCard: "0 1px 3px rgba(0, 0, 0, 0.03)",

  /* ---- 动效 ---- */
  transition: "0.15s ease",

  /* ---- 状态色（进行中 / 待办） ---- */
  /** 青蓝：进行中（对齐 CLI prompt.thinking） */
  progress: "#0e7490",

  /* ---- 深墨态 ---- */
  /** 墨色按钮的按下态（#coara-text 更深一档） */
  textActive: "#000000",
  /** 深色底上的文字/图标 */
  onInk: "#ffffff",

  /* ---- 强调色浅色端点（渐变底色） ---- */
  accentTintA: "#f0f9ff",
  accentTintB: "#e0f2fe",

  /* ---- 深色文字变体（浅底上的高对比文本） ---- */
  successStrong: "#166534",
  dangerStrong: "#991b1b",

  /* ---- 半透明洗色与滚条 ---- */
  accentWash: "rgba(59, 130, 246, 0.08)",
  dangerWash: "rgba(239, 68, 68, 0.04)",
  scrollThumb: "rgba(0, 0, 0, 0.12)",
  scrollThumbHover: "rgba(0, 0, 0, 0.22)",

  /* ---- 阴影（整条 token 化，避免 rgba 散落） ---- */
  shadowXs: "0 1px 2px rgba(0, 0, 0, 0.03)",
  shadowHover: "0 4px 14px rgba(0, 0, 0, 0.04)",
  shadowBtn: "0 1px 3px rgba(0, 0, 0, 0.06)",
  shadowBtnHover: "0 3px 10px rgba(0, 0, 0, 0.09)",
  shadowChip: "0 1px 4px rgba(0, 0, 0, 0.06)",
  shadowModal: "0 4px 20px rgba(0, 0, 0, 0.08)",
  shadowPop: "0 8px 28px rgba(0, 0, 0, 0.08), 0 2px 6px rgba(0, 0, 0, 0.03)",
  shadowPopLg: "0 10px 32px rgba(0, 0, 0, 0.08)",
  shadowFloat: "0 2px 10px rgba(0, 0, 0, 0.06), 0 10px 30px rgba(0, 0, 0, 0.05)",
  shadowFloatHover: "0 4px 14px rgba(0, 0, 0, 0.09), 0 14px 36px rgba(0, 0, 0, 0.07)",
  focusRing: "0 0 0 3px rgba(59, 130, 246, 0.15)",
  shadowPrimary: "0 1px 2px rgba(59, 130, 246, 0.12)",
  shadowPrimaryHover: "0 2px 8px rgba(59, 130, 246, 0.16)",
  shadowAccentSm: "0 2px 8px rgba(59, 130, 246, 0.1)",

  /* ---- 代码画布（深色） ---- */
  codeCanvas: "#0f1115",
  codeBar: "#161a20",
  codeBorder: "#1f242c",
  codeText: "#e6edf3",
  codeMuted: "#7d8590",
  codeDot: "#3fb950",

  /* ---- diff ---- */
  diffAddBg: "#e6ffec",
  diffDelBg: "#ffebe9",
  diffAddFg: "#1a7f37",
  diffDelFg: "#cf222e",

  /* ---- 语法高亮（GitHub 浅色） ---- */
  syntaxRed: "#cf222e",
  syntaxString: "#0a3069",
  syntaxNumber: "#0550ae",
  syntaxTitle: "#6639ba",
  syntaxVariable: "#953800",
} as const;

/**
 * CSS 变量表。样式表统一用 var(--coara-*) 引用。
 * 注意 --coara-brand 此前被 index.css 引用却从未定义（靠 fallback 兜着），
 * 在此补齐，与 --coara-accent 同值。
 */
export const cssVars: Record<string, string> = {
  "--coara-bg": tokens.bg,
  "--coara-bg-subtle": tokens.bgSubtle,
  "--coara-surface": tokens.surface,

  "--coara-text": tokens.text,
  "--coara-text-secondary": tokens.textSecondary,
  "--coara-text-tertiary": tokens.textTertiary,
  "--coara-text-muted": tokens.textMuted,
  "--coara-text-faint": tokens.textFaint,
  "--coara-text-strong": tokens.textStrong,

  "--coara-border": tokens.border,
  "--coara-border-muted": tokens.borderMuted,
  "--coara-border-soft": tokens.borderSoft,
  "--coara-border-faint": tokens.borderFaint,

  "--coara-accent": tokens.accent,
  "--coara-accent-subtle": tokens.accentSubtle,
  "--coara-brand": tokens.accent,
  "--coara-success": tokens.success,
  "--coara-error": tokens.error,
  "--coara-danger": tokens.danger,
  "--coara-warning": tokens.warning,
  "--coara-link": tokens.link,

  "--coara-code-bg": tokens.codeBg,
  "--coara-table-head": tokens.tableHead,
  "--coara-table-body": tokens.tableBody,

  "--coara-bubble-user": tokens.bubbleUser,
  "--coara-bubble-agent": tokens.bubbleAgent,

  "--coara-bubble-radius": `${tokens.bubbleRadius}px`,
  "--coara-bubble-pad-h": `${tokens.bubblePadH}px`,
  "--coara-bubble-pad-v": `${tokens.bubblePadV}px`,
  "--coara-item-spacing": `${tokens.itemSpacing}px`,

  "--coara-shadow-sm": tokens.shadowSm,
  "--coara-shadow-md": tokens.shadowMd,

  "--coara-transition": tokens.transition,

  "--coara-progress": tokens.progress,
  "--coara-text-active": tokens.textActive,
  "--coara-on-ink": tokens.onInk,
  "--coara-accent-tint-a": tokens.accentTintA,
  "--coara-accent-tint-b": tokens.accentTintB,
  "--coara-success-strong": tokens.successStrong,
  "--coara-danger-strong": tokens.dangerStrong,
  "--coara-accent-wash": tokens.accentWash,
  "--coara-danger-wash": tokens.dangerWash,
  "--coara-scroll-thumb": tokens.scrollThumb,
  "--coara-scroll-thumb-hover": tokens.scrollThumbHover,

  "--coara-shadow-xs": tokens.shadowXs,
  "--coara-shadow-hover": tokens.shadowHover,
  "--coara-shadow-btn": tokens.shadowBtn,
  "--coara-shadow-btn-hover": tokens.shadowBtnHover,
  "--coara-shadow-chip": tokens.shadowChip,
  "--coara-shadow-modal": tokens.shadowModal,
  "--coara-shadow-pop": tokens.shadowPop,
  "--coara-shadow-pop-lg": tokens.shadowPopLg,
  "--coara-shadow-float": tokens.shadowFloat,
  "--coara-shadow-float-hover": tokens.shadowFloatHover,
  "--coara-focus-ring": tokens.focusRing,
  "--coara-shadow-primary": tokens.shadowPrimary,
  "--coara-shadow-primary-hover": tokens.shadowPrimaryHover,
  "--coara-shadow-accent-sm": tokens.shadowAccentSm,

  "--coara-code-canvas": tokens.codeCanvas,
  "--coara-code-bar": tokens.codeBar,
  "--coara-code-border": tokens.codeBorder,
  "--coara-code-text": tokens.codeText,
  "--coara-code-muted": tokens.codeMuted,
  "--coara-code-dot": tokens.codeDot,

  "--coara-diff-add-bg": tokens.diffAddBg,
  "--coara-diff-del-bg": tokens.diffDelBg,
  "--coara-diff-add-fg": tokens.diffAddFg,
  "--coara-diff-del-fg": tokens.diffDelFg,

  "--coara-syntax-red": tokens.syntaxRed,
  "--coara-syntax-string": tokens.syntaxString,
  "--coara-syntax-number": tokens.syntaxNumber,
  "--coara-syntax-title": tokens.syntaxTitle,
  "--coara-syntax-variable": tokens.syntaxVariable,
};

/** 把令牌注入 :root。须在 React 渲染前调用（main.tsx）。 */
export function applyTokens(): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  for (const [name, value] of Object.entries(cssVars)) {
    root.style.setProperty(name, value);
  }
}

/** antd 组件库级主题。token 与 components 全部取自上方令牌。 */
export const coaraAntdTheme: ThemeConfig = {
  algorithm: antdThemeApi.defaultAlgorithm,
  token: {
    colorPrimary: tokens.accent,
    borderRadius: tokens.radius,
    fontSize: 14,
    colorBgContainer: tokens.surface,
    colorBorder: tokens.borderMuted,
    colorBorderSecondary: tokens.borderFaint,
    colorText: tokens.text,
    colorTextSecondary: tokens.textSecondary,
    colorBgLayout: tokens.bgSubtle,
  },
  components: {
    Layout: {
      headerBg: tokens.surface,
      headerHeight: 48,
      siderBg: tokens.surface,
      bodyBg: tokens.bgSubtle,
    },
    Menu: {
      itemBorderRadius: tokens.radius,
      itemMarginInline: 8,
      itemSelectedBg: tokens.accentSubtle,
      itemSelectedColor: tokens.accent,
    },
    Card: {
      borderRadiusLG: tokens.radiusLg,
      boxShadowTertiary: tokens.shadowCard,
    },
    Button: {
      borderRadius: tokens.radius,
      controlHeight: 32,
    },
    Input: {
      borderRadius: tokens.radius,
    },
    Select: {
      borderRadius: tokens.radius,
    },
  },
};
