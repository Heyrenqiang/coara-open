// 工作流页插件入口（内置插件形态）：双构建共用（见 usage 同款注释）。
// 两条路由（/workflow 与 /workflow/editor/:draftId）分别由壳装载器取
// 对应页面组件；槽位 mount 用主页面。
import { createRoot, type Root } from "react-dom/client";
import { WorkflowView } from "./WorkflowPage";
import WorkflowEditorView from "./WorkflowEditorPage";

export default WorkflowView;
export { WorkflowEditorView };

export function mount(el: HTMLElement): () => void {
  const root: Root = createRoot(el);
  root.render(<WorkflowView />);
  return () => root.unmount();
}
