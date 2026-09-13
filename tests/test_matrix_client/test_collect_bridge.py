"""Tests for Matrix collect side-channel → records/user."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.matrix_client.collect_bridge import (
    build_collect_ack,
    is_collect_message,
    matrix_event_source_url,
    parse_collect,
    parse_collect_ack,
    try_handle_collect,
)
from src.matrix_client.ingress_helpers import prefilter_matrix_text_event
from src.records.facade import RecordsFacade
from src.records.store import RecordsStore


def test_parse_collect_text_multiline() -> None:
    body = """[COARA_COLLECT]
action: add
event_id: $abc
msgtype: m.text
title: Hello
body:
line1
line2
[/COARA_COLLECT]"""
    parsed = parse_collect(body)
    assert parsed is not None
    assert parsed["action"] == "add"
    assert parsed["event_id"] == "$abc"
    assert parsed["body"] == "line1\nline2"
    assert is_collect_message(body)


def test_parse_collect_remove() -> None:
    body = """[COARA_COLLECT]
action: remove
event_id: $abc
id: col_1
[/COARA_COLLECT]"""
    parsed = parse_collect(body)
    assert parsed is not None
    assert parsed["action"] == "remove"
    assert parsed["id"] == "col_1"


def test_build_and_parse_ack() -> None:
    ack = build_collect_ack(event_id="$e1", ok=True, action="add", entry_id="col_x")
    parsed = parse_collect_ack(ack)
    assert parsed is not None
    assert parsed["ok"] == "true"
    assert parsed["action"] == "add"
    assert parsed["id"] == "col_x"


@pytest.mark.asyncio
async def test_ack_via_client_when_no_remote_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """prefilter path has no turn — must ACK via Matrix client."""
    store = RecordsStore(tmp_path / "records", agent_enabled=False)
    root = MagicMock()
    root.records_store = store
    monkeypatch.setattr("src.matrix_client.collect_bridge.get_turn_send_text", lambda: None)

    sent: list[tuple[str, str]] = []

    async def fake_room_send(client: object, room_id: str, body: str, **kwargs: object) -> bool:
        sent.append((room_id, body))
        return True

    monkeypatch.setattr(
        "src.matrix_client.send_guard.matrix_room_send_text",
        fake_room_send,
    )

    body = """[COARA_COLLECT]
action: add
event_id: $no_turn
msgtype: m.text
title: hi
body:
hello
[/COARA_COLLECT]"""
    client = MagicMock()
    assert await try_handle_collect(root, "!room:local", body, client=client) is True
    assert sent
    ack = parse_collect_ack(sent[0][1])
    assert ack is not None
    assert ack["ok"] == "true"
    assert ack.get("action") == "add"


@pytest.mark.asyncio
async def test_invalid_collect_still_consumed_with_error_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    async def _send(room_id: str, text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(
        "src.matrix_client.collect_bridge.get_turn_send_text",
        lambda: _send,
    )
    body = "[COARA_COLLECT]\naction: nope\n[/COARA_COLLECT]"
    root = MagicMock()
    assert await try_handle_collect(root, "!r", body, client=None) is True
    assert sent
    ack = parse_collect_ack(sent[0])
    assert ack is not None
    assert ack["ok"] == "false"


@pytest.mark.asyncio
async def test_try_handle_collect_text_writes_md(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = RecordsStore(tmp_path / "records", agent_enabled=False)
    root = MagicMock()
    root.records_store = store

    sent: list[tuple[str, str]] = []

    async def _send(room_id: str, text: str) -> bool:
        sent.append((room_id, text))
        return True

    monkeypatch.setattr(
        "src.matrix_client.collect_bridge.get_turn_send_text",
        lambda: _send,
    )

    body = """[COARA_COLLECT]
action: add
event_id: $msg1
msgtype: m.text
title: 要点
body:
这是正文第一行
第二行
[/COARA_COLLECT]"""
    assert await try_handle_collect(root, "!room:local", body, client=None) is True
    assert sent
    ack = parse_collect_ack(sent[0][1])
    assert ack is not None
    assert ack["ok"] == "true"
    assert ack.get("action") == "add"
    entry_id = ack["id"]
    entry = await store.user.read(entry_id)
    assert entry is not None
    assert entry.source_url == matrix_event_source_url("$msg1")
    assert "这是正文第一行" in entry.content
    md_files = list((tmp_path / "records" / "user" / "entries").rglob("*.md"))
    assert md_files


@pytest.mark.asyncio
async def test_try_handle_collect_file_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = RecordsStore(tmp_path / "records", agent_enabled=False)
    root = MagicMock()
    root.records_store = store

    async def _send(room_id: str, text: str) -> bool:
        return True

    monkeypatch.setattr(
        "src.matrix_client.collect_bridge.get_turn_send_text",
        lambda: _send,
    )

    async def fake_download(client: object, mxc: str) -> bytes:
        assert mxc.startswith("mxc://")
        return b"%PDF-fake"

    monkeypatch.setattr(
        "src.matrix_client.remote_vision.download_mxc_bytes",
        fake_download,
    )

    body = """[COARA_COLLECT]
action: add
event_id: $file1
msgtype: m.file
title: report.pdf
mxc: mxc://coara.local/abc
filename: report.pdf
mime: application/pdf
[/COARA_COLLECT]"""
    client = MagicMock()
    assert await try_handle_collect(root, "!room:local", body, client=client) is True
    existing = await store.user.find_by_url(matrix_event_source_url("$file1"))
    assert existing is not None
    assert existing.source_type == "file"
    files = list((tmp_path / "records" / "user" / "files").rglob("*"))
    assert any(p.is_file() and p.read_bytes() == b"%PDF-fake" for p in files)


@pytest.mark.asyncio
async def test_try_handle_collect_remove_deletes_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = RecordsStore(tmp_path / "records", agent_enabled=False)
    facade = RecordsFacade(store)
    added = await facade.add_user_file(
        title="doc.pdf",
        summary="收藏文件：doc.pdf",
        filename="doc.pdf",
        file_bytes=b"%PDF",
        source_url=matrix_event_source_url("$file_rm"),
    )
    assert added.ok
    entry_id = added.metadata["id"]
    files_dir = tmp_path / "records" / "user" / "files" / entry_id
    assert files_dir.is_dir()

    root = MagicMock()
    root.records_store = store
    monkeypatch.setattr("src.matrix_client.collect_bridge.get_turn_send_text", lambda: None)

    body = f"""[COARA_COLLECT]
action: remove
event_id: $file_rm
id: {entry_id}
[/COARA_COLLECT]"""
    assert await try_handle_collect(root, "!room:local", body, client=None) is True
    assert await store.user.read(entry_id) is None
    assert not files_dir.exists()


def test_prefilter_swallows_collect(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduled: list[dict] = []

    def fake_schedule(
        root: object,
        room_id: str,
        body: str,
        *,
        client: object | None = None,
        send_text: object | None = None,
    ) -> None:
        scheduled.append({"room_id": room_id, "body": body, "client": client, "send_text": send_text})

    monkeypatch.setattr(
        "src.matrix_client.collect_bridge.schedule_handle_collect",
        fake_schedule,
    )
    monkeypatch.setattr(
        "src.matrix_client.vault_bridge.try_resolve_vault_reply",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "src.coara.updates_matrix_sync.is_updates_control_message",
        lambda body: False,
    )
    monkeypatch.setattr(
        "src.coara.mobile_sync.try_handle_mobile_sync_query",
        lambda *a, **k: False,
    )

    body = "[COARA_COLLECT]\naction: add\nevent_id: $x\n[/COARA_COLLECT]"
    root = MagicMock()
    client = MagicMock()
    send = MagicMock()
    assert prefilter_matrix_text_event(root=root, room_id="!r", body=body, client=client, send_text=send) is True
    assert scheduled
    assert scheduled[0]["client"] is client
    assert scheduled[0]["send_text"] is send
