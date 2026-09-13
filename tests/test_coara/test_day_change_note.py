"""跨天注记：环境上下文 seed 日期过期时垫一条 <系统消息> 并就地修正 seed。"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.coara.turn_loop.user_turn_injectors import inject_day_change_note
from src.core.types import Message, MessageRole


def _seed(days_ago: int) -> str:
    day = datetime.now() - timedelta(days=days_ago)
    return f"<系统消息>\n环境上下文：\n- 今天日期：{day.strftime('%Y年%m月%d日 %A')}\n- 地点：测试\n</系统消息>"


def _coara_with_seed(seed_content: str) -> SimpleNamespace:
    return SimpleNamespace(
        delegate_depth=0,
        message_history=[Message(role=MessageRole.USER, content=seed_content)],
    )


@pytest.mark.asyncio
async def test_day_change_injects_note_and_fixes_seed() -> None:
    coara = _coara_with_seed(_seed(days_ago=1))
    await inject_day_change_note(coara, SimpleNamespace(content="hi"))

    assert len(coara.message_history) == 2
    note = str(coara.message_history[-1].content)
    assert "日期变更" in note
    today_str = datetime.now().strftime("%Y年%m月%d日")
    assert today_str in note
    # seed 就地修正为今天（同时是当天去重依据）
    assert today_str in str(coara.message_history[0].content)


@pytest.mark.asyncio
async def test_day_change_fires_only_once_per_day() -> None:
    coara = _coara_with_seed(_seed(days_ago=1))
    await inject_day_change_note(coara, SimpleNamespace(content="hi"))
    await inject_day_change_note(coara, SimpleNamespace(content="again"))
    assert len(coara.message_history) == 2


@pytest.mark.asyncio
async def test_day_change_noop_when_seed_is_today() -> None:
    coara = _coara_with_seed(_seed(days_ago=0))
    await inject_day_change_note(coara, SimpleNamespace(content="hi"))
    assert len(coara.message_history) == 1


@pytest.mark.asyncio
async def test_day_change_noop_without_seed() -> None:
    coara = SimpleNamespace(
        delegate_depth=0,
        message_history=[Message(role=MessageRole.USER, content="hello")],
    )
    await inject_day_change_note(coara, SimpleNamespace(content="hi"))
    assert len(coara.message_history) == 1
