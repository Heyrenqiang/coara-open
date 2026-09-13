"""Todo store corruption guard: .corrupt quarantine + refuse-to-save."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.todos.store import TodoStore, TodoStoreError
from src.todos.types import TodoItem, TodoStatus


def _write_raw(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_corrupt_file_is_quarantined_and_save_refused(tmp_path: Path) -> None:
    session = "sess-corrupt"
    storage = tmp_path / ".coara" / "todos" / f"{session}.json"
    _write_raw(storage, "{not valid json")

    store = TodoStore(workspace_dir=tmp_path, session_id=session)

    # 损坏文件被改名保留为 .corrupt，不再被空状态覆盖
    assert not storage.exists()
    corrupt = storage.with_name(f"{storage.name}.corrupt")
    assert corrupt.is_file()
    assert corrupt.read_text(encoding="utf-8") == "{not valid json"

    # 护栏解除前（本实例生命周期内）写操作被拒，返回明确错误
    with pytest.raises(TodoStoreError, match="corrupt"):
        store.merge_updates([TodoItem(id="1", content="a", status=TodoStatus.PENDING)])
    with pytest.raises(TodoStoreError, match="corrupt"):
        store.remove_ids(["1"])
    with pytest.raises(TodoStoreError, match="corrupt"):
        store.clear()
    # 原文件仍不存在（未被空态静默重建）
    assert not storage.exists()


def test_valid_store_loads_normally_after_guard_introduced(tmp_path: Path) -> None:
    session = "sess-ok"
    store = TodoStore(workspace_dir=tmp_path, session_id=session)
    store.merge_updates([TodoItem(id="1", content="a", status=TodoStatus.PENDING)])
    storage = tmp_path / ".coara" / "todos" / f"{session}.json"
    assert storage.is_file()

    reloaded = TodoStore(workspace_dir=tmp_path, session_id=session)
    assert [t.id for t in reloaded.get_all()] == ["1"]
    reloaded.merge_updates([TodoItem(id="2", content="b", status=TodoStatus.PENDING)])
    assert {t.id for t in reloaded.get_all()} == {"1", "2"}
