"""Command parsing and dispatch registry.

``execute_command`` is the single entry point for all slash commands. It:

1. Parses the raw user input into ``(name, args)``.
2. Looks up the handler in :data:`_HANDLERS`.
3. Invokes the handler and returns its :class:`CommandResult`.

Handlers are async functions with signature::

    async def handler(root: RootCoara, args: CommandArgs) -> CommandResult

``CommandArgs`` is a structured view of the raw tokens (``parts``, ``sub``,
``flag``, ``value``) so each handler doesn't re-parse strings. Unknown commands
return an error result; frontends decide whether to fall back to LLM processing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.coara.commands.types import CommandResult
from src.core.logger import logger

if TYPE_CHECKING:
    from src.coara.root import RootCoara


@dataclass
class CommandArgs:
    """Structured view of a slash command's tokens.

    Examples
    --------
    ``/new``            → ``CommandArgs(name="new", parts=["new"], sub=None, raw="/new")``
    ``/ws switch shop`` → ``CommandArgs(name="ws", parts=["ws","switch","shop"], sub="switch",
                                       value="shop", raw="/ws switch shop")``
    ``/sandbox on``     → ``CommandArgs(name="sandbox", parts=["sandbox","on"], sub="on", raw="/sandbox on")``
    ``/model 2``        → ``CommandArgs(name="model", parts=["model","2"], value="2", raw="/model 2")``
    """

    name: str
    parts: list[str]
    raw: str
    sub: str | None = None  # second token, lowercased — the "subcommand" (e.g. "switch", "on")
    value: str | None = None  # third token — the positional value (e.g. alias name, model id)
    flags: dict[str, str] = None  # type: ignore[assignment]  # --key value pairs (lazy)
    # 模块会话命令路由：Web 模块主体（FlowRoot 构建对话等）里执行会话级命令时，
    # 由前端入口（web_server）设置为该模块主体实例；处理器优先于 root.foreground_coara。
    target_coara: Any = None
    # 命令发起端（cli/web/matrix/cli-attached）：跨端语义分流的命令（如 /ws switch——
    # matrix/web 只切本端视图不动全局前台）据此判断。缺省空 = 主 CLI 前台语义。
    origin_source: str = ""

    def has_flag(self, *names: str) -> bool:
        """True if any of *names* appears as a ``--flag`` (value-less flags included)."""
        return any(n in (self.flags or {}) for n in names)

    def flag(self, name: str, default: str | None = None) -> str | None:
        return (self.flags or {}).get(name, default)


def parse_command(user_input: str) -> CommandArgs | None:
    """Parse a raw slash-command string into :class:`CommandArgs`.

    Returns ``None`` if *user_input* is not a slash command (doesn't start with ``/``)
    or is empty.
    """
    stripped = user_input.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    if not parts:
        return None

    name = parts[0].lstrip("/").lower()
    if not name:
        return None

    sub = parts[1].lower() if len(parts) >= 2 else None
    value = parts[2] if len(parts) >= 3 else None

    # Extract --flag value pairs (and value-less flags like --default)
    flags: dict[str, str] = {}
    i = 1
    while i < len(parts):
        tok = parts[i]
        if tok.startswith("--"):
            key = tok[2:]
            if i + 1 < len(parts) and not parts[i + 1].startswith("--"):
                flags[key] = parts[i + 1]
                i += 2
                continue
            flags[key] = ""
        i += 1

    return CommandArgs(
        name=name,
        parts=parts,
        raw=stripped,
        sub=sub,
        value=value,
        flags=flags,
    )


# Handler type: async (root, args) -> CommandResult
CommandHandler = Any  # callable type; kept loose to avoid heavy typing import cycle


# Keyed by command name (without leading ``/``).
_HANDLERS: dict[str, CommandHandler] = {}

# Slash commands safe to run while a turn holds ``_process_lock``.
# Criteria: no await on that lock; no session/history rewrite; UI / read-only /
# interrupt-only; or workspace switch (leaves the busy session running).
# /model included: switch_llm only updates the foreground agent's provider/model
# (no lock, no history rewrite) — the running turn's next LLM call picks it up.
# Not included: /new /exit /restart /workflow run /
# thinking|stream|sandbox toggles (wait for idle or use their
# dedicated interrupt paths).
RUN_WHILE_BUSY: frozenset[str] = frozenset(
    {
        "help",
        "status",
        "tools",
        "events",
        "login",
        "vault",
        "ws",
        "report",
        "message",
        "model",
        "log",
        "qrcode",
    }
)


def is_run_while_busy_command(user_input: str) -> bool:
    """True when *user_input* is a registered slash command allowed mid-turn."""
    args = parse_command(user_input)
    if args is None:
        return False
    return args.name in RUN_WHILE_BUSY


def register(name: str) -> Any:
    """Decorator: register an async handler for command *name*.

    Usage::

        @register("new")
        async def handle_new(root: RootCoara, args: CommandArgs) -> CommandResult:
            ...
    """

    def decorator(func: CommandHandler) -> CommandHandler:
        _HANDLERS[name] = func
        return func

    return decorator


async def execute_command(
    root: RootCoara,
    user_input: str,
    *,
    target_coara: Any = None,
    origin_source: str = "",
    interaction_channel: Any = None,
) -> CommandResult | None:
    """Dispatch a slash command to its handler.

    ``target_coara``: 端 pin 视图空间的会话主体（见 resolve_target_coara）；
    缺省 None = 主会话前台实例（root.foreground_coara）。
    ``interaction_channel``: 命令发起端的远程交互通道（web interaction channel /
    attach for_connection / matrix room channel）。非 None 时，B 类会话配置命令
    （/new /model /compact …）在该会话正被他端回合占用时先经 ApprovalCenter
    发起三端统一确认；批准才执行，拒绝/超时/无通道一律不执行（fail-closed）。

    Returns
    -------
    CommandResult or None
        - ``CommandResult`` if *user_input* was a recognised slash command (the
          frontend should render it and NOT forward to the LLM).
        - ``None`` if *user_input* is not a slash command (frontend should forward
          it to the LLM as a normal chat message).
        - ``CommandResult`` with ``data={"unknown": True}`` if it looks like a
          slash command (starts with ``/``) but no handler matched — frontend
          may surface "unknown command".
    """
    args = parse_command(user_input)
    if args is None:
        return None

    handler = _HANDLERS.get(args.name)
    if handler is None:
        return CommandResult(
            output=f"未知命令：/{args.name}。输入 /help 查看可用命令。",
            data={"unknown": True, "name": args.name},
        )

    # Any slash other than /report abandons a pending report description step.
    if args.name != "report":
        from src.coara.commands.report import clear_pending_report

        clear_pending_report(root)

    args.target_coara = target_coara
    args.origin_source = origin_source

    # B 类会话配置命令确认门：目标会话正被他端回合占用时，先让用户在发起端
    # 确认再执行（避免 /new /compact /model 悄悄掐掉/改写他端的进行中回合）。
    if interaction_channel is not None:
        gate = await _maybe_confirm_session_config(args, root, interaction_channel)
        if gate is not None:
            return gate

    return await handler(root, args)


def resolve_target_coara(root: RootCoara, args: CommandArgs) -> Any:
    """命令的目标会话主体：优先 args.target_coara（端 pin 的视图空间），缺省前台。

    阶段3各端独立视图：CLI/Web/Matrix/attach 各持自己的 view_workspace，
    会话级命令（/new /stop /model /log /report /status …）作用于「调用端的
    视图空间」而非全局前台。端入口经 execute_command(target_coara=本端 pin)
    注入；未注入时退回前台（兼容 CLI 主前端与旧调用方）。
    """
    return args.target_coara if args.target_coara is not None else root.foreground_coara


# B 类会话配置命令：会改写共享会话状态/历史、影响他端进行中回合的命令。
# 只对「有写入动作」的形态加确认门（纯查看/列表形态不加）。
_SESSION_CONFIG_ACTION_NAMES = frozenset(
    {
        "new",  # 重开会话（清历史 + 打断他端回合）
        "compact",  # 压缩共享历史
        "model",  # 切模型（deferred 也会改会话配置）
        "thinking",  # 思考模式开关
        "sandbox",  # 沙箱开关
        "tools",  # 工具开关（/tools on|off <name>）
    }
)


def _is_session_config_action(args: CommandArgs) -> bool:
    """True 当命令是对共享会话的**写动作**（而非查看/列表形态）。

    - 纯查看形态（/model 无参列表、/tools 无参列表、/thinking 无参查看等）不改
      状态，不触发确认门。
    - /model <arg>、/tools on|off <name>、/new、/compact、/sandbox … 才拦。
    """
    name = args.name
    if name not in _SESSION_CONFIG_ACTION_NAMES:
        return False
    if name in ("new", "compact"):
        return True
    if name == "model":
        # 有目标参数才是切换动作；裸 /model 或仅 --global 前缀是查看/用法提示
        parts = [p for p in args.parts[1:] if not p.startswith("--")]
        return bool(parts)
    if name == "tools":
        return args.sub in ("on", "off")
    if name in ("thinking", "sandbox"):
        # 带明确开关形态（on/off/档位）是写动作；无参仅查看
        return args.sub in ("on", "off") or bool(args.value)
    return False


def maybe_confirm_session_config(
    args: CommandArgs,
    root: RootCoara,
    interaction_channel: Any,
) -> Any:
    """B 类会话配置写命令在他端占用该会话时的确认门（公开入口）。

    供端命令入口在**绕过 execute_command 的特判分支**（如 attach 的 /model、
    /new pin 实现）动作前调用；execute_command 内的通用命令由内部自动走同一
    门。返回 None=放行；CommandResult=用户取消/无通道（端侧应直接回执该结果、
    不再执行原动作）。
    """
    return _maybe_confirm_session_config(args, root, interaction_channel)


async def _maybe_confirm_session_config(
    args: CommandArgs,
    root: RootCoara,
    interaction_channel: Any,
) -> CommandResult | None:
    """B 类会话配置写命令在他端占用该会话时，先让发起端确认。

    判定：目标 coara（命令作用会话）此刻有活跃回合，且该回合归属不是发起端
    （他端/后台在跑）→ 走 ApprovalCenter 三端统一审批（question+options 帧，
    与工具审批同一套渲染）。批准返回 None 继续执行；拒绝/超时/无通道返回
    一个「未执行」的 CommandResult，绝不静默放行。
    """
    if not _is_session_config_action(args):
        return None

    coara = args.target_coara if args.target_coara is not None else root.foreground_coara
    if coara is None:
        return None
    _has_turn = getattr(coara, "has_active_turn", None)
    if not callable(_has_turn) or not _has_turn():
        return None

    owner = str(getattr(coara, "_active_turn_source", "") or "").strip().lower()
    if not owner:
        return None
    # 来源无法映射到已知端族（后台系统未知标签 / 测试桩 / 防御值）→ 不拦。
    # 只有能明确判定「他端交互回合」才触发确认，避免无谓打扰与测试误触发。
    if not _turn_family(owner):
        return None
    # 本端自己的回合占着：不确认（本端回合=用户在敲命令的同一端）
    origin = str(args.origin_source or "").strip().lower()
    if _same_turn_family(owner, origin):
        return None

    from src.coara.approval_center import get_approval_center
    from src.core.abort import OperationAborted

    owner_zh = _turn_family_label(owner)
    verb = {
        "new": "新开一轮对话",
        "compact": "压缩会话历史",
        "model": "切换模型",
        "thinking": "切换思考模式",
        "sandbox": "切换沙箱",
        "tools": "切换工具开关",
    }.get(args.name, f"执行 /{args.name}")
    question = (
        f"{owner_zh} 端正在该会话执行回合。确认要{verb}吗？该操作会改写共享会话状态，可能影响对方正在进行的回合。"
    )
    try:
        approved = await get_approval_center().request(
            question=question,
            options=[
                {"label": "确认执行", "description": f"执行 /{args.name}"},
                {"label": "取消", "description": "不做改动"},
            ],
            timeout_seconds=300.0,
            workspace=str(getattr(coara, "workspace_dir", "") or ""),
            channel=interaction_channel,
        )
    except (TimeoutError, OperationAborted):
        approved = False
    except Exception as exc:  # noqa: BLE001 — 无通道等送达失败：fail-closed 不执行
        logger.debug(f"session-config confirm failed for /{args.name}: {exc}")
        approved = False
    if not approved:
        return CommandResult(
            output=f"已取消 /{args.name}（未改动共享会话）。",
            data={"cancelled": True, "command": args.name},
        )
    return None


def _same_turn_family(a: str, b: str) -> bool:
    """两个来源标签是否同一端族：cli/cli-attached 同族、web/web-<x> 同族。

    子智能体/后台继承父 source 或为 background/event，均视为与交互端不同。
    转发自 turn_source.same_turn_family（族抽象唯一事实源）。
    """
    from src.coara.turn_source import same_turn_family

    return same_turn_family(a, b)


def _turn_family(source: str) -> str:
    from src.coara.turn_source import turn_source_family

    return turn_source_family(source)


def _turn_family_label(source: str) -> str:
    return {
        "cli": "CLI",
        "web": "Web",
        "matrix": "手机",
        "system": "系统",
    }.get(_turn_family(source), source or "其它")


# Import handler modules so their @register decorators run at package import time.
# These imports MUST come after ``register`` is defined.
# 可选能力（如账户）自带的内核命令由接入位自行注册；未装实现的发行版没有它们
from src.ext import register_optional_commands as _register_optional_commands  # noqa: E402

_register_optional_commands()
from src.coara.commands import compact as _compact  # noqa: E402,F401  # /compact
from src.coara.commands import config as _config  # noqa: E402,F401
from src.coara.commands import email as _email  # noqa: E402,F401  # /email
from src.coara.commands import log as _log  # noqa: E402,F401  # /log
from src.coara.commands import messages as _messages  # noqa: E402,F401  # /message
from src.coara.commands import qrcode as _qrcode  # noqa: E402,F401  # /qrcode
from src.coara.commands import report as _report  # noqa: E402,F401
from src.coara.commands import restart as _restart  # noqa: E402,F401  # /restart（静默）
from src.coara.commands import session as _session  # noqa: E402,F401
from src.coara.commands import tools as _tools  # noqa: E402,F401
from src.coara.commands import vault as _vault  # noqa: E402,F401
from src.coara.commands import workspace as _workspace  # noqa: E402,F401
