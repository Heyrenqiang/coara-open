"""LLM 调用磁盘镜像读取器 — devtools 专用，不依赖运行中的 coara 进程。

直接读各工作空间 ``<workspace>/.coara/llm/llm-calls.jsonl``（append-only JSONL，
每智能体实例一行全文 entry），以及 coara_home 级伪键镜像（flow / daily）。
按 workspace × agent 归并出「每实例最后一轮」，供 LLM log 查看器展示。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 与 src/coara/llmlog.py 保持一致的镜像布局与淘汰规则（单一事实源在写侧，
# 读侧复制常量以免 import coara 运行态——devtools 要脱离 agent 进程独立可跑）。
_MIRROR_SUBDIR = Path(".coara") / "llm"
_MIRROR_FILE_NAME = "llm-calls.jsonl"
_MIRROR_MAX_AGE_DAYS = 7
_MAX_MIRROR_AGENTS = 50
_SUMMARY_USER_INPUT_CHARS = 200

# 伪工作空间键（与 llmlog 写侧一致）：flow / daily 的镜像落在 coara_home/.coara/llm/
FLOW_WORKSPACE_KEY = "工作流（独立）"
DAILY_WORKSPACE_KEY = "日报（全局）"
_PSEUDO_SLUGS = {"flow": FLOW_WORKSPACE_KEY, "daily": DAILY_WORKSPACE_KEY}


def _ts(entry: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(entry.get("ts") or "")).timestamp()
    except ValueError:
        return 0.0


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        # 旧格式（append-only 摘要行，无全文）不认，与写侧口径一致
        if "response" not in entry and "conversation" not in entry:
            continue
        entries.append(entry)
    return entries


def _prune_agents(agents: dict[str, dict[str, Any]]) -> None:
    """超龄 + 超实例数删最旧（与写侧 _prune_agents_map 同规则）。"""
    cutoff = datetime.now(UTC).timestamp() - _MIRROR_MAX_AGE_DAYS * 86400
    for aid in [aid for aid, e in agents.items() if _ts(e) < cutoff]:
        agents.pop(aid, None)
    if len(agents) > _MAX_MIRROR_AGENTS:
        ordered = sorted(agents.items(), key=lambda kv: _ts(kv[1]))
        for aid, _ in ordered[: len(agents) - _MAX_MIRROR_AGENTS]:
            agents.pop(aid, None)


def _entries_for(mirror_path: Path) -> dict[str, dict[str, Any]]:
    """读一个镜像文件，按 agent_id 归并（同实例后出现覆盖先前），淘汰后返回。"""
    agents: dict[str, dict[str, Any]] = {}
    for entry in _read_jsonl(mirror_path):
        agent_id = str(entry.get("agent_id") or "unknown")
        agents[agent_id] = entry
    _prune_agents(agents)
    return agents


def collect_latest(coara_home: Path, workspace_paths: list[tuple[str, Path]]) -> dict[str, dict[str, Any]]:
    """汇总全部工作空间 + 伪键镜像的每实例最后一轮 entry。

    ``workspace_paths`` 为 (显示名, 物理路径) 列表（来自 workspaces.yaml）。
    返回 ``{workspace_key: {agent_id: entry}}``，workspace_key 物理空间用
    resolved 路径字符串，伪键用原键（FLOW/DAILY_WORKSPACE_KEY）。
    """
    result: dict[str, dict[str, Any]] = {}
    for _name, ws_path in workspace_paths:
        key = str(ws_path.resolve())
        agents = _entries_for(ws_path / _MIRROR_SUBDIR / _MIRROR_FILE_NAME)
        if agents:
            result[key] = agents
    # 伪键镜像：coara_home/.coara/llm/llm-calls-{flow,daily}.jsonl
    pseudo_dir = Path(coara_home) / _MIRROR_SUBDIR
    for slug, ws_key in _PSEUDO_SLUGS.items():
        agents = _entries_for(pseudo_dir / f"llm-calls-{slug}.jsonl")
        if agents:
            result[ws_key] = agents
    return result


def collect_tool_names(entry: dict[str, Any]) -> list[str]:
    """从镜像全文提取实际调用过的工具名（保序去重）。

    来源：conversation 各小轮的 assistant.tool_calls 与 tool_results，以及
    response 中尚未写入 conversation 的当前小轮 tool_calls。
    """
    names: list[str] = []
    seen: set[str] = set()

    def _add(name: Any) -> None:
        n = str(name or "").strip()
        if n and n not in seen:
            seen.add(n)
            names.append(n)

    conversation = entry.get("conversation")
    if isinstance(conversation, list):
        for round_obj in conversation:
            if not isinstance(round_obj, dict):
                continue
            for it in round_obj.get("iterations") or []:
                if not isinstance(it, dict):
                    continue
                assistant = it.get("assistant") or {}
                if isinstance(assistant, dict):
                    for call in assistant.get("tool_calls") or []:
                        if isinstance(call, dict):
                            _add(call.get("name"))
                for result in it.get("tool_results") or []:
                    if isinstance(result, dict):
                        _add(result.get("tool"))
    response = entry.get("response")
    if isinstance(response, dict):
        for call in response.get("tool_calls") or []:
            if isinstance(call, dict):
                _add(call.get("name"))
    return names


def summarize(entry: dict[str, Any]) -> dict[str, Any]:
    """全文 entry → 列表摘要（不含大字段）。"""
    user_input = str(entry.get("user_input") or "")
    if len(user_input) > _SUMMARY_USER_INPUT_CHARS:
        user_input = user_input[:_SUMMARY_USER_INPUT_CHARS] + "…"
    return {
        "agent_id": entry.get("agent_id") or "unknown",
        "kind": entry.get("kind") or entry.get("agent_id") or "unknown",
        "overview": entry.get("overview") or entry.get("kind") or entry.get("agent_id") or "unknown",
        "ts": entry.get("ts"),
        "model": entry.get("model"),
        "provider_name": entry.get("provider_name"),
        "llm_call": entry.get("llm_call") or 0,
        "tool_call_count": entry.get("tool_call_count") or 0,
        "session_id": entry.get("session_id"),
        "turn_id": entry.get("turn_id"),
        "user_input": user_input,
        "has_error": bool(entry.get("error")),
        "tool_count": entry.get("tool_count") or 0,
        "tool_names": collect_tool_names(entry),
    }
