"""原子 JSON 文件写入。"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, payload: Any, *, indent: int = 2) -> None:
    """Write JSON to *path* via a same-directory temp file + replace."""
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=indent))


def write_text_atomic(path: Path, text: str) -> None:
    """Write text to *path* atomically (temp file + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(4):
            try:
                os.replace(tmp_path, path)
                break
            except PermissionError:
                if attempt >= 3:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
