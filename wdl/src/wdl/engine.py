"""wdl 执行引擎 — in-process asyncio 运行时（去 multiprocessing 版）。

承袭 coara WorkflowEngineRuntime 的执行主循环思想，但：

- 引擎直接 in-process asyncio 运行，命令是方法调用（``start`` /
  ``resume_instance`` / ``cancel_instance`` / ``shutdown``），不走 mp.Queue。
- 事件经 asyncio 回调下发：``engine.on_event(cb)``，事件类型
  workflow_started / node_started / node_completed / node_failed /
  workflow_completed / workflow_failed / workflow_cancelled，以及工具循环
  透出的 tool_start / tool_result（负载含 node_id / tool / 摘要）。
- 保留 fencing owner（``wdlengine-<pid>-<rand>``）、孤儿实例恢复
  （启动时 running → interrupted）、关停时 ``persistence.close()`` 收口
  aiosqlite 非守护线程防进程挂死。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from wdl.core.serde import parse_graph
from wdl.executor import LLMNodeExecutor
from wdl.logging import logger
from wdl.paths import instances_db_path
from wdl.persistence import WorkflowPersistence
from wdl.providers import ToolSettings, load_providers_config
from wdl.scanner import WorkflowWakeScanner
from wdl.scheduler import WorkflowOwnershipLost, WorkflowScheduler

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


class WdlEngine:
    """WDL 执行引擎：接收 WDL 文本，自主执行，回调下发进度事件。"""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        node_executor_factory: Callable[..., Any] | None = None,
        scan_interval: float | None = None,
        workdir: str | Path | None = None,
    ) -> None:
        db_path = db_path if db_path is not None else instances_db_path()
        # 引擎 fencing 令牌：pid + 随机串，崩溃重启后不会被复用
        self._owner_id = f"wdlengine-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.persistence = WorkflowPersistence(db_path=db_path, owner_id=self._owner_id)
        self.scheduler = WorkflowScheduler(self.persistence)
        self.scanner: WorkflowWakeScanner | None = None
        self._scan_interval = scan_interval
        # 默认节点执行器工厂：agentic 工具循环 LLM 调用（可注入 fake 便于测试）
        self._executor_factory = node_executor_factory or self._default_executor_factory
        # 内置工具工作目录根（默认当前工作目录）；providers.yaml 供 settings 兜底
        self._workdir = Path(workdir).resolve() if workdir is not None else Path.cwd().resolve()
        self._providers_config = load_providers_config()
        self._running = False
        self._running_tasks: set[asyncio.Task[Any]] = set()
        self._event_callbacks: list[EventCallback] = []
        # 当前正在推进的实例 id（node executor 发事件用）。并发实例的事件
        # 仍可能串（事件仅用于进度展示，不影响执行正确性）
        self._active_instance: str = ""
        # 每个实例的解析后图（节点级/图级 LLM 覆盖从这里取）
        self._task_graphs: dict[str, Any] = {}

    # ── 事件 ────────────────────────────────────────────────────────────

    def on_event(self, callback: EventCallback) -> None:
        """注册事件回调（async；可注册多个）。"""
        self._event_callbacks.append(callback)

    async def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        for cb in list(self._event_callbacks):
            try:
                await cb(event_type, payload)
            except Exception as exc:  # noqa: BLE001 — 回调异常不得拖垮引擎
                logger.warning(f"event callback failed ({event_type}): {exc}")

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动引擎：恢复孤儿 running 实例 + 拉起 wake scanner。"""
        if self._running:
            return
        self._running = True
        recovered = await self.persistence.recover_orphaned_running()
        if recovered:
            logger.info(f"WdlEngine: recovered {recovered} orphaned instance(s) → interrupted")
        self.scanner = WorkflowWakeScanner(
            persistence=self.persistence,
            resume_callback=self._on_wake_resume,
            interval_seconds=self._scan_interval,
        )
        self.scanner.start()
        logger.info("WdlEngine started")

    async def shutdown(self) -> None:
        """关停引擎：停 scanner、取消在跑任务、关闭 persistence（防 aiosqlite 挂死）。"""
        self._running = False
        if self.scanner:
            self.scanner.stop()
            self.scanner = None
        if self._running_tasks:
            for task in list(self._running_tasks):
                task.cancel()
            done, pending = await asyncio.wait(list(self._running_tasks), timeout=5.0)
            for task in pending:
                logger.warning(f"Workflow task still running after shutdown timeout: {task!r}")
        # aiosqlite 连接线程非守护：不显式 close 进程退出会挂死
        try:
            await self.persistence.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"WdlEngine: persistence close failed: {exc}")
        logger.info("WdlEngine stopped")

    # ── 命令（直接方法调用，替代 mp.Queue 命令） ───────────────────────

    async def start_workflow(
        self,
        wdl_text: str,
        inputs: dict[str, Any] | None = None,
        *,
        instance_id: str | None = None,
    ) -> str:
        """提交一个 WDL 工作流异步执行；返回 instance_id。"""
        wdl_text = wdl_text.strip()
        if not wdl_text:
            raise ValueError("start_workflow 缺少 wdl 文本")
        task_id = instance_id or f"wf-{uuid.uuid4().hex[:8]}"
        task = asyncio.create_task(self._execute_workflow(task_id, wdl_text, dict(inputs or {})))
        self._track_task(task)
        return task_id

    async def resume_instance(self, instance_id: str, event_data: dict[str, Any] | None = None) -> None:
        """恢复一个挂起/中断的实例（异步执行）。"""
        task = asyncio.create_task(self._resume_workflow(instance_id, event_data))
        self._track_task(task)

    async def cancel_instance(self, instance_id: str) -> None:
        """取消一个实例。"""
        await self.scheduler.cancel(instance_id)
        await self.persistence.cancel_instance(instance_id)
        await self._emit_event("workflow_cancelled", {"task_id": instance_id})

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        self._running_tasks.add(task)

        def _on_done(t: asyncio.Task[Any]) -> None:
            self._running_tasks.discard(t)
            if not t.cancelled() and t.exception():
                logger.warning(f"workflow task failed: {t.exception()}")

        task.add_done_callback(_on_done)

    # ── 执行主循环 ──────────────────────────────────────────────────────

    def _default_executor_factory(
        self,
        graph_llm: tuple[str, str],
        node_llms: dict[str, tuple[str, str]],
        node_tools: dict[str, list[str] | None],
        tool_settings: ToolSettings,
    ) -> Any:
        async def forward_tool_event(event_type: str, payload: dict[str, Any]) -> None:
            # executor 不知道 instance_id，这里补打后透传给引擎事件订阅者
            await self._emit_event(event_type, {"instance_id": self._active_instance, **payload})

        return LLMNodeExecutor(
            self._providers_config,
            graph_llm=graph_llm,
            node_llms=node_llms,
            node_tools=node_tools,
            tool_settings=tool_settings,
            workdir=self._workdir,
            on_event=forward_tool_event,
        )

    def _build_node_executor(self, instance_id: str) -> Any:
        graph = self._task_graphs.get(instance_id)
        graph_llm = ("", "")
        node_llms: dict[str, tuple[str, str]] = {}
        node_tools: dict[str, list[str] | None] = {}
        tool_settings = ToolSettings()
        if graph is not None:
            graph_llm = (graph.provider, graph.model)
            node_llms = {nid: (n.provider, n.model) for nid, n in graph.nodes.items()}
            node_tools = {nid: n.tools for nid, n in graph.nodes.items()}
            raw_settings = graph.settings or {}
            tool_settings = ToolSettings(
                max_tool_rounds=int(raw_settings.get("max_tool_rounds") or 0),
                shell_timeout=float(raw_settings.get("shell_timeout") or 0.0),
            )
        raw_executor = self._executor_factory(graph_llm, node_llms, node_tools, tool_settings)

        async def node_executor(*, node_id: str, prompt: str, task: str) -> str:
            await self._emit_event("node_started", {"instance_id": instance_id, "step_id": node_id})
            try:
                result = await raw_executor(node_id=node_id, prompt=prompt, task=task)
            except Exception as exc:
                await self._emit_event(
                    "node_failed",
                    {"instance_id": instance_id, "step_id": node_id, "error": str(exc)},
                )
                raise
            await self._emit_event("node_completed", {"instance_id": instance_id, "step_id": node_id})
            return str(result or "")

        return node_executor

    async def _execute_workflow(self, task_id: str, wdl_text: str, inputs: dict[str, Any]) -> None:
        self._active_instance = task_id
        try:
            graph = parse_graph(wdl_text)
            self._task_graphs[task_id] = graph
            await self._emit_event("workflow_started", {"task_id": task_id, "name": graph.name})
            context = await self.scheduler.execute(
                instance_id=task_id,
                wdl_text=wdl_text,
                inputs=inputs,
                node_executor=self._build_node_executor(task_id),
            )
            await self._emit_event(
                "workflow_completed",
                {"task_id": task_id, "context": self._serialize_context(context)},
            )
        except WorkflowOwnershipLost:
            # 实例归另一引擎（fencing 失配）：安静退出，不发终态事件、不改状态
            logger.info(f"Workflow {task_id} is owned by another engine; aborting local run")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Workflow {task_id} failed: {exc}")
            await self._emit_event("workflow_failed", {"task_id": task_id, "error": str(exc)})
        finally:
            self._task_graphs.pop(task_id, None)
            self._active_instance = ""

    async def _resume_workflow(self, instance_id: str, event_data: dict[str, Any] | None) -> None:
        self._active_instance = instance_id
        try:
            if instance_id not in self._task_graphs:
                row = await self.persistence.load_instance(instance_id)
                if row is not None and row.wdl_text:
                    with contextlib.suppress(Exception):
                        self._task_graphs[instance_id] = parse_graph(row.wdl_text)
            context = await self.scheduler.resume(
                instance_id=instance_id,
                event_data=event_data,
                node_executor=self._build_node_executor(instance_id),
            )
            await self._emit_event(
                "workflow_completed",
                {"task_id": instance_id, "context": self._serialize_context(context)},
            )
        except WorkflowOwnershipLost:
            logger.info(f"Resume {instance_id} skipped: instance owned by another engine")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Resume {instance_id} failed: {exc}")
            await self._emit_event("workflow_failed", {"task_id": instance_id, "error": str(exc)})
        finally:
            self._task_graphs.pop(instance_id, None)
            self._active_instance = ""

    async def _on_wake_resume(self, instance_id: str, event_data: dict[str, Any] | None = None) -> None:
        """wake scanner 回调：直接走 resume（原 coara 经 mp.Queue 回投命令）。"""
        await self._resume_workflow(instance_id, event_data)

    def _serialize_context(self, context: dict[str, Any]) -> dict[str, Any]:
        """序列化 context 供事件负载（去内部键，非 JSON 值转字符串）。"""
        import json

        result: dict[str, Any] = {}
        for key, value in context.items():
            if key.startswith("_"):
                continue
            try:
                json.dumps(value)
                result[key] = value
            except (TypeError, ValueError):
                result[key] = str(value)
        return result
