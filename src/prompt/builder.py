"""System prompt builder — modular YAML assembly with ${VAR} substitution.

Loads YAML configs from src/coara/prompts/agents/ that assemble shared modules,
with runtime variable injection (${COARA_WORK_DIR} and YAML system_prompt_args).
"""

from __future__ import annotations

import os
from pathlib import Path

from src.core.logger import logger
from src.prompt.yaml_loader import AgentPromptConfig


class PromptBuilder:
    """Build system prompts from static Markdown files or YAML assemblies."""

    def __init__(self):
        self._role_prompt: str = ""
        self._yaml_config: AgentPromptConfig | None = None
        self._workspace_dir: Path | None = None

    # ── Fluent setters ──

    def set_role_prompt(self, role_prompt: str) -> PromptBuilder:
        """Set the complete system prompt from agent definition."""
        self._role_prompt = role_prompt
        return self

    def set_yaml_config(self, config: AgentPromptConfig) -> PromptBuilder:
        """Set the assembled prompt config from YAML (new modular mode)."""
        self._yaml_config = config
        return self

    def set_workspace_dir(self, workspace_dir: str | Path) -> PromptBuilder:
        """Set the workspace directory for environment variable substitution."""
        self._workspace_dir = Path(workspace_dir)
        return self

    # ── Build ──

    def build(self) -> str:
        """Build and return the complete system prompt."""
        if self._yaml_config is not None:
            prompt = self._inject_runtime_variables(self._yaml_config.system_prompt)
        else:
            prompt = self._inject_runtime_variables(self._role_prompt)

        logger.debug(f"Built system prompt: {len(prompt)} chars")

        if os.environ.get("COARA_DEBUG_PROMPT"):
            self._dump_prompt(prompt)

        return prompt

    def _dump_prompt(self, prompt: str) -> None:
        """Write the fully assembled prompt to disk for debugging."""
        if self._workspace_dir is None:
            return
        dump_path = self._workspace_dir / ".coara" / "debug_prompt.md"
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(prompt, encoding="utf-8")
        logger.info(f"Dumped assembled prompt to {dump_path}")

    # ── Runtime variable injection ──

    def _inject_runtime_variables(self, template: str) -> str:
        """Inject dynamic runtime variables into the prompt template.

        Supported variables:
        - ${COARA_WORK_DIR} — Current working directory
        - Any key declared in the YAML config's ``system_prompt_args``
          (matched case-insensitively against ${VAR} placeholders)
        """
        from src.prompt.yaml_loader import substitute_variables

        work_dir = self._workspace_dir if self._workspace_dir is not None else Path.cwd()

        substitutions: dict[str, str] = {
            "COARA_WORK_DIR": str(work_dir),
        }

        if self._yaml_config is not None:
            for key, value in self._yaml_config.system_prompt_args.items():
                var_name = key.upper()
                if var_name not in substitutions or not substitutions[var_name]:
                    substitutions[var_name] = str(value)

        return substitute_variables(template, substitutions)
