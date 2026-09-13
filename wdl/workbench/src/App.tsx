import { useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { WorkflowView } from "./views/WorkflowView";
import { getWS } from "./lib/ws";

export default function App() {
  // 挂载即建立引擎事件通道（WS 断线自动重连）
  useEffect(() => {
    getWS();
  }, []);

  return (
    <Routes>
      <Route path="/workflow" element={<WorkflowView />} />
      <Route path="*" element={<Navigate to="/workflow" replace />} />
    </Routes>
  );
}
