"""P0-11: one busy predicate — status IDLE mid-teardown must not look idle."""

from __future__ import annotations

from src.core.types import CoaraStatus
from tests.helpers import make_test_coara


def test_has_active_turn_ignores_early_idle_status(tmp_path) -> None:
    """Turn loop may set status=IDLE before finally clears runtime markers."""
    coara = make_test_coara(tmp_path)
    coara._active_turn = object()
    coara._inside_turn = True
    coara.status = CoaraStatus.IDLE

    assert coara.has_active_turn() is True
    assert coara.is_turn_busy() is True


def test_has_active_turn_and_is_turn_busy_agree(tmp_path) -> None:
    coara = make_test_coara(tmp_path)
    assert coara.has_active_turn() is False
    assert coara.is_turn_busy() is False

    coara._inside_turn = True
    assert coara.has_active_turn() is coara.is_turn_busy() is True

    coara._inside_turn = False
    coara._active_turn = object()
    coara.status = CoaraStatus.FAILED
    assert coara.has_active_turn() is coara.is_turn_busy() is True

    coara._active_turn = None
    assert coara.has_active_turn() is coara.is_turn_busy() is False


def test_has_active_turn_covers_queued_turn_window(tmp_path) -> None:
    """P0 回归：锁已释放、排队回合尚未拿锁的调度窗口内，跟话不得被误判为无活跃回合。

    09-23 web 跟话「空间级基线」恰好撞上该窗口——has_active_turn 假 →
    走完整回合路径排 _process_lock → 整回合后才被 leftover 重开注入。
    队列深度是回合链未断的最宽判据。
    """
    coara = make_test_coara(tmp_path)
    assert coara.has_active_turn() is False

    coara._turn_queue.enqueue("turn-queued-1", "web")
    assert coara.has_active_turn() is True
    assert coara.is_turn_busy() is True

    coara._turn_queue.cancel_all()
    assert coara.has_active_turn() is False
