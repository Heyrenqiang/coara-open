"""Composable context modules injected around conversation history.

Default stack (devtools can reorder / toggle)::

    system prompt
    user_rules   → <系统提醒>
    environment  → <系统消息>
    AGENTS.md    → <系统消息>
    ws.md        → <系统消息>
    [conversation]
    situation.md → <情境>  (ephemeral, appended each LLM turn)

Config: ``<coara_home>/system/context_modules.yaml``.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# Re-export / share prefixes with environment_injector
from src.coara.injections.environment_injector import (
    ENV_CONTEXT_PREFIX,
    WS_OVERVIEW_PLACEHOLDER,
    WS_PROTOCOL_PREFIX,
)
from src.core.message_tags import situation, system_info, system_reminder
from src.core.types import Message, MessageRole

AGENT_MD_PREFIX = "AGENTS.md：\n"
USER_RULES_PREFIX = "用户规则：\n"
SITUATION_PREFIX = "情境：\n"

CONVERSATION_ID = "conversation"
MODULE_IDS = ("user_rules", "environment", "agent_md", "ws_overview", "situation")

_CONFIG_NAME = "context_modules.yaml"
_DEFAULT_LOCATION = "江西赣州信丰"

# Marker prefixes used to recognize seed messages (any wrap tag).
MODULE_CONTENT_PREFIXES: tuple[str, ...] = (
    USER_RULES_PREFIX,
    ENV_CONTEXT_PREFIX,
    AGENT_MD_PREFIX,
    WS_PROTOCOL_PREFIX,
    SITUATION_PREFIX,
)

# 模块 id → 该模块正文的内容前缀。补注入按它逐模块认领：历史里已经有哪个前缀，
# 就只跳过那一个模块。「有任一种子就整批跳过」会让部分丢失变成永久丢失。
MODULE_CONTENT_PREFIX_BY_ID: dict[str, str] = {
    "user_rules": USER_RULES_PREFIX,
    "environment": ENV_CONTEXT_PREFIX,
    "agent_md": AGENT_MD_PREFIX,
    "ws_overview": WS_PROTOCOL_PREFIX,
}

MODULE_META: dict[str, dict[str, str]] = {
    "user_rules": {"label": "用户规则", "file": "user_rules.md", "scope": "system"},
    "environment": {"label": "环境上下文", "file": "", "scope": "runtime"},
    "agent_md": {"label": "AGENTS.md", "file": "AGENTS.md", "scope": "workspace"},
    "ws_overview": {"label": "工作空间概况", "file": "ws.md", "scope": "workspace_coara"},
    "situation": {"label": "情境", "file": "situation.md", "scope": "system"},
}


@dataclass(frozen=True, slots=True)
class ModuleSlot:
    id: str
    enabled: bool = True


def default_module_order() -> list[ModuleSlot]:
    return [
        ModuleSlot("user_rules", True),
        ModuleSlot("environment", True),
        ModuleSlot("agent_md", True),
        ModuleSlot("ws_overview", True),
        ModuleSlot(CONVERSATION_ID, True),
        ModuleSlot("situation", True),
    ]


def _resolve_system_dir() -> Path | None:
    try:
        from src.core.coara_home import resolve_coara_home, system_dir_for_home
        from src.core.config import config_manager

        configured = getattr(getattr(config_manager, "config", None), "coara_home", None)
        home = resolve_coara_home(Path.cwd(), configured)
        return system_dir_for_home(home)
    except Exception:
        return None


def context_modules_config_path(system_dir: Path | None = None) -> Path | None:
    root = system_dir if system_dir is not None else _resolve_system_dir()
    if root is None:
        return None
    return Path(root) / _CONFIG_NAME


def _normalize_order(raw: Any) -> list[ModuleSlot]:
    """Parse yaml order; ensure conversation sentinel once; fill missing modules."""
    slots: list[ModuleSlot] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                mid = item.strip()
                enabled = True
            elif isinstance(item, dict):
                mid = str(item.get("id") or "").strip()
                enabled = bool(item.get("enabled", True))
            else:
                continue
            if not mid or mid in seen:
                continue
            if mid != CONVERSATION_ID and mid not in MODULE_IDS:
                continue
            if mid == CONVERSATION_ID:
                enabled = True
            slots.append(ModuleSlot(mid, enabled))
            seen.add(mid)
    # Append any missing defaults (preserve relative default order for missing ones)
    for slot in default_module_order():
        if slot.id not in seen:
            # Insert conversation before situation if both missing handled by defaults walk
            slots.append(slot)
            seen.add(slot.id)
    # Exactly one conversation
    conv_idxs = [i for i, s in enumerate(slots) if s.id == CONVERSATION_ID]
    if not conv_idxs:
        slots.append(ModuleSlot(CONVERSATION_ID, True))
    elif len(conv_idxs) > 1:
        keep = conv_idxs[0]
        slots = [s for i, s in enumerate(slots) if s.id != CONVERSATION_ID or i == keep]
    return slots


def load_module_order(*, system_dir: Path | None = None) -> list[ModuleSlot]:
    path = context_modules_config_path(system_dir)
    if path is None or not path.is_file():
        return default_module_order()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return default_module_order()
    return _normalize_order(data.get("order") if isinstance(data, dict) else None)


def save_module_order(order: list[ModuleSlot] | list[dict[str, Any]], *, system_dir: Path | None = None) -> Path:
    path = context_modules_config_path(system_dir)
    if path is None:
        raise RuntimeError("无法定位 coara_home/system，无法保存 context_modules.yaml")
    slots = _normalize_order(order)
    payload = {
        "order": [{"id": s.id} if s.id == CONVERSATION_ID else {"id": s.id, "enabled": s.enabled} for s in slots]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def order_as_dicts(order: list[ModuleSlot] | None = None) -> list[dict[str, Any]]:
    slots = order if order is not None else load_module_order()
    out: list[dict[str, Any]] = []
    for s in slots:
        meta = MODULE_META.get(s.id, {})
        out.append(
            {
                "id": s.id,
                "enabled": True if s.id == CONVERSATION_ID else s.enabled,
                "label": meta.get("label") or ("对话" if s.id == CONVERSATION_ID else s.id),
                "file": meta.get("file") or "",
                "scope": meta.get("scope") or ("sentinel" if s.id == CONVERSATION_ID else ""),
                "sentinel": s.id == CONVERSATION_ID,
            }
        )
    return out


def is_context_module_seed(text: str) -> bool:
    """True when message body is a context-module seed (prefix at body start).

    Unwraps ``<系统消息>`` / ``<系统提醒>`` / ``<情境>`` then requires
    ``startswith`` on a module marker — avoids false positives when user prose
    merely mentions 「工作空间概况」等字样. ``<情境>`` 块本身即视为种子
    （其 body 无内容前缀，端声明是开放文本）。
    """
    if not text:
        return False
    body = text.strip()
    if body.startswith("<情境>"):
        return True
    for open_tag, close_tag in (
        ("<系统消息>", "</系统消息>"),
        ("<系统提醒>", "</系统提醒>"),
    ):
        if body.startswith(open_tag):
            body = body[len(open_tag) :].lstrip("\n")
            if body.endswith(close_tag):
                body = body[: -len(close_tag)].rstrip("\n")
            break
    return any(body.startswith(prefix) for prefix in MODULE_CONTENT_PREFIXES)


def _read_file_text(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    return raw.strip()


def _configured_location() -> str:
    from src.core.config import config_manager

    raw_cfg = getattr(config_manager, "_raw_config", {}) or {}
    env_cfg = raw_cfg.get("environment") or {}
    return str(env_cfg.get("location", _DEFAULT_LOCATION) or "").strip()


def build_environment_body(workspace_dir: Path | str, *, include_git: bool = False) -> str:
    from src.coara.injections.environment_injector import git_status_snapshot

    now = datetime.now().strftime("%Y年%m月%d日 %A")
    body_lines = [f"- 今天日期：{now}"]
    location = _configured_location()
    if location:
        body_lines.append(f"- 地点：{location}")
    body_lines += [
        f"- 操作系统：{platform.system()} {platform.release()}",
        f"- 工作目录：{workspace_dir}",
    ]
    if include_git:
        body_lines.append(f"- Git：\n{git_status_snapshot(workspace_dir)}")
    return ENV_CONTEXT_PREFIX + "\n".join(body_lines)


def load_workspace_protocol(workspace_dir: Path | str) -> str:
    from src.coara.injections.environment_injector import load_workspace_protocol as _load

    return _load(workspace_dir)


def workspace_overview_block(workspace_dir: Path | str) -> str:
    return load_workspace_protocol(workspace_dir) or WS_OVERVIEW_PLACEHOLDER


def _system_file(name: str, *, system_dir: Path | None = None) -> Path | None:
    root = system_dir if system_dir is not None else _resolve_system_dir()
    if root is None:
        return None
    return Path(root) / name


def _wrap_module(module_id: str, body: str) -> str:
    if module_id == "user_rules":
        return system_reminder(body)
    if module_id == "situation":
        return situation(body)
    return system_info(body)


def _end_label(source: str) -> str:
    return {
        "web": "web 端",
        "matrix": "手机端",
        "cli": "CLI 终端",
        "cli-attached": "CLI 终端",
    }.get(source, "本地端")


def build_current_end_line() -> str:
    """当前回合对话端声明（情境正文首行，每轮现取 EndChannel ContextVar）。

    只声明端身份，不附带能力说明；CLI/本地端追加 send_file 禁令（该端无
    文件接收渲染能力，机制层也会拒绝，这里是让 LLM 提前不要发起）。
    """
    try:
        from src.coara.turn_context import get_end_channel

        end = get_end_channel()
        source = str(getattr(end, "source", "") or "").strip().lower()
    except Exception:
        source = ""
    label = _end_label(source)
    line = f"当前对话来自 {label}"
    if source not in ("web", "matrix"):
        line += "\n禁止调用 send_file 工具，交付文件时直接给出文件绝对路径"
    # 禁令内聚到情境本体：块在输入最末尾，注意力最高处管最容易犯的错——
    # 模型回「情境已知会」类知晓回执的根因就是规则在远端系统提示词里。
    line += "\n本块仅供你参考，禁止复述、禁止回复、禁止任何形式的知晓回执，输出里不得出现任何针对本块的痕迹"
    return line


def _build_module_body(
    module_id: str,
    workspace_dir: Path | str,
    *,
    include_git: bool = False,
    system_dir: Path | None = None,
) -> str | None:
    """Return unwrapped body for a module, or None to skip."""
    ws = Path(workspace_dir)
    if module_id == "environment":
        return build_environment_body(ws, include_git=include_git)
    if module_id == "ws_overview":
        return WS_PROTOCOL_PREFIX + workspace_overview_block(ws)
    if module_id == "agent_md":
        text = _read_file_text(ws / "AGENTS.md")
        return f"{AGENT_MD_PREFIX}{text}" if text else None
    if module_id == "user_rules":
        path = _system_file("user_rules.md", system_dir=system_dir)
        if path is None:
            return None
        text = _read_file_text(path)
        return f"{USER_RULES_PREFIX}{text}" if text else None
    if module_id == "situation":
        # 当前端声明始终注入（每轮现取，随端切换变化）；situation.md 文案叠加其后。
        # body 不再带「情境：」前缀——外层 <情境> 标签已表明身份，避免重复。
        lines = [build_current_end_line()]
        path = _system_file("situation.md", system_dir=system_dir)
        if path is not None:
            text = _read_file_text(path)
            if text:
                lines.append(text)
        return "\n".join(lines)
    return None


def _slots_around_conversation(
    order: list[ModuleSlot] | None = None,
) -> tuple[list[ModuleSlot], list[ModuleSlot]]:
    slots = order if order is not None else load_module_order()
    prefix: list[ModuleSlot] = []
    suffix: list[ModuleSlot] = []
    seen_conv = False
    for s in slots:
        if s.id == CONVERSATION_ID:
            seen_conv = True
            continue
        if not s.enabled:
            continue
        if seen_conv:
            suffix.append(s)
        else:
            prefix.append(s)
    return prefix, suffix


def build_prefix_seed_messages(
    workspace_dir: Path | str,
    *,
    include_git: bool = False,
    order: list[ModuleSlot] | None = None,
    system_dir: Path | None = None,
) -> list[Message]:
    """Prefix modules (before conversation) as separate USER messages."""
    prefix, _ = _slots_around_conversation(order)
    messages: list[Message] = []
    for slot in prefix:
        body = _build_module_body(
            slot.id,
            workspace_dir,
            include_git=include_git,
            system_dir=system_dir,
        )
        if not body:
            continue
        messages.append(Message(role=MessageRole.USER, content=_wrap_module(slot.id, body)))
    return messages


# 补注入认领扫描上界：只看历史头部这么多条。种子永远注入在历史头部
# （inject_environment_seed 插到 [:0]，压缩的头部保护只覆盖开头连续系统消息），
# 更靠后出现的模块字样只可能是对话正文/压缩摘要复述，不算种子。
SEED_SCAN_HEAD_LIMIT = 10


def _seed_body_startswith_marker(text: str, marker: str) -> bool:
    """判据与 ``is_context_module_seed`` 同源：剥掉包裹标签后按 ``startswith`` 认领。"""
    if not text:
        return False
    body = text.strip()
    for open_tag, close_tag in (
        ("<系统消息>", "</系统消息>"),
        ("<系统提醒>", "</系统提醒>"),
    ):
        if body.startswith(open_tag):
            body = body[len(open_tag) :].lstrip("\n")
            if body.endswith(close_tag):
                body = body[: -len(close_tag)].rstrip("\n")
            break
    return body.startswith(marker)


def build_missing_prefix_messages(
    history_texts: list[str],
    workspace_dir: Path | str,
    *,
    include_git: bool = False,
    order: list[ModuleSlot] | None = None,
    system_dir: Path | None = None,
) -> list[Message]:
    """历史里缺失的前缀模块种子——**逐模块**判定，缺哪个补哪个。

    不能「历史里有任一种子就整批跳过」：那样一旦只剩概况（压缩、恢复、或任何
    原因让某条丢过），环境上下文与 AGENTS.md 就再也不会回来。

    认领判据与 ``is_context_module_seed`` 同源：只在历史头部
    ``SEED_SCAN_HEAD_LIMIT`` 条里按 ``startswith`` 认领（剥包裹标签后）。
    旧的「前缀出现在整段拼接文本里」判据会被压缩摘要复述模块标题（如
    ``AGENTS.md：``）误判成「已存在」，该模块从此静默缺失、永不补注入。
    """
    prefix, _ = _slots_around_conversation(order)
    head = history_texts[:SEED_SCAN_HEAD_LIMIT]
    messages: list[Message] = []
    for slot in prefix:
        marker = MODULE_CONTENT_PREFIX_BY_ID.get(slot.id)
        if marker and any(_seed_body_startswith_marker(text, marker) for text in head):
            continue
        body = _build_module_body(
            slot.id,
            workspace_dir,
            include_git=include_git,
            system_dir=system_dir,
        )
        if not body:
            continue
        messages.append(Message(role=MessageRole.USER, content=_wrap_module(slot.id, body)))
    return messages


def build_suffix_messages(
    workspace_dir: Path | str,
    *,
    order: list[ModuleSlot] | None = None,
    system_dir: Path | None = None,
) -> list[Message]:
    """Modules after conversation (ephemeral; typically situation)."""
    _, suffix = _slots_around_conversation(order)
    messages: list[Message] = []
    for slot in suffix:
        body = _build_module_body(slot.id, workspace_dir, system_dir=system_dir)
        if not body:
            continue
        messages.append(Message(role=MessageRole.USER, content=_wrap_module(slot.id, body)))
    return messages


def preview_context_stack(
    workspace_dir: Path | str,
    *,
    order: list[ModuleSlot] | None = None,
    system_dir: Path | None = None,
    include_git: bool = False,
) -> list[dict[str, Any]]:
    """Devtools preview: ordered segments with presence / wrap tag."""
    # 传 system_dir：不传会回落到环境变量/默认位置推导的目录，预览读到的开关与
    # 顺序可能不是你保存的那一份（与实际注入不一致）。
    slots = order if order is not None else load_module_order(system_dir=system_dir)
    out: list[dict[str, Any]] = []
    for s in slots:
        if s.id == CONVERSATION_ID:
            out.append({"id": s.id, "label": "对话", "enabled": True, "present": True, "wrap": "", "body": "…"})
            continue
        if not s.enabled:
            out.append(
                {
                    "id": s.id,
                    "label": MODULE_META.get(s.id, {}).get("label", s.id),
                    "enabled": False,
                    "present": False,
                    "wrap": "",
                    "body": "",
                }
            )
            continue
        body = _build_module_body(s.id, workspace_dir, include_git=include_git, system_dir=system_dir)
        wrap = {"user_rules": "系统提醒", "situation": "情境"}.get(s.id, "系统消息")
        out.append(
            {
                "id": s.id,
                "label": MODULE_META.get(s.id, {}).get("label", s.id),
                "enabled": True,
                "present": body is not None,
                "wrap": wrap,
                "body": body or "",
            }
        )
    return out


def find_ws_overview_index(history: list[Any]) -> int:
    """Index of the message carrying 工作空间概况, or -1."""
    from src.utils.message_content import message_content_to_text

    for i, msg in enumerate(history):
        text = message_content_to_text(getattr(msg, "content", "") or "")
        if WS_PROTOCOL_PREFIX in text:
            return i
    return -1


def replace_ws_overview_in_history(history: list[Any], workspace_dir: Path | str) -> bool:
    """Replace workspace overview in-place. True when history mutated.

    - Standalone ws message: rewrite whole content.
    - Legacy combined env+ws seed: splice overview segment only.
    """
    from src.coara.injections.environment_injector import replace_overview_in_seed

    idx = find_ws_overview_index(history)
    if idx < 0:
        return False
    msg = history[idx]
    content = getattr(msg, "content", "")
    if not isinstance(content, str):
        return False

    # Legacy: env + overview in one message
    if ENV_CONTEXT_PREFIX in content and WS_PROTOCOL_PREFIX in content:
        new_content = replace_overview_in_seed(content, workspace_dir)
        if new_content is None:
            return False
        history[idx] = msg.model_copy(update={"content": new_content})
        return True

    # Standalone overview message
    new_body = WS_PROTOCOL_PREFIX + workspace_overview_block(workspace_dir)
    new_content = system_info(new_body)
    if new_content == content:
        return False
    history[idx] = msg.model_copy(update={"content": new_content})
    return True
