"""K3 dynamic tool loading：声明消息改写（kimi openai 驱动）与 activate 声明追加。"""

from __future__ import annotations

import json
import types

import pytest

from src.core.message_tags import tool_declaration
from src.core.types import Message, MessageRole
from src.llm.openai import OpenAIProvider
from src.tools.builtin.integration.tool import ToolGatewayInvocation


def _decl_message() -> Message:
    payload = json.dumps(
        {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "dyn_echo",
                        "description": "回声",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        },
        ensure_ascii=False,
    )
    return Message(role=MessageRole.USER, content=tool_declaration(payload))


def test_kimi_openai_converts_declaration_to_system_tools() -> None:
    p = OpenAIProvider("kimi", "k", "https://api.kimi.com/coding/v1", "k3")
    converted = p._convert_messages(
        [
            Message(role=MessageRole.SYSTEM, content="sys"),
            _decl_message(),
            Message(role=MessageRole.USER, content="你好"),
        ]
    )
    assert converted[0] == {"role": "system", "content": "sys"}
    assert converted[1]["role"] == "system"
    assert converted[1]["tools"][0]["function"]["name"] == "dyn_echo"
    assert "content" not in converted[1]  # system+tools 消息不得带 content（官方 400 约束）
    assert converted[2] == {"role": "user", "content": "你好"}


def test_declaration_not_converted_for_non_k3_model() -> None:
    p = OpenAIProvider("kimi", "k", "https://api.kimi.com/coding/v1", "kimi-for-coding")
    converted = p._convert_messages([_decl_message()])
    assert converted[0]["role"] == "user"  # 非 k3 按普通文本透传，不发声明


def test_declaration_not_converted_for_other_providers() -> None:
    p = OpenAIProvider("zhipu", "k", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3")
    converted = p._convert_messages([_decl_message()])
    assert converted[0]["role"] == "user"


def test_invalid_declaration_falls_back_to_plain_text() -> None:
    p = OpenAIProvider("kimi", "k", "https://api.kimi.com/coding/v1", "k3")
    bad = Message(role=MessageRole.USER, content=tool_declaration("not json"))
    converted = p._convert_messages([bad])
    assert converted[0]["role"] == "user"


def _fake_coara(*, driver: str, base_url: str, model: str, owner: bool = True) -> types.SimpleNamespace:
    from src.llm.anthropic import AnthropicProvider
    from src.llm.openai import OpenAIProvider

    if driver == "openai":
        provider = OpenAIProvider("kimi", "test-key", base_url, model)
    else:
        provider = AnthropicProvider("kimi", "test-key", base_url, model)
    return types.SimpleNamespace(
        provider=provider,
        provider_name="kimi",
        model_name=model,
        message_history=[],
        identity=types.SimpleNamespace(is_owner_context=owner),
        _wrap_tool_result=lambda _name, result: result.content,
    )


def test_activate_appends_declaration_on_kimi_k3(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.executor import ToolExecution
    from src.coara.turn_loop.tool_results import apply_tool_results_to_history
    from src.core.types import ToolCall

    tool = types.SimpleNamespace(
        name="embody",
        description="具身器官",
        parameters_schema={"type": "object", "properties": {}},
        should_defer=True,
        owner_only=False,
    )
    manager = types.SimpleNamespace(tools={"embody": tool}, _revealed=set(), reveal_tool=lambda n: True)
    coara = _fake_coara(driver="openai", base_url="https://api.kimi.com/coding/v1", model="k3")
    coara._tool_manager = manager

    inv = ToolGatewayInvocation({"action": "activate", "name": "embody"}, coara)
    import asyncio

    result = asyncio.run(inv.execute())

    assert not result.is_error
    assert coara.message_history == []  # execute 阶段不直接追加声明

    tc = ToolCall(id="t-activate", name="tool", arguments={"action": "activate", "name": "embody"})
    assistant = Message(role=MessageRole.ASSISTANT, content="", tool_calls=[tc])
    coara.message_history.append(assistant)
    apply_tool_results_to_history(
        coara,
        executions=[ToolExecution(index=0, tool_call=tc, result=result)],
        assistant_message_index=0,
        assistant_message=assistant,
        recent_progress_signatures=set(),
    )

    assert len(coara.message_history) == 3
    assert coara.message_history[0].role == MessageRole.ASSISTANT
    assert coara.message_history[1].role == MessageRole.TOOL_RESULT
    decl_msg = coara.message_history[2]
    assert decl_msg.role == MessageRole.USER
    assert decl_msg.content.startswith("<工具声明>")


def test_activate_declaration_after_tool_result_converts_cleanly() -> None:
    from src.agent.executor import ToolExecution
    from src.coara.turn_loop.tool_results import apply_tool_results_to_history
    from src.core.types import ToolCall

    tool = types.SimpleNamespace(
        name="embody",
        description="具身器官",
        parameters_schema={"type": "object", "properties": {}},
        should_defer=True,
        owner_only=False,
    )
    manager = types.SimpleNamespace(tools={"embody": tool}, _revealed=set(), reveal_tool=lambda n: True)
    coara = _fake_coara(driver="openai", base_url="https://api.kimi.com/coding/v1", model="k3")
    coara._tool_manager = manager

    inv = ToolGatewayInvocation({"action": "activate", "name": "embody"}, coara)
    import asyncio

    result = asyncio.run(inv.execute())
    tc = ToolCall(id="tool:434", name="tool", arguments={"action": "activate", "name": "embody"})
    assistant = Message(role=MessageRole.ASSISTANT, content="", tool_calls=[tc])
    coara.message_history = [
        Message(role=MessageRole.USER, content="hi"),
        assistant,
    ]
    apply_tool_results_to_history(
        coara,
        executions=[ToolExecution(index=0, tool_call=tc, result=result)],
        assistant_message_index=1,
        assistant_message=assistant,
        recent_progress_signatures=set(),
    )

    p = OpenAIProvider("kimi", "k", "https://api.kimi.com/coding/v1", "k3")
    converted = p._convert_messages(coara.message_history)
    roles = [m["role"] for m in converted]
    assert roles == ["user", "assistant", "tool", "system"]
    assert converted[2]["tool_call_id"] == "tool:434"
    assert converted[3]["tools"][0]["function"]["name"] == "embody"


def test_orphan_tool_call_closed_in_conversion() -> None:
    from src.core.types import ToolCall

    p = OpenAIProvider("kimi", "k", "https://api.kimi.com/coding/v1", "k3")
    orphan = Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id="tool:434", name="tool", arguments={"action": "activate", "name": "embody"})],
    )
    converted = p._convert_messages(
        [Message(role=MessageRole.USER, content="hi"), orphan, Message(role=MessageRole.USER, content="next")]
    )
    # 转换后：user → assistant(tool_calls) → 占位 tool 响应 → user，配对完整
    roles = [m["role"] for m in converted]
    assert roles == ["user", "assistant", "tool", "user"]
    assert converted[2]["tool_call_id"] == "tool:434"


def test_answered_tool_call_not_duplicated() -> None:
    from src.core.types import ToolCall

    p = OpenAIProvider("kimi", "k", "https://api.kimi.com/coding/v1", "k3")
    msgs = [
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[ToolCall(id="t1", name="read", arguments={})]),
        Message(role=MessageRole.TOOL_RESULT, tool_call_id="t1", name="read", content="ok"),
        Message(role=MessageRole.USER, content="next"),
    ]
    converted = p._convert_messages(msgs)
    assert [m["role"] for m in converted] == ["assistant", "tool", "user"]


def test_multiple_tool_calls_partial_answer_closed() -> None:
    from src.core.types import ToolCall

    p = OpenAIProvider("kimi", "k", "https://api.kimi.com/coding/v1", "k3")
    msgs = [
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="t1", name="a", arguments={}), ToolCall(id="t2", name="b", arguments={})],
        ),
        Message(role=MessageRole.TOOL_RESULT, tool_call_id="t1", name="a", content="ok"),
        Message(role=MessageRole.USER, content="next"),
    ]
    converted = p._convert_messages(msgs)
    assert [m["role"] for m in converted] == ["assistant", "tool", "tool", "user"]
    assert converted[2]["tool_call_id"] == "t2"


def test_orphan_tool_result_removed_in_conversion() -> None:
    p = OpenAIProvider("kimi", "k", "https://api.kimi.com/coding/v1", "k3")
    msgs = [
        Message(role=MessageRole.USER, content="hi"),
        Message(role=MessageRole.TOOL_RESULT, tool_call_id="ghost", name="read", content="孤儿响应"),
        Message(role=MessageRole.USER, content="next"),
    ]
    converted = p._convert_messages(msgs)
    assert [m["role"] for m in converted] == ["user", "user"]


def test_k3_revealed_deferred_excluded_from_request_tools() -> None:
    """K3 动态装载：revealed 挂起工具只走历史声明，不进请求级 tools。"""
    from src.coara.tool_manager import ToolManager

    coara = _fake_coara(driver="openai", base_url="https://api.kimi.com/coding/v1", model="k3")
    manager = ToolManager(owner=coara)
    tool = types.SimpleNamespace(
        name="embody",
        description="具身器官",
        parameters_schema={"type": "object", "properties": {}},
        should_defer=True,
        owner_only=False,
        definition={
            "name": "embody",
            "description": "具身器官",
            "parameters": {"type": "object", "properties": {}},
        },
    )
    manager._tools["embody"] = tool
    manager._deferred.add("embody")
    manager._revealed.add("embody")

    names = {td["name"] for td in manager.get_tool_definitions_for_llm(True)}
    assert "embody" not in names


def test_non_k3_revealed_deferred_in_request_tools() -> None:
    from src.coara.tool_manager import ToolManager

    coara = _fake_coara(driver="openai", base_url="https://open.bigmodel.cn/api/paas/v4", model="glm-5.3")
    manager = ToolManager(owner=coara)
    tool = types.SimpleNamespace(
        name="embody",
        description="具身器官",
        parameters_schema={"type": "object", "properties": {}},
        should_defer=True,
        owner_only=False,
        definition={
            "name": "embody",
            "description": "具身器官",
            "parameters": {"type": "object", "properties": {}},
        },
    )
    manager._tools["embody"] = tool
    manager._deferred.add("embody")
    manager._revealed.add("embody")

    names = {td["name"] for td in manager.get_tool_definitions_for_llm(True)}
    assert "embody" in names


def test_restore_reveal_reissues_declaration_when_history_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.coara.tool_manager import ToolManager

    tool = types.SimpleNamespace(
        name="embody",
        description="具身器官",
        parameters_schema={"type": "object", "properties": {}},
        should_defer=True,
        owner_only=False,
    )
    coara = _fake_coara(driver="openai", base_url="https://api.kimi.com/coding/v1", model="k3")
    manager = ToolManager(owner=coara)
    manager._tools["embody"] = tool
    manager._deferred.add("embody")
    manager._pending_restored_reveals.add("embody")

    manager._apply_pending_restored_reveal("embody")

    assert "embody" in manager._revealed
    assert len(coara.message_history) == 1
    assert coara.message_history[0].content.startswith("<工具声明>")


def test_restore_reveal_skips_reissue_when_declaration_present() -> None:
    from src.coara.tool_manager import ToolManager

    tool = types.SimpleNamespace(
        name="embody",
        description="具身器官",
        parameters_schema={"type": "object", "properties": {}},
        should_defer=True,
        owner_only=False,
    )
    coara = _fake_coara(driver="openai", base_url="https://api.kimi.com/coding/v1", model="k3")
    # 会话恢复场景：历史里已有声明 → 不重复补发（K3 重复声明 = 400）
    existing = json.dumps(
        {"tools": [{"type": "function", "function": {"name": "embody", "description": "x", "parameters": {}}}]},
        ensure_ascii=False,
    )
    coara.message_history.append(Message(role=MessageRole.USER, content=tool_declaration(existing)))
    manager = ToolManager(owner=coara)
    manager._tools["embody"] = tool
    manager._deferred.add("embody")
    manager._pending_restored_reveals.add("embody")

    manager._apply_pending_restored_reveal("embody")

    assert len(coara.message_history) == 1  # 仍只有原有那一条

    tool = types.SimpleNamespace(
        name="embody",
        description="具身器官",
        parameters_schema={"type": "object", "properties": {}},
        should_defer=True,
        owner_only=False,
    )
    manager = types.SimpleNamespace(tools={"embody": tool}, _revealed=set(), reveal_tool=lambda n: True)
    coara = _fake_coara(driver="openai", base_url="https://open.bigmodel.cn/api/paas/v4", model="glm-5.3")
    coara._tool_manager = manager

    inv = ToolGatewayInvocation({"action": "activate", "name": "embody"}, coara)
    import asyncio

    result = asyncio.run(inv.execute())

    assert not result.is_error
    assert coara.message_history == []  # 非 kimi 会话不追加声明消息
