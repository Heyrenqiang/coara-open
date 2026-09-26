"""Command parsing and dispatch registry"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.coara.commands.types import CommandResult
from src.core.logger import logger

if TYPE_CHECKING:
    from src.coara.root import RootCoara


@dataclass
class CommandArgs:
    """Structured view of a slash command's tokens"""

    name: str
    parts: list[str]
    raw: str
    sub: str | None = None  # second token, lowercased — the "subcommand" (e.g. "switch", "on")
    value: str | None = None  # third token — the positional value (e.g. alias name, model id)
    flags: dict[str, str] = None  # type: ignore[assignment]  # --key value pairs (lazy)
    # 模块会话命令路由：Web 模块主体（FlowRoot 构建对话等）里执行会话级命令时，
    # 由前端入口（web_server）设置为该模块主体实例；处理器优先于 root.foreground_coara。
    target_coara: Any = None
    # matrix/web 只切本端视图不动全局前台）据此判断。缺省空 = 当前前台会话。
    origin_source: str = ""

    def has_flag(self, *names: str) -> bool:
        """True if any of *names* appears as a ``--flag`` (value-less flags included)."""
        return any(n in (self.flags or {}) for n in names)

    def flag(self, name: str, default: str | None = None) -> str | None:
        return (self.flags or {}).get(name, default)


def parse_command(user_input: str) -> CommandArgs | None:
    """Parse a raw slash-command string into :class:`CommandArgs`"""
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

# dedicated interrupt paths).
RUN_WHILE_BUSY: frozenset[str] = frozenset(
    {
        "help",
        "status",
        "tools",
        "events",
        "login",
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
    """Decorator: register an async handler for command *name*"""

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
    """Dispatch a slash command to its handler."""
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

    # B 写动作：夺 source；需停回合的整会话打断（含该端后台；/new 不杀后台）。
    await settle_b_class_before_handler(args, root)

    return await handler(root, args)


def resolve_target_coara(root: RootCoara, args: CommandArgs) -> Any:
    """命令的目标会话主体：优先 args.target_coara（端 pin 的视图空间），缺省前台"""
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
        "stop",  # 打断当前回合（影响他端进行中回合）
    }
)


def _is_session_config_action(args: CommandArgs) -> bool:
    """True 当命令是对共享会话的**写动作**（而非查看/列表形态）"""
    name = args.name
    if name not in _SESSION_CONFIG_ACTION_NAMES:
        return False
    if name in ("new", "compact", "stop"):
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
    """B 类会话配置写命令在他端占用该会话时的确认门（公开入口）"""
    return _maybe_confirm_session_config(args, root, interaction_channel)


async def _maybe_confirm_session_config(
    args: CommandArgs,
    root: RootCoara,
    interaction_channel: Any,
) -> CommandResult | None:
    """B 类会话配置写命令在他端占用该会话时，先让发起端确认"""
    if not _is_session_config_action(args):
        return None

    coara = args.target_coara if args.target_coara is not None else root.foreground_coara
    if coara is None:
        return None
    _has_turn = getattr(coara, "has_active_turn", None)
    if not callable(_has_turn) or not _has_turn():
        return None

    # 与 confirm_cross_end_action 同一把尺：段归属优先（易主后仍准）。
    owner = str(
        getattr(getattr(coara, "_segments", None), "source", "") or getattr(coara, "_active_turn_source", "") or ""
    ).strip().lower()
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

    verb = {
        "new": "新开一轮对话",
        "compact": "压缩会话历史",
        "model": "切换模型",
        "thinking": "切换思考模式",
        "sandbox": "切换沙箱",
        "tools": "切换工具开关",
        "stop": "打断当前回合",
    }.get(args.name, f"执行 /{args.name}")
    approved = await confirm_cross_end_action(
        coara,
        origin_source=str(args.origin_source or ""),
        verb=verb,
        detail="该操作会改写共享会话状态，可能影响对方正在进行的回合。",
        interaction_channel=interaction_channel,
    )
    if not approved:
        return CommandResult(
            output=f"已取消 /{args.name}（未改动共享会话）。",
            data={"cancelled": True, "command": args.name},
        )
    return None


async def settle_b_class_before_handler(args: CommandArgs, root: RootCoara) -> None:
    """B 写动作执行前：夺 source；需停回合的则整会话打断。

    - 切模型 / 压缩 / 思考 / 沙箱 / 工具开关：停回合并杀该端后台（与打断同面）
    - /new：停回合但不杀后台（产品口径）
    - /stop：由 handler 自己 interrupt + 夺权，此处跳过
    - 空闲写动作：仍开段夺权，与注入同权
    """
    if not _is_session_config_action(args) or args.name == "stop":
        return
    coara = resolve_target_coara(root, args)
    origin = str(args.origin_source or "").strip()
    if coara is None or not origin:
        return

    stop_turn = args.name in ("compact", "model", "thinking", "sandbox", "tools", "new")
    kill_bg = args.name != "new"
    _has_turn = getattr(coara, "has_active_turn", None)
    busy = bool(_has_turn()) if callable(_has_turn) else False

    if stop_turn and busy:
        interrupt = getattr(coara, "interrupt_current_turn", None)
        if callable(interrupt):
            interrupt(
                f"session_config_{args.name}",
                interrupt_source=f"{origin}_{args.name}_command",
                cancel_delegates=kill_bg,
                take_ownership_source=origin,
            )
        if args.name == "compact":
            # 压缩必须在回合真正停下后执行；先等锁，再等到 has_active_turn 落下
            # （锁释放瞬间还有队列窗口，否则 handler 忙闲门会误拒）。
            wait = getattr(coara, "_wait_for_process_lock_release", None)
            if callable(wait):
                await wait()
            deadline = time.monotonic() + 30.0
            while callable(_has_turn) and _has_turn() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
        return

    take = getattr(coara, "take_session_ownership", None)
    if callable(take):
        take(origin)


def _same_turn_family(a: str, b: str) -> bool:
    """两个来源标签是否同一端族：cli/cli-attached 同族、web/web-<x> 同族"""
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


async def confirm_cross_end_action(
    coara: Any,
    *,
    origin_source: str,
    verb: str,
    detail: str,
    interaction_channel: Any,
    workspace: str = "",
) -> bool:
    """影响他端操作的统一征求同意门：它端回合在跑且与操作端不同族时，先经审批征得同意。

    判据与 B 类确认门同一把尺（会话活跃 + 归属端族 ≠ 操作端族 + 归属可归类）。
    通道缺失/超时/送达失败一律 fail-closed（返回 False，不执行）。
    """
    _has_turn = getattr(coara, "has_active_turn", None)
    active = False
    if callable(_has_turn):
        try:
            active = bool(_has_turn())
        except Exception:
            active = False
    if not active:
        return True
    # owner 取段归属优先（归属可能已易主），回退回合发起端
    owner = str(
        getattr(getattr(coara, "_segments", None), "source", "") or getattr(coara, "_active_turn_source", "") or ""
    ).strip().lower()
    if not owner or not _turn_family(owner):
        return True
    if _same_turn_family(owner, str(origin_source or "").strip().lower()):
        return True
    if interaction_channel is None:
        return False

    from src.coara.approval_center import get_approval_center
    from src.core.abort import OperationAborted

    owner_zh = _turn_family_label(owner)
    question = f"{owner_zh} 端正在该会话执行回合。确认要{verb}吗？{detail}"
    try:
        return bool(
            await get_approval_center().request(
                question=question,
                options=[
                    {"label": "确认执行", "description": verb},
                    {"label": "取消", "description": "不做改动"},
                ],
                timeout_seconds=300.0,
                workspace=workspace or str(getattr(coara, "workspace_dir", "") or ""),
                channel=interaction_channel,
            )
        )
    except (TimeoutError, OperationAborted):
        return False
    except Exception as exc:  # noqa: BLE001 — 送达失败 fail-closed
        logger.debug(f"cross-end confirm failed for '{verb}': {exc}")
        return False


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
from src.coara.commands import workspace as _workspace  # noqa: E402,F401
