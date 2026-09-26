"""grep 路径自愈回归：路径不存在时按末级名定位候选重试（F821 缺 import 曾使该路径 NameError）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools.builtin.file_io.grep import GrepTool


@pytest.mark.asyncio
async def test_grep_relocates_missing_path_by_basename(tmp_path: Path) -> None:
    real = tmp_path / "uniq_target_dir"
    real.mkdir()
    (real / "a.py").write_text("needle_here = 1\n", encoding="utf-8")
    tool = GrepTool(workspace_root=tmp_path)

    result = await tool.create_invocation(
        {"pattern": "needle_here", "path": str(tmp_path / "no_such" / "uniq_target_dir")}
    ).execute()
    assert not result.is_error
    assert "a.py" in result.content
