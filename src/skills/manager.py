"""
Coara v8 - Skill Manager

Skill 发现与注册（磁盘目录 → SkillDefinition 池）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.core.coara_home import user_paths
from src.core.errors import SkillNotFoundError
from src.core.logger import logger
from src.core.types import SkillDefinition
from src.skills.loader import load_skills_from_dir


def infer_skill_source(location: str) -> str:
    """Infer skill origin from its SKILL.md path."""
    normalized = location.replace("\\", "/").lower()
    user_prefix = str(Path.home() / ".coara" / "skills").replace("\\", "/").lower()
    if normalized.startswith(user_prefix):
        return "user"
    if "/users/default/skills/" in normalized:
        return "user"
    if "/.coara/skills/" in normalized:
        return "workspace"
    if "/skills/" in normalized:
        return "builtin"
    return "extra"


def _dir_mtime(path: Path) -> float:
    """Directory fingerprint: max mtime of the dir and immediate SKILL.md files."""
    try:
        latest = path.stat().st_mtime
    except OSError:
        return 0.0
    try:
        for child in path.rglob("SKILL.md"):
            try:
                latest = max(latest, child.stat().st_mtime)
            except OSError:
                continue  # 单个文件 stat 失败：跳过该项，mtime 指纹照旧累计
    except OSError:
        pass  # 目录遍历失败（权限/坏链接）：沿用已收集的 mtime 指纹
    return latest


class SkillManager:
    """
    Skill 管理器。

    负责技能的发现、加载、激活和查询。
    """

    def __init__(self):
        self._skills: dict[str, SkillDefinition] = {}
        self._skill_dirs: set[str] = set()
        self._discover_lock = asyncio.Lock()
        self._discover_cache_key: tuple[object, ...] | None = None
        self._discover_cache_mtimes: tuple[float, ...] | None = None

    def _discover_sources(
        self,
        workspace_dir: Path,
        extra_paths: list[Path] | None,
        is_trusted: bool,
        coara_home: Path | None,
    ) -> list[tuple[Path, str]]:
        sources: list[tuple[Path, str]] = []
        builtin_skills_dir = Path(__file__).parent.parent.parent / "skills"
        if builtin_skills_dir.exists():
            sources.append((builtin_skills_dir, "builtin"))
        if coara_home is not None:
            home = Path(coara_home).expanduser().resolve()
            user_skill_dir = user_paths(home).skills_dir
            if user_skill_dir.exists():
                sources.append((user_skill_dir, "user"))
        if is_trusted:
            workspace_skills_dir = Path(workspace_dir) / ".coara" / "skills"
            if workspace_skills_dir.exists():
                sources.append((workspace_skills_dir, "workspace"))
        if extra_paths:
            for extra_path in extra_paths:
                if extra_path.exists():
                    sources.append((extra_path, "extra"))
        return sources

    async def discover(
        self,
        workspace_dir: Path,
        extra_paths: list[Path] | None = None,
        is_trusted: bool = True,
        coara_home: Path | None = None,
        *,
        force: bool = False,
    ) -> None:
        """
        发现并加载所有可用的 Skill。

        技能来源（后加载的覆盖先加载的）：
        1. 内置 skills/ 目录
        2. 用户级技能 ``<coara_home>/users/default/skills/``
        3. 工作区技能 ``.coara/skills/`` (需要信任)
        4. 额外指定的路径

        When directories are unchanged (mtime fingerprint), skip the full rescan.
        """
        async with self._discover_lock:
            sources = self._discover_sources(workspace_dir, extra_paths, is_trusted, coara_home)
            cache_key = (
                str(Path(workspace_dir).resolve()),
                bool(is_trusted),
                str(Path(coara_home).expanduser().resolve()) if coara_home is not None else "",
                tuple(str(Path(p).resolve()) for p, _ in sources),
            )
            mtimes = tuple(_dir_mtime(path) for path, _ in sources)
            if (
                not force
                and self._skills
                and cache_key == self._discover_cache_key
                and mtimes == self._discover_cache_mtimes
            ):
                return

            self._skills.clear()
            self._skill_dirs.clear()

            for dir_path, source in sources:
                await self._load_and_merge(dir_path, source)

            self._discover_cache_key = cache_key
            self._discover_cache_mtimes = mtimes
            logger.info(f"Discovered {len(self._skills)} skills")

    async def _load_and_merge(self, dir_path: Path, source: str) -> None:
        """加载并合并技能"""
        try:
            skills = await load_skills_from_dir(dir_path)
            self._skill_dirs.add(str(dir_path))

            for skill in skills:
                # 后加载的覆盖先加载的（优先级更高）
                if skill.name in self._skills:
                    logger.debug(f"Skill '{skill.name}' from {source} is overriding existing")

                self._skills[skill.name] = skill

            logger.debug(f"Loaded {len(skills)} skills from {dir_path} ({source})")
        except Exception as exc:
            logger.warning(f"Failed to load skills from {dir_path}: {exc}")

    def get(self, name: str) -> SkillDefinition:
        """获取单个 Skill"""
        if name not in self._skills:
            raise SkillNotFoundError(name)
        return self._skills[name]

    def get_all(self) -> list[SkillDefinition]:
        """Return all discovered skills."""
        return list(self._skills.values())

    def __repr__(self) -> str:
        return f"SkillManager(skills={len(self._skills)})"
