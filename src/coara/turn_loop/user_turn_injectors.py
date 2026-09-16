"""User-turn injectors — pluggable steps for ``begin_user_turn``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from src.core.types import Message, MessageRole


@dataclass(slots=True)
class UserTurnContext:
    """Mutable turn state passed through the injector chain."""

    content: str
    image_blocks: list[dict[str, Any]] | None = None


class UserTurnInjector(Protocol):
    """Append to history or mutate ``ctx`` during user-turn preparation."""

    async def __call__(self, coara: Any, ctx: UserTurnContext) -> None: ...


async def inject_environment_seed(coara: Any, ctx: UserTurnContext) -> None:
    if not getattr(coara, "inject_environment_seed", True):
        return
    from src.coara.injections.context_modules import SEED_SCAN_HEAD_LIMIT, build_missing_prefix_messages
    from src.utils.message_content import message_content_to_text

    history = coara.message_history
    # 认领判据只看历史头部若干条（与 is_context_module_seed 同源 startswith），
    # 不再每回合全量转文本——长会话每轮 O(历史) 的拼接随之消除。
    texts = [message_content_to_text(getattr(m, "content", "") or "") for m in history[:SEED_SCAN_HEAD_LIMIT]]
    import asyncio

    messages = await asyncio.to_thread(
        build_missing_prefix_messages,
        texts,
        coara.workspace_dir,
        include_git=getattr(coara.identity.persona, "name", "") == "coaras",
    )
    if not messages:
        return
    from src.core.logger import logger

    logger.info(
        "inject missing context modules: {}",
        [str(getattr(m, "content", ""))[len("<系统消息>") :][:14].replace("\n", " ") for m in messages],
    )
    # 种子插回历史头部：extend 到尾部会让种子跑到对话中段，压缩的头部保护
    # 扫描不到（只保护开头连续的系统消息），种子被压进摘要——上下文错乱根源。
    coara.message_history[:0] = messages


_SEED_DATE_LINE_RE = re.compile(r"(- 今天日期：)(\d{4})年(\d{2})月(\d{2})日 ([^\n]+)")


async def inject_day_change_note(coara: Any, ctx: UserTurnContext) -> None:
    """跨天注记：环境上下文 seed 里的日期落后于今天时，垫一条 <系统消息> 并把 seed 日期就地修正。

    seed 只在会话新建时写日期；持续活跃不触发自动 /new 的会话跨过午夜后日期一直是错的。
    修正 seed 既是当天去重依据（修正后日期 == 今天，不再注入），也避免 seed 与注记长期矛盾；
    代价是每天至多一次 prompt 前缀失效，可接受。
    """
    if getattr(coara, "delegate_depth", 0) != 0:
        return
    from datetime import datetime

    from src.coara.injections.environment_injector import ENV_CONTEXT_PREFIX
    from src.core.message_tags import system_info
    from src.utils.message_content import message_content_to_text

    history = coara.message_history
    seed_msg = None
    seed_text = ""
    # Prefix modules may occupy several head slots; scan past them for 环境上下文.
    for msg in history[:12]:
        text = message_content_to_text(msg.content)
        if ENV_CONTEXT_PREFIX in text:
            seed_msg = msg
            seed_text = text
            break
    if seed_msg is None:
        return
    match = _SEED_DATE_LINE_RE.search(seed_text)
    if match is None:
        return
    seed_ymd = (int(match.group(2)), int(match.group(3)), int(match.group(4)))
    now = datetime.now()
    today = (now.year, now.month, now.day)
    if seed_ymd >= today:
        return
    today_str = now.strftime("%Y年%m月%d日 %A")
    seed_str = f"{match.group(2)}年{match.group(3)}月{match.group(4)}日 {match.group(5)}"
    history.append(
        Message(
            role=MessageRole.USER,
            content=system_info(
                f"日期变更：现在是 {today_str}。会话开头环境上下文标注的日期（{seed_str}）已过期，"
                "已就地修正；此前对话中的「今天」「昨天」等相对时间按其当时语境理解。"
            ),
        )
    )
    if isinstance(seed_msg.content, str):
        seed_msg.content = _SEED_DATE_LINE_RE.sub(f"- 今天日期：{today_str}", seed_msg.content, count=1)


async def inject_flow_draft_overview(coara: Any, ctx: UserTurnContext) -> None:
    """当前草案画布概况（仅构建对话 FlowRoot）：切草案/画布变更时在用户消息前垫一条。

    文本由草案投影现算（draft_overview），sha1 指纹没变不重复注入——
    尾部注入 + 变更才注入，prompt 前缀不受影响。
    """
    if getattr(coara, "_session_agent_kind", "") != "flow":
        return
    root = getattr(coara, "_root_coara", None)
    web_server = getattr(root, "_web_server", None) if root is not None else None
    draft_id = str(getattr(web_server, "active_workflow_draft_id", "") or "")
    if not draft_id:
        coara._flow_draft_overview_fp = ""
        return
    import hashlib

    from src.core.message_tags import system_info
    from src.workflow.draft_overview import build_draft_overview
    from src.workflow.draft_store import WorkflowDraftStore

    try:
        store = WorkflowDraftStore()
        draft = store.get(draft_id)
        overview = build_draft_overview(draft) if draft is not None else None
    except Exception:
        overview = None
    if not overview:
        # 草案被删/解析失败：复位指纹，下一份草案注入全新概况
        coara._flow_draft_overview_fp = ""
        return
    fp = hashlib.sha1(overview.encode("utf-8", "ignore")).hexdigest()
    if getattr(coara, "_flow_draft_overview_fp", "") == fp:
        return
    coara._flow_draft_overview_fp = fp
    coara.message_history.append(
        Message(
            role=MessageRole.USER,
            content=system_info("当前草案概况（画布最新状态，以此为准）：\n" + overview),
        )
    )


async def inject_user_message(coara: Any, ctx: UserTurnContext) -> None:
    from src.utils.multimodal_content import build_user_content

    # 用户消息进历史一律裸文本：当前端由情境后缀承载，历史归属由投影
    # turn/start 的 source 字段承载，不再现包来源标签。
    user_content = build_user_content(ctx.content, ctx.image_blocks)
    coara.message_history.append(Message(role=MessageRole.USER, content=user_content))


async def inject_vision_reminder(coara: Any, ctx: UserTurnContext) -> None:
    if not ctx.image_blocks or coara.delegate_depth != 0:
        return
    from src.core.message_tags import system_reminder

    coara.message_history.append(
        Message(
            role=MessageRole.USER,
            content=system_reminder("本条消息附带图片，请直接据图作答；图片已在上下文中，不要再用工具获取或截图。"),
        )
    )


async def inject_workspace_message_refs(coara: Any, ctx: UserTurnContext) -> None:
    if coara.delegate_depth != 0:
        return
    from src.coara.injections.workspace_message_injector import (
        build_workspace_message_reminder_messages,
        extract_workspace_message_refs,
    )

    workspace_refs = extract_workspace_message_refs(ctx.content)
    if workspace_refs:
        coara.message_history.extend(build_workspace_message_reminder_messages(workspace_refs))


async def inject_rules(coara: Any, ctx: UserTurnContext) -> None:
    if coara.delegate_depth != 0:
        return
    from src.coara.rules_glob import maybe_build_rules_messages

    messages = maybe_build_rules_messages(ctx.content, Path(coara.workspace_dir))
    if not messages:
        return
    # 去重：同一规则正文已在历史里（含重启前注入的）就不再重复注入
    existing = {m.content for m in coara.message_history if isinstance(m.content, str)}
    fresh = [m for m in messages if m.content not in existing]
    coara.message_history.extend(fresh)


async def inject_config_overview(coara: Any, ctx: UserTurnContext) -> None:
    """当前配置概况（仅 config-assistant 模块会话）：配置变更时在用户消息前垫一条。

    文本由 config_manager 现算（build_config_overview），sha1 指纹没变不重复注入——
    尾部注入 + 变更才注入，prompt 前缀不受影响。
    """
    if getattr(coara, "_session_agent_kind", "") != "config":
        return
    import hashlib

    from src.coara.config_overview import build_config_overview
    from src.core.message_tags import system_info

    try:
        overview = build_config_overview()
    except Exception:
        overview = None
    if not overview:
        coara._config_overview_fp = ""
        return
    fp = hashlib.sha1(overview.encode("utf-8", "ignore")).hexdigest()
    if getattr(coara, "_config_overview_fp", "") == fp:
        return
    coara._config_overview_fp = fp
    coara.message_history.append(
        Message(
            role=MessageRole.USER,
            content=system_info("当前配置概况（以此为准）：\n" + overview),
        )
    )


DEFAULT_USER_TURN_INJECTORS: tuple[UserTurnInjector, ...] = (
    inject_environment_seed,
    inject_day_change_note,
    inject_flow_draft_overview,
    inject_config_overview,
    inject_user_message,
    inject_vision_reminder,
    inject_workspace_message_refs,
    inject_rules,
)
