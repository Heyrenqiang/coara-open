"""Unit tests for turn / launch source helpers."""

from __future__ import annotations

from types import SimpleNamespace

from src.coara.turn_source import (
    cli_shows_foreground_spinner,
    cli_shows_source,
    current_turn_source,
    normalize_launch_source,
    normalize_turn_source,
    resolve_trace_end_source,
    should_push_matrix,
    web_shows_source,
)


def test_turn_source_helpers() -> None:
    assert normalize_turn_source("event") == "event"
    assert normalize_turn_source("matrix") == "matrix"
    assert normalize_turn_source("background") == "background"
    assert normalize_turn_source("unknown") == "cli-attached"

    # 内核化后 CLI 全为 attach；非 launch source（event/background/未知）塌缩为 cli-attached
    assert normalize_launch_source("event") == "cli-attached"
    assert normalize_launch_source("matrix") == "matrix"
    assert normalize_launch_source("background") == "cli-attached"
    assert normalize_launch_source("") == "cli-attached"

    assert should_push_matrix("matrix") is True
    assert should_push_matrix("event") is True
    assert should_push_matrix("cli") is False
    assert should_push_matrix("web") is False

    # 三端独立零镜像：CLI 端 spinner/活动树只跟随本端（cli/cli-attached）回合
    assert cli_shows_foreground_spinner("cli") is True
    assert cli_shows_foreground_spinner("cli-attached") is True
    assert cli_shows_foreground_spinner("matrix") is False
    assert cli_shows_foreground_spinner("event") is False
    assert cli_shows_foreground_spinner("web") is False
    assert cli_shows_foreground_spinner("web-flow") is False
    assert cli_shows_foreground_spinner(None) is False

    assert current_turn_source(SimpleNamespace(_active_turn_source="matrix")) == "matrix"
    assert current_turn_source(SimpleNamespace()) == "cli-attached"


def test_cli_shows_source_single_source_of_truth() -> None:
    """CLI 可见性单一事实源：本端显示，它端/后台不显示，空来源按 unknown 参数。"""
    # 本端来源 → 显示
    assert cli_shows_source("cli") is True
    assert cli_shows_source("cli-attached") is True
    assert cli_shows_source("CLI") is True  # 大小写归一
    # 它端来源 → 不显示（各端显示独立）
    assert cli_shows_source("web") is False
    assert cli_shows_source("matrix") is False
    assert cli_shows_source("event") is False
    # 后台唤醒回合输出 → 不镜像
    assert cli_shows_source("background") is False
    # 空来源：unknown=True 宁多勿丢（回显兼容）；unknown=False 严格路由（回合活跃）
    assert cli_shows_source("") is True
    assert cli_shows_source(None) is True
    assert cli_shows_source("", unknown=False) is False
    assert cli_shows_source(None, unknown=False) is False


def test_web_shows_source_and_resolve_trace_end_source() -> None:
    assert web_shows_source("web") is True
    assert web_shows_source("web-flow") is True
    assert web_shows_source("cli-attached") is False
    assert web_shows_source("", unknown=False) is False
    assert resolve_trace_end_source({"origin_source": "web", "source": "cli"}) == "cli"
    assert resolve_trace_end_source({"origin_source": "web"}) == "web"
    assert resolve_trace_end_source({"subagent_origin": "matrix"}) == "matrix"
    assert resolve_trace_end_source({}) == ""


def test_cli_attached_source_isolation() -> None:
    """外挂 CLI（coara attach）的 source 是私有前端：
    - 登记进 TURN_SOURCES（不被塌缩成 cli，回投/落盘正确）
    - 不推手机（should_push_matrix 只认 matrix/event）
    - 一等 launch source（外挂可启动后台任务，origin 保留不塌缩）
    """
    from src.coara.turn_source import TURN_SOURCES

    assert "cli-attached" in TURN_SOURCES
    assert normalize_turn_source("cli-attached") == "cli-attached"
    # 不推手机
    assert should_push_matrix("cli-attached") is False
    # 一等 launch source：origin 保留为 cli-attached
    assert normalize_launch_source("cli-attached") == "cli-attached"
    # 主 CLI spinner 不跟随模块式 attach（非 web- 前缀返回 True，但 attach 回合
    # 不跑在主 CLI foreground_coara 上——见占用规则，spinner 天然不触发）。
