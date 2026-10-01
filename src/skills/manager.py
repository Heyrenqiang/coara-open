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


def factory_skills_dir() -> Path:
    """出厂技能目录（程序包内 skills/）：在线只读，升级随包覆盖。"""
    return Path(__file__).parent.parent.parent / "skills"


def is_factory_skill(location: str) -> bool:
    """该 SKILL.md 是否在出厂目录里（只读判据的唯一真源）。"""
    try:
        Path(location).resolve().relative_to(factory_skills_dir().resolve())
        return True
    except (ValueError, OSError):
        return False


def infer_skill_source(location: str) -> str:
    """推断技能来源（两级语义）：global = 全局级（含出厂技能，随程序目录或全局目录）；
    workspace = 工作空间级；extra = 额外挂载路径。SKILL.md 路径是唯一判据。"""
    normalized = location.replace("\\", "/").lower()
    if "/.coara/skills/" in normalized:
        return "workspace"
    if "/skills/" in normalized:
        return "global"
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
        coara_home: Path | None,
    ) -> list[tuple[Path, str]]:
        sources: list[tuple[Path, str]] = []
        # 出厂默认：程序目录里的 skills/，全局级可被同名覆盖（用户改过的版本生效）
        if factory_skills_dir().exists():
            sources.append((factory_skills_dir(), "global"))
        if coara_home is not None:
            home = Path(coara_home).expanduser().resolve()
            user_skill_dir = user_paths(home).skills_dir
            if user_skill_dir.exists():
                sources.append((user_skill_dir, "global"))
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
        coara_home: Path | None = None,
        *,
        force: bool = False,
    ) -> None:
        """
        发现并加载所有可用的 Skill。

        技能来源（后加载的覆盖先加载的）：
        1. 出厂默认 skills/（程序目录，全局级）
        2. 全局级 ``<coara_home>/users/default/skills/``（覆盖出厂同名）
        3. 工作区技能 ``.coara/skills/``
        4. 额外指定的路径

        When directories are unchanged (mtime fingerprint), skip the full rescan.
        """
        async with self._discover_lock:
            sources = self._discover_sources(workspace_dir, extra_paths, coara_home)
            cache_key = (
                str(Path(workspace_dir).resolve()),
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

    def apply_deferred(self, deferred: list[str], *, allowed: set[str] | None = None) -> None:
        """按配置挂起名单给已发现技能打戳；名单是唯一真相，文件内字段不参与。

        allowed 为空间级技能白名单（space.yaml 的 skills 字段）：非 None 时名单外技能
        一并视同挂起（不进主清单，仍可 search/activate）。
        """
        deferred_set = set(deferred)
        for skill in self._skills.values():
            skill.listed = skill.name not in deferred_set and (allowed is None or skill.name in allowed)

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
