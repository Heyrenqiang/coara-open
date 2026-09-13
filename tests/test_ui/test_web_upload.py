"""Tests for /api/upload size enforcement."""

from __future__ import annotations

import pytest

from src.ui.web_server import WebServer


class _FakePart:
    def __init__(self, filename: str, chunks: list[bytes]):
        self.name = "file"
        self.filename = filename
        self._chunks = list(chunks)

    async def read_chunk(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _FakeReader:
    def __init__(self, parts: list[_FakePart]):
        self._parts = parts

    async def __aiter__(self):
        for part in self._parts:
            yield part


class _FakeRequest:
    def __init__(self, parts: list[_FakePart]):
        self._parts = parts

    async def multipart(self):
        return _FakeReader(self._parts)


def _make_server(tmp_path, monkeypatch) -> WebServer:
    server = WebServer.__new__(WebServer)
    server.workspace_dir = tmp_path
    server.coara_home = None
    monkeypatch.setattr(server, "_check_token", lambda _request: None)
    return server


@pytest.mark.asyncio
async def test_upload_writes_small_file(tmp_path, monkeypatch) -> None:
    server = _make_server(tmp_path, monkeypatch)
    request = _FakeRequest([_FakePart("hello.txt", [b"hello ", b"world"])])

    response = await server._handle_upload(request)

    assert response.status == 200
    assert (tmp_path / ".coara" / "uploads" / "hello.txt").read_bytes() == b"hello world"


@pytest.mark.asyncio
async def test_upload_rejects_oversized_file_and_cleans_partial(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.ui.handlers.files.MAX_UPLOAD_FILE_BYTES", 10)
    server = _make_server(tmp_path, monkeypatch)
    request = _FakeRequest([_FakePart("big.bin", [b"x" * 8, b"y" * 8, b"z" * 8])])

    response = await server._handle_upload(request)

    assert response.status == 413
    assert not (tmp_path / ".coara" / "uploads" / "big.bin").exists()


@pytest.mark.asyncio
async def test_upload_sanitizes_windows_reserved_and_trailing_dots(tmp_path, monkeypatch) -> None:
    server = _make_server(tmp_path, monkeypatch)
    request = _FakeRequest(
        [
            _FakePart("CON.txt", [b"a"]),
            _FakePart("report.pdf. ", [b"b"]),
            _FakePart("aux ", [b"c"]),
            _FakePart("../../evil.txt", [b"d"]),
        ]
    )

    response = await server._handle_upload(request)

    assert response.status == 200
    uploads = tmp_path / ".coara" / "uploads"
    names = {p.name for p in uploads.iterdir()}
    # Reserved names prefixed so they're creatable on Win32
    assert "_CON.txt" in names
    # Trailing dots/spaces stripped
    assert "report.pdf" in names
    assert "_aux" in names
    # Directory traversal contained
    assert "evil.txt" in names
    assert not (tmp_path / "evil.txt").exists()
