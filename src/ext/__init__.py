"""扩展点：可选能力的接入位。

内核不直接依赖任何可选能力实现。遥测、账户门禁、许可证校验这类东西只存在于
商业发行版；开源发行版把对应实现包删掉（`src/telemetry/`、`src/account/` 与
许可证服务目录），本文件里的每个入口就自动退化为「没有这项能力」：

- 遥测：服务为 None，不采集、不上报
- 账户门禁：不拦回合、不提示登录
- 活动保活：没有任务要起

内核代码因此一行都不用改——删实现包即完成剥离。第三方想接自己的实现，照这里
的函数签名挂上即可（同名的 import 路径优先）。

约定：这里只做「取实现 + 兜底」，不含任何业务判定；业务判定一律留在实现包里。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


def register_optional_commands() -> None:
    """注册可选能力自带的内核命令；未装实现时什么也不做。"""
    try:
        from src.account import commands as _commands  # noqa: F401 — 导入即注册
    except Exception:
        return


def telemetry_service(root: Any, coara_home: Path | None) -> Any | None:
    """构造遥测服务；未安装实现返回 None。"""
    if coara_home is None:
        return None
    try:
        from src.telemetry import TelemetryService
    except Exception:
        return None
    try:
        return TelemetryService(root, coara_home)
    except Exception:
        return None


def account_activity_pinger() -> Callable[[Path], Awaitable[Any]] | None:
    """账户活跃保活函数；未安装账户实现返回 None。"""
    try:
        from src.account.gate import maybe_refresh_activity
    except Exception:
        return None
    return maybe_refresh_activity


async def startup_gate(coara_home: Path) -> Any | None:
    """启动期门禁判定；未安装账户实现返回 None。"""
    try:
        from src.account.gate import evaluate_gate
    except Exception:
        return None
    try:
        return await evaluate_gate(coara_home)
    except Exception:
        return None


async def turn_gate(coara_home: Path) -> Any | None:
    """回合入口门禁判定；未安装账户实现返回 None。"""
    try:
        from src.account.gate import evaluate_gate_for_turn
    except Exception:
        return None
    try:
        return await evaluate_gate_for_turn(coara_home)
    except Exception:
        return None


def turn_gate_offline_fallback(coara_home: Path | None) -> Any | None:
    """门禁评估异常时的本地放行判定；未安装账户实现返回 None。

    判定逻辑留在实现包里（可用性优先：有凭证离线放行，无凭证按本地状态）。
    """
    try:
        from src.account.allowance import offline_allowance
    except Exception:
        return None
    if coara_home is None:
        return None
    try:
        return offline_allowance(coara_home)
    except Exception:
        return None


def gate_is_trial(gate: Any) -> bool:
    """这个门禁结果是不是试用态（未安装实现时恒 False）。"""
    try:
        from src.account.gate import GateState
    except Exception:
        return False
    return getattr(gate, "state", None) == GateState.TRIAL


def register_account_web(router: Any, server: Any) -> bool:
    """注册账户接口；未安装账户实现返回 False（不注册，对应路由不存在）。"""
    try:
        from src.account.web import register_routes
    except Exception:
        return False
    try:
        register_routes(router, server)
        return True
    except Exception:
        return False


def register_telemetry_web(router: Any, server: Any) -> bool:
    """注册遥测中继接口；未安装遥测实现返回 False。"""
    try:
        from src.telemetry.web import register_routes
    except Exception:
        return False
    try:
        register_routes(router, server)
        return True
    except Exception:
        return False


def config_rows() -> list[str]:
    """可选能力向配置概况追加的行；未装实现返回空。"""
    try:
        from src.telemetry.service import telemetry_enabled
    except Exception:
        return []
    try:
        return [f"遥测={'开' if telemetry_enabled() else '关'}"]
    except Exception:
        return []


def home_dirs() -> tuple[str, ...]:
    """可选能力在 coara home 下自带的目录名（迁移白名单用）；无实现返回空。"""
    try:
        from src.telemetry.service import _QUEUE_DIRNAME  # 遥测队列目录
    except Exception:
        return ()
    return (str(_QUEUE_DIRNAME),)
