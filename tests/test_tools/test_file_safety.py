"""File tool path checks and edit/write/delete behavior."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.tools.builtin.file_io.delete import DeleteTool
from src.tools.builtin.file_io.edit import EditTool
from src.tools.builtin.file_io.file_support import _has_windows_alternate_data_stream
from src.tools.builtin.file_io.read import ReadTool
from src.tools.builtin.file_io.write import WriteTool


@pytest.fixture
def read_states() -> dict:
    return {}


@pytest.fixture
def read_tool(read_states: dict, tmp_path: Path) -> ReadTool:
    return ReadTool(read_state_store=read_states, workspace_root=tmp_path)


@pytest.fixture
def write_tool(read_states: dict, tmp_path: Path) -> WriteTool:
    return WriteTool(read_state_store=read_states, workspace_root=tmp_path)


@pytest.fixture
def edit_tool(read_states: dict, tmp_path: Path) -> EditTool:
    return EditTool(read_state_store=read_states, workspace_root=tmp_path)


@pytest.fixture
def delete_tool(read_states: dict, tmp_path: Path) -> DeleteTool:
    return DeleteTool(read_state_store=read_states, workspace_root=tmp_path)


@pytest.mark.asyncio
async def test_edit_without_prior_read(edit_tool: EditTool, tmp_path: Path) -> None:
    test_file = tmp_path / "test.txt"
    test_file.write_text("original content")

    result = await edit_tool.create_invocation(
        {"path": str(test_file), "old_string": "original", "new_string": "modified"}
    ).execute()

    assert not result.is_error
    assert test_file.read_text() == "modified content"


@pytest.mark.asyncio
async def test_edit_uses_live_disk_after_external_change(
    edit_tool: EditTool, read_tool: ReadTool, read_states: dict, tmp_path: Path
) -> None:
    test_file = tmp_path / "changed.txt"
    test_file.write_text("original content")

    await read_tool.create_invocation({"path": str(test_file)}).execute()
    time.sleep(0.05)
    test_file.write_text("external content")
    read_states[str(test_file.resolve())].timestamp = 0

    result = await edit_tool.create_invocation(
        {"path": str(test_file), "old_string": "external", "new_string": "modified"}
    ).execute()

    assert not result.is_error
    assert test_file.read_text() == "modified content"


@pytest.mark.asyncio
async def test_edit_after_partial_read(edit_tool: EditTool, read_tool: ReadTool, tmp_path: Path) -> None:
    test_file = tmp_path / "partial.txt"
    test_file.write_text("alpha beta gamma")

    await read_tool.create_invocation({"path": str(test_file), "offset": 2, "limit": 1}).execute()
    result = await edit_tool.create_invocation(
        {"path": str(test_file), "old_string": "beta", "new_string": "coara"}
    ).execute()

    assert not result.is_error
    assert test_file.read_text() == "alpha coara gamma"


@pytest.mark.asyncio
async def test_edit_requires_path_param(edit_tool: EditTool, tmp_path: Path) -> None:
    test_file = tmp_path / "sample.txt"
    test_file.write_text("alpha beta")

    with pytest.raises(ValueError, match="Missing required parameter: path"):
        edit_tool.create_invocation({"file_path": str(test_file), "old_string": "alpha", "new_string": "omega"})


@pytest.mark.asyncio
async def test_write_existing_file_without_prior_read(write_tool: WriteTool, tmp_path: Path) -> None:
    test_file = tmp_path / "existing.txt"
    test_file.write_text("original content")

    result = await write_tool.create_invocation({"path": str(test_file), "contents": "replaced content"}).execute()

    assert not result.is_error
    assert test_file.read_text() == "replaced content"


@pytest.mark.asyncio
async def test_edit_not_found_includes_recovery_hint(edit_tool: EditTool, read_tool: ReadTool, tmp_path: Path) -> None:
    test_file = tmp_path / "missing.txt"
    test_file.write_text("alpha beta gamma")

    await read_tool.create_invocation({"path": str(test_file)}).execute()
    result = await edit_tool.create_invocation(
        {"path": str(test_file), "old_string": "delta", "new_string": "epsilon"}
    ).execute()

    assert result.is_error
    assert "未找到要替换的文本" in result.content
    assert "附近的原文" in result.content


@pytest.mark.asyncio
async def test_read_rejects_unc_path(read_tool: ReadTool) -> None:
    result = await read_tool.create_invocation({"path": r"\\server\share\file.txt"}).execute()

    assert result.is_error
    assert "拒绝 UNC" in result.content


@pytest.mark.asyncio
async def test_write_rejects_alternate_data_stream_path(write_tool: WriteTool, tmp_path: Path) -> None:
    ads_path = str(tmp_path / "note.txt") + ":secret"

    result = await write_tool.create_invocation({"path": ads_path, "contents": "hidden"}).execute()

    assert result.is_error
    assert "备用数据流" in result.content


@pytest.mark.parametrize(
    "path,expected",
    [
        (r"D:\code_ws\gora\file.txt", False),
        ("D:/code_ws/gora/file.txt", False),
        (r"D:\file.txt:secret", True),
        ("D:/file.txt:secret", True),
        ("file.txt:secret", True),
    ],
)
def test_windows_alternate_data_stream_detection(path: str, expected: bool) -> None:
    assert _has_windows_alternate_data_stream(path) is expected


@pytest.mark.asyncio
async def test_delete_removes_file(delete_tool: DeleteTool, tmp_path: Path) -> None:
    test_file = tmp_path / "obsolete.txt"
    test_file.write_text("remove me")

    result = await delete_tool.create_invocation({"path": str(test_file)}).execute()

    assert not result.is_error
    assert not test_file.exists()
    # 工作区内删除改为入回收站（#90），文件可从 .coara/trash/ 恢复
    assert "回收站" in result.content
    trash_entries = list((tmp_path / ".coara" / "trash").glob("obsolete.txt.*"))
    assert len(trash_entries) == 1
    assert trash_entries[0].read_text() == "remove me"


@pytest.mark.asyncio
async def test_read_rejects_relative_path(read_tool: ReadTool, tmp_path: Path) -> None:
    test_file = tmp_path / "test.txt"
    test_file.write_text("content")

    result = await read_tool.create_invocation({"path": "test.txt"}).execute()

    assert result.is_error
    assert "绝对路径" in result.content


@pytest.mark.asyncio
async def test_edit_rejects_relative_path(edit_tool: EditTool, tmp_path: Path) -> None:
    test_file = tmp_path / "test.txt"
    test_file.write_text("original")

    result = await edit_tool.create_invocation(
        {"path": "test.txt", "old_string": "original", "new_string": "modified"}
    ).execute()

    assert result.is_error
    assert test_file.read_text() == "original"
    assert "绝对路径" in result.content


@pytest.mark.asyncio
async def test_edit_rejects_file_over_size_limit(
    edit_tool: EditTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 阈值对齐 read_text_file 默认上限；测试用 monkeypatch 缩小阈值避免写大文件
    monkeypatch.setattr("src.tools.builtin.file_io.edit.DEFAULT_MAX_READ_BYTES", 100)
    big_file = tmp_path / "big.txt"
    big_file.write_text("x" * 200)

    result = await edit_tool.create_invocation(
        {"path": str(big_file), "old_string": "xxx", "new_string": "yyy"}
    ).execute()

    assert result.is_error
    assert "过大" in result.content
    assert "shell" in result.content
    assert big_file.read_text() == "x" * 200


@pytest.mark.asyncio
async def test_edit_accepts_file_within_size_limit(
    edit_tool: EditTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.tools.builtin.file_io.edit.DEFAULT_MAX_READ_BYTES", 100)
    small_file = tmp_path / "small.txt"
    small_file.write_text("foo = 1\n" + "x" * 90)

    result = await edit_tool.create_invocation(
        {"path": str(small_file), "old_string": "foo = 1", "new_string": "foo = 42"}
    ).execute()

    assert not result.is_error
    assert small_file.read_text().startswith("foo = 42")


@pytest.mark.asyncio
async def test_write_rejects_file_over_size_limit(
    write_tool: WriteTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 覆盖已存在文件需整体读入原文件（保留编码/换行、拦截二进制），与 edit 同档收口；
    # monkeypatch 缩小阈值避免写大文件
    monkeypatch.setattr("src.tools.builtin.file_io.write.DEFAULT_MAX_READ_BYTES", 100)
    big_file = tmp_path / "big.txt"
    big_file.write_text("x" * 200)

    result = await write_tool.create_invocation({"path": str(big_file), "contents": "new"}).execute()

    assert result.is_error
    assert "过大" in result.content
    assert "shell" in result.content
    assert big_file.read_text() == "x" * 200


@pytest.mark.asyncio
async def test_write_accepts_file_within_size_limit(
    write_tool: WriteTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.tools.builtin.file_io.write.DEFAULT_MAX_READ_BYTES", 100)
    small_file = tmp_path / "small.txt"
    small_file.write_text("old")

    result = await write_tool.create_invocation({"path": str(small_file), "contents": "new"}).execute()

    assert not result.is_error
    assert small_file.read_text() == "new"


@pytest.mark.asyncio
async def test_write_new_file_not_size_guarded(
    write_tool: WriteTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新建文件无需读入原文件 不受上限约束"""
    monkeypatch.setattr("src.tools.builtin.file_io.write.DEFAULT_MAX_READ_BYTES", 100)
    new_file = tmp_path / "new.txt"

    result = await write_tool.create_invocation({"path": str(new_file), "contents": "x" * 200}).execute()

    assert not result.is_error
    assert new_file.read_text() == "x" * 200


@pytest.mark.asyncio
async def test_write_rejects_relative_path(write_tool: WriteTool, tmp_path: Path) -> None:
    result = await write_tool.create_invocation({"path": "new.txt", "contents": "hello"}).execute()

    assert result.is_error
    assert not (tmp_path / "new.txt").exists()
    assert "绝对路径" in result.content


@pytest.mark.asyncio
async def test_delete_rejects_relative_path(delete_tool: DeleteTool, tmp_path: Path) -> None:
    test_file = tmp_path / "obsolete.txt"
    test_file.write_text("remove me")

    result = await delete_tool.create_invocation({"path": "obsolete.txt"}).execute()

    assert result.is_error
    assert test_file.exists()
    assert "绝对路径" in result.content


@pytest.mark.asyncio
async def test_delete_missing_file_fails(delete_tool: DeleteTool, tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"

    result = await delete_tool.create_invocation({"path": str(missing)}).execute()

    assert result.is_error
    assert "文件不存在" in result.content


@pytest.mark.asyncio
async def test_delete_directory_fails(delete_tool: DeleteTool, tmp_path: Path) -> None:
    folder = tmp_path / "subdir"
    folder.mkdir()

    result = await delete_tool.create_invocation({"path": str(folder)}).execute()

    assert result.is_error
    assert "目录" in result.content


@pytest.mark.asyncio
async def test_delete_clears_read_state(
    delete_tool: DeleteTool, read_tool: ReadTool, read_states: dict, tmp_path: Path
) -> None:
    test_file = tmp_path / "tracked.txt"
    test_file.write_text("tracked")

    await read_tool.create_invocation({"path": str(test_file)}).execute()
    assert str(test_file.resolve()) in read_states or str(test_file) in read_states

    result = await delete_tool.create_invocation({"path": str(test_file)}).execute()

    assert not result.is_error
    assert str(test_file.resolve()) not in read_states
    assert str(test_file) not in read_states


@pytest.mark.asyncio
async def test_read_image_returns_multimodal_blocks(read_tool: ReadTool, tmp_path: Path) -> None:
    # Minimal valid 1x1 PNG
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    image_path = tmp_path / "icon.png"
    image_path.write_bytes(png_bytes)

    result = await read_tool.create_invocation({"path": str(image_path)}).execute()

    assert not result.is_error
    assert isinstance(result.content, list)
    assert result.content[0]["type"] == "text"
    assert str(image_path) in result.content[0]["text"]
    assert result.content[1]["type"] == "image"
    assert result.content[1]["source"]["type"] == "base64"
    assert result.metadata.get("format") == "image"


@pytest.mark.asyncio
async def test_read_returns_raw_content(read_tool: ReadTool, tmp_path: Path) -> None:
    test_file = tmp_path / "numbered.txt"
    test_file.write_text("alpha\nbeta\ngamma\n")

    result = await read_tool.create_invocation({"path": str(test_file)}).execute()

    assert not result.is_error
    assert result.content == "1|alpha\n2|beta\n3|gamma"


@pytest.mark.asyncio
async def test_read_offset(read_tool: ReadTool, tmp_path: Path) -> None:
    test_file = tmp_path / "lines.txt"
    test_file.write_text("line1\nline2\nline3\n")

    result = await read_tool.create_invocation({"path": str(test_file), "offset": 2}).execute()

    assert not result.is_error
    assert result.content == "2|line2\n3|line3"
    assert "line1" not in result.content


@pytest.mark.asyncio
async def test_read_limit_lines(read_tool: ReadTool, tmp_path: Path) -> None:
    test_file = tmp_path / "limit.txt"
    test_file.write_text("a\nb\nc\nd\n")

    result = await read_tool.create_invocation({"path": str(test_file), "offset": 2, "limit": 2}).execute()

    assert not result.is_error
    assert result.content == "2|b\n3|c"
    assert "d" not in result.content


@pytest.mark.asyncio
async def test_read_negative_offset(read_tool: ReadTool, tmp_path: Path) -> None:
    test_file = tmp_path / "neg.txt"
    test_file.write_text("x\ny\nz\n")

    result = await read_tool.create_invocation({"path": str(test_file), "offset": -1}).execute()

    assert not result.is_error
    assert result.content == "3|z"
    assert "x" not in result.content
