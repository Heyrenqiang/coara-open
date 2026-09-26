import { Component, type ReactNode } from "react";
import { Button, Result } from "antd";

/** 动态 import 失败的典型特征（vite chunk 在重新构建后旧 hash 404）。 */
function isChunkLoadError(error: unknown): boolean {
  const message = String((error as Error)?.message || error || "");
  return (
    message.includes("Failed to fetch dynamically imported module") ||
    message.includes("ChunkLoadError") ||
    message.includes("Importing a module script failed") ||
    message.includes("error loading dynamically imported module")
  );
}

const RELOAD_FLAG = "coara_chunk_reload_at";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * 全局错误边界：渲染期异常（尤其是 dist 重建后旧标签页加载失效 chunk）
 * 不再白屏。chunk 加载失败自动刷新一次（带 sessionStorage 防循环）；
 * 其他错误给恢复界面。
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error): void {
    if (!isChunkLoadError(error)) return;
    const last = Number(sessionStorage.getItem(RELOAD_FLAG) || 0);
    // 30 秒内只自动刷新一次，防止坏部署造成刷新循环
    if (Date.now() - last > 30_000) {
      sessionStorage.setItem(RELOAD_FLAG, String(Date.now()));
      window.location.reload();
    }
  }

  render(): ReactNode {
    const { error } = this.state;
    if (error === null) return this.props.children;
    const chunk = isChunkLoadError(error);
    return (
      <Result
        status="warning"
        title={chunk ? "页面资源已更新" : "页面出错了"}
        subTitle={
          chunk
            ? "前端已发布新版本，刷新后即可继续使用。"
            : "页面出错了，刷新后重试；若持续出现请反馈。"
        }
        extra={
          <Button
            type="primary"
            onClick={() => {
              sessionStorage.removeItem(RELOAD_FLAG);
              window.location.reload();
            }}
          >
            刷新页面
          </Button>
        }
      />
    );
  }
}
