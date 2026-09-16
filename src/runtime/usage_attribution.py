"""Agent / workspace attribution for usage accounting.

Writers stamp ``agent_kind`` from the live coara identity. Readers only accept
an explicit known ``agent_kind`` — no inference from legacy coara names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.coara_home import workspace_id_for
from src.core.logger import logger

# Known callers that appear on the usage dashboard. No residual 「其他」 bucket.
KNOWN_AGENT_KINDS: frozenset[str] = frozenset({"root", "coaras", "aide", "janitor", "daily"})

# 历史 agent_kind 写法（改名前的落盘值）：归档里的老记录不能因为改名就从统计里消失
# ——「参谋」是 aide 在 2026-08-24 更名前的名字，它的前身是 shadow。
_LEGACY_AGENT_KIND_ALIASES: dict[str, str] = {
    "参谋": "aide",
    "shadow": "aide",
}

_SUBAGENT_KINDS: frozenset[str] = frozenset({"coaras", "aide", "janitor", "daily"})

AGENT_KIND_LABELS: dict[str, str] = {
    "root": "主会话",
    "coaras": "coaras",
    "aide": "aide",
    "janitor": "janitor",
    "daily": "daily",
}


def parse_agent_kind(value: Any) -> str | None:
    """Return a known agent_kind, or ``None`` if missing / invalid.

    历史别名（改名前的写入值）按同一归因计入——老记录属于哪个智能体是确定的，
    不能因为后来改名就整段掉出统计。
    """
    kind = str(value or "").strip().lower()
    if kind in KNOWN_AGENT_KINDS:
        return kind
    return _LEGACY_AGENT_KIND_ALIASES.get(kind)


def resolve_agent_kind(*, persona: str = "", user_facing: bool = False) -> str:
    """Derive agent_kind for a live coara (write path only)."""
    persona_name = str(persona or "").strip().lower()
    if persona_name in _SUBAGENT_KINDS:
        return persona_name
    return "root"


def attribution_from_coara(coara: Any) -> dict[str, Any]:
    """Build attribution fields for ``llm_turn_complete`` / usage JSONL."""
    persona_obj = getattr(getattr(coara, "identity", None), "persona", None)
    persona = str(getattr(persona_obj, "name", "") or "")
    identity = getattr(coara, "identity", None)
    user_facing = bool(getattr(identity, "user_facing", False))

    workspace_dir = Path(getattr(coara, "workspace_dir", Path.cwd()))
    try:
        workspace_id = workspace_id_for(workspace_dir)
    except Exception:
        workspace_id = ""

    workspace_name = ""
    wm = getattr(coara, "workspace_manager", None)
    if wm is not None:
        try:
            matched = wm.match_path_to_workspace_id(workspace_dir)
            if matched:
                workspace_id = matched
                entry = wm.registry.get_by_id(matched)
                if entry is not None:
                    workspace_name = str(getattr(entry, "name", "") or "")
        except Exception:
            logger.debug(
                f"usage attribution: match workspace for {workspace_dir} failed; degrade to empty", exc_info=True
            )

    return {
        "agent_kind": resolve_agent_kind(persona=persona, user_facing=user_facing),
        "persona": persona,
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "workspace_dir": str(workspace_dir),
    }


__all__ = [
    "AGENT_KIND_LABELS",
    "KNOWN_AGENT_KINDS",
    "attribution_from_coara",
    "parse_agent_kind",
    "resolve_agent_kind",
]
