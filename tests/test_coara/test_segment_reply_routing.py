"""注入段正文路由不变量（评审 P1 回归）。

「CLI 正忙 + web/matrix 来源跟话注入同一回合」时，注入开新段，注入段之后
的正文按段来源路由回该端；该端无 EndRegistry 通道时兜底到回合发起端；
两端都无通道才丢弃（且 WARNING 留痕，绝不静默）。

端侧通道登记（matrix ingress_helpers / web_server busy 注入）保证注入段
永远有通道；本测试钉死的是路由层不变量——通道缺失时正文不归零。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.coara.end_registry import EndRegistry
from tests.helpers import make_test_coara


def _bind(coara, registry: EndRegistry) -> None:
    coara._root_ref = type("R", (), {"end_registry": registry})()


@pytest.mark.asyncio
async def test_injected_segment_routes_to_its_end(tmp_path: Path) -> None:
    """注入段（web）有通道：注入段正文路由到 web，不去发起端 cli。"""
    coara = make_test_coara(tmp_path)
    registry = EndRegistry()
    _bind(coara, registry)
    coara._active_turn_source = "cli"  # 回合由 CLI 发起

    got: dict[str, list[str]] = {"cli": [], "web": []}

    async def cli_sender(frame: dict) -> None:
        got["cli"].append(str(frame.get("text") or ""))

    async def web_sender(frame: dict) -> None:
        got["web"].append(str(frame.get("text") or ""))

    registry.register("cli", cli_sender)
    registry.register("web", web_sender)

    await coara._route_chunk_to_current_end("首段正文", "cli")
    # busy 中 web 跟话注入开新段：注入段正文归属 web
    await coara._route_chunk_to_current_end("注入段正文", "web")

    assert got["cli"] == ["首段正文"]
    assert got["web"] == ["注入段正文"]


@pytest.mark.asyncio
async def test_injected_segment_without_channel_falls_back_to_launch(tmp_path: Path) -> None:
    """注入段（web）无通道：正文兜底到发起端 cli，不静默丢弃（P1 核心）。"""
    coara = make_test_coara(tmp_path)
    registry = EndRegistry()
    _bind(coara, registry)
    coara._active_turn_source = "cli"

    got: list[str] = []

    async def cli_sender(frame: dict) -> None:
        got.append(str(frame.get("text") or ""))

    registry.register("cli", cli_sender)
    # web 端通道缺失（连接断开/登记遗漏）

    await coara._route_chunk_to_current_end("注入段正文", "web")
    assert got == ["注入段正文"]


@pytest.mark.asyncio
async def test_no_channel_anywhere_drops_with_warning(tmp_path: Path) -> None:
    """注入段与发起端都无通道：丢弃但必须 WARNING 留痕（正文绝不静默丢）。

    loguru 不走 logging.caplog——用 sink 捕获。"""

    from src.core.logger import logger

    coara = make_test_coara(tmp_path)
    registry = EndRegistry()
    _bind(coara, registry)
    coara._active_turn_source = "cli"

    seen: list[str] = []
    sink_id = logger.add(lambda msg: seen.append(str(msg)), level="WARNING")
    try:
        await coara._route_chunk_to_current_end("孤儿正文", "web")
    finally:
        logger.remove(sink_id)
    assert any("no sender" in s for s in seen)


@pytest.mark.asyncio
async def test_stale_sender_does_not_hijack_new_session(tmp_path: Path) -> None:
    """旧会话残留 sender（followup 未注销的旧闭包）不得劫持新会话键：
    sender 按 (end, session_id) 键隔离——同端不同会话互不可见。"""
    coara = make_test_coara(tmp_path)
    registry = EndRegistry()
    _bind(coara, registry)
    coara._active_turn_source = "cli"

    got_old: list[str] = []

    async def stale_sender(frame: dict) -> None:
        got_old.append(str(frame.get("text") or ""))

    coara.session_id = "sess-old"
    registry.register("web", stale_sender, "sess-old")
    # 新会话没有 web 通道：注入段正文兜底发起端，绝不进旧会话的残留 sender
    coara.session_id = "sess-new"
    await coara._route_chunk_to_current_end("新会话正文", "web")
    assert got_old == []
