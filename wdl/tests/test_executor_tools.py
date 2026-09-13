"""executor agentic 工具循环：httpx MockTransport fake LLM（先回 tool_calls 再回终稿）。

覆盖：openai / anthropic 两拍循环、工具执行与结果回注、事件透出、
路径越界拒绝回注、工具轮数上限、未声明 tools 的单回合兼容、
settings 解析（providers.yaml 与 WDL 图级）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from wdl.core.serde import parse_graph
from wdl.errors import WDLError
from wdl.executor import DEFAULT_MAX_TOOL_ROUNDS, LLMNodeExecutor
from wdl.providers import ProviderConfig, ProvidersConfig, ToolSettings, load_providers_config

KEY = "test-key"


def _openai_config() -> ProvidersConfig:
    return ProvidersConfig(
        default="fake",
        providers={
            "fake": ProviderConfig(type="openai", base_url="https://fake.local/v1", api_key=KEY, models=["m1"]),
            "claude": ProviderConfig(type="anthropic", base_url="https://fake.local", api_key=KEY, models=["c1"]),
        },
    )


def _client(captured: list[dict], handler) -> httpx.AsyncClient:
    def _handle(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json=handler(captured[-1]))

    return httpx.AsyncClient(transport=httpx.MockTransport(_handle))


def _executor(tmp_path: Path, captured: list[dict], handler, *, provider: str = "fake", **kwargs) -> LLMNodeExecutor:
    events = kwargs.pop("events", None)
    node_tools = kwargs.pop("node_tools", {"n": []})
    hooks = kwargs.pop("on_event", None)
    if events is not None:

        async def on_event(event_type: str, payload: dict) -> None:
            events.append((event_type, payload))

        hooks = on_event
    return LLMNodeExecutor(
        _openai_config(),
        node_tools=node_tools,
        workdir=tmp_path,
        client=_client(captured, handler),
        on_event=hooks,
        node_llms={"n": (provider, "")},
        **kwargs,
    )


# ── openai 家族 ──────────────────────────────────────────────────────────


def _openai_tool_call_payload(path: str, content: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": path, "content": content}, ensure_ascii=False),
                            },
                        }
                    ],
                }
            }
        ]
    }


async def test_openai_two_beat_tool_loop(tmp_path) -> None:
    captured: list[dict] = []
    events: list[tuple[str, dict]] = []

    def handler(payload: dict) -> dict:
        if len(captured) == 1:
            return _openai_tool_call_payload("out.txt", "工具写入内容")
        return {"choices": [{"message": {"role": "assistant", "content": "终稿文本"}}]}

    ex = _executor(tmp_path, captured, handler, events=events)
    try:
        result = await ex(node_id="n", prompt="写个文件", task="写个文件")
    finally:
        await ex.close()

    assert result == "终稿文本"
    # 工具真实执行
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "工具写入内容"
    # 第二次调用携带 tools 声明、assistant tool_calls 与 tool 结果回注
    assert len(captured) == 2
    assert any(t["function"]["name"] == "write_file" for t in captured[0]["tools"])
    messages = captured[1]["messages"]
    assert messages[1]["role"] == "assistant" and messages[1]["tool_calls"]
    assert messages[2]["role"] == "tool" and messages[2]["tool_call_id"] == "call_1"
    assert "已写入" in messages[2]["content"]
    # 事件透出
    types = [t for t, _ in events]
    assert "tool_start" in types and "tool_result" in types
    tool_result = next(p for t, p in events if t == "tool_result")
    assert tool_result["tool"] == "write_file" and tool_result["ok"] is True


async def test_openai_path_escape_injected_back(tmp_path) -> None:
    captured: list[dict] = []

    def handler(payload: dict) -> dict:
        if len(captured) == 1:
            return _openai_tool_call_payload("../evil.txt", "x")
        return {"choices": [{"message": {"role": "assistant", "content": "收到拒绝"}}]}

    ex = _executor(tmp_path, captured, handler)
    try:
        result = await ex(node_id="n", prompt="越界写", task="越界写")
    finally:
        await ex.close()

    assert result == "收到拒绝"
    assert not (tmp_path.parent / "evil.txt").exists()
    tool_msg = captured[1]["messages"][2]
    assert "[工具错误]" in tool_msg["content"] and "越出工作目录" in tool_msg["content"]


async def test_openai_max_rounds_returns_last_text(tmp_path) -> None:
    captured: list[dict] = []

    def handler(payload: dict) -> dict:
        # 每轮都要 tool_call，并夹带一段文本
        payload_out = _openai_tool_call_payload("loop.txt", "x")
        payload_out["choices"][0]["message"]["content"] = f"第 {len(captured)} 轮"
        return payload_out

    ex = _executor(tmp_path, captured, handler, tool_settings=ToolSettings(max_tool_rounds=3))
    try:
        result = await ex(node_id="n", prompt="循环", task="循环")
    finally:
        await ex.close()

    assert result == "第 3 轮"
    assert len(captured) == 3  # 达到上限即停，不再发起第 4 次调用


async def test_no_tools_declaration_single_call(tmp_path) -> None:
    """节点未声明 tools：单回合、payload 不带 tools（旧行为兼容）。"""
    captured: list[dict] = []

    def handler(payload: dict) -> dict:
        return {"choices": [{"message": {"role": "assistant", "content": "单回合"}}]}

    ex = _executor(tmp_path, captured, handler, node_tools={})  # 无 n 条目 = 未声明
    try:
        result = await ex(node_id="n", prompt="你好", task="你好")
    finally:
        await ex.close()

    assert result == "单回合"
    assert len(captured) == 1
    assert "tools" not in captured[0]


async def test_unknown_tool_in_whitelist_raises(tmp_path) -> None:
    captured: list[dict] = []
    ex = _executor(tmp_path, captured, lambda p: {}, node_tools={"n": ["nope"]})
    try:
        with pytest.raises(WDLError, match="未知工具"):
            await ex(node_id="n", prompt="x", task="x")
    finally:
        await ex.close()
    assert captured == []  # 未发起任何 LLM 调用


# ── anthropic 家族 ───────────────────────────────────────────────────────


async def test_anthropic_two_beat_tool_loop(tmp_path) -> None:
    captured: list[dict] = []
    events: list[tuple[str, dict]] = []

    def handler(payload: dict) -> dict:
        if len(captured) == 1:
            return {
                "content": [
                    {"type": "text", "text": "我来写文件"},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "write_file",
                        "input": {"path": "a.md", "content": "正文"},
                    },
                ],
                "stop_reason": "tool_use",
            }
        return {"content": [{"type": "text", "text": "写完了"}], "stop_reason": "end_turn"}

    ex = _executor(tmp_path, captured, handler, provider="claude", events=events)
    try:
        result = await ex(node_id="n", prompt="写文件", task="写文件")
    finally:
        await ex.close()

    assert result == "写完了"
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "正文"
    assert len(captured) == 2
    tool_names = [t["name"] for t in captured[0]["tools"]]
    assert "write_file" in tool_names
    assert "input_schema" in captured[0]["tools"][0]
    messages = captured[1]["messages"]
    # assistant 回注原始 content blocks
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"][1]["type"] == "tool_use"
    # user 回注 tool_result blocks
    assert messages[2]["role"] == "user"
    block = messages[2]["content"][0]
    assert block["type"] == "tool_result" and block["tool_use_id"] == "tu_1"
    assert block["is_error"] is False and "已写入" in block["content"]
    assert [t for t, _ in events] == ["tool_start", "tool_result"]


async def test_anthropic_tool_error_marked_is_error(tmp_path) -> None:
    captured: list[dict] = []

    def handler(payload: dict) -> dict:
        if len(captured) == 1:
            return {
                "content": [{"type": "tool_use", "id": "tu_9", "name": "read_file", "input": {"path": "../x"}}],
                "stop_reason": "tool_use",
            }
        return {"content": [{"type": "text", "text": "放弃"}], "stop_reason": "end_turn"}

    ex = _executor(tmp_path, captured, handler, provider="claude")
    try:
        result = await ex(node_id="n", prompt="读越界文件", task="读越界文件")
    finally:
        await ex.close()

    assert result == "放弃"
    block = captured[1]["messages"][2]["content"][0]
    assert block["is_error"] is True and "越出工作目录" in block["content"]


# ── settings 解析 ────────────────────────────────────────────────────────


def test_providers_yaml_settings(tmp_path) -> None:
    (tmp_path / "providers.yaml").write_text(
        "default: p\nsettings:\n  max_tool_rounds: 7\n  shell_timeout: 30\n"
        "providers:\n  p:\n    type: openai\n    base_url: https://x\n    api_key: k\n    models: [m]\n",
        encoding="utf-8",
    )
    cfg = load_providers_config(tmp_path / "providers.yaml")
    assert cfg.settings.max_tool_rounds == 7
    assert cfg.settings.shell_timeout == 30.0
    # 缺省为空（由 executor 落默认 20 / 120）
    (tmp_path / "bare.yaml").write_text("providers: {}\n", encoding="utf-8")
    bare = load_providers_config(tmp_path / "bare.yaml")
    assert bare.settings.max_tool_rounds == 0 and bare.settings.shell_timeout == 0.0


def test_wdl_graph_settings_and_node_tools() -> None:
    graph = parse_graph(
        """
name: t
settings:
  max_tool_rounds: 5
  shell_timeout: 15
nodes:
  a:
    task: 干活
    tools: [read_file, write_file]
  b:
    task: 全量
    tools: []
  c:
    task: 无工具
"""
    )
    assert graph.settings == {"max_tool_rounds": 5, "shell_timeout": 15.0}
    assert graph.nodes["a"].tools == ["read_file", "write_file"]
    assert graph.nodes["b"].tools == []
    assert graph.nodes["c"].tools is None


def test_wdl_node_tools_must_be_string_list() -> None:
    with pytest.raises(ValueError, match="tools"):
        parse_graph("name: t\nnodes:\n  a:\n    task: x\n    tools: read_file\n")


def test_default_max_tool_rounds() -> None:
    assert DEFAULT_MAX_TOOL_ROUNDS == 20
