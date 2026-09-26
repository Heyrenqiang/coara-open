"""子智能体花费上抛父会话：web/CLI 的「花费」含子智能体消耗。

只累加费用，不动父的 token 累计（输入基线 / 缓存命中率仍按本会话自身口径）。
"""

from __future__ import annotations

from src.coara.base import CoaraBase
from src.context.window import LlmUsageSnapshot


class _Stub:
    def __init__(self, *, depth: int, parent: object | None = None) -> None:
        self.delegate_depth = depth
        self._subagent_parent = parent
        self._llm_usage_snapshot = LlmUsageSnapshot()


def _credit(node: _Stub, cost: float | None) -> None:
    CoaraBase._credit_cost_to_parent(node, cost)  # type: ignore[arg-type]


def test_subagent_cost_credits_root_snapshot() -> None:
    root = _Stub(depth=0)
    sub = _Stub(depth=1, parent=root)
    _credit(sub, 0.42)
    assert root._llm_usage_snapshot.cumulative_cost == 0.42


def test_root_and_invalid_cost_are_ignored() -> None:
    root = _Stub(depth=0)
    _credit(root, 1.0)  # 顶层实例没有父，不该自增
    assert root._llm_usage_snapshot.cumulative_cost == 0.0
    sub = _Stub(depth=1, parent=root)
    _credit(sub, None)
    _credit(sub, 0.0)
    _credit(sub, -1.0)
    assert root._llm_usage_snapshot.cumulative_cost == 0.0


def test_nested_subagent_walks_up_to_root_once() -> None:
    root = _Stub(depth=0)
    mid = _Stub(depth=1, parent=root)
    leaf = _Stub(depth=2, parent=mid)
    _credit(leaf, 0.25)
    assert root._llm_usage_snapshot.cumulative_cost == 0.25
    # 只记到根会话一次，中间层不重复记账
    assert mid._llm_usage_snapshot.cumulative_cost == 0.0


def test_snapshot_add_cost_accumulates_and_ignores_non_positive() -> None:
    snap = LlmUsageSnapshot()
    snap.add_cost(None)
    snap.add_cost(0.0)
    assert snap.cumulative_cost == 0.0
    snap.add_cost(0.5)
    snap.add_cost(0.25)
    assert abs(snap.cumulative_cost - 0.75) < 1e-9
    # 费用随会话快照持久化（刷新/重连后仍显示同一口径）
    assert snap.to_dict()["cumulative_cost"] == snap.cumulative_cost
