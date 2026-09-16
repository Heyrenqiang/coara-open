"""CLI 本地路径点击打开（OSC 8）。

Windows 上 ``file://`` 对中文路径常被终端 ShellExecute **静默失败**（如
``htmls/待办.html`` → ``file:///.../%E5%BE%85%E5%8A%9E.html``）。因此本机路径
**一律**走 ``coara-open:`` + ``pythonw``，用真实 ``Path`` 打开：

- 文件夹 → 资源管理器
- 普通文件（.html / .docx / 图片…）→ 系统默认关联（浏览器 / WPS 等）
- 脚本（.py 等）→ Cursor/编辑器，不执行

``coara-open`` 写入 WT ``safeUriSchemes``，一般不再弹「不安全位置」。
不用 ``cursor://file/…`` 作 OSC 8（部分终端点击无反馈）。

网页仍走 ``https://``，不经本模块。
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from src.core.workspace_layout import UPLOAD_DIR_NAME

PROTOCOL = "coara-open"
_B64_PREFIX = "b64:"

# 默认「打开」等于运行的后缀：改走编辑器，避免点一下就执行
_RUN_ON_OPEN_SUFFIXES = frozenset(
    {
        ".py",
        ".pyw",
        ".ps1",
        ".psm1",
        ".bat",
        ".cmd",
        ".com",
        ".exe",
        ".msi",
        ".vbs",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".msc",
        ".scr",
        ".cpl",
    }
)

_DETACHED = 0x00000008
_NEW_GROUP = 0x00000200
# coara-open 由短命 pythonw 承接：UWP 看图（Photos 等）常在父进程立刻退出后激活失败
_PROTOCOL_HOLD_S = 0.45

# 模型常把「工作空间根/文件名」链成绝对路径，真实文件多在这些子目录里
_NEARBY_SUBDIRS = (
    "media",
    "media_gen",
    "htmls",
    "out",
    "artifacts",
    "uploads",
    "images",
    "img",
)

_registered = False
_editor_exe_cache: Path | None | bool = False  # False=未解析；None=没有；Path=有


def path_from_protocol_uri(uri: str) -> Path | None:
    """Parse ``coara-open:…`` / ``coara-open://…`` / ``coara-open:b64:…`` into a Path."""
    raw = (uri or "").strip()
    if not raw.lower().startswith(f"{PROTOCOL}:"):
        return None
    rest = raw[len(PROTOCOL) + 1 :]
    if rest.startswith("//"):
        rest = rest[2:]
    if not rest:
        return None
    # 非 ASCII 路径用 b64，避开 ShellExecute/%1 的 %ENV% 展开撕碎 percent-encoding
    if rest.lower().startswith(_B64_PREFIX):
        token = rest[len(_B64_PREFIX) :]
        try:
            # 协议 URI 省略 `=` 填充，解码时补回
            pad = "=" * ((4 - len(token) % 4) % 4)
            decoded = base64.urlsafe_b64decode((token + pad).encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeError):
            return None
        return Path(decoded) if decoded else None
    # 兼容旧版 ASCII 正斜杠 / 曾 percent-encode 的链接（仅当 % 未被系统吃掉时）
    rest = urllib.parse.unquote(rest)
    if not rest:
        return None
    return Path(rest)


def href_for_local_path(path: Path) -> str:
    """Build OSC 8 href for a local path.

    Windows：一律 ``coara-open:``（避开 file:// 中文路径静默失败，脚本也不被运行）。
    路径用正斜杠（避免 ``\\b`` ``\\n`` 被命令行当转义）。
    含非 ASCII 时改 ``b64:``——``urllib.quote`` 的 ``%E6%…`` 会被 ShellExecute
    替换 ``%1`` 时当成 ``%ENV%`` 展开，路径撕成 ``W\\`` 一类残片并弹「找不到文件」。
    其它平台：``file://``。
    """
    resolved = path.expanduser()
    try:
        resolved = resolved.resolve(strict=False)
    except OSError:
        resolved = resolved.absolute()
    if sys.platform == "win32":
        ensure_protocol_registered()
        as_posix = str(resolved).replace("\\", "/")
        if as_posix.isascii():
            return f"{PROTOCOL}:{as_posix}"
        # 去掉 `=` 填充：部分 ShellExecute/%1 路径会吃掉尾部 `=`
        token = base64.urlsafe_b64encode(as_posix.encode("utf-8")).decode("ascii").rstrip("=")
        return f"{PROTOCOL}:{_B64_PREFIX}{token}"
    return resolved.as_uri()


def find_gui_editor() -> Path | None:
    """Resolve Cursor/VS Code GUI exe（勿用 cursor.cmd，否则会闪控制台）。"""
    global _editor_exe_cache
    if _editor_exe_cache is not False:
        return _editor_exe_cache if isinstance(_editor_exe_cache, Path) else None

    found: Path | None = None
    env = (os.environ.get("COARA_EDITOR") or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "").strip()
    if env:
        candidate = Path(env.strip('"'))
        if candidate.is_file() and candidate.suffix.lower() in {".exe", ""}:
            found = candidate

    if found is None:
        for name in ("cursor", "code"):
            which = shutil.which(name)
            if not which:
                continue
            resolved = _gui_exe_from_shim(Path(which))
            if resolved is not None:
                found = resolved
                break

    if found is None and sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        pf = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        for candidate in (
            Path(r"D:\cursor\Cursor.exe"),
            local / "Programs" / "cursor" / "Cursor.exe",
            local / "Programs" / "Cursor" / "Cursor.exe",
            pf / "Cursor" / "Cursor.exe",
            local / "Programs" / "Microsoft VS Code" / "Code.exe",
            pf / "Microsoft VS Code" / "Code.exe",
        ):
            if candidate.is_file():
                found = candidate
                break

    _editor_exe_cache = found if found is not None else None
    return found


def _gui_exe_from_shim(shim: Path) -> Path | None:
    """``…/bin/cursor.cmd`` → install-root ``Cursor.exe``."""
    try:
        shim = shim.resolve()
    except OSError:
        shim = shim.absolute()
    if shim.suffix.lower() == ".exe":
        return shim if shim.is_file() else None
    # resources/app/bin/cursor.cmd → 上三级为安装根
    if shim.suffix.lower() == ".cmd" and len(shim.parents) >= 4:
        for name in ("Cursor.exe", "Code.exe"):
            cand = shim.parents[3] / name
            if cand.is_file():
                return cand
    return None


def _protocol_launcher_exe() -> str:
    """Prefer pythonw.exe so coara-open clicks do not flash a console window."""
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return str(exe)


def ensure_protocol_registered() -> None:
    """Idempotent HKCU URL protocol + WT safeUriSchemes (Windows only)."""
    global _registered
    if _registered or sys.platform != "win32":
        return
    import winreg

    launcher = _protocol_launcher_exe()
    # -m 走已安装/可编辑包；%1 为完整 URI。必须用 pythonw，否则每次点击闪黑窗。
    command = f'"{launcher}" -m src.cli.local_open "%1"'
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{PROTOCOL}")
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, f"URL:{PROTOCOL} Protocol")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        winreg.CloseKey(key)
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{PROTOCOL}\shell\open\command")
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, command)
        winreg.CloseKey(key)
        _registered = True
    except OSError:
        pass  # 注册表写入失败（权限不足等）：协议注册是 best-effort，有意静默
    _ensure_windows_terminal_safe_scheme(PROTOCOL)
    _ensure_windows_terminal_safe_scheme("cursor")
    _ensure_windows_terminal_safe_scheme("vscode")


def _windows_terminal_settings_paths() -> list[Path]:
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return []
    root = Path(local)
    return [
        root / "Packages" / "Microsoft.WindowsTerminal_8wekyb3d8bbwe" / "LocalState" / "settings.json",
        root / "Packages" / "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe" / "LocalState" / "settings.json",
        root / "Microsoft" / "Windows Terminal" / "settings.json",
    ]


def _ensure_windows_terminal_safe_scheme(scheme: str) -> None:
    """Add scheme to WT ``safeUriSchemes`` so custom-protocol clicks skip the dialog."""
    for path in _windows_terminal_settings_paths():
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            updated = _patch_safe_uri_schemes(text, scheme)
            if updated is not None and updated != text:
                path.write_text(updated, encoding="utf-8", newline="\n")
        except OSError:
            continue  # 单个 WT settings 读写失败：跳过该文件，其余路径照常尝试


_SAFE_SCHEMES_RE = re.compile(
    r'("safeUriSchemes"\s*:\s*)\[(.*?)\]',
    re.DOTALL,
)


def _patch_safe_uri_schemes(text: str, scheme: str) -> str | None:
    """Return patched settings text, or None if the file looks unusable."""
    quoted = f'"{scheme}"'
    m = _SAFE_SCHEMES_RE.search(text)
    if m:
        body = m.group(2)
        if re.search(rf'"{re.escape(scheme)}"', body):
            return text
        body_stripped = body.strip().rstrip(",")
        new_body = quoted if not body_stripped else f"{body_stripped}, {quoted}"
        return text[: m.start(2)] + new_body + text[m.end(2) :]

    brace = text.find("{")
    if brace < 0:
        return None
    insert = f'\n    "safeUriSchemes": [ {quoted} ],'
    return text[: brace + 1] + insert + text[brace + 1 :]


def open_local_path(path: Path) -> None:
    """Open folder in Explorer / file without executing scripts when possible."""
    target = path.expanduser()
    try:
        target = target.resolve(strict=False)
    except OSError:
        target = target.absolute()

    if target.is_dir():
        _open_dir_best_effort(target)
        return

    if not target.exists():
        recovered = _recover_nearby_same_name(target)
        if recovered is not None:
            target = recovered
        else:
            # 勿对虚路径 start——会弹「Windows 找不到文件」；有父目录则打开父目录
            if target.parent.is_dir():
                _open_dir_best_effort(target.parent)
            return

    if target.suffix.lower() in _RUN_ON_OPEN_SUFFIXES:
        _open_without_running(target)
        return

    _open_file_best_effort(target)


def _recover_nearby_same_name(path: Path) -> Path | None:
    """路径不存在时，在相邻常见子目录里找同名唯一文件。

    例：链到 ``D:/ws/欧拉公式_PPT.png``，实际在 ``D:/ws/media_gen/欧拉公式_PPT.png``。
    """
    name = path.name
    if not name or path.exists():
        return None
    parent = path.parent
    bases: list[Path] = []
    for candidate in (parent, parent.parent if parent != parent.parent else None):
        if candidate is not None and candidate.is_dir() and candidate not in bases:
            bases.append(candidate)

    hits: list[Path] = []
    seen: set[Path] = set()
    for base in bases:
        for sub in _NEARBY_SUBDIRS:
            cand = base / sub / name
            try:
                if cand.is_file():
                    resolved = cand.resolve(strict=False)
                    if resolved not in seen:
                        seen.add(resolved)
                        hits.append(resolved)
            except OSError:
                continue  # 单个候选路径探测失败（坏链接/权限）：跳过继续找
        # 工作空间下 uploads（端上传来的附件统一落点）
        nested = base / UPLOAD_DIR_NAME / name
        try:
            if nested.is_file():
                resolved = nested.resolve(strict=False)
                if resolved not in seen:
                    seen.add(resolved)
                    hits.append(resolved)
        except OSError:
            pass  # uploads 候选路径探测失败：跳过继续找
    if len(hits) == 1:
        return hits[0]
    return None


def _spawn_detached(argv: list[str]) -> bool:
    try:
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = _DETACHED | _NEW_GROUP
            kwargs["close_fds"] = True
            kwargs["stdin"] = subprocess.DEVNULL
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
        subprocess.Popen(argv, **kwargs)  # noqa: S603
        return True
    except OSError:
        return False


def _win_shell_execute(path: Path, *, operation: str = "open") -> bool:
    """ShellExecuteW 直开（Unicode）；勿用 cmd start+CREATE_NO_WINDOW（中文路径会撕成 ``W\\`` 乱码）。"""
    try:
        import ctypes

        # 返回值 > 32 表示成功（见 Win32 SE_ERR_*）
        rc = int(
            ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
                None,
                operation,
                str(path),
                None,
                None,
                1,  # SW_SHOWNORMAL
            )
        )
        return rc > 32
    except (AttributeError, OSError, ValueError, TypeError):
        return False


def _open_dir_best_effort(path: Path) -> None:
    if sys.platform == "win32":
        if _spawn_detached(["explorer", str(path)]):
            return
        if _win_shell_execute(path):
            return
        with contextlib.suppress(OSError):
            os.startfile(str(path))  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
        return
    subprocess.run(["xdg-open", str(path)], check=False)


def _explorer_select(path: Path) -> None:
    """资源管理器选中文件；路径加引号，避免空格/残缺路径落到「此电脑」。"""
    subprocess.run(["explorer", f'/select,"{path}"'], check=False)


def _open_without_running(path: Path) -> None:
    # 1) Cursor / VS Code GUI（本机 .py 往往无 Edit 动词，旧逻辑会落到记事本弹窗）
    editor = find_gui_editor()
    if editor is not None and _spawn_detached([str(editor), str(path)]):
        return

    if sys.platform == "win32":
        if _win_shell_execute(path, operation="edit"):
            return
        try:
            os.startfile(str(path), "edit")  # type: ignore[attr-defined]
            return
        except OSError:
            pass  # 打开方式回落：edit 动词不可用时落到记事本/资源管理器兜底
        # 最后才记事本；explorer 选中不打开内容
        if _spawn_detached(["notepad.exe", str(path)]):
            return
        _explorer_select(path)
        return
    if sys.platform == "darwin":
        subprocess.run(["open", "-t", str(path)], check=False)
        return
    env_editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if env_editor:
        subprocess.run([env_editor, str(path)], check=False)
        return
    subprocess.run(["xdg-open", str(path)], check=False)


def _open_file_best_effort(path: Path) -> None:
    if sys.platform == "win32":
        # ShellExecuteW：短命 pythonw 下仍可靠，且不经 cmd 代码页撕中文路径
        if _win_shell_execute(path):
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
            return
        except OSError:
            pass  # 打开方式回落：startfile 失败时落到 explorer/选中兜底
        if _spawn_detached(["explorer", str(path)]):
            return
        _explorer_select(path)
        return
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
        return
    subprocess.run(["xdg-open", str(path)], check=False)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m src.cli.local_open <coara-open:path|path>", file=sys.stderr)
        return 2
    raw = args[0]
    path = path_from_protocol_uri(raw)
    if path is None:
        path = Path(urllib.parse.unquote(raw))
    try:
        open_local_path(path)
    except Exception as exc:  # noqa: BLE001
        print(f"coara-open failed: {exc}", file=sys.stderr)
        return 1
    # 协议处理器进程极短；稍候再退，给 UWP 激活留窗口
    if sys.platform == "win32":
        time.sleep(_PROTOCOL_HOLD_S)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
