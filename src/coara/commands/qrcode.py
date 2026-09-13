"""CLI /qrcode — 在终端渲染 gomatrix 手机配对二维码供手机扫码连接。

二维码内容（账号/隧道/一次性注册 ticket）完全由 gomatrix 生成，CLI 只
抓取 /dashboard/qr.png 并解码为终端块字符，保证与 Web 手机接入页同一份数据、
手机 App 扫码即连。
"""

from __future__ import annotations

import io
import shutil
from typing import TYPE_CHECKING

from src.coara.commands.registry import CommandArgs, register
from src.coara.commands.types import CommandResult

if TYPE_CHECKING:
    from src.coara.root import RootCoara

# QR 各版本模块数（4*v + 17），用于在解码时锁定实际版本。
_VALID_VERSIONS = [4 * v + 17 for v in range(1, 41)]


def _gomatrix_port() -> int:
    try:
        from src.core.config import config_manager

        cfg = config_manager.config
        if cfg is not None and getattr(cfg, "matrix", None) is not None:
            port = int(getattr(cfg.matrix, "port", 0) or 0)
            if port:
                return port
    except Exception:
        pass
    return 8008


def _fetch_bytes(url: str, timeout: float = 5.0) -> bytes | None:
    import httpx

    try:
        with httpx.Client(verify=False, trust_env=False, timeout=timeout) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                return resp.content
    except (httpx.HTTPError, OSError):
        return None
    return None


def _dashboard_status() -> dict | None:
    import json

    body = _fetch_bytes(f"http://127.0.0.1:{_gomatrix_port()}/api/dashboard/status")
    if body is None:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _finder_expected(i: int, j: int) -> bool:
    """QR 左上 7x7 定位符的期望明暗（黑环 + 中心 3x3 黑块）。"""
    if i in (0, 6) or j in (0, 6):
        return True
    return bool(2 <= i <= 4 and 2 <= j <= 4)


def render_qr_png(png: bytes) -> str:
    """把 gomatrix 的二维码 PNG 解码为终端块字符矩阵（1 字符=1 模块）。

    通过 7x7 定位符与各候选版本比对锁定真实模块数，再逐模块采样渲染，
    保证手机可扫。空串表示解码失败。
    """
    if _pil_image_module() is None:
        return ""
    try:
        return _render_qr_image(png)
    except Exception:
        return ""


def _pil_image_module():
    try:
        from PIL import Image  # noqa: F401

        return Image
    except ImportError:
        return None


def _render_qr_image(png: bytes) -> str:
    pil_image = _pil_image_module()
    if pil_image is None:
        return ""
    img = pil_image.open(io.BytesIO(png)).convert("L")
    px = img.load()
    w, h = img.size
    dark = 128
    min_x, min_y, max_x, max_y = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            if px[x, y] < dark:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
    if max_x < 0:
        return ""

    bbox_w = max_x - min_x + 1
    best = (0, -1, 1.0)
    for modules in _VALID_VERSIONS:
        if modules > bbox_w:
            break
        scale = bbox_w / modules
        matches = 0
        for i in range(7):
            for j in range(7):
                cx = min_x + int((j + 0.5) * scale)
                cy = min_y + int((i + 0.5) * scale)
                if cx >= w or cy >= h:
                    continue
                if (px[cx, cy] < dark) == _finder_expected(i, j):
                    matches += 1
        if matches > best[1]:
            best = (modules, matches, scale)

    modules, _matches, scale = best
    if modules <= 0:
        return ""

    matrix = [[False] * modules for _ in range(modules)]
    for i in range(modules):
        for j in range(modules):
            cx = min_x + int((j + 0.5) * scale)
            cy = min_y + int((i + 0.5) * scale)
            if 0 <= cx < w and 0 <= cy < h:
                matrix[i][j] = px[cx, cy] < dark

    lines = []
    for i in range(0, modules, 2):
        row = ""
        for j in range(modules):
            top = matrix[i][j]
            bottom = matrix[i + 1][j] if i + 1 < modules else False
            if top and bottom:
                row += "\u2588"
            elif top:
                row += "\u2580"
            elif bottom:
                row += "\u2584"
            else:
                row += " "
        lines.append(row)
    return "\n".join(lines)


@register("qrcode")
async def handle_qrcode(root: RootCoara, args: CommandArgs) -> CommandResult:
    """用手机 App 扫码连接 coara 手机端。

    Usage:
      /qrcode    在终端显示手机配对二维码
    """
    status = _dashboard_status()
    port = _gomatrix_port()
    if status is None:
        return CommandResult.error(
            f"gomatrix 不可达（端口 {port}），手机接入未就绪。\n"
            "请用 coara / coara -x / coara -cwx 启动（含 Matrix 接入），稍候再试 /qrcode。"
        )
    if not (status.get("tunnel_ready") and status.get("tunnel_url")):
        if status.get("tunnel_enabled"):
            tunnel_error = status.get("tunnel_error") or "隧道仍在建立，稍候重试"
            return CommandResult.error(f"隧道尚未就绪：{tunnel_error}\n稍候再执行 /qrcode")
        conn_url = status.get("connection_url") or "（未获取到局域网地址）"
        return CommandResult.error(
            f"当前未启用 Cloudflare 隧道，终端二维码暂不可用（gomatrix 仅在有隧道时生成配对码）。\n"
            f"局域网地址：{conn_url}\n"
            "手机需与电脑同一 WiFi，在 App 内手动填写 homeserver 地址连接。"
        )
    try:
        png = _fetch_bytes(f"http://127.0.0.1:{port}/dashboard/qr.png")
    except Exception:
        png = None
    if not png:
        return CommandResult.error("获取二维码失败，请稍候重试 /qrcode。")
    art = render_qr_png(png)
    tunnel_url = str(status.get("tunnel_url") or "")
    if not art:
        if _pil_image_module() is None:
            return CommandResult.error(
                "终端二维码渲染需要 Pillow。\n"
                "请执行 pip install coara[desktop] 或 pip install pillow 后重试 /qrcode。"
            )
        return CommandResult.error("二维码渲染失败，请稍候重试 /qrcode。")

    art_width = max(len(line) for line in art.splitlines())
    term_width = shutil.get_terminal_size(fallback=(200, 50)).columns
    if term_width < art_width:
        return CommandResult.error(
            f"终端宽度不足（需 ≥{art_width} 列，当前 {term_width} 列），二维码会换行无法扫描。\n"
            f"请拉宽窗口后重试 /qrcode，或用手机 App 手动填写以下地址配对：\n{tunnel_url}"
        )

    output = f"{art}\n\n用手机 App 扫上方二维码连接"
    return CommandResult(output=output, data={"qr": True, "tunnel_url": tunnel_url})
