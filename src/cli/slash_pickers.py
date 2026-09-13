"""CLI slash-command ↑↓ pickers (options + submit/cancel rules).

Typing ``/ws`` / ``/model`` / ``/sandbox`` … opens a completion menu; Enter
applies+submits; Esc restores the bare command stem.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.completion import Completer, Completion


@dataclass(frozen=True, slots=True)
class SlashPickerOption:
    """One ↑↓ row: *insert* is the full line written into the buffer."""

    insert: str
    display: str
    meta: str = ""


# Commands whose exact token (and trailing args) are owned by a picker, not SlashCommandCompleter.
PICKER_COMMANDS: frozenset[str] = frozenset(
    {
        "/ws",
        "/model",
        "/thinking",
        "/sandbox",
        "/events",
        "/vault",
        "/detail",
    }
)


def is_ws_switch_submit_line(text: str) -> bool:
    parts = (text or "").strip().split()
    return len(parts) == 3 and parts[0].lower() == "/ws" and parts[1].lower() == "switch"


def is_slash_picker_submit_line(text: str) -> bool:
    """True when Enter should apply+submit after picking a completion row."""
    t = (text or "").strip()
    if not t.startswith("/"):
        return False
    parts = t.split()
    if not parts:
        return False
    cmd = parts[0].lower()
    if cmd not in PICKER_COMMANDS:
        return False

    if cmd == "/ws":
        return is_ws_switch_submit_line(t)

    if cmd == "/model":
        return len(parts) == 2 and bool(parts[1])

    if cmd == "/thinking":
        return len(parts) == 2 and parts[1].lower() in {
            "on",
            "off",
            "toggle",
            "low",
            "medium",
            "high",
        }

    if cmd == "/sandbox":
        return t == "/sandbox"

    if cmd == "/events":
        return t == "/events" or (len(parts) == 2 and parts[1].lower() == "reload")

    if cmd == "/vault":
        return t == "/vault" or (len(parts) == 2 and parts[1].lower() in {"lock", "status"})

    # `/detail` 的折叠块候选行应用即提交（回看是一步动作）。
    return cmd == "/detail"


def slash_picker_cancel_stem(text: str) -> str | None:
    """Restore bare command after Esc cancels an applied picker row."""
    parts = (text or "").strip().split()
    if len(parts) < 2:
        return None
    head = parts[0].lower()
    if head in PICKER_COMMANDS:
        return head
    return None


class SlashOptionPicker(Completer):
    """Generic completer: ``/cmd`` or ``/cmd <filter>`` → option rows."""

    def __init__(
        self,
        command: str,
        provider: Callable[[], Sequence[SlashPickerOption]] | None = None,
        *,
        # When first token after command is in this set, do not offer options
        # (e.g. ``/ws rename`` is not a switch picker).
        owned_subcommands: frozenset[str] | None = None,
    ) -> None:
        self.command = command
        self._provider: Callable[[], Sequence[SlashPickerOption]] = provider or (lambda: ())
        self._owned = owned_subcommands or frozenset()

    def set_provider(self, provider: Callable[[], Sequence[SlashPickerOption]]) -> None:
        self._provider = provider

    def should_complete(self, document) -> bool:
        text = document.text_before_cursor
        if document.text_after_cursor.strip():
            return False
        return text == self.command or text.startswith(self.command + " ")

    def get_completions(self, document, complete_event):
        if not self.should_complete(document):
            return

        text = document.text_before_cursor
        rest = text[len(self.command) :]
        tokens = rest.strip().split() if rest.strip() else []
        if tokens and tokens[0].lower() in self._owned:
            return
        # Already a full multi-arg choice (e.g. `/workflow run abc extra`) — leave alone
        filter_prefix = ""
        if tokens:
            # Filter against the remainder of the line
            filter_prefix = rest.strip()

        filter_l = filter_prefix.lower()
        for opt in self._provider():
            if filter_l:
                insert_l = opt.insert.lower()
                display_l = opt.display.lower()
                meta_l = (opt.meta or "").lower()
                if not (
                    insert_l.startswith(self.command.lower() + " " + filter_l)
                    or insert_l == self.command.lower()
                    or display_l.startswith(filter_l)
                    or filter_l in display_l
                    or filter_l in meta_l
                    or filter_l in insert_l
                ):
                    continue
            yield Completion(
                text=opt.insert,
                start_position=-len(text),
                display=opt.display,
                display_meta=opt.meta,
            )


# ---------------------------------------------------------------------------
# Option providers
# ---------------------------------------------------------------------------


def thinking_options() -> list[SlashPickerOption]:
    return [
        SlashPickerOption("/thinking on", "开启思考", "on"),
        SlashPickerOption("/thinking off", "关闭思考", "off"),
        SlashPickerOption("/thinking toggle", "切换开/关", "toggle"),
        SlashPickerOption("/thinking low", "强度 low", "low"),
        SlashPickerOption("/thinking medium", "强度 medium", "medium"),
        SlashPickerOption("/thinking high", "强度 high", "high"),
    ]


def sandbox_options() -> list[SlashPickerOption]:
    return [SlashPickerOption("/sandbox", "切换沙箱开/关", "toggle")]


def events_options() -> list[SlashPickerOption]:
    return [
        SlashPickerOption("/events", "查看事件源", "list"),
        SlashPickerOption("/events reload", "重新加载", "reload"),
    ]


def vault_options() -> list[SlashPickerOption]:
    return [
        SlashPickerOption("/vault", "查看状态", "status"),
        SlashPickerOption("/vault lock", "锁定保险柜", "lock"),
    ]


def detail_options(display: Any) -> list[SlashPickerOption]:
    """``/detail <关键词>`` 的候选行：子智能体折叠块摘要（最近在前）。

    ``insert`` 直接用块自己的任务摘要——与定位口径同一把尺（同一字符串既是候选
    也是关键词，选中后必然命中；类型/摘要子串过滤在 ``SlashOptionPicker`` 侧完成）。
    """
    source = getattr(display, "detail_candidates", None)
    if not callable(source):
        return []
    rows: list[SlashPickerOption] = []
    for block in source():
        keyword = str(block.task_summary() or "").strip()
        if not keyword:
            continue
        rows.append(
            SlashPickerOption(
                insert=f"/detail {keyword}",
                display=f"▸ {block.title()} · {keyword}",
                meta=str(block.status_text() or ""),
            )
        )
    return rows


def model_options_from_root(root: Any) -> list[SlashPickerOption]:
    try:
        from src.core.config import config_manager
        from src.llm.model_catalog import list_model_choices

        current = f"{root.foreground_coara.provider_name}/{root.foreground_coara.model_name}"
        rows: list[SlashPickerOption] = []
        for i, choice in enumerate(list_model_choices(config_manager), start=1):
            meta_parts = [f"#{i}"]
            if choice.key == current:
                meta_parts.insert(0, "当前")
            rows.append(
                SlashPickerOption(
                    insert=f"/model {choice.key}",
                    display=choice.label or choice.key,
                    meta=" · ".join(meta_parts),
                )
            )
        # 底部固定一条：添加模型（选厂商+填 API key，首发与随时增补的入口）
        rows.append(
            SlashPickerOption(
                insert="/model --add",
                display="＋ 添加模型",
                meta="选厂商填入 API key 即可用",
            )
        )
        return rows
    except Exception:
        return []


def workspace_options_from_root(root: Any) -> list[SlashPickerOption]:
    manager = getattr(root, "workspace_manager", None)
    if manager is None:
        return []
    try:
        from src.workspace.catalog import (
            resolve_foreground_active_id,
            resolve_workspace_summary,
        )

        root.sync_workspace_manager_to_foreground()
        active_id = resolve_foreground_active_id(root, manager)
        rows: list[SlashPickerOption] = []
        for i, entry in enumerate(manager.list_workspaces(), start=1):
            meta_parts: list[str] = [f"#{i}"]
            if entry.id == active_id:
                meta_parts.insert(0, "当前")
            summary = resolve_workspace_summary(entry) or ""
            if summary:
                meta_parts.append(summary)
            meta_parts.append(str(entry.path))
            rows.append(
                SlashPickerOption(
                    insert=f"/ws switch {entry.name}",
                    display=entry.name,
                    meta=" · ".join(meta_parts),
                )
            )
        return rows
    except Exception:
        return []


class WorkspaceSwitchCompleter(Completer):
    """``/ws`` picker with rename/updates ownership and numeric filter."""

    _OWNED = frozenset({"list", "rename", "default", "updates"})

    def __init__(self, provider: Callable[[], Sequence[SlashPickerOption]] | None = None) -> None:
        self._provider: Callable[[], Sequence[SlashPickerOption]] = provider or (lambda: ())

    def set_provider(self, provider: Callable[[], Sequence[SlashPickerOption]]) -> None:
        self._provider = provider

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if document.text_after_cursor.strip():
            return
        if not (text == "/ws" or text.startswith("/ws ")):
            return
        rest = text[3:]
        filter_prefix = ""
        if rest.startswith(" "):
            tokens = rest.strip().split()
            if not tokens:
                filter_prefix = ""
            elif tokens[0].lower() in self._OWNED:
                return
            elif tokens[0].lower() == "switch":
                if len(tokens) >= 3:
                    return
                filter_prefix = tokens[1] if len(tokens) >= 2 else ""
            else:
                if len(tokens) >= 2:
                    return
                filter_prefix = tokens[0]
        elif rest != "":
            return

        filter_l = filter_prefix.lower()
        for opt in self._provider():
            # insert is `/ws switch name`
            name = opt.insert.split(None, 2)[-1] if opt.insert.startswith("/ws switch ") else opt.display
            if filter_l and not (
                name.lower().startswith(filter_l)
                or opt.display.lower().startswith(filter_l)
                or filter_l in (opt.meta or "").lower()
            ):
                continue
            yield Completion(
                text=opt.insert,
                start_position=-len(text),
                display=opt.display,
                display_meta=opt.meta,
            )


def build_static_pickers() -> list[SlashOptionPicker]:
    """Pickers that do not need Root (fixed option lists)."""
    return [
        SlashOptionPicker("/thinking", thinking_options),
        SlashOptionPicker("/sandbox", sandbox_options),
        SlashOptionPicker("/events", events_options),
        SlashOptionPicker("/vault", vault_options),
    ]


def bind_root_pickers(session: Any, root: Any) -> None:
    """Attach Root-backed providers onto session completers."""
    ws = getattr(session, "coara_ws_completer", None)
    if ws is not None and hasattr(ws, "set_provider"):
        ws.set_provider(lambda: workspace_options_from_root(root))

    model = getattr(session, "coara_model_completer", None)
    if model is not None and hasattr(model, "set_provider"):
        model.set_provider(lambda: model_options_from_root(root))


def bind_detail_picker(session: Any, display: Any) -> None:
    """把折叠块候选源接到 ``/detail`` picker（display 建好后才有数据，故单独绑定）。"""
    picker = getattr(session, "coara_detail_completer", None)
    if picker is not None and hasattr(picker, "set_provider"):
        picker.set_provider(lambda: detail_options(display))


def _serialize_picker_options(options: Sequence[SlashPickerOption]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for opt in options:
        rows.append(
            {
                "insert": opt.insert,
                "display": opt.display,
                "meta": opt.meta or "",
                "submit": is_slash_picker_submit_line(opt.insert),
            }
        )
    return rows


def pickers_payload_for_root(root: Any) -> dict[str, list[dict[str, Any]]]:
    """JSON-friendly picker menus for Web slash autocomplete (same sources as CLI)."""
    return {
        "/ws": _serialize_picker_options(workspace_options_from_root(root)),
        "/model": _serialize_picker_options(model_options_from_root(root)),
        "/thinking": _serialize_picker_options(thinking_options()),
        "/sandbox": _serialize_picker_options(sandbox_options()),
        "/events": _serialize_picker_options(events_options()),
        "/vault": _serialize_picker_options(vault_options()),
    }
