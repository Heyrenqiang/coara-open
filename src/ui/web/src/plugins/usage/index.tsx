// 用量页插件入口（内置插件形态）：mount 契约 + default export 页面组件。
// 双构建共用本文件：主构建的路由装载器 import default 组件直接渲染；
// 插件构建（plugin-kit）经 mount 挂到槽位 DOM。
import { createRoot, type Root } from "react-dom/client";
import { UsageView } from "./UsagePage";

export default UsageView;

/** 槽位 mount 契约：路由外的整页槽位（空间 home_view=plugin:…）用。 */
export function mount(el: HTMLElement): () => void {
  const root: Root = createRoot(el);
  root.render(<UsageView />);
  return () => root.unmount();
}
