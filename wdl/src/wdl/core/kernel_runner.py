"""内核图执行器 — 引擎侧持久宿主（与 coara 的 FlowCoordinator 共用同一份图语义）。

执行内核投影（nodes + edges，节点=智能体）；legacy WdlRunner 已退役，
投影文本是唯一输入格式。调度语义与内部 FlowCoordinator 完全一致
（wait/kick 边分类、轮次就绪、激活上限），差别只在宿主职责：这里持久化
每步状态到 SQLite、支持挂起恢复。

数据流：input 模板 {{steps.x.text}} 渲染自 context[x]["text"]；
{{inputs.k}} 渲染自运行输入。one 路由在无人值守宿主按静态顺序取第一条
出边（设计上由节点结果引导，见 deliver 的 next 提取）。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from wdl.core import FlowGraph, validate_graph
from wdl.core.semantics import (
    ActivationEngine,
    classify_edges,
    template_step_refs,
)
from wdl.core.serde import parse_graph
from wdl.errors import WDLError as CoaraError
from wdl.logging import logger
from wdl.persistence import WorkflowOwnershipLost, WorkflowPersistence
from wdl.types import WorkflowInstanceState


class KernelGraphRunner:
    """执行内核投影图：持久宿主版激活引擎。"""

    def __init__(self, persistence: WorkflowPersistence) -> None:
        self.persistence = persistence
        self._active: dict[str, set[asyncio.Task[Any]]] = {}
        self._cancelled: set[str] = set()

    async def cancel(self, instance_id: str) -> None:
        self._cancelled.add(instance_id)
        for task in list(self._active.get(instance_id, ())):
            task.cancel()
        await self.persistence.cancel_instance(instance_id, error="cancelled by user")

    async def execute(
        self,
        instance_id: str,
        wdl_text: str,
        inputs: dict[str, Any],
        node_executor,
    ) -> dict[str, Any]:
        graph = self._load_graph(wdl_text)
        issues = validate_graph(graph)
        errors = [i.message for i in issues if i.level == "error"]
        if errors:
            raise CoaraError("内核图校验失败：" + "；".join(errors))
        # 实例行由这里建（与旧 WdlRunner 的 execute 语义一致）：外部只给
        # instance_id + 投影文本，不要求先 create_instance。幂等：调用方
        # 已建过（如引擎外部 create + wdl 归档）则跳过
        row = await self.persistence.load_instance(instance_id)
        if row is None:
            await self.persistence.create_instance(
                instance_id, name=graph.name, wdl_text=wdl_text, inputs=dict(inputs or {})
            )
        await self.persistence.save_state(instance_id, None, {}, status=WorkflowInstanceState.RUNNING)
        return await self._drive(instance_id, graph, node_executor)

    async def resume(
        self,
        instance_id: str,
        event_data: dict[str, Any] | None = None,
        node_executor=None,
        *,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = await self.persistence.load_instance(instance_id)
        if row is None:
            raise CoaraError(f"实例不存在：{instance_id}")
        if row.status in (
            WorkflowInstanceState.COMPLETED,
            WorkflowInstanceState.FAILED,
            WorkflowInstanceState.CANCELLED,
        ):
            raise CoaraError(
                f"Cannot resume instance {instance_id}: status is {row.status.value} "
                f"(only interrupted or waiting instances can be resumed)"
            )
        wdl_text = row.wdl_text or ""
        graph = self._load_graph(wdl_text)
        context = dict(row.context_snapshot or {})
        # resume 即认领：写 RUNNING（fenced 下 owner 换成当前令牌；旧引擎
        # 未死透时的下一次写入必然失配）。recover 后的 interrupted 行上
        # 残留旧 owner，不认领会在终态写入时被守卫拒绝
        await self.persistence.save_state(instance_id, None, context, status=WorkflowInstanceState.RUNNING)
        return await self._drive(instance_id, graph, node_executor, context=context)

    def _load_graph(self, wdl_text: str) -> FlowGraph:
        return parse_graph(wdl_text)

    async def _drive(
        self,
        instance_id: str,
        graph: FlowGraph,
        node_executor,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = dict(context or {})
        engine = ActivationEngine(graph)
        cls = classify_edges(graph)
        pending_arrivals: dict[str, list[tuple[str, str]]] = {}

        def render(text: str) -> str:
            out = text or ""
            for ref in template_step_refs(out):
                value = str(((context.get(ref) or {}).get("text")) or "")
                out = re.sub(r"\{\{\s*steps\." + re.escape(ref) + r"\.\w+\s*\}\}", value, out)
            return out

        async def run_node(node_id: str, activation_arrivals: list[tuple[str, str]]) -> None:
            node = graph.nodes[node_id]
            prompt_parts = [render(node.task)]
            if node.input:
                prompt_parts.append(f"【输入】\n{render(node.input)}")
            for frm, text in activation_arrivals:
                prompt_parts.append(f"【来自 {frm}】\n{text}")
            if node.routes == "one":
                choices = "、".join(graph.routes_to(node_id))
                prompt_parts.append(f"你的可选后继节点：{choices}。完成后用 deliver(message=结果, next=后继) 交付。")
            prompt = "\n\n".join(p for p in prompt_parts if p)
            result = await node_executor(node_id=node_id, prompt=prompt, task=node.task)
            context[node_id] = {"text": str(result or "")}

        running: dict[asyncio.Task[Any], str] = {}
        delivered: set[tuple[str, int]] = set()  # (node_id, 激活轮次) 已投递

        def _deliver_done(nid: str, round_idx: int, text: str) -> None:
            if (nid, round_idx) in delivered:
                return
            delivered.add((nid, round_idx))
            node_ctx = context.setdefault(nid, {})
            node_ctx.pop("status", None)
            node_ctx["text"] = text
            self._deliver(graph, cls, engine, nid, text, pending_arrivals)

        try:
            while True:
                if instance_id in self._cancelled:
                    raise CoaraError("cancelled")
                # 投递 pending 到达
                for nid in list(pending_arrivals):
                    for frm, text in pending_arrivals.pop(nid):
                        engine.record_arrival(frm, nid, text)
                started_any = False
                for nid in graph.nodes:
                    if not engine.is_ready(nid):
                        continue
                    activation = engine.consume(nid)
                    if activation is None:
                        continue
                    started_any = True
                    context[nid] = {"status": "running"}

                    async def _run_and_deliver(node_id: str, act) -> None:
                        try:
                            await run_node(node_id, act.arrivals)
                        except Exception as exc:  # noqa: BLE001
                            context[node_id] = {"text": f"[错误] {exc}"}
                        text = str((context.get(node_id) or {}).get("text", ""))
                        _deliver_done(node_id, act.round_index, text)

                    task = asyncio.create_task(_run_and_deliver(nid, activation))
                    running[task] = nid
                if started_any or running:
                    await self._persist(instance_id, graph, context)
                if running:
                    done, _ = await asyncio.wait(set(running), return_when=asyncio.FIRST_COMPLETED)
                    for t in done:
                        running.pop(t, None)
                    continue
                if not started_any:
                    break
            # 终态：只统计没有 error 兜底成功消化掉的失败
            failures = []
            for nid in graph.nodes:
                text = str((context.get(nid) or {}).get("text", ""))
                if not text.startswith("[错误]"):
                    continue
                out_edges = graph.out_edges(nid)
                has_error_route = any(e.on == "error" for e in out_edges)
                if not has_error_route:
                    failures.append(nid)
            error = f"节点失败：{failures}" if failures else None
            ok = await self.persistence.complete_instance(instance_id, context, error=error)
            if not ok:
                # fenced 失守：终态写入被拒（实例归另一引擎）——返回实际行状态
                row = await self.persistence.load_instance(instance_id)
                actual = row.status.value if row else WorkflowInstanceState.FAILED.value
                return {"instance_id": instance_id, "status": actual, "context": context}
            final_status = WorkflowInstanceState.FAILED.value if failures else WorkflowInstanceState.COMPLETED.value
            return {"instance_id": instance_id, "status": final_status, "context": context}
        except WorkflowOwnershipLost:
            # 失守：实例归另一引擎，安静放弃——不写终态（cancel 会覆写状态）
            logger.info(f"kernel graph workflow {instance_id} ownership lost; aborting quietly")
            raise
        except asyncio.CancelledError:
            await self.persistence.cancel_instance(instance_id, error="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"kernel graph workflow {instance_id} failed: {exc}")
            await self.persistence.cancel_instance(instance_id, error=str(exc))
            raise

    def _deliver(
        self,
        graph: FlowGraph,
        cls: dict,
        engine: ActivationEngine,
        node_id: str,
        result_text: str,
        pending_arrivals: dict[str, list[tuple[str, str]]],
    ) -> None:
        """按出边与路由模式投递结果（one：context 里 next 提示优先）。"""
        node = graph.nodes.get(node_id)
        out_edges = graph.out_edges(node_id)
        failed = str(result_text).startswith("[错误]")
        if failed:
            chosen = [e for e in out_edges if e.on == "error"]
        elif node is not None and node.routes == "all":
            chosen = [e for e in out_edges if e.on == "success"]
        else:
            # one：静态顺序取第一条（无人值守宿主的静态条件占位）
            chosen = [e for e in out_edges if e.on == "success"][:1]
        for e in chosen:
            pending_arrivals.setdefault(e.to, []).append((e.frm, str(result_text)))

    async def _persist(self, instance_id: str, graph: FlowGraph, context: dict[str, Any]) -> None:
        await self.persistence.save_state(instance_id, None, context)
