"""Agent Registry - loads agent definitions from Markdown and YAML files.

Legacy mode: each .md file in src/coara/prompts/ is a pure system prompt.
New mode: YAML configs in src/coara/prompts/agents/ support modular assembly
with extends, variable substitution, and tool whitelists.
"""

from __future__ import annotations

from pathlib import Path

from src.core.logger import logger
from src.prompt.yaml_loader import AgentPromptConfig, YamlPromptLoader


class AgentDefinition:
    """Represents a single agent definition loaded from Markdown or YAML."""

    def __init__(
        self,
        path: Path,
        content: str,
        yaml_config: AgentPromptConfig | None = None,
    ):
        self.path = path
        self.id: str = path.stem
        self.name: str = yaml_config.name if yaml_config else self.id
        self.system_prompt_template: str = content
        self.yaml_config: AgentPromptConfig | None = yaml_config


class AgentRegistry:
    """Singleton to manage agent definitions from Markdown and YAML files."""

    _instance: AgentRegistry | None = None

    def __new__(cls):
        if cls._instance is None:
            instance = super().__new__(cls)
            # 实例属性在 __new__ 里创建：绕过 __init__ 的构造路径
            # （如 copy/pickle）也能拿到属于自己的 _agents，不再落回共享类属性
            instance._agents = {}
            cls._instance = instance
        return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        """Drop the singleton and its cached definitions (tests only)."""
        cls._instance = None

    def scan(self, directories: list[Path | str] | None = None) -> None:
        """Scan directories for agent definitions (*.md and *.yaml files).

        YAML files take precedence over Markdown files with the same stem.
        """
        if directories is None:
            root = Path(__file__).parent.parent.parent
            directories = [
                root / "src" / "coara" / "prompts" / "agents",
            ]

        self._agents.clear()
        count_md = 0
        count_yaml = 0

        for directory in directories:
            dir_path = Path(directory)
            if not dir_path.exists():
                continue

            # First pass: collect all candidate files
            md_files = {f.stem: f for f in dir_path.glob("*.md")}
            yaml_files = {f.stem: f for f in dir_path.glob("*.yaml")}

            # YAML takes precedence over MD for the same stem
            all_stems = set(md_files.keys()) | set(yaml_files.keys())

            for stem in all_stems:
                if stem in self._agents:
                    continue  # already loaded from an earlier directory

                if stem in yaml_files:
                    try:
                        agent = self._load_from_yaml(yaml_files[stem])
                        self._agents[agent.id] = agent
                        count_yaml += 1
                    except Exception as e:
                        logger.warning(f"Failed to load agent YAML '{stem}': {e}")
                elif stem in md_files:
                    try:
                        agent = self._load_from_md(md_files[stem])
                        self._agents[agent.id] = agent
                        count_md += 1
                    except Exception as e:
                        logger.warning(f"Failed to load agent MD '{stem}': {e}")

        logger.info(
            f"AgentRegistry loaded {count_yaml} YAML + {count_md} MD agents from {len(directories)} directories."
        )

    def get_agent(self, agent_id: str) -> AgentDefinition | None:
        """Get an agent definition by ID."""
        if not self._agents:
            self.scan()
        return self._agents.get(agent_id)

    def reload(self) -> None:
        """Force reload all agent definitions."""
        self.scan()

    # ── Internal loaders ──

    @staticmethod
    def _load_from_md(md_file: Path) -> AgentDefinition:
        """Load an agent definition from Markdown.

        Supports two formats:
        1. YAML frontmatter + Markdown body (primary)
        2. Pure Markdown (fallback)
        """
        content = md_file.read_text(encoding="utf-8")
        if not content.strip():
            raise ValueError(f"Empty agent prompt: {md_file}")

        from src.prompt.yaml_loader import parse_markdown_frontmatter

        # Try YAML frontmatter first
        frontmatter_result = parse_markdown_frontmatter(content)
        if frontmatter_result is not None:
            agent_data, _body = frontmatter_result
            loader = YamlPromptLoader()
            config = loader.load_from_data(agent_data, md_file.parent, md_file.stem)
            return AgentDefinition(md_file, config.system_prompt, yaml_config=config)

        # Legacy mode: no frontmatter
        return AgentDefinition(md_file, content)

    @staticmethod
    def _load_from_yaml(yaml_file: Path) -> AgentDefinition:
        """Load a modular YAML agent definition.

        Paired mode: if a .md file with the same stem exists, its content
        is used as the system prompt, while the .yaml provides metadata
        (tools, role, when_to_use, etc.).
        """
        loader = YamlPromptLoader()
        config = loader.load(yaml_file)

        # Paired mode: look for a .md file with the same stem
        md_file = yaml_file.with_suffix(".md")
        if md_file.exists():
            md_body = md_file.read_text(encoding="utf-8").strip()
            prefix = config.system_prompt.strip()
            if prefix and md_body:
                config.system_prompt = f"{prefix}\n\n{md_body}"
            elif md_body:
                config.system_prompt = md_body
            system_prompt = config.system_prompt
        else:
            system_prompt = config.system_prompt

        return AgentDefinition(
            path=yaml_file,
            content=system_prompt,
            yaml_config=config,
        )
