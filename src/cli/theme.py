"""coara CLI 主题系统 — 暗/亮色统一配色。

集中管理所有 UI 颜色的**语义令牌**：渲染处只引用角色名
（``text.assistant`` / ``status.error`` / …），不直接写 hex。
切换主题只需 ``set_active_theme()``；配置 ``cli.theme`` 或环境变量
``COARA_CLI_THEME``（dark / light）在 CLI 启动时解析。

层次约定（暗色基调）：
  用户输入（青，最醒目）> assistant 正文（柔白，次醒目）> 工具行（暗灰，次要）
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from prompt_toolkit.styles import Style as PTKStyle
from rich.style import Style as RichStyle

ThemeName: TypeAlias = Literal["dark", "light"]

# ---------------------------------------------------------------------------
# 语义令牌
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextColors:
    """对话正文角色色（hex 字符串，Rich / prompt_toolkit 通用）。"""

    assistant: str  # assistant 正文：柔白，长时间阅读不刺眼
    assistant_label: str  # 「Coara:」角色标签：略亮于正文
    user: str  # 「你：」角色标签：青色，最醒目（仅标签，见 user_content）
    user_content: str  # 用户输入正文：与标签区分、又不与工具灰/assistant 柔白撞色
    tool: str  # ✓ 工具行：暗灰，次要信息
    tool_error: str  # ✗ 工具失败行：淡红，可扫出但不刺眼
    system: str  # 系统注入（后台/子智能体结果等动态产出）：正文色
    system_label: str  # 系统注入前缀标签：比正文更醒目的 Teal
    muted: str  # 提示/分隔等最弱一档
    link: str  # Markdown 可点链接：常规超链接蓝（非品牌青）
    code: str  # 行内/围栏代码：与正文同亮度档、略偏冷，忌 dim 灰


@dataclass(frozen=True, slots=True)
class StatusColors:
    """状态语义色（命令结果、通知行）。"""

    ok: str
    warn: str
    error: str
    info: str


@dataclass(frozen=True, slots=True)
class DiffColors:
    add_bg: RichStyle
    del_bg: RichStyle
    add_lineno: str
    del_lineno: str
    add_marker: str
    del_marker: str
    ctx_lineno: str
    header_add: str
    header_del: str
    header_path: str


@dataclass(frozen=True, slots=True)
class CliTheme:
    """一套完整主题：文本 + 状态 + diff + prompt_toolkit chrome + 工作空间调色板。"""

    name: ThemeName
    text: TextColors
    status: StatusColors
    diff: DiffColors
    prompt_style: PTKStyle
    # 工作空间色板：(亮色, 暗色) 成对——前台行用亮+加粗，后台行用暗色
    workspace_palette: tuple[tuple[str, str], ...]


# ---------------------------------------------------------------------------
# Prompt / toolbar / completion chrome
# ---------------------------------------------------------------------------


def _prompt_style_dark(t: TextColors) -> PTKStyle:
    return PTKStyle.from_dict(
        {
            # 提示符
            "prompt": "#67e8f9 bold",  # 青色，正常状态
            "prompt.thinking": "#67e8f9 bold",  # 青蓝色，思考中（整行含轮换文案，统一同色）
            "prompt.subagent": "#7dd3fc bold",  # 浅蓝色，子代理运行中
            "prompt.subagent.pending": "#64748b",  # 灰蓝，flow 停车节点（未运行）
            "input": t.user_content,  # 输入框正文：与「你：」标签区分、又不撞工具灰/柔白
            # 底部状态栏
            "bottom-toolbar": "noreverse",
            "toolbar": "#94a3b8",
            "toolbar.workdir": "#94a3b8",
            "toolbar.plan-mode": "#38bdf8 bold",
            "toolbar.bg-tasks": "#a78bfa",
            "toolbar.separator": "#475569",
            # 运行中提示分隔线
            "running-prompt-separator": "#475569",
            # 补全菜单（默认 PTK）
            "completion-menu": "bg:#1e293b #e2e8f0",
            "completion-menu.completion": "bg:#1e293b #e2e8f0",
            "completion-menu.completion.current": "bg:#334155 #ffffff bold",
            "completion-menu.meta.completion": "bg:#1e293b #94a3b8",
            "completion-menu.meta.completion.current": "bg:#334155 #94a3b8 bold",
            # Slash command 补全菜单
            "slash-completion-menu": "",
            "slash-completion-menu.separator": "fg:#475569",
            "slash-completion-menu.marker": "fg:#475569",
            "slash-completion-menu.marker.current": "fg:#38bdf8",
            "slash-completion-menu.command": "fg:#a6adba",
            "slash-completion-menu.meta": "fg:#64748b",
            "slash-completion-menu.command.current": "fg:#7dd3fc bold",
            "slash-completion-menu.meta.current": "fg:#38bdf8",
        }
    )


def _prompt_style_light(t: TextColors) -> PTKStyle:
    return PTKStyle.from_dict(
        {
            "prompt": "#0e7490 bold",
            "prompt.thinking": "#0e7490 bold",
            "prompt.subagent": "#2563eb bold",
            "prompt.subagent.pending": "#94a3b8",  # 灰蓝，flow 停车节点（未运行）
            "input": t.user_content,
            "bottom-toolbar": "noreverse",
            "toolbar": "#64748b",
            "toolbar.workdir": "#64748b",
            "toolbar.plan-mode": "#0284c7 bold",
            "toolbar.bg-tasks": "#7c3aed",
            "toolbar.separator": "#cbd5e1",
            "running-prompt-separator": "#94a3b8",
            "completion-menu": "bg:#f8fafc #1e293b",
            "completion-menu.completion": "bg:#f8fafc #1e293b",
            "completion-menu.completion.current": "bg:#e2e8f0 #0f172a bold",
            "completion-menu.meta.completion": "bg:#f8fafc #64748b",
            "completion-menu.meta.completion.current": "bg:#e2e8f0 #64748b bold",
            "slash-completion-menu": "",
            "slash-completion-menu.separator": "fg:#cbd5e1",
            "slash-completion-menu.marker": "fg:#94a3b8",
            "slash-completion-menu.marker.current": "fg:#2563eb",
            "slash-completion-menu.command": "fg:#4b5563",
            "slash-completion-menu.meta": "fg:#6b7280",
            "slash-completion-menu.command.current": "fg:#1d4ed8 bold",
            "slash-completion-menu.meta.current": "fg:#2563eb",
        }
    )


# 工作空间色板暗/亮主题共用（后台行暗色在亮主题下仍可读）
_WORKSPACE_PALETTE: tuple[tuple[str, str], ...] = (
    ("#f59e0b", "#8a6a2f"),  # amber
    ("#38bdf8", "#2b6a8c"),  # sky
    ("#4ade80", "#357a52"),  # green
    ("#f472b6", "#8a4a68"),  # pink
    ("#a78bfa", "#6656a0"),  # violet
    ("#2dd4bf", "#2a7a70"),  # teal
    ("#fb7185", "#8a4a54"),  # rose
    ("#facc15", "#8a7c2a"),  # yellow
)


def _build_dark() -> CliTheme:
    text = TextColors(
        assistant="#c3cddb",  # 柔白：长读不刺眼
        assistant_label="#d1dae5",
        user="#67e8f9",  # 「你：」标签：青，最醒目的品牌主色
        user_content="#a5b4fc",  # 用户输入正文：柔紫，区别于标签的青、工具灰与 assistant 柔白
        tool="#64748b",
        tool_error="#c08080",
        system="#7aa2f7",  # 系统注入正文：柔和蓝（眼睛友好、不刺眼）
        system_label="#fbbf24",  # 系统注入前缀：琥珀金标签，与 teal 正文冷暖区分
        muted="#475569",
        link="#a8bdd4",  # 与行内代码同色，链与 `code` 一体
        code="#a8bdd4",  # 冷灰蓝：贴 assistant，比 dim 灰清亮
    )
    return CliTheme(
        name="dark",
        text=text,
        status=StatusColors(ok="#86c98f", warn="#d8b26e", error="#d97f7f", info="#7aa2f7"),
        diff=DiffColors(
            add_bg=RichStyle(bgcolor="#1a3f2b"),
            del_bg=RichStyle(bgcolor="#472528"),
            add_lineno="green",
            del_lineno="red",
            add_marker="bold bright_green",
            del_marker="bold bright_red",
            ctx_lineno="dim",
            header_add="bold green",
            header_del="bold red",
            header_path="#93c5fd",
        ),
        prompt_style=_prompt_style_dark(text),
        workspace_palette=_WORKSPACE_PALETTE,
    )


def _build_light() -> CliTheme:
    text = TextColors(
        assistant="#475569",  # slate-600：白底上柔和耐读
        assistant_label="#3b4a5f",
        user="#0e7490",
        user_content="#5b54c4",  # 用户输入正文：深紫，白底可读且区别工具灰与 assistant
        tool="#94a3b8",
        tool_error="#c47a7a",  # 淡玫瑰红：可辨识失败，避免 #b91c1c 刺眼
        system="#2563eb",  # 系统注入正文：蓝，白底可读
        system_label="#b45309",  # 系统注入前缀：深琥珀标签，白底可读
        muted="#94a3b8",
        link="#3d6a8a",  # 与行内代码同色
        code="#3d6a8a",  # 钢蓝：贴 slate 正文，白底可辨
    )
    return CliTheme(
        name="light",
        text=text,
        status=StatusColors(ok="#2f7a44", warn="#9a6b1e", error="#b91c1c", info="#1d4ed8"),
        diff=DiffColors(
            add_bg=RichStyle(bgcolor="#c9efd6"),
            del_bg=RichStyle(bgcolor="#f5d3cf"),
            add_lineno="green",
            del_lineno="red",
            add_marker="bold bright_green",
            del_marker="bold bright_red",
            ctx_lineno="dim",
            header_add="bold green",
            header_del="bold red",
            header_path="#93c5fd",
        ),
        prompt_style=_prompt_style_light(text),
        workspace_palette=_WORKSPACE_PALETTE,
    )


# ---------------------------------------------------------------------------
# 主题解析与切换
# ---------------------------------------------------------------------------

_ACTIVE: CliTheme = _build_dark()


def resolve_theme_name(value: str | None) -> ThemeName | None:
    """把配置/环境变量值解析为主题名；无法识别返回 None。"""
    raw = (value or "").strip().lower()
    return raw if raw in ("dark", "light") else None  # type: ignore[return-value]


def set_active_theme(name: ThemeName) -> None:
    global _ACTIVE
    _ACTIVE = _build_dark() if name == "dark" else _build_light()


def get_theme() -> CliTheme:
    return _ACTIVE


def apply_theme_from_config(raw_config: dict[str, Any] | None) -> ThemeName:
    """CLI 启动时解析主题：``COARA_CLI_THEME`` 覆盖 ``cli.theme``，缺省 dark。"""
    env = resolve_theme_name(os.environ.get("COARA_CLI_THEME"))
    cfg = resolve_theme_name(str((raw_config or {}).get("cli", {}).get("theme", "") or ""))
    name = env or cfg or "dark"
    set_active_theme(name)
    return name


# ---------------------------------------------------------------------------
# 角色取色（渲染处只引用角色，不写 hex）
# ---------------------------------------------------------------------------


def fg(role: str) -> str:
    """取语义角色的 hex 颜色（``text.assistant`` / ``status.error`` / ``text.user`` …）。"""
    group, _, key = role.partition(".")
    colors: Any = {"text": _ACTIVE.text, "status": _ACTIVE.status}.get(group)
    value = getattr(colors, key, None) if colors is not None else None
    if not isinstance(value, str) or not value:
        raise KeyError(f"unknown theme color role: {role!r}")
    return value


def get_workspace_palette() -> tuple[tuple[str, str], ...]:
    return _ACTIVE.workspace_palette


# ---------------------------------------------------------------------------
# 公开 API（向后兼容的旧 getter）
# ---------------------------------------------------------------------------


def get_diff_colors() -> DiffColors:
    return _ACTIVE.diff


def get_prompt_style() -> PTKStyle:
    return _ACTIVE.prompt_style


def get_assistant_text_style() -> str:
    """Return the assistant output text color (works in both Rich and prompt_toolkit)."""
    return _ACTIVE.text.assistant


def get_tool_text_style() -> str:
    """Return the tool output text color — dimmer than assistant, distinct from user."""
    return _ACTIVE.text.tool


def get_tool_error_style() -> str:
    """Return the ✗ tool-error line color — muted red, noticeable but not glaring."""
    return _ACTIVE.text.tool_error
