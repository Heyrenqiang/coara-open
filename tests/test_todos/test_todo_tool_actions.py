"""Tool-level tests for todo actions: read / update (full-list replace) / park."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.todos.registry import get_todo_store
from src.tools.builtin.todo.todo import TodoTool


def _tool(tmp_path: Path) -> TodoTool:
    parent = SimpleNamespace(workspace_dir=tmp_path, session_id="s-todo-actions")
    return TodoTool(parent_coara=parent)  # type: ignore[arg-type]


async def _run(tool: TodoTool, params: dict):
    params = {**params, "description": params.get("description", "测试操作")}
    return await tool.create_invocation(params).execute()


def test_requires_description(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    with pytest.raises(ValueError, match="description"):
        tool.create_invocation({"action": "update", "todos": [{"content": "写方案"}]})
    with pytest.raises(ValueError, match="description"):
        tool.create_invocation({"action": "read"})


def test_description_echoed_in_metadata(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    result = asyncio.run(
        _run(
            tool,
            {
                "action": "update",
                "todos": [{"content": "写方案"}],
                "description": "建立审查清单",
            },
        )
    )
    assert not result.is_error
    assert result.metadata["description"] == "建立审查清单"
    assert result.metadata["action"] == "update"


@pytest.mark.asyncio
async def test_update_creates_full_list_with_defaults(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    result = await _run(tool, {"action": "update", "todos": [{"content": "写方案"}, {"content": "评审"}]})
    assert not result.is_error
    todos = result.metadata["todos"]
    assert [t["content"] for t in todos] == ["写方案", "评审"]
    assert all(t["status"] == "pending" for t in todos)
    assert all(t["priority"] == "medium" for t in todos)
    assert [t["id"] for t in todos] == ["t1", "t2"]


@pytest.mark.asyncio
async def test_update_preserves_id_by_same_content(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    first = await _run(
        tool,
        {"action": "update", "todos": [{"id": "outline", "content": "写大纲", "status": "in_progress"}]},
    )
    assert first.metadata["todos"][0]["id"] == "outline"
    second = await _run(
        tool,
        {
            "action": "update",
            "todos": [
                {"content": "写大纲", "status": "completed"},
                {"content": "写正文", "status": "in_progress"},
            ],
        },
    )
    assert not second.is_error
    by_content = {t["content"]: t for t in second.metadata["todos"]}
    assert by_content["写大纲"]["id"] == "outline"
    assert by_content["写大纲"]["status"] == "completed"
    assert by_content["写正文"]["status"] == "in_progress"
    assert "已完成：outline" in second.content


@pytest.mark.asyncio
async def test_update_allows_multiple_in_progress(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    result = await _run(
        tool,
        {
            "action": "update",
            "todos": [
                {"content": "甲", "status": "in_progress"},
                {"content": "乙", "status": "in_progress"},
            ],
        },
    )
    assert not result.is_error
    in_progress = [t for t in result.metadata["todos"] if t["status"] == "in_progress"]
    assert len(in_progress) == 2


@pytest.mark.asyncio
async def test_update_empty_clears(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    await _run(tool, {"action": "update", "todos": [{"content": "甲"}]})
    result = await _run(tool, {"action": "update", "todos": []})
    assert not result.is_error
    assert result.metadata["todos"] == []
    assert "已清空" in result.content


@pytest.mark.asyncio
async def test_update_requires_content(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    result = await _run(tool, {"action": "update", "todos": [{"status": "pending"}]})
    assert result.is_error
    assert "content" in result.content


@pytest.mark.asyncio
async def test_update_rejects_duplicate_ids(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    result = await _run(
        tool,
        {
            "action": "update",
            "todos": [
                {"id": "a", "content": "甲"},
                {"id": "a", "content": "乙"},
            ],
        },
    )
    assert result.is_error
    assert "重复" in result.content


@pytest.mark.asyncio
async def test_update_explanation_in_metadata(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    result = await _run(
        tool,
        {
            "action": "update",
            "todos": [{"content": "改做集成测试", "status": "in_progress"}],
            "explanation": "单测已够，改集成",
        },
    )
    assert not result.is_error
    assert result.metadata.get("explanation") == "单测已够，改集成"
    assert "改做集成测试" in result.content


@pytest.mark.asyncio
async def test_read_returns_current_list(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    await _run(tool, {"action": "update", "todos": [{"id": "1", "content": "写方案"}]})
    result = await _run(tool, {"action": "read"})
    assert not result.is_error
    assert result.metadata["todos"][0]["content"] == "写方案"


@pytest.mark.asyncio
async def test_completed_change_injects_next_guidance(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    await _run(
        tool,
        {
            "action": "update",
            "todos": [
                {"id": "a", "content": "第一项", "status": "in_progress"},
                {"id": "b", "content": "第二项"},
            ],
        },
    )
    result = await _run(
        tool,
        {
            "action": "update",
            "todos": [
                {"id": "a", "content": "第一项", "status": "completed"},
                {"id": "b", "content": "第二项", "status": "in_progress"},
            ],
        },
    )
    assert not result.is_error
    assert "a → completed" in result.content  # 仅状态翻转：只回变化行
    assert "已完成：a" in result.content
    assert "剩余 1 项：b" in result.content
    assert "下一个：b" in result.content
    assert "不要复述整表" not in result.content  # 行为指导归工具描述，回执不重复


def test_unknown_action_raises(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    with pytest.raises(ValueError, match="未知 todo action"):
        tool.create_invocation({"action": "add", "todos": [], "description": "测试"})


@pytest.mark.asyncio
async def test_park_stages_closing_message_and_keeps_todos(tmp_path: Path) -> None:
    """park 一步收尾：结束语挂上 coara 实例（编排器在工具批次后交付并立即结束
    回合），待办保持未完成、不伪装完结。"""
    tool = _tool(tmp_path)
    await _run(tool, {"action": "update", "todos": [{"content": "等后台构建", "status": "in_progress"}]})

    result = await _run(
        tool,
        {"action": "park", "description": "等后台构建完成", "message": "构建在后台运行，完成后我会继续。"},
    )

    assert not result.is_error
    assert result.metadata["action"] == "park"
    assert result.metadata["open_count"] == 1
    assert tool._parent._todo_park_message == "构建在后台运行，完成后我会继续。"
    store = get_todo_store(workspace_dir=tmp_path, session_id="s-todo-actions")
    assert store.get_all()[0].status.value == "in_progress"


@pytest.mark.asyncio
async def test_park_with_todos_updates_list_in_one_call(tmp_path: Path) -> None:
    """park 可顺带整表更新：一次调用把已出结果的项标掉，同时结束回合。"""
    tool = _tool(tmp_path)
    await _run(
        tool,
        {
            "action": "update",
            "todos": [
                {"id": "a", "content": "读取输入", "status": "completed"},
                {"id": "b", "content": "运行构建", "status": "in_progress"},
            ],
        },
    )

    result = await _run(
        tool,
        {
            "action": "park",
            "description": "构建失败已定位，等用户确认方案",
            "message": "构建失败了，原因已定位，需要你确认修复方案。",
            "todos": [
                {"id": "a", "content": "读取输入", "status": "completed"},
                {"id": "b", "content": "运行构建", "status": "failed", "notes": "依赖缺失"},
            ],
        },
    )

    assert not result.is_error
    assert result.metadata["open_count"] == 0
    store = get_todo_store(workspace_dir=tmp_path, session_id="s-todo-actions")
    # 整表全为终态（completed/failed）：自动清理为空——终态项不堆积。
    assert store.get_all() == []
    assert tool._parent._todo_park_message == "构建失败了，原因已定位，需要你确认修复方案。"


@pytest.mark.asyncio
async def test_park_with_no_open_todos_still_closes(tmp_path: Path) -> None:
    """没有未完成待办时 park 也无害：照常交付 message 并结束回合。"""
    tool = _tool(tmp_path)
    await _run(tool, {"action": "update", "todos": [{"content": "已完成的事", "status": "completed"}]})

    result = await _run(tool, {"action": "park", "description": "收尾", "message": "全部处理完了。"})

    assert not result.is_error
    assert result.metadata["open_count"] == 0
    assert tool._parent._todo_park_message == "全部处理完了。"


def test_park_requires_message(tmp_path: Path) -> None:
    """message 是 park 的必填参数——结束语就放在工具参数里，不允许省略。"""
    tool = _tool(tmp_path)
    with pytest.raises(ValueError, match="message"):
        tool.create_invocation({"action": "park", "description": "等后台任务"})
