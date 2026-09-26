"""Tests for the /qrcode command (terminal mobile-pairing QR)."""

from __future__ import annotations

import io

import pytest

from src.coara.commands import execute_command
from src.coara.commands import qrcode as qrcode_cmd


def _qr_png(payload: str = "http://127.0.0.1:8008") -> bytes:
    """Generate a real QR PNG (python ``qrcode`` lib) for render testing."""
    import qrcode

    qr = qrcode.QRCode(border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_render_qr_png_produces_module_grid() -> None:
    png = _qr_png()
    art = qrcode_cmd.render_qr_png(png)
    lines = art.splitlines()
    assert lines, "should render a QR"
    # 每行一个字符 = 一个模块，行宽 = QR 模块数
    assert any("\u2588" in line for line in lines)


def test_render_qr_png_empty_on_garbage() -> None:
    assert qrcode_cmd.render_qr_png(b"not an image") == ""


@pytest.mark.asyncio
async def test_qrcode_renders_when_tunnel_ready(monkeypatch) -> None:
    png = _qr_png()
    monkeypatch.setattr(qrcode_cmd, "_gomatrix_port", lambda: 8008)
    monkeypatch.setattr(
        qrcode_cmd,
        "_dashboard_status",
        lambda: {"tunnel_ready": True, "tunnel_enabled": True, "tunnel_url": "https://x"},
    )
    monkeypatch.setattr(qrcode_cmd, "_fetch_bytes", lambda url: png if "qr.png" in url else b"{}")

    result = await execute_command(None, "/qrcode")
    assert result is not None
    assert not result.data.get("error")
    assert "隧道地址" not in result.output
    assert any("\u2588" in line for line in result.output.splitlines())


@pytest.mark.asyncio
async def test_qrcode_error_when_gomatrix_down(monkeypatch) -> None:
    monkeypatch.setattr(qrcode_cmd, "_gomatrix_port", lambda: 8008)
    monkeypatch.setattr(qrcode_cmd, "_dashboard_status", lambda: None)

    result = await execute_command(None, "/qrcode")
    assert result is not None
    assert result.data.get("error") is True


@pytest.mark.asyncio
async def test_qrcode_error_when_tunnel_not_ready(monkeypatch) -> None:
    monkeypatch.setattr(qrcode_cmd, "_gomatrix_port", lambda: 8008)
    monkeypatch.setattr(
        qrcode_cmd,
        "_dashboard_status",
        lambda: {"tunnel_ready": False, "tunnel_enabled": True, "tunnel_error": "starting"},
    )

    result = await execute_command(None, "/qrcode")
    assert result is not None
    assert result.data.get("error") is True
    assert "starting" in result.output


@pytest.mark.asyncio
async def test_qrcode_error_when_pillow_missing(monkeypatch) -> None:
    monkeypatch.setattr(qrcode_cmd, "_pil_image_module", lambda: None)
    monkeypatch.setattr(qrcode_cmd, "_gomatrix_port", lambda: 8008)
    monkeypatch.setattr(
        qrcode_cmd,
        "_dashboard_status",
        lambda: {"tunnel_ready": True, "tunnel_enabled": True, "tunnel_url": "https://x"},
    )
    monkeypatch.setattr(qrcode_cmd, "_fetch_bytes", lambda url: b"png" if "qr.png" in url else b"{}")

    result = await execute_command(None, "/qrcode")
    assert result is not None
    assert result.data.get("error") is True
    assert "Pillow" in result.output


@pytest.mark.asyncio
async def test_qrcode_error_when_tunnel_disabled_shows_lan_url(monkeypatch) -> None:
    monkeypatch.setattr(qrcode_cmd, "_gomatrix_port", lambda: 8008)
    monkeypatch.setattr(
        qrcode_cmd,
        "_dashboard_status",
        lambda: {"tunnel_ready": False, "tunnel_enabled": False, "connection_url": "http://192.168.1.5:8008"},
    )

    result = await execute_command(None, "/qrcode")
    assert result is not None
    assert result.data.get("error") is True
    assert "192.168.1.5" in result.output
    assert "Cloudflare" in result.output or "隧道" in result.output
