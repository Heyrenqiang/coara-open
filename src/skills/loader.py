"""
Coara v8 - Skill Loader

加载和解析 SKILL.md 文件。
参考 OpenCode 和 Gemini CLI 的 skill 加载机制。
"""

from __future__ import annotations

import re
from pathlib import Path

import frontmatter

from src.core.errors import SkillError
from src.core.logger import logger
from src.core.types import SkillDefinition


class SkillLoader:
    """
    Skill 文件加载器。

    负责解析 SKILL.md 文件的 frontmatter 和正文。
    """

    @staticmethod
    def load_from_file(file_path: str | Path) -> SkillDefinition:
        """
        从文件加载 Skill。

        Args:
            file_path: SKILL.md 文件路径

        Returns:
            SkillDefinition 实例

        Raises:
            SkillError: 加载失败时
        """
        path = Path(file_path)

        if not path.exists():
            raise SkillError(f"Skill file not found: {path}")

        if not path.is_file():
            raise SkillError(f"Not a file: {path}")

        try:
            content = path.read_text(encoding="utf-8")
            return SkillLoader.parse(content, str(path))
        except Exception as e:
            raise SkillError(f"Failed to load skill from {path}: {e}") from e

    @staticmethod
    def parse(content: str, location: str) -> SkillDefinition:
        """
        解析 Skill 内容。

        Args:
            content: SKILL.md 文件内容
            location: 文件路径（用于记录来源）

        Returns:
            SkillDefinition 实例
        """
        try:
            # 使用 python-frontmatter 解析
            post = frontmatter.loads(content)

            # 提取元数据
            name = post.get("name", "")
            description = post.get("description", "")
            references = post.get("references", [])
            listed_raw = post.get("listed", True)
            listed = (
                listed_raw
                if isinstance(listed_raw, bool)
                else str(listed_raw).strip().lower()
                not in {
                    "false",
                    "no",
                    "0",
                    "off",
                }
            )

            if not name:
                raise SkillError(f"Missing 'name' in frontmatter: {location}")

            if not description:
                raise SkillError(f"Missing 'description' in frontmatter: {location}")

            # 清理名称：保留 Unicode 词字符（中文技能名原样保留）、下划线、连字符，其余替换为 _
            sanitized_name = re.sub(r"[^\w\-]", "_", name)

            return SkillDefinition(
                name=sanitized_name,
                description=description,
                location=location,
                body=post.content.strip(),
                references=references if isinstance(references, list) else [],
                listed=listed,
            )

        except SkillError:
            raise
        except Exception as e:
            raise SkillError(f"Failed to parse skill at {location}: {e}") from e


async def load_skills_from_dir(dir_path: str | Path) -> list[SkillDefinition]:
    """
    从目录加载所有 Skill。

    递归搜索所有 SKILL.md 文件。

    Args:
        dir_path: 目录路径

    Returns:
        SkillDefinition 列表
    """
    path = Path(dir_path)
    if not path.exists() or not path.is_dir():
        logger.debug(f"Skill directory not found or not a directory: {path}")
        return []

    skills = []

    # Use pathlib.rglob for cross-platform compatibility (avoids Windows glob issues)
    for skill_file_path in path.rglob("SKILL.md"):
        try:
            skill = SkillLoader.load_from_file(str(skill_file_path))
            skills.append(skill)
            logger.debug(f"Loaded skill: {skill.name} from {skill_file_path}")
        except SkillError as e:
            logger.warning(f"Failed to load skill: {e}")

    return skills
