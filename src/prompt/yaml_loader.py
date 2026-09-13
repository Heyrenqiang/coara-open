"""YAML-based prompt loader with modular assembly and variable substitution.

Prompt assembly architecture:
- Agent configs are YAML files that assemble prompt modules
- Supports `extends` to include shared markdown modules
- Supports `${VAR}` substitution for dynamic content
- Supports `{{INCLUDE:path}}` inline file inclusion
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.core.logger import logger

# ${VAR} substitution (same pattern as delegate dynamic description templates)
_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_INCLUDE_PATTERN = re.compile(r"\{\{INCLUDE:([^}]+)\}\}")


def substitute_variables(template: str, substitutions: dict[str, str]) -> str:
    """Apply ${VAR} substitutions to *template* using the shared regex pattern."""
    if not substitutions:
        return template

    def _replacer(match: re.Match) -> str:
        key = match.group(1)
        if key in substitutions:
            return str(substitutions[key])
        return match.group(0)

    return _VAR_PATTERN.sub(_replacer, template)


def parse_markdown_frontmatter(raw: str) -> tuple[dict[str, Any], str] | None:
    """Parse YAML frontmatter from a Markdown agent prompt file.

    Returns ``(agent_data, body)`` when the file starts with ``---`` and the
    frontmatter contains an ``agent`` key, otherwise ``None``.
    """
    if not raw.startswith("---"):
        return None
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        frontmatter = yaml.safe_load(parts[1])
    except Exception:
        return None
    if not (frontmatter and isinstance(frontmatter, dict) and "agent" in frontmatter):
        return None
    body = parts[2].strip()
    agent_data = frontmatter["agent"]
    # Inject body as system_prompt_template if not already set
    if not agent_data.get("system_prompt_template"):
        agent_data["system_prompt_template"] = body
    return agent_data, body


@dataclass(slots=True)
class AgentPromptConfig:
    """Assembled agent prompt configuration from YAML."""

    name: str
    system_prompt: str
    tools_include: list[str] = field(default_factory=list)
    tools_exclude: list[str] = field(default_factory=list)
    skills_include: list[str] = field(default_factory=list)
    skills_exclude: list[str] = field(default_factory=list)
    when_to_use: str | None = None
    role: str | None = None
    # Raw args for later variable injection at runtime
    system_prompt_args: dict[str, str] = field(default_factory=dict)


class YamlPromptLoader:
    """Load and assemble agent prompts from YAML configuration files."""

    def __init__(self, prompts_dir: Path | None = None):
        if prompts_dir is None:
            self._prompts_dir = Path(__file__).parent.parent / "coara" / "prompts"
        else:
            self._prompts_dir = prompts_dir

    # ── Public API ──

    def load(self, yaml_path: Path | str) -> AgentPromptConfig:
        """Load an agent prompt config from a YAML or Markdown file.

        Supports three modes:
        - Markdown files with YAML frontmatter (--- frontmatter --- body)
        - Pure YAML files (with top-level `agent` key)
        - Plain Markdown files (no frontmatter — treated as complete system prompt)

        Args:
            yaml_path: Path to the agent config file.

        Returns:
            Assembled AgentPromptConfig with all modules resolved.
        """
        yaml_path = Path(yaml_path)
        if not yaml_path.exists():
            raise FileNotFoundError(f"Agent config not found: {yaml_path}")

        raw = yaml_path.read_text(encoding="utf-8")

        # Try Markdown frontmatter first
        frontmatter_result = parse_markdown_frontmatter(raw)
        if frontmatter_result is not None:
            agent_data, _body = frontmatter_result
            return self.load_from_data(agent_data, yaml_path.parent, yaml_path.stem)

        # Try pure YAML mode
        try:
            data = yaml.safe_load(raw)
            if data and isinstance(data, dict) and "agent" in data:
                return self.load_from_data(data["agent"], yaml_path.parent, yaml_path.stem)
        except yaml.YAMLError:
            pass  # Not valid YAML, fall through to plain Markdown

        # Plain Markdown mode: treat entire file as the complete system prompt.
        # Only .md files fall back to this; .yaml/.yml must have valid agent config.
        if yaml_path.suffix.lower() == ".md":
            default_data: dict[str, Any] = {
                "name": yaml_path.stem,
                "system_prompt_template": raw.strip(),
                "tools": {"include": ["*"], "exclude": []},
            }
            # Special case: root agent gets its canonical name
            if yaml_path.stem == "root":
                default_data["name"] = "考拉助手"

            return self.load_from_data(default_data, yaml_path.parent, yaml_path.stem)

        raise ValueError(f"Invalid agent config (missing 'agent' key): {yaml_path}")

    def load_from_data(
        self,
        agent_data: dict[str, Any],
        base_dir: Path,
        default_name: str = "",
    ) -> AgentPromptConfig:
        """Assemble an AgentPromptConfig from a raw agent data dict.

        This is used by both YAML files and Markdown frontmatter.
        """
        # 1. Resolve extends (module files)
        assembled_prompt = self._resolve_extends(agent_data, base_dir)

        # 2. Append custom system_prompt_template if present
        template = agent_data.get("system_prompt_template", "")
        if template:
            if assembled_prompt:
                assembled_prompt += "\n\n"
            assembled_prompt += template

        # 3. Resolve {{INCLUDE:path}} directives
        assembled_prompt = self._resolve_includes(assembled_prompt, base_dir)

        # 4. Apply static system_prompt_args substitutions
        args = agent_data.get("system_prompt_args", {})
        assembled_prompt = self._substitute_vars(assembled_prompt, args)
        # If any args have no matching placeholder, append them at the end
        for key, value in args.items():
            if value and f"${{{key}}}" not in assembled_prompt:
                if assembled_prompt:
                    assembled_prompt += "\n\n"
                assembled_prompt += str(value)

        # 5. Parse tool configuration
        tools_data = agent_data.get("tools", {})
        tools_include = self._parse_tool_list(tools_data.get("include", []))
        tools_exclude = self._parse_tool_list(tools_data.get("exclude", []))

        # 6. Parse skill configuration
        skills_data = agent_data.get("skills", {})
        skills_include = self._parse_tool_list(skills_data.get("include", []))
        skills_exclude = self._parse_tool_list(skills_data.get("exclude", []))

        return AgentPromptConfig(
            name=agent_data.get("name", default_name),
            system_prompt=assembled_prompt,
            tools_include=tools_include,
            tools_exclude=tools_exclude,
            skills_include=skills_include,
            skills_exclude=skills_exclude,
            when_to_use=agent_data.get("when_to_use"),
            role=agent_data.get("role"),
            system_prompt_args=args,
        )

    # ── Internal helpers ──

    def _resolve_extends(self, agent_data: dict[str, Any], base_dir: Path) -> str:
        """Resolve the `extends` list into concatenated module content."""
        extends = agent_data.get("extends", [])
        if isinstance(extends, str):
            extends = [extends]

        parts: list[str] = []
        for module_ref in extends:
            # Resolve relative to the prompts root directory（越界引用被拒即跳过该模块）
            try:
                module_path = self._resolve_path(module_ref, base_dir)
            except ValueError:
                continue
            if not module_path.exists():
                logger.warning(f"Prompt module not found: {module_path}")
                continue
            content = module_path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(content)

        return "\n\n".join(parts)

    def _resolve_includes(self, text: str, base_dir: Path) -> str:
        """Replace {{INCLUDE:path}} directives with file contents."""

        def _replacer(match: re.Match) -> str:
            ref = match.group(1).strip()
            try:
                include_path = self._resolve_path(ref, base_dir)
            except ValueError:
                return f"<!-- include rejected: {ref} -->"
            if not include_path.exists():
                logger.warning(f"Include file not found: {include_path}")
                return f"<!-- include missing: {ref} -->"
            return include_path.read_text(encoding="utf-8").strip()

        return _INCLUDE_PATTERN.sub(_replacer, text)

    def _substitute_vars(self, text: str, substitutions: dict[str, str]) -> str:
        """Apply ${VAR} substitutions using the same pattern as tool descriptions."""
        return substitute_variables(text, substitutions)

    def _resolve_path(self, ref: str, base_dir: Path) -> Path:
        """Resolve a module/include reference to an absolute path.

        根约束：只允许 prompts 根目录与 YAML 同目录两个来源，拒绝绝对路径与
        ``..`` 逃逸——agent YAML 属可被外部写入的内容，extends/INCLUDE 若能
        指向任意文件（如 ../../.env），密钥会被直接拼进 system prompt。
        """
        raw = Path(ref)
        if raw.is_absolute():
            logger.warning(f"Prompt include rejected (absolute path): {ref}")
            raise ValueError(f"prompt include must be relative: {ref}")
        local = (base_dir / raw).resolve()
        root = self._prompts_dir.resolve()
        if local != root and root not in local.parents:
            logger.warning(f"Prompt include rejected (escapes prompts root): {ref}")
            raise ValueError(f"prompt include escapes prompts root: {ref}")
        if local.exists():
            return local
        # Fall back to the prompts root directory（同样受根约束）
        return root / raw

    @staticmethod
    def _parse_tool_list(value: Any) -> list[str]:
        """Normalize tool list from YAML (string, list, or '*')."""
        if value == "*" or value == ["*"]:
            return ["*"]
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(v) for v in value]
        return []
