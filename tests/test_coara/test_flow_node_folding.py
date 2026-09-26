"""编排节点的归属：带父标识才折叠，绝不进主会话流。

编排织出的 flow 节点不是 delegate 产物（录像带标记 `session_tape="flow"`），历史上
它的输出按主流投递——用户要的是三级折叠：orchestrator 行 → 节点清单 → 单个节点的
任务/过程/结果，所以节点必须带着「父行 call_id + 节点 id」出生。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from src.coara.base import _is_subagent_instance, _route_subagent_tool_line
from src.coara.flow_coordinator import _stamp_node_origin


class _Node:
    _session_tape = "flow"
    _session_agent_kind = ""
    _cli_silent = False


class _Parent:
    session_id = "sess-1"
    workspace_dir = r"D:\ws"


def test_flow_node_without_parent_stays_mainstream() -> None:
    """无父标的老图：保持原状（按主流投递），不因新规则改行为。"""
    assert _is_subagent_instance(_Node()) is False


def test_stamped_flow_node_is_folded() -> None:
    """盖上归属后节点即走折叠通道——它的输出只会挂在 orchestrator 那行下面。"""
    node: Any = _Node()
    _stamp_node_origin(
        node,
        parent=_Parent(),
        flow="demo",
        node_id="a",
        parent_tool_call_id="call_1",
    )

    assert node._delegate_parent_tool_call_id == "call_1"
    assert node._delegate_subagent_id == "flow-demo-a"
    assert node._delegate_parent_session_id == "sess-1"
    assert _is_subagent_instance(node) is True


def test_stamp_tolerates_readonly_stand_in() -> None:
    """替身对象不可写时跳过而非抛错（测试/重建路径）。"""

    class _Frozen:
        __slots__ = ()

    _stamp_node_origin(
        _Frozen(),
        parent=_Parent(),
        flow="demo",
        node_id="a",
        parent_tool_call_id="call_1",
    )


class _Registry:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def deliver(self, source: str, session_id: str, frame: dict[str, Any], channel_id: str = "") -> Any:
        self.calls.append(frame)
        return SimpleNamespace(hit=True, value=None)


def test_node_frame_carries_node_identity() -> None:
    """同一父行下多个节点共用一个 call_id：帧上必须带节点身份，端上才分得开。

    丢了这两个字段，三级折叠的第三级就只能是整片平铺（线上即表现为「认不出节点」）。
    """
    registry = _Registry()
    node: Any = _Node()
    node._delegate_parent_tool_call_id = "call_1"
    node._delegate_subagent_id = "flow-demo-a"
    node._delegate_parent_session_id = "sess-1"
    node._delegate_parent_workspace_dir = r"D:\ws"
    node._subagent_origin = ("web", "")
    node._root_ref = SimpleNamespace(end_registry=registry)
    node.identity = SimpleNamespace(coara_id="coara-9")

    _route_subagent_tool_line(node, {"tool_name": "read", "tool_call_id": "tc1"}, "read(x)")

    assert len(registry.calls) == 1
    frame = registry.calls[0]
    assert frame["parent_tool_call_id"] == "call_1"
    assert frame["subagent_id"] == "flow-demo-a"
    assert frame["coara_id"] == "coara-9"
