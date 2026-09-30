/**
 * DOM helpers (原创, framework-agnostic).
 *
 * Small utilities for the workflow UI that don't warrant a dependency:
 * clipboard, tooltip positioning, escape handling, class composition.
 */

/** Copy text to clipboard, gracefully degrading on older browsers. */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to legacy path
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/** Compose class names, dropping falsy values. */
export function cx(...parts: unknown[]): string {
  return parts.filter(Boolean).join(' ');
}

/** Stop propagation + prevent default — useful for nested click handlers. */
interface StoppableEvent {
  stopPropagation?: () => void;
  preventDefault?: () => void;
}
export function swallow(e: StoppableEvent | null | undefined): void {
  if (e && typeof e.stopPropagation === 'function') e.stopPropagation();
  if (e && typeof e.preventDefault === 'function') e.preventDefault();
}
