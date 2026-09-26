/**
 * Design tokens for coara workflow UI.
 *
 * 单一事实源（single source of truth）。一个干净、高对比度的浅色主题，
 * 灵感来自 Linear / Figma / Notion 与 n8n / Dify 的工作流画布。
 *
 * 令牌通过 injectTheme() 注入为 :root 上的 CSS 变量，任何组件（JS 或
 * 原生 CSS）都可通过 var(--wf-*) 引用。同时输出短别名（--bg / --brand
 * 等）以兼容外层 HTML 外壳在 React 挂载前的渲染。
 *
 * 设计原则：
 *   1. 中性灰承担 80% 的界面，主色仅做强调（60/30/10 法则）
 *   2. 节点语义色按类型区分（智能体 / 工作流输入）
 *   3. 组件零硬编码色值，一律走令牌
 *   4. 覆盖 React Flow 的 --xy-* 变量以统一画布外观
 */

export const TOKENS = {
  color: {
    bg: '#ffffff',
    surface: '#f7f8fa',
    'surface-hover': '#f1f3f5',
    'surface-active': '#e5e7eb',
    border: '#e5e7eb',
    'border-strong': '#d1d5db',
    text: '#111827',
    'text-secondary': '#4b5563',
    'text-tertiary': '#6b7280',
    brand: '#2563eb',
    'brand-hover': '#1d4ed8',
    'brand-subtle': '#eff6ff',
    /* brand 派生 alpha 色：供 chip 边框 / 已绑定端口边框使用 */
    'brand-border': 'rgba(37,99,235,0.15)',
    'brand-bound': 'rgba(37,99,235,0.25)',
    success: '#16a34a',
    'success-subtle': '#f0fdf4',
    warning: '#d97706',
    'warning-subtle': '#fffbeb',
    error: '#dc2626',
    'error-subtle': '#fef2f2',
    focus: '#2563eb',
    /* focus 派生 alpha 色：输入框聚焦光环 */
    'focus-ring': 'rgba(37,99,235,0.12)',
    status: {
      running: '#2563eb',
      success: '#16a34a',
      completed: '#16a34a',
      error: '#dc2626',
      failed: '#dc2626',
      pending: '#6b7280',
      waiting: '#d97706',
      skipped: '#9ca3af',
    },
    node: {
      bg: '#ffffff',
      /* 节点语义色：智能体（run）强调色 + 浅底 */
      'run-accent': '#2563eb',
      'run-subtle': '#eff6ff',
      /* 选中态光环色：accent 的低 alpha 派生，避免 color-mix 触发 IDE 告警 */
      'run-ring': 'rgba(37,99,235,0.12)',
    },
  },
  spacing: {
    '0': '0px',
    '1': '4px',
    '2': '8px',
    '3': '12px',
    '4': '16px',
    '5': '24px',
    '6': '32px',
    xs: '2px',
    sm: '8px',
    md: '16px',
    lg: '24px',
    xl: '32px',
  },
  radius: {
    sm: '6px',
    md: '8px',
    lg: '12px',
    pill: '999px',
  },
  shadow: {
    card: '0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.02)',
    panel: '0 4px 12px rgba(0,0,0,0.05), 0 1px 3px rgba(0,0,0,0.03)',
    modal: '0 16px 48px rgba(0,0,0,0.08), 0 4px 12px rgba(0,0,0,0.04)',
    node: '0 1px 2px rgba(0,0,0,0.03), 0 2px 4px rgba(0,0,0,0.02)',
    'node-hover': '0 4px 12px rgba(0,0,0,0.08), 0 1px 3px rgba(0,0,0,0.04)',
    'node-selected': '0 0 0 2px rgba(37,99,235,0.2)',
    dropdown: '0 8px 24px rgba(0,0,0,0.08), 0 2px 6px rgba(0,0,0,0.03)',
  },
  font: {
    family: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans SC', sans-serif",
    mono: "ui-monospace, 'Cascadia Code', 'SF Mono', Menlo, monospace",
    size: {
      xs: '11px',
      sm: '12px',
      md: '13px',
      lg: '14px',
      xl: '16px',
    },
  },
};

/**
 * 短别名映射：把 --wf-color-bg 等同时暴露为 --bg / --brand，
 * 供 HTML 外壳（toolbar 等在 React 挂载前渲染的部分）直接使用。
 * 保持键值与 TOKENS 同步。
 */
const ALIASES: Record<string, () => string> = {
  '--bg': () => TOKENS.color.bg,
  '--surface': () => TOKENS.color.surface,
  '--surface-hover': () => TOKENS.color['surface-hover'],
  '--surface-active': () => TOKENS.color['surface-active'],
  '--border': () => TOKENS.color.border,
  '--border-strong': () => TOKENS.color['border-strong'],
  '--text': () => TOKENS.color.text,
  '--text-secondary': () => TOKENS.color['text-secondary'],
  '--text-tertiary': () => TOKENS.color['text-tertiary'],
  '--brand': () => TOKENS.color.brand,
  '--brand-hover': () => TOKENS.color['brand-hover'],
  '--brand-subtle': () => TOKENS.color['brand-subtle'],
  '--success': () => TOKENS.color.success,
  '--success-subtle': () => TOKENS.color['success-subtle'],
  '--warning': () => TOKENS.color.warning,
  '--warning-subtle': () => TOKENS.color['warning-subtle'],
  '--error': () => TOKENS.color.error,
  '--error-subtle': () => TOKENS.color['error-subtle'],
  '--radius-sm': () => TOKENS.radius.sm,
  '--radius-md': () => TOKENS.radius.md,
  '--radius-lg': () => TOKENS.radius.lg,
  '--shadow-card': () => TOKENS.shadow.card,
  '--shadow-panel': () => TOKENS.shadow.panel,
  '--shadow-dropdown': () => TOKENS.shadow.dropdown,
};

/**
 * React Flow CSS 变量覆盖。把 React Flow 内部使用的 --xy-* 变量
 * 指向我们的令牌，使画布原生控件（边、迷你地图、控制按钮、背景点）
 * 与主题保持一致，无需在内联 style 中硬编码。
 */
const REACT_FLOW_OVERRIDES: Record<string, () => string> = {
  '--xy-edge-stroke-default': () => TOKENS.color['text-tertiary'],
  '--xy-edge-stroke-width-default': () => '1.5',
  '--xy-edge-stroke-selected-default': () => TOKENS.color.brand,
  '--xy-connectionline-stroke-default': () => TOKENS.color['text-tertiary'],
  '--xy-connectionline-stroke-width-default': () => '1.5',
  '--xy-minimap-background-color-default': () => TOKENS.color.surface,
  '--xy-background-pattern-dots-color-default': () => TOKENS.color['border-strong'],
  '--xy-controls-button-background-color-default': () => TOKENS.color.bg,
  '--xy-controls-button-color-default': () => TOKENS.color.text,
  '--xy-controls-button-border-color-default': () => TOKENS.color.border,
  '--xy-controls-button-background-color-hover-default': () => TOKENS.color['surface-hover'],
  '--xy-controls-button-color-hover-default': () => TOKENS.color.text,
  '--xy-attribution-background-color-default': () => 'transparent',
};

/** Flatten tokens to CSS variable declarations. */
function flatten(obj: object, prefix = '--wf', out: Record<string, string> = {}): Record<string, string> {
  for (const [key, value] of Object.entries(obj)) {
    const name = `${prefix}-${key}`;
    if (value !== null && typeof value === 'object') {
      flatten(value, name, out);
    } else {
      out[name] = String(value);
    }
  }
  return out;
}

let _injected = false;

/**
 * Inject all tokens as CSS variables on :root. Idempotent.
 * 输出三组变量：--wf-*（规范命名）/ 短别名 / --xy-*（React Flow 覆盖）。
 */
export function injectTheme(): void {
  if (_injected) return;
  const wfVars = flatten(TOKENS);
  const aliasVars: Record<string, string> = {};
  for (const [k, fn] of Object.entries(ALIASES)) aliasVars[k] = String(fn());
  const rfVars: Record<string, string> = {};
  for (const [k, fn] of Object.entries(REACT_FLOW_OVERRIDES)) rfVars[k] = String(fn());
  const all = { ...wfVars, ...aliasVars, ...rfVars };
  const css = `:root { ${Object.entries(all).map(([k, v]) => `${k}: ${v};`).join(' ')} }`;
  const style = document.createElement('style');
  style.id = 'wf-theme-tokens';
  style.textContent = css;
  document.head.appendChild(style);
  _injected = true;
}

/** Resolve a token path like 'color.status.running' to its value. */
export function token(path: string): unknown {
  const parts = path.split('.');
  let cur: unknown = TOKENS;
  for (const p of parts) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = (cur as Record<string, unknown>)[p];
  }
  return cur;
}
