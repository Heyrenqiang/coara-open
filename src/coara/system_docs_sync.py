"""系统文档同步：内核启动时把真源文档同步到 ``<coara_home>/docs/``"""

from __future__ import annotations

import shutil
from pathlib import Path

from src.core.logger import logger


def _source_docs_dir() -> Path | None:
    """定位真源 docs 目录：开发仓库 → 发布安装包，都没有返回 None。"""
    try:
        from src.core.config import _repo_root

        root = _repo_root()
        if root is not None:
            candidate = root / "deploy" / "official" / "docs"
            if candidate.is_dir():
                return candidate
    except Exception:
        logger.debug("system docs: repo source probe failed", exc_info=True)
    # 发布安装包：coara 的 python 嵌入在 <install>/bin/，docs 在 <install>/docs/
    import sys

    for base in (Path(sys.executable).resolve().parent, Path(sys.prefix).resolve()):
        candidate = base.parent / "docs"
        if candidate.is_dir() and any(candidate.glob("*.md")):
            return candidate
    return None


def sync_system_docs(coara_home: Path | None) -> None:
    """把真源文档镜像到 ``<coara_home>/docs/``；真源缺失或 coara_home 为空则跳过。"""
    if coara_home is None:
        return
    source = _source_docs_dir()
    if source is None:
        return
    dest = Path(coara_home) / "docs"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        source_files = {p.name: p for p in source.glob("*.md")}
        written = 0
        for name, src in source_files.items():
            dst = dest / name
            if dst.is_file() and dst.read_bytes() == src.read_bytes():
                continue
            shutil.copyfile(src, dst)
            written += 1
        removed = 0
        for dst in dest.glob("*.md"):
            if dst.name not in source_files:
                dst.unlink()
                removed += 1
        if written or removed:
            logger.info(f"system docs: 已同步到 {dest}（写 {written} 删 {removed}）")
    except OSError as exc:
        # 同步失败不阻塞启动——配置助手读不到新文档只是内容旧一点
        logger.warning(f"system docs sync failed: {exc}")
