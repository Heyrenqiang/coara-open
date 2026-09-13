"""Load and match workspace rules (.coara/rules/*.mdc) by path globs."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import frontmatter

from src.core.config import config_manager
from src.core.types import RulesGlobConfig


@dataclass(slots=True)
class RuleDefinition:
    name: str
    globs: list[str]
    body: str
    priority: int = 0
    source: str = ""


def get_rules_glob_config() -> RulesGlobConfig:
    cfg = getattr(config_manager, "_config", None)
    if cfg is not None:
        return cfg.runtime_enhancements.rules_glob
    raw = getattr(config_manager, "_raw_config", {}).get("runtime_enhancements") or {}
    rules_raw = (raw.get("rules_glob") or {}) if isinstance(raw, dict) else {}
    return RulesGlobConfig.model_validate(rules_raw)


def discover_rules(workspace_dir: Path) -> list[RuleDefinition]:
    """Load rules from workspace ``.coara/rules/`` then ``~/.coara/rules/``."""
    rules: dict[str, RuleDefinition] = {}
    search_dirs = [
        (workspace_dir / ".coara" / "rules", "workspace"),
        (Path.home() / ".coara" / "rules", "user"),
    ]
    for directory, source in search_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.mdc")):
            try:
                post = frontmatter.load(path)
            except Exception:
                continue
            meta = post.metadata or {}
            globs_raw = meta.get("globs") or meta.get("glob") or []
            if isinstance(globs_raw, str):
                globs = [globs_raw]
            else:
                globs = [str(item) for item in globs_raw if str(item).strip()]
            if not globs:
                continue
            body = (post.content or "").strip()
            if not body:
                continue
            name = str(meta.get("name") or path.stem)
            priority = int(meta.get("priority") or 0)
            rules[name] = RuleDefinition(
                name=name,
                globs=globs,
                body=body,
                priority=priority,
                source=source,
            )
    return sorted(rules.values(), key=lambda item: (-item.priority, item.name))


def path_matches_glob(rel_posix_path: str, pattern: str) -> bool:
    normalized = rel_posix_path.replace("\\", "/").lstrip("./")
    path = PurePosixPath(normalized)
    pat = pattern.replace("\\", "/").lstrip("./")
    if path.match(pat):
        return True
    if not pat.startswith("**/") and path.match(f"**/{pat}"):
        return True
    return fnmatch.fnmatch(normalized, pat)


def match_rules_for_paths(
    rules: list[RuleDefinition],
    rel_paths: list[str],
) -> list[RuleDefinition]:
    if not rules or not rel_paths:
        return []
    matched: list[RuleDefinition] = []
    seen: set[str] = set()
    for rule in rules:
        if any(path_matches_glob(rel, glob) for rel in rel_paths for glob in rule.globs) and rule.name not in seen:
            matched.append(rule)
            seen.add(rule.name)
    return matched


def collect_turn_paths(content: str, workspace_dir: Path) -> list[str]:
    """Paths referenced this turn for rules glob.

    ``@`` 不再表示文件引用，本函数恒返回空列表（规则仍可由其它路径来源匹配）。
    """
    del content, workspace_dir
    return []


def trim_rule_body(body: str, max_chars: int) -> str:
    text = body.strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "…"


def build_rules_reminder(
    matched: list[RuleDefinition],
    *,
    max_total_chars: int,
    max_rule_chars: int,
) -> str:
    from src.core.message_tags import system_reminder

    if not matched:
        return ""
    parts = ["以下规则适用于本轮涉及的路径，请遵守："]
    budget = max_total_chars
    for rule in matched:
        header = f"\n## rule:{rule.name}"
        body = trim_rule_body(rule.body, max_rule_chars)
        block = f"{header}\n{body}"
        if len(block) > budget:
            if budget < len(header) + 20:
                break
            body = trim_rule_body(body, max(0, budget - len(header) - 1))
            block = f"{header}\n{body}"
        parts.append(block)
        budget -= len(block)
        if budget <= 0:
            break
    return system_reminder("\n".join(parts))


def maybe_build_rules_messages(
    content: str,
    workspace_dir: Path,
    *,
    extra_paths: list[str] | None = None,
) -> list:
    """Return USER messages to inject when rules match (@-mention or delta paths)."""
    from src.core.types import Message, MessageRole

    settings = get_rules_glob_config()
    if not settings.enabled:
        return []
    rel_paths = collect_turn_paths(content, workspace_dir)
    if extra_paths:
        for rel in extra_paths:
            normalized = str(rel).replace("\\", "/").lstrip("./")
            if normalized and normalized not in rel_paths:
                rel_paths.append(normalized)
    if not rel_paths:
        return []
    matched = match_rules_for_paths(discover_rules(workspace_dir), rel_paths)
    if not matched:
        return []
    reminder = build_rules_reminder(
        matched,
        max_total_chars=settings.max_total_chars,
        max_rule_chars=settings.max_rule_chars,
    )
    if not reminder:
        return []
    return [Message(role=MessageRole.USER, content=reminder)]
