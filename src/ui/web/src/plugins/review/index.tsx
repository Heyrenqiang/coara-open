// 消息页插件入口（内置插件形态）：双构建共用（见 usage 同款注释）。
import { createRoot, type Root } from "react-dom/client";
import { ReviewView } from "./ReviewPage";

export default ReviewView;

export function mount(el: HTMLElement): () => void {
  const root: Root = createRoot(el);
  root.render(<ReviewView />);
  return () => root.unmount();
}
