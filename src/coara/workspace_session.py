"""WorkspaceSession：一个工作空间的完整运行时状态管理。

持有 CoaraBase 实例，管理工作空间的对话历史、工具、技能等状态。
每个已切入的工作空间（含启动默认空间）都是对等的 WorkspaceSession；
RootCoara 只做进程宿主（EventBus / scheduler / vault 服务等）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.logger import logger

if TYPE_CHECKING:
    from src.coara.base import CoaraBase
    from src.coara.root import RootCoara



class WorkspaceSession:
    """Per-workspace runtime container with its own ``CoaraBase``.

    All user-facing workspaces (including the startup one) use this class.
    RootCoara holds process-global services and never doubles as the
    conversation agent once initialization has bound a foreground session.
    """

    def __init__(
        self,
        coara: CoaraBase,
        *,
        workspace_id: str,
        workspace_name: str,
        root_coara: RootCoara,
    ) -> None:
        self.coara = coara
        self.workspace_id = workspace_id
        self.workspace_name = workspace_name
        self.root_coara = root_coara

    @property
    def workspace_dir(self) -> Path:
        return self.coara.workspace_dir

    @property
    def session_id(self) -> str:
        return self.coara.session_id
    async def persist_to_disk(self) -> None:
        """Persist session_id + full message_history to disk."""
        try:
            await asyncio.to_thread(self.coara.persist_session_to_disk)
        except Exception as exc:
            logger.warning(f"Failed to persist session state for {self.workspace_name}: {exc}")

    @classmethod
    async def create_for_workspace(
        cls,
        entry: Any,
        root_coara: RootCoara,
    ) -> WorkspaceSession:
        """为工作空间创建新的 WorkspaceSession。

        1. 创建 CoaraBase 实例（和 coara 分身创建方式一致）
        2. 注册 runtime tools
        3. 设置 trace sink（共享 RootCoara 的 EventBus）
        4. 从磁盘恢复 message_history（如果有）
        5. 加载技能
        """
        from src.coara.base import CoaraBase
        from src.coara.runtime_tools import register_runtime_tools
        from src.workspace.llm_binding import entry_llm_override
        from src.workspace.types import WorkspaceKind

        # 空间级 LLM 绑定：条目带 provider/model 则覆盖；否则跟随 Root（= 全局默认）
        bind_provider, bind_model = entry_llm_override(entry)

        is_internal = getattr(entry, "kind", None) == WorkspaceKind.INTERNAL

        # 专属对话主体：条目带 persona 即解析（internal 系统空间如记录 persona=daily；
        # 创作者空间如工作流 persona=flow-root）。缺省 None = 无专属主体，用 root 身份
        # 对话（展示名不是 agent 名，不可拿去查注册表）。persona 与 kind 解耦——创作者
        # 空间是普通用户空间（可移出/可删），但同样要自己的会话主体。
        internal_cfg = None
        persona_key = str(getattr(entry, "persona", None) or "").strip()
        if persona_key:
            from src.coara.builtin_agents import get_module_persona, get_subagent

            # 先查 delegate 名单（daily），再查模块 persona 惰性加载器
            # （flow-root / config-assistant 等：文件齐备即得，不是可委派类型）。
            internal_cfg = get_subagent(persona_key) or get_module_persona(persona_key)
            if internal_cfg is None and is_internal:
                raise RuntimeError(f"internal workspace '{entry.name}' missing builtin agent config")
        coara_name = internal_cfg.name if internal_cfg is not None else root_coara.identity.name

        # 创建 CoaraBase 实例（用户空间用 root persona；internal 空间用专属 persona）
        persona = root_coara.identity.persona
        if internal_cfg is not None:
            from src.core.types import CoaraPersona

            persona = CoaraPersona(
                name=internal_cfg.name,
                role=internal_cfg.role,
                system_prompt_template=internal_cfg.system_prompt,
                yaml_config=internal_cfg.yaml_config,
            )
        kwargs: dict[str, Any] = {}
        if bind_provider is not None:
            kwargs = {"provider_name": bind_provider, "model": bind_model}
        else:
            kwargs = {
                "provider": root_coara.provider,
                "provider_name": root_coara.provider_name,
                "model": root_coara.model_name,
            }
        coara = CoaraBase(
            name=coara_name,
            persona=persona,
            workspace_dir=entry.resolved_path(),
            user_facing=True,
            is_owner_context=True,
            max_tool_iterations=root_coara._max_tool_iterations,
            session_agent_kind=(persona_key if is_internal else None),
            **kwargs,
        )
        if is_internal:
            # daily 统筹全部工作空间：不注入单空间环境种子（对齐旧 delegate 语义）
            coara.inject_environment_seed = False

        # 共享 workspace_manager（用于 VFS resolver）；对等会话镜像 Root 的运行时属性
        coara.workspace_manager = root_coara.workspace_manager  # type: ignore[attr-defined]
        # 反向引用：压缩成功后经此派 janitor 沉淀（覆盖连续工作不触发 janitor 的盲区）
        coara._root_ref = root_coara

        # 进程级无状态工具（web_fetch / task / …）— 与 Root.bootstrap_tools 对齐
        from src.tools import register_builtin_tools
        from src.tools.registry import tool_registry

        register_builtin_tools()
        coara.register_tools(tool_registry.list_all())

        # 注册 runtime tools（文件工具、delegate、todo 等）
        register_runtime_tools(coara)

        # 注册 Root-scoped 工具（plan_mode, workflow, skill, ws）
        # 与 RootCoara 保持一致的 tool surface，避免切换工作空间后工具缺失。
        # ws_parent 指向 RootCoara，使 WsTool 能触发跨 session 切换。
        from src.coara.tool_registry import register_root_scoped_tools

        register_root_scoped_tools(coara, ws_parent=root_coara)

        # 注册挂起工具入口
        try:
            from src.tools.builtin.integration.tool import ToolGatewayTool

            coara.register_tool(ToolGatewayTool(parent_coara=coara), replace=True)
        except Exception as exc:
            logger.warning(f"Tool gateway registration failed for workspace session {entry.name}: {exc}")

        # record 常驻主会话（与 janitor 工具表一致，利于 prompt cache）；local_search 仍挂起。
        coara.records_store = getattr(root_coara, "records_store", None)  # type: ignore[attr-defined]
        if getattr(root_coara, "records_store", None) is not None:
            from src.tools.builtin.records.local_search import LocalSearchTool
            from src.tools.builtin.records.record import RecordTool

            coara.register_tool(
                RecordTool(store=root_coara.records_store, parent_coara=coara),
                replace=True,
            )
            coara.register_tool(
                LocalSearchTool(store=root_coara.records_store, parent_coara=coara),
                replace=True,
            )

        # Vault / reminder：服务仍在 Root，工具挂到每个对等 session
        if root_coara.vault_service is not None:
            from src.tools.builtin.vault.vault import VaultTool

            # 对等会话不带自己的服务，镜像 Root 的引用——否则工具内
            # get_vault_service(coara) 拿到 None，误报「宝箱未启用」。
            coara.vault_service = root_coara.vault_service  # type: ignore[attr-defined]
            coara.register_tool(VaultTool(parent_coara=coara), replace=True)
        if root_coara.reminder_service is not None:
            from src.tools.builtin.manifest import REMINDER_TOOL_TYPE

            coara.register_tool(REMINDER_TOOL_TYPE(root_coara.reminder_service), replace=True)

        # 如果 Root 有 send_file（OutboundFileRouter：Matrix 与/或 Web），镜像到 session
        root_tool_manager = getattr(root_coara, "_tool_manager", None)
        if root_tool_manager is not None and "send_file" in root_tool_manager.tools:
            from src.tools.builtin.integration.outbound_file import OutboundFileRouter
            from src.tools.builtin.integration.send_file import SendFileTool

            root_send_file = root_tool_manager.tools["send_file"]
            bridge = getattr(root_send_file, "_bridge", None)
            if bridge is None:
                bridge = getattr(root_coara, "_outbound_file_router", None)
            if isinstance(bridge, OutboundFileRouter):
                coara.register_tool(
                    SendFileTool(bridge=bridge, workspace_root=entry.resolved_path()),
                    replace=True,
                )
            else:
                logger.warning(f"Root send_file exists but bridge not found for workspace session {entry.name}")

        # 设置 trace sink（共享 RootCoara 的 EventBus）
        coara.set_trace_sink(root_coara.event_bus.publish)

        await coara.load_skills()

        # 从磁盘恢复状态（如果有）
        await cls._restore_from_disk(coara, entry, root_coara)

        if internal_cfg is not None:
            cls._customize_internal_session(coara, entry, internal_cfg, root_coara)

        session = cls(
            coara,
            workspace_id=entry.id,
            workspace_name=entry.name,
            root_coara=root_coara,
        )
        logger.info(f"WorkspaceSession created for {entry.name} (id={entry.id})")
        return session

    @staticmethod
    def _customize_internal_session(
        coara: CoaraBase,
        entry: Any,
        cfg: Any,
        root_coara: RootCoara,
    ) -> None:
        """internal 系统空间定制：工具白名单物理裁剪 + record/local_search 绑定。

        时序：先恢复磁盘历史再裁剪——裁剪会触发 prompt 缓存失效，恢复不受影响；
        record 挂进 _tool_manager 后 owner_only 门在 system prompt 构建时放行
        （is_owner_context=True）。daily.md 职责说明已在 persona.system_prompt_template
        （yaml+md 配对由 get_subagent 装载），不再额外注入。
        """
        allow = set(cfg.tools or [])
        if allow:
            for tool_name in list(coara._tool_manager.tools.keys()):
                if tool_name not in allow:
                    coara._tool_manager.tools.pop(tool_name, None)
        store = getattr(root_coara, "records_store", None)
        if store is not None:
            from src.tools.builtin.records.local_search import LocalSearchTool
            from src.tools.builtin.records.record import RecordTool

            coara.register_tool(RecordTool(store=store, parent_coara=coara), replace=True)
            coara.register_tool(LocalSearchTool(store=store, parent_coara=coara, defer=False), replace=True)

    @staticmethod
    async def _restore_from_disk(
        coara: CoaraBase,
        entry: Any,
        root_coara: RootCoara,
    ) -> None:
        """从磁盘恢复 message_history 和 session_id。"""
        if root_coara.workspace_manager is None:
            return

        from src.coara.workspace_state import (
            load_usage_snapshot_for_session,
            recover_session,
        )

        # recover_session 内部是 read_events + project_session 的同步文件 I/O，
        # 本方法在 _sessions_lock 内被调用，直接同步跑会阻塞事件循环——
        # 与 persist_to_disk 一致走 to_thread（recover_session 纯同步、无循环依赖）
        recovered = await asyncio.to_thread(
            recover_session,
            entry.resolved_path(),
            coara_home=root_coara.workspace_manager.coara_home,
            label=entry.name,
            keep_session_id_on_replay_failure=True,
        )
        if recovered is None:
            return
        last_sid, history, projection = recovered

        from src.coara.injections.context_modules import is_context_module_seed
        from src.coara.injections.environment_injector import build_environment_seed_messages
        from src.utils.message_content import message_content_to_text

        has_seed = any(is_context_module_seed(message_content_to_text(m.content)) for m in history)
        if not has_seed:
            seed = build_environment_seed_messages(coara.workspace_dir)
            history = [*seed, *history]
            # 种子是纯前置、注记是纯追加：两者都不丢弃投影本体。游标始终对齐投影，
            # 首次落盘走 sync_history 的前缀对账（追加补记 / 影子截断重写）；
            # 若不喂游标，恢复后首次落盘会把整段历史当新事件重复写盘（续盘复制），
            # 重启后同一 tool_call 在投影里出现两份、provider 以
            # "No tool output found for tool call ..." 400 拒掉整个请求

        if projection is not None:
            from src.session_log.recorder import message_key as _mk

            coara._apply_restored_state(
                last_sid,
                history,
                restored_event_keys=[_mk(m) for m in projection.messages],
                restored_event_seqs=list(projection.seqs),
                usage_snapshot=projection.usage_snapshot,
            )
        else:
            coara._apply_restored_state(last_sid, history)

        # Restore the last provider-reported usage snapshot so the toolbar can
        # show the real context size immediately (no local estimation).
        usage_snapshot = load_usage_snapshot_for_session(
            last_sid,
            entry.resolved_path(),
            coara_home=root_coara.workspace_manager.coara_home,
        )
        if usage_snapshot:
            coara._llm_usage_snapshot.restore(usage_snapshot)
            # If history shrank vs the fingerprint (e.g. env-seed missing on the
            # restored rows), clamp so resolve_input_tokens does not trigger a
            # full recount. Growth (env-seed prepend) keeps the old baseline so
            # only the delta is estimated. 恢复是全量的，历史不会被裁剪。
            n = len(coara.message_history)
            hl = coara._llm_usage_snapshot.history_len
            if hl is not None and n < hl:
                coara._llm_usage_snapshot.history_len = n
        logger.info(f"Restored session {last_sid} for {entry.name} from disk ({len(history)} messages)")
