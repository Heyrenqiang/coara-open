"""Tests for tool output spill store (persist, threshold, batch, retention)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.agent.executor import ToolExecutor
from src.coara.base import CoaraBase
from src.coara.tool_manager import ToolManager
from src.core.read_format import DEFAULT_REF_READ_LIMIT_LINES, resolve_ref_read_limit
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.core.types import CoaraPersona, ToolCall
from src.runtime.spill_policy import SpillKeep
from src.runtime.spill_read import read_spill_api_payload, read_spill_lines
from src.runtime.tool_output_store import (
    ToolOutputStore,
    apply_batch_spill_budget,
    format_spilled_tool_content,
    maybe_spill_tool_result,
    model_facing_byte_len,
    prune_stale_tool_outputs,
)
from src.tools.builtin.file_io.read import ReadTool
from tests.helpers import FakeProvider
from tests.test_runtime.conftest import make_tool_output_settings


def test_format_spilled_tool_content_includes_ref() -> None:
    text = format_spilled_tool_content(
        ref="abc12345",
        content="x" * 1000,
        store_path=Path("/tmp/out.json"),
        preview="preview snippet",
        keep=SpillKeep.TAIL,
    )
    assert 'ref="abc12345"' in text
    assert "read(ref=" in text


def test_maybe_spill_skips_web_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "src.runtime.tool_output_store.get_tool_output_store_config",
        lambda: make_tool_output_settings(spill_threshold_bytes=10, preview_head_chars=100, preview_tail_chars=100),
    )
    big = "x" * 1000
    result = maybe_spill_tool_result(
        workspace_dir=tmp_path,
        session_id="sess-web",
        tool_name="web_fetch",
        tool_call_id="call-wf",
        arguments={"url": "https://example.com"},
        result=ToolResult.success(content=big),
    )
    assert result.metadata.get("output_spilled") is not True
    assert result.content == big


def test_maybe_spill_tool_result_replaces_large_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "src.runtime.tool_output_store.get_tool_output_store_config",
        lambda: make_tool_output_settings(spill_threshold_bytes=10, preview_head_chars=100, preview_tail_chars=100),
    )
    big = "x" * 1000
    result = maybe_spill_tool_result(
        workspace_dir=tmp_path,
        session_id="sess-2",
        tool_name="workflow",
        tool_call_id="call-2",
        arguments={"action": "run"},
        result=ToolResult.success(content=big),
    )
    assert result.metadata.get("output_spilled") is True
    assert "<tool_output" in str(result.content)
    assert len(str(result.content)) < len(big)


def test_maybe_spill_skips_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "src.runtime.tool_output_store.get_tool_output_store_config",
        lambda: make_tool_output_settings(spill_threshold_bytes=10, preview_head_chars=100, preview_tail_chars=100),
    )
    big = "信丰" * 5000
    result = maybe_spill_tool_result(
        workspace_dir=tmp_path,
        session_id="sess-read",
        tool_name="read",
        tool_call_id="call-read",
        arguments={"path": "report.md"},
        result=ToolResult.success(content=big),
    )
    assert result.metadata.get("output_spilled") is not True
    assert result.content == big


def test_list_and_find_tool_output_index(tmp_path: Path) -> None:
    store = ToolOutputStore(workspace_dir=tmp_path, session_id="sess-list")
    record = store.write(
        tool_name="grep",
        tool_call_id="call-list",
        content="line-one\nline-two",
        arguments={"pattern": "line"},
    )
    rows = store.list_index()
    assert len(rows) == 1
    assert rows[0]["ref"] == record.ref

    from src.runtime.tool_output_store import find_tool_output_record, list_tool_output_index

    found = find_tool_output_record(workspace_dir=tmp_path, ref=record.ref)
    assert found.session_id == "sess-list"
    assert "line-one" in found.content

    all_rows = list_tool_output_index(workspace_dir=tmp_path, limit=10)
    assert len(all_rows) == 1
    assert all_rows[0]["tool_name"] == "grep"


def test_maybe_spill_preserves_display(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.coara.tool_output.types import DiffDisplayBlock

    monkeypatch.setattr(
        "src.runtime.tool_output_store.get_tool_output_store_config",
        lambda: make_tool_output_settings(spill_threshold_bytes=10, preview_head_chars=100, preview_tail_chars=100),
    )
    display = [
        DiffDisplayBlock(path="a.txt", old_text="a", new_text="b"),
    ]
    big = "x" * 1000
    result = maybe_spill_tool_result(
        workspace_dir=tmp_path,
        session_id="sess-display",
        tool_name="workflow",
        tool_call_id="call-d",
        arguments={},
        result=ToolResult.success(content=big, display=display),
    )
    assert result.metadata.get("output_spilled") is True
    assert len(result.display) == 1


def test_shell_spills_above_30k_not_below(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "src.runtime.tool_output_store.get_tool_output_store_config",
        lambda: make_tool_output_settings(spill_threshold_bytes=25_000),
    )
    under = "y" * 29_000
    kept = maybe_spill_tool_result(
        workspace_dir=tmp_path,
        session_id="sess-shell",
        tool_name="shell",
        tool_call_id="call-sh1",
        arguments={"command": "pytest"},
        result=ToolResult.success(content=under),
    )
    assert kept.metadata.get("output_spilled") is not True

    over = "z" * 31_000
    spilled = maybe_spill_tool_result(
        workspace_dir=tmp_path,
        session_id="sess-shell",
        tool_name="shell",
        tool_call_id="call-sh2",
        arguments={"command": "pytest"},
        result=ToolResult.success(content=over),
    )
    assert spilled.metadata.get("output_spilled") is True
    assert spilled.metadata.get("spill_keep") == "tail"


def test_batch_budget_offloads_largest_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "src.runtime.tool_output_store.get_tool_output_store_config",
        lambda: make_tool_output_settings(
            batch_budget_bytes=5_000,
            spill_threshold_bytes=100_000,
            preview_head_chars=100,
            preview_tail_chars=100,
        ),
    )
    small = ToolResult.success(content="a" * 2_000)
    large = ToolResult.success(content="b" * 4_000)
    updated = apply_batch_spill_budget(
        workspace_dir=tmp_path,
        session_id="sess-batch",
        items=[
            ("grep", "c1", small, None),
            ("grep", "c2", large, None),
        ],
    )
    assert updated[0].metadata.get("output_spilled") is not True
    assert updated[1].metadata.get("output_spilled") is True
    assert sum(model_facing_byte_len(r) for r in updated) <= 5_000


@pytest.mark.asyncio
async def test_executor_batch_spill_syncs_tool_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_coara_home: Path
) -> None:
    from src.tools.cache import tool_cache, tool_execution_cache_scope

    class BigSearchTool(BaseTool):
        name = "big_search"
        description = "Return a large fixed payload."
        display_name = "BigSearch"
        category = "search"
        kind = ToolKind.SEARCH
        parameters_schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }

        def create_invocation(self, params: dict[str, str]) -> ToolInvocation:
            return _BigSearchInvocation(params)

    class _BigSearchInvocation(ToolInvocation):
        def __init__(self, params: dict[str, str]) -> None:
            super().__init__(params)
            self.q = params["q"]

        def get_description(self) -> str:
            return f"Search: {self.q}"

        async def execute(self, signal=None) -> ToolResult:
            return ToolResult.success("x" * 4_000)

    monkeypatch.setattr(
        "src.runtime.tool_output_store.get_tool_output_store_config",
        lambda: make_tool_output_settings(
            batch_budget_bytes=3_000,
            spill_threshold_bytes=100_000,
            preview_head_chars=100,
            preview_tail_chars=100,
        ),
    )
    tool_cache.invalidate("big_search")

    coara = CoaraBase(
        name="root",
        persona=CoaraPersona(name="root", role="root"),
        provider=FakeProvider([]),
        workspace_dir=tmp_path,
    )
    coara.session_id = "sess-exec-cache"
    manager = ToolManager()
    manager.register_tool(BigSearchTool())
    coara._tool_manager = manager
    coara.tool_executor = ToolExecutor()

    args = {"q": "cache-sync"}
    executions = await coara.tool_executor.execute(
        coara,
        [ToolCall(id="call-big", name="big_search", arguments=args)],
        is_owner=True,
    )
    assert executions[0].result.metadata.get("output_spilled") is True
    cached = tool_cache.get("big_search", args, scope=tool_execution_cache_scope(coara))
    assert cached is not None
    assert cached.metadata.get("output_spilled") is True
    assert len(str(cached.content)) < 4_000


def test_prune_stale_tool_outputs(tmp_path: Path) -> None:
    store = ToolOutputStore(workspace_dir=tmp_path, session_id="sess-prune")
    record = store.write(tool_name="shell", tool_call_id="call-old", content="old output")
    payload_path = store.session_dir / f"{record.ref}.json"
    data = json.loads(payload_path.read_text(encoding="utf-8"))
    data["created_at"] = (datetime.now() - timedelta(days=60)).isoformat()
    payload_path.write_text(json.dumps(data), encoding="utf-8")

    removed = prune_stale_tool_outputs(workspace_dir=tmp_path, retention_days=30)
    assert removed == 1
    assert not payload_path.is_file()
    assert store.list_index() == []


def test_resolve_ref_read_limit_auto_caps() -> None:
    assert resolve_ref_read_limit(limit=None, total_lines=100) is None
    assert resolve_ref_read_limit(limit=None, total_lines=600) == DEFAULT_REF_READ_LIMIT_LINES
    assert resolve_ref_read_limit(limit=50, total_lines=600) == 50


def test_read_spill_lines_respects_explicit_limit() -> None:
    content = "\n".join(f"L{i}" for i in range(10))
    chunk, total, effective, is_partial = read_spill_lines(content, offset=1, limit=3)
    assert total == 10
    assert effective == 3
    assert is_partial is True
    assert chunk.startswith("L0\nL1\nL2")


def test_read_spill_lines_full_body_not_partial() -> None:
    content = "a\nb\nc\n"
    _chunk, total, _effective, is_partial = read_spill_lines(content, offset=1, limit=10)
    assert total == 3
    assert is_partial is False


def test_read_spill_api_payload_pagination() -> None:
    content = "\n".join(f"line-{i}" for i in range(5))
    page = read_spill_api_payload(content, line_offset=1, line_limit=2)
    assert page["offset"] == 1
    assert page["lines_shown"] == 2
    assert page["total_lines"] == 5
    assert page["truncated"] is True
    assert page["next_offset"] == 3


@pytest.mark.asyncio
async def test_read_tool_resolves_output_ref(tmp_path, isolated_coara_home) -> None:
    coara = CoaraBase(
        name="root",
        persona=CoaraPersona(name="root", role="root"),
        provider=FakeProvider([]),
        workspace_dir=tmp_path,
    )
    coara.session_id = "sess-read-ref"
    store = ToolOutputStore(
        workspace_dir=tmp_path,
        session_id=coara.session_id,
        coara_home=isolated_coara_home,
    )
    record = store.write(
        tool_name="grep",
        tool_call_id="call-1",
        content="alpha\nbeta\ngamma",
    )

    tool = ReadTool(read_state_store={}, workspace_root=tmp_path, parent_coara=coara)
    result = await tool.create_invocation({"ref": record.ref, "offset": 1, "limit": 1}).execute()

    assert result.is_error is False
    assert result.content == "1|alpha"
    assert result.metadata.get("ref") == record.ref
    assert result.metadata.get("from_spill_ref") is True


@pytest.mark.asyncio
async def test_read_ref_auto_limits_large_spill(tmp_path, isolated_coara_home) -> None:
    coara = CoaraBase(
        name="root",
        persona=CoaraPersona(name="root", role="root"),
        provider=FakeProvider([]),
        workspace_dir=tmp_path,
    )
    coara.session_id = "sess-ref-cap"
    store = ToolOutputStore(
        workspace_dir=tmp_path,
        session_id=coara.session_id,
        coara_home=isolated_coara_home,
    )
    body = "\n".join(f"line-{i}" for i in range(600))
    record = store.write(tool_name="grep", tool_call_id="call-cap", content=body)

    tool = ReadTool(read_state_store={}, workspace_root=tmp_path, parent_coara=coara)
    result = await tool.create_invocation({"ref": record.ref}).execute()

    assert result.is_error is False
    assert result.metadata.get("auto_limited") is True
    assert result.metadata.get("is_partial") is True
    assert result.metadata.get("total_lines") == 600
    assert "仅显示前 500/600 行" in str(result.content)


def test_store_write_and_load_roundtrip(tmp_path) -> None:
    store = ToolOutputStore(workspace_dir=tmp_path, session_id="sess-1")
    record = store.write(
        tool_name="grep",
        tool_call_id="call-1",
        content="line1\nline2\nline3",
        arguments={"pattern": "TODO"},
    )
    loaded = store.load(record.ref)
    chunk, total_lines, effective_limit, is_partial = read_spill_lines(loaded.content, offset=1, limit=2)
    assert chunk.startswith("line1")
    assert loaded.tool_name == "grep"
    assert total_lines == 3
    assert effective_limit == 2
    assert is_partial is True
