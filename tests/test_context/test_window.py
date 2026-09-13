from __future__ import annotations

import pytest

from src.context.window import ContextWindowManager, LlmUsageSnapshot, payload_fingerprint
from src.core.types import Message, MessageRole
from src.llm.provider import LLMProvider, LLMResponse, StreamChunk
from src.llm.registry import provider_registry
from src.llm.service import llm_service


class CompressionProvider(LLMProvider):
    def __init__(self, response: LLMResponse):
        super().__init__(name="compression", api_key="test", default_model="compression-model")
        self.response = response
        self.calls: list[dict] = []

    async def complete(self, *args, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        return self.response

    async def stream_complete(self, *args, **kwargs):
        yield StreamChunk(delta_content="")

    def get_context_window(self, model: str | None = None) -> int:
        return 100

    async def close(self) -> None:
        pass

    def abort(self) -> None:
        pass


def _messages(count: int, *, suffix: str = "x") -> list[Message]:
    messages: list[Message] = []
    for idx in range(count):
        role = MessageRole.USER if idx % 2 == 0 else MessageRole.ASSISTANT
        messages.append(Message(role=role, content=f"message-{idx}-{suffix * 80}"))
    return messages


@pytest.fixture
def compression_llm_setup():
    """Register a stub provider and wire context.compression profile for window tests."""
    provider_registry.clear()
    provider = CompressionProvider(
        LLMResponse(content="<state_snapshot><overall_goal>keep going</overall_goal></state_snapshot>")
    )
    provider_registry.register("compression", provider)
    llm_service.configure_test_profiles(
        default_provider="compression",
        default_model="compression-model",
    )
    yield provider
    provider_registry.clear()
    llm_service.reset_for_tests()


@pytest.mark.asyncio
async def test_maybe_compress_messages_uses_llm_summary_and_preserves_recent_history(compression_llm_setup):
    provider = compression_llm_setup
    manager = ContextWindowManager(model_context_window=1000, compression_preserve_ratio=0.4)
    messages = _messages(10, suffix="a")

    compressed, info = await manager.maybe_compress_messages(
        messages,
        max_tokens=100,
    )

    assert info["compressed"] is True
    assert info["method"] == "llm"
    assert info["status"] == "COMPRESSED"
    assert info["preserve_ratio"] == 0.4
    assert compressed[0].role == MessageRole.USER
    assert "<state_snapshot>" in str(compressed[0].content)
    assert compressed[1].role == MessageRole.ASSISTANT
    assert "Got it." in str(compressed[1].content)
    assert len(compressed) < len(messages)
    assert provider.calls
    compression_call = provider.calls[0]
    assert compression_call["system_prompt"]
    assert compression_call["tools"] is None
    assert compression_call["temperature"] == 0.2


@pytest.mark.asyncio
async def test_maybe_compress_messages_skips_when_compressible_fraction_too_small():
    provider_registry.clear()
    provider = CompressionProvider(LLMResponse(content="<state_snapshot>unused</state_snapshot>"))
    provider_registry.register("compression", provider)
    llm_service.configure_test_profiles(
        default_provider="compression",
        default_model="compression-model",
    )

    manager = ContextWindowManager(
        model_context_window=1000,
        compression_preserve_ratio=0.95,
        min_compressible_fraction=0.2,
        compression_preserve_last_n=None,
    )
    messages = _messages(6, suffix="b")

    compressed, info = await manager.maybe_compress_messages(
        messages,
        max_tokens=100,
    )

    assert compressed == messages
    assert info["compressed"] is False
    assert info["status"] == "NOOP"
    assert info["reason"] == "below_min_compressible_fraction"
    assert not provider.calls

    provider_registry.clear()
    llm_service.reset_for_tests()


@pytest.mark.asyncio
async def test_compress_with_llm_falls_back_to_truncation_when_summary_is_empty():
    provider_registry.clear()
    provider = CompressionProvider(LLMResponse(content=""))
    provider_registry.register("compression", provider)
    llm_service.configure_test_profiles(
        default_provider="compression",
        default_model="compression-model",
    )

    manager = ContextWindowManager(model_context_window=1000)
    messages = _messages(8, suffix="c")

    compressed, info = await manager.maybe_compress_messages(
        messages,
        max_tokens=200,
    )

    assert info["compressed"] is True
    assert info["method"] == "truncation"
    assert info["status"] == "FAILED_EMPTY_SUMMARY"
    assert len(compressed) < len(messages)

    provider_registry.clear()
    llm_service.reset_for_tests()


def test_llm_usage_snapshot_marks_partial_turn() -> None:
    """#46 残余②：finish_reason=partial 的回合按 partial 口径记账并随快照持久化"""
    snapshot = LlmUsageSnapshot()
    snapshot.record_turn(
        usage={"input_tokens": 1000, "output_tokens": 200},
        history_len=3,
        system_len=10,
        tool_count=2,
        partial=True,
    )
    assert snapshot.last_turn_partial is True
    assert snapshot.usage == {"input_tokens": 1000, "output_tokens": 200}
    assert snapshot.cumulative_prompt_tokens == 1000

    restored = LlmUsageSnapshot()
    restored.restore(snapshot.to_dict())
    assert restored.last_turn_partial is True

    snapshot.record_turn(
        usage={"input_tokens": 1200, "output_tokens": 300},
        history_len=5,
        system_len=10,
        tool_count=2,
    )
    assert snapshot.last_turn_partial is False
    assert snapshot.cumulative_prompt_tokens == 2200


def test_llm_usage_snapshot_partial_without_usage_keeps_previous_accounting() -> None:
    """#46 残余②：无 usage 上报的 partial 回合只打标记，不覆盖上一轮完整记账"""
    snapshot = LlmUsageSnapshot()
    snapshot.record_turn(usage={"input_tokens": 500}, history_len=2, system_len=5, tool_count=1)
    snapshot.record_turn(usage={}, history_len=4, system_len=5, tool_count=1, partial=True)
    assert snapshot.last_turn_partial is True
    assert snapshot.usage == {"input_tokens": 500}
    assert snapshot.cumulative_prompt_tokens == 500

    snapshot.clear()
    assert snapshot.last_turn_partial is False


def test_resolve_input_tokens_prefers_reported_usage() -> None:
    manager = ContextWindowManager(model_context_window=128_000)
    messages = [
        Message(role=MessageRole.USER, content="hello"),
        Message(role=MessageRole.ASSISTANT, content="world"),
        Message(role=MessageRole.USER, content="follow up"),
    ]

    snapshot = LlmUsageSnapshot(
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 19_900,
        },
        history_len=2,
    )
    resolved = manager.resolve_input_tokens(
        system_prompt="system",
        messages=messages,
        tool_definitions=[],
        snapshot=snapshot,
    )

    delta_only = manager.estimate_messages_tokens(messages[2:])
    assert resolved == 20_000 + delta_only
    assert resolved > 20_000


def test_resolve_input_tokens_invalidates_when_system_or_tools_change() -> None:
    manager = ContextWindowManager(model_context_window=128_000)
    messages = [Message(role=MessageRole.USER, content="hello")]
    tools = [{"name": "read", "description": "Read", "parameters": {"type": "object", "properties": {}}}]

    snapshot = LlmUsageSnapshot(
        usage={"input_tokens": 20_000},
        history_len=1,
        system_len=len("short"),
        tool_count=1,
    )
    with_report = manager.resolve_input_tokens(
        system_prompt="short",
        messages=messages,
        tool_definitions=tools,
        snapshot=snapshot,
    )
    assert with_report == 20_000

    after_skill = manager.resolve_input_tokens(
        system_prompt="short" + (" extra skill instructions" * 50),
        messages=messages,
        tool_definitions=tools,
        snapshot=snapshot,
    )
    assert after_skill != 20_000
    assert after_skill == manager.estimate_llm_input_tokens(
        system_prompt="short" + (" extra skill instructions" * 50),
        messages=messages,
        tool_definitions=tools,
    )


def test_resolve_input_tokens_detects_equal_length_payload_change() -> None:
    """#31：等长替换 system prompt/工具时内容指纹失配，回退全量估算而非沿用旧值。"""
    manager = ContextWindowManager(model_context_window=128_000)
    prompt_a = "system prompt version A"
    prompt_b = "system prompt version B"  # 与 A 等长
    assert len(prompt_a) == len(prompt_b)
    messages = [Message(role=MessageRole.USER, content="hello")]

    snapshot = LlmUsageSnapshot(
        usage={"input_tokens": 20_000},
        history_len=1,
        system_len=len(prompt_a),
        tool_count=0,
        payload_hash=payload_fingerprint(prompt_a, []),
    )

    changed = manager.resolve_input_tokens(
        system_prompt=prompt_b,
        messages=messages,
        tool_definitions=[],
        snapshot=snapshot,
    )
    assert changed != 20_000
    assert changed == manager.estimate_llm_input_tokens(
        system_prompt=prompt_b,
        messages=messages,
        tool_definitions=[],
    )

    unchanged = manager.resolve_input_tokens(
        system_prompt=prompt_a,
        messages=messages,
        tool_definitions=[],
        snapshot=snapshot,
    )
    assert unchanged == 20_000


def test_llm_usage_snapshot_persists_payload_hash() -> None:
    """#31：内容指纹随快照持久化/恢复。"""
    snapshot = LlmUsageSnapshot()
    snapshot.record_turn(
        usage={"input_tokens": 100},
        history_len=1,
        system_len=3,
        tool_count=0,
        payload_hash="abc123",
    )
    restored = LlmUsageSnapshot()
    restored.restore(snapshot.to_dict())
    assert restored.payload_hash == "abc123"


def test_estimate_llm_input_tokens_includes_system_and_tools() -> None:
    manager = ContextWindowManager(model_context_window=128_000)
    messages = [Message(role=MessageRole.USER, content="hello")]
    tools = [{"name": "read", "description": "Read a file", "parameters": {"type": "object", "properties": {}}}]

    msg_only = manager.estimate_messages_tokens(messages)
    full = manager.estimate_llm_input_tokens(
        system_prompt="You are coara. " * 200,
        messages=messages,
        tool_definitions=tools,
    )

    assert full > msg_only
    assert full >= manager.estimate_tokens("You are coara. " * 200) + msg_only


def test_estimate_messages_tokens_caps_vision_image_blocks() -> None:
    manager = ContextWindowManager(model_context_window=128_000)
    huge_b64 = "A" * 400_000
    msg = Message(
        role=MessageRole.USER,
        content=[
            {"type": "text", "text": "请描述图片"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": huge_b64},
            },
        ],
    )
    tokens = manager.estimate_messages_tokens([msg])
    assert tokens < 20_000
    assert tokens > 100


def _tool_loop_messages(rounds: int) -> list[Message]:
    """One user turn with many assistant/tool pairs (typical multi-tool iteration)."""
    from src.core.types import ToolCall

    messages: list[Message] = [Message(role=MessageRole.USER, content="do the task")]
    for idx in range(rounds):
        tc_id = f"tc-{idx}"
        messages.append(
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id=tc_id, name="read", arguments={"path": f"f{idx}.txt"})],
            )
        )
        messages.append(
            Message(
                role=MessageRole.TOOL,
                content="x" * 5000,
                tool_call_id=tc_id,
            )
        )
    return messages


def test_find_compression_split_point_preserves_last_n_during_tool_loop() -> None:
    manager = ContextWindowManager(compression_preserve_last_n=2)
    messages = _tool_loop_messages(20)

    split = manager.find_compression_split_point(messages, preserve_last_n=2)

    assert split == len(messages) - 2
    assert split > 0
    assert manager._is_tool_safe_split(messages, split)


@pytest.mark.asyncio
async def test_maybe_compress_messages_works_during_single_turn_tool_loop(compression_llm_setup):
    provider = compression_llm_setup
    manager = ContextWindowManager(
        model_context_window=128_000,
        compression_preserve_last_n=2,
        compression_threshold=0.01,
    )
    messages = _tool_loop_messages(24)

    compressed, info = await manager.maybe_compress_messages(
        messages,
        max_tokens=128_000,
    )

    assert info["compressed"] is True
    assert info["status"] == "COMPRESSED"
    assert len(compressed) < len(messages)
    assert "<state_snapshot>" in str(compressed[0].content)
    assert provider.calls


def test_find_head_boundary_protects_system_and_env_seed() -> None:
    manager = ContextWindowManager()
    messages = [
        Message(role=MessageRole.SYSTEM, content="system rules"),
        Message(role=MessageRole.USER, content="<系统消息>\n环境上下文：\n- 今天日期：2026年09月02日\n</系统消息>"),
        Message(role=MessageRole.USER, content="hello"),
        Message(role=MessageRole.ASSISTANT, content="hi"),
        Message(role=MessageRole.USER, content="bye"),
    ]
    assert manager._find_head_boundary(messages) == 2
    # 环境种子紧跟普通用户消息后：不在头部连续区（_find_head_boundary 仍为 0），
    # 但 _is_env_seed 独立识别——压缩时会把它从中段/尾部提回头部保护。
    messages = [
        Message(role=MessageRole.USER, content="hello"),
        Message(role=MessageRole.USER, content="<系统消息>环境上下文：\n- 今天日期：2026年09月02日</系统消息>"),
    ]
    assert manager._find_head_boundary(messages) == 0
    assert manager._is_env_seed(messages[1]) is True
    assert manager._is_env_seed(messages[0]) is False


@pytest.mark.asyncio
async def test_compress_preserves_stray_seed_in_middle(compression_llm_setup):
    """环境种子不在头部（注入历史遗留把它追加到中段）时，压缩仍须提回头部。"""
    provider = compression_llm_setup
    manager = ContextWindowManager(model_context_window=128_000, compression_preserve_last_n=2)
    seed = Message(
        role=MessageRole.USER,
        content="<系统消息>\n环境上下文：\n- 今天日期：2026年09月02日\n- 工作目录：D:\\code_ws\\v8\n</系统消息>",
    )
    # 历史开头是上次压缩遗留的摘要（非 protected），种子被追加在中段
    messages = [
        Message(role=MessageRole.USER, content="<state_snapshot>\n旧摘要内容\n</state_snapshot>"),
        Message(role=MessageRole.ASSISTANT, content="Got it. Thanks for the additional context!"),
        *[
            Message(
                role=MessageRole.USER if idx % 2 == 0 else MessageRole.ASSISTANT,
                content=f"history-{idx}-" + "y" * 200,
            )
            for idx in range(8)
        ],
        seed,
        *[
            Message(
                role=MessageRole.USER if idx % 2 == 0 else MessageRole.ASSISTANT,
                content=f"recent-{idx}-" + "y" * 200,
            )
            for idx in range(6)
        ],
    ]

    compressed, info = await manager.maybe_compress_messages(messages, max_tokens=128_000, force=True)

    assert info["compressed"] is True
    # 种子被提到头部（摘要之前），不再被压进摘要
    assert compressed[0] is seed
    assert "<state_snapshot" in manager._get_message_content(compressed[1])
    # 种子不在尾部重复
    assert all(not manager._is_env_seed(m) for m in compressed[2:])
    assert provider.calls


@pytest.mark.asyncio
async def test_compress_keeps_head_system_messages_in_place(compression_llm_setup):
    """压缩只替换中间对话：头部系统级消息（环境种子）必须原位保留。"""
    provider = compression_llm_setup
    manager = ContextWindowManager(model_context_window=128_000, compression_preserve_last_n=2)
    seed = Message(
        role=MessageRole.USER,
        content="<系统消息>\n环境上下文：\n- 今天日期：2026年09月02日\n- 工作目录：D:\\code_ws\\v8\n</系统消息>",
    )
    messages = [
        seed,
        *[
            Message(
                role=MessageRole.USER if idx % 2 == 0 else MessageRole.ASSISTANT,
                content=f"history-{idx}-" + "y" * 200,
            )
            for idx in range(12)
        ],
    ]

    compressed, info = await manager.maybe_compress_messages(messages, max_tokens=128_000, force=True)

    assert info["compressed"] is True
    # 环境种子仍在原位（头部），摘要紧随其后——顺序不被破坏
    assert "环境上下文" in manager._get_message_content(compressed[0])
    assert "<state_snapshot" in manager._get_message_content(compressed[1])
    assert compressed[0] is seed
    # 头部1 + 摘要2 + 尾部保留2
    assert len(compressed) == 1 + 2 + 2
    assert provider.calls
