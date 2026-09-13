"""Latest-LLM-call 镜像写盘：每工作空间 × 每智能体实例最后一轮全文落盘。"""

from __future__ import annotations

import json
from pathlib import Path

from src.coara.llmlog import (
    _MAX_MIRROR_AGENTS,
    DAILY_WORKSPACE_KEY,
    FLOW_WORKSPACE_KEY,
    Meta,
    build_agent_overview,
    log_llm_call,
)
from src.core.types import Message, MessageRole, ToolCall
from src.llm.provider import LLMResponse


def _mirror_file(ws: str) -> Path:
    return Path(ws) / ".coara" / "llm" / "llm-calls.jsonl"


def _read_entries(ws: str) -> dict[str, dict]:
    path = _mirror_file(ws)
    if not path.is_file():
        return {}
    agents: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(entry, dict) and ("response" in entry or "conversation" in entry):
            agents[str(entry.get("agent_id") or "unknown")] = entry
    return agents


def _log_call(ws: str, agent_id: str, content: str, *, llm_call: int = 1) -> None:
    log_llm_call(
        ws,
        Meta(agent_id=agent_id, agent_kind=agent_id, llm_call=llm_call, user_input=content),
        system_prompt="",
        messages=[Message(role=MessageRole.USER, content=content)],
        model="m",
        max_tokens=1,
        temperature=0.0,
        tools=[],
        response=LLMResponse(content=content, finish_reason="stop"),
        call_err=None,
    )


def test_log_llm_call_conversation_layout(tmp_path) -> None:
    ws = str(tmp_path / "nx")
    messages = [
        Message(role=MessageRole.SYSTEM, content="dup"),
        Message(role=MessageRole.USER, content="<系统消息>\n环境上下文"),
        Message(role=MessageRole.USER, content="几点钟了"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="tc1", name="shell", arguments={"command": "date"})],
        ),
        Message(role=MessageRole.TOOL_RESULT, name="shell", tool_call_id="tc1", content="Mon Jun  9 15:00:00 CST 2026"),
    ]
    response = LLMResponse(
        content="现在是下午3点。",
        finish_reason="stop",
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    log_llm_call(
        ws,
        Meta(
            turn_id="turn-1",
            user_input="几点钟了",
            llm_call=2,
            tool_call_count=3,
            provider_name="xiaomi",
            agent_id="root",
            agent_kind="主会话",
            overview="主会话",
        ),
        system_prompt="sys",
        messages=messages,
        model="test-model",
        max_tokens=100,
        temperature=0.5,
        tools=[{"name": "shell", "description": "run shell", "parameters": {}}],
        response=response,
        call_err=None,
    )

    entry = _read_entries(ws)["root"]
    assert entry["user_input"] == "几点钟了"
    assert entry["llm_call"] == 2
    assert entry["tool_call_count"] == 3
    assert entry["kind"] == "主会话"
    assert entry["model"] == "test-model"
    assert entry["system_prompt"] == "sys"
    assert entry["response"]["content"] == "现在是下午3点。"
    assert entry["response"]["usage"]["prompt_tokens"] == 10
    assert entry["tool_count"] == 1

    rounds = entry["conversation"]
    assert len(rounds) == 2
    assert rounds[0]["context"] is True
    assert rounds[1]["user"] == "几点钟了"
    iterations = rounds[1]["iterations"]
    assert iterations[0]["assistant"]["tool_calls"][0]["name"] == "shell"
    assert iterations[0]["tool_results"][0]["tool"] == "shell"


def test_parallel_subagents_each_kept(tmp_path) -> None:
    """并行同类型分身按实例各自保留，互不覆盖。"""
    ws = str(tmp_path / "v8")
    for i in range(4):
        _log_call(ws, f"sa-coaras-x{i}", f"分身{i}回复")
    agents = _read_entries(ws)
    for i in range(4):
        assert agents[f"sa-coaras-x{i}"]["response"]["content"] == f"分身{i}回复"


def test_same_agent_overwrites_keeps_last(tmp_path) -> None:
    ws = str(tmp_path / "v8")
    _log_call(ws, "root", "第一轮", llm_call=1)
    _log_call(ws, "root", "第二轮", llm_call=2)
    entry = _read_entries(ws)["root"]
    assert entry["response"]["content"] == "第二轮"
    assert entry["llm_call"] == 2


def test_log_llm_call_records_error(tmp_path) -> None:
    ws = str(tmp_path / "ws")
    log_llm_call(
        ws,
        Meta(agent_id="root", llm_call=1),
        system_prompt="",
        messages=[Message(role=MessageRole.USER, content="hi")],
        model="m",
        max_tokens=1,
        temperature=0.0,
        tools=[],
        response=None,
        call_err=RuntimeError("provider down"),
    )
    assert _read_entries(ws)["root"]["error"] == "provider down"


def test_empty_workspace_dir_ignored(tmp_path) -> None:
    log_llm_call(
        "",
        Meta(),
        system_prompt="",
        messages=[Message(role=MessageRole.USER, content="hi")],
        model="m",
        max_tokens=1,
        temperature=0.0,
        tools=[],
        response=None,
        call_err=None,
    )
    # 空 workspace_dir 直接 return，不落任何文件
    assert not _mirror_file(str(tmp_path)).exists()


def test_unknown_agent_fallback(tmp_path) -> None:
    log_llm_call(
        str(tmp_path),
        Meta(),
        system_prompt="",
        messages=[Message(role=MessageRole.USER, content="hi")],
        model="m",
        max_tokens=1,
        temperature=0.0,
        tools=[],
        response=None,
        call_err=None,
    )
    assert "unknown" in _read_entries(str(tmp_path))


def test_build_agent_overview_labels() -> None:
    assert build_agent_overview(user_facing=True) == "主会话"
    coaras = build_agent_overview(user_facing=False, persona_name="coaras", background=False, bound_tool_names=[])
    assert coaras == "前台 · coaras · 可编辑"
    aide = build_agent_overview(
        user_facing=False, persona_name="aide", background=True, bound_tool_names=["read", "grep"]
    )
    assert aide == "后台 · aide · 只读"
    janitor = build_agent_overview(
        user_facing=False, persona_name="janitor", background=True, bound_tool_names=["read", "write"]
    )
    assert janitor == "后台 · janitor · 可编辑"


def test_mirror_prunes_by_agent_count(tmp_path) -> None:
    ws = str(tmp_path / "ws")
    for i in range(_MAX_MIRROR_AGENTS + 5):
        _log_call(ws, f"sa-x{i}", f"第{i}轮")

    mirror = _mirror_file(ws)
    lines = [ln for ln in mirror.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == _MAX_MIRROR_AGENTS  # 超上限删最旧

    agents = _read_entries(ws)
    assert len(agents) == _MAX_MIRROR_AGENTS
    assert "sa-x0" not in agents  # 最旧的被淘汰
    assert f"sa-x{_MAX_MIRROR_AGENTS + 4}" in agents


def test_mirror_prunes_by_age(tmp_path) -> None:
    ws = str(tmp_path / "ws")
    _log_call(ws, "root", "新")
    # 塞一条 8 天前的旧条目进镜像，再写一条新的触发淘汰
    mirror = _mirror_file(ws)
    old = {
        "ts": "2020-01-01T00:00:00+00:00",
        "agent_id": "sa-old",
        "kind": "coaras",
        "user_input": "旧",
        "llm_call": 1,
        "tool_call_count": 0,
        "model": "m",
        "tool_count": 0,
        "response": {"content": "旧"},
    }
    with open(mirror, "a", encoding="utf-8") as f:
        f.write(json.dumps(old, ensure_ascii=False) + "\n")
    _log_call(ws, "root", "触发淘汰")
    agents = _read_entries(ws)
    assert "root" in agents
    assert "sa-old" not in agents  # 超龄被淘汰


def test_mirror_bad_lines_skipped(tmp_path) -> None:
    ws = str(tmp_path / "ws")
    _log_call(ws, "root", "正常")
    mirror = _mirror_file(ws)
    with open(mirror, "a", encoding="utf-8") as f:
        f.write("{bad json\n")
    # 坏行不炸后续写入
    _log_call(ws, "root", "第二轮")
    assert _read_entries(ws)["root"]["user_input"] == "第二轮"


def test_legacy_summary_lines_discarded(tmp_path) -> None:
    """旧格式摘要行（无全文）不认：新写入后随之清掉。"""
    ws = str(tmp_path / "ws")
    mirror = _mirror_file(ws)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    legacy = {"ts": "2026-08-20T00:00:00+00:00", "agent_id": "root", "user_input": "旧摘要", "llm_call": 1}
    mirror.write_text(json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")
    _log_call(ws, "root", "新全文")
    lines = [ln for ln in mirror.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    assert _read_entries(ws)["root"]["user_input"] == "新全文"


def test_pseudo_workspace_keys_persist_full_detail(tmp_path, monkeypatch) -> None:
    """FlowRoot / daily 伪键：落盘到 coara_home 级目录。"""
    monkeypatch.setenv("COARA_HOME", str(tmp_path / "home"))
    for key, agent_id in ((FLOW_WORKSPACE_KEY, "root"), (DAILY_WORKSPACE_KEY, "sa-daily-x1")):
        _log_call(key, agent_id, f"{key} 回复")

    def read_pseudo(slug: str) -> dict[str, dict]:
        path = tmp_path / "home" / ".coara" / "llm" / f"llm-calls-{slug}.jsonl"
        agents: dict[str, dict] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                agents[str(entry.get("agent_id"))] = entry
        return agents

    assert read_pseudo("flow")["root"]["response"]["content"] == "工作流（独立） 回复"
    assert read_pseudo("daily")["sa-daily-x1"]["response"]["content"] == "日报（全局） 回复"


def test_iteration_usages_accumulate_across_calls(tmp_path) -> None:
    """同会话连续 LLM 调用：iteration_usages 累加，历史小轮 usage 戳回 conversation。"""
    ws = str(tmp_path / "cost")
    sid = "sess-cost-1"

    # 第 1 小轮：仅用户消息 → response（尚不进 conversation）
    log_llm_call(
        ws,
        Meta(session_id=sid, agent_id="root", agent_kind="主会话", llm_call=1, user_input="hi"),
        system_prompt="sys",
        messages=[Message(role=MessageRole.USER, content="hi")],
        model="m",
        max_tokens=1,
        temperature=0.0,
        tools=[],
        response=LLMResponse(
            content="",
            finish_reason="tool_calls",
            tool_calls=[ToolCall(id="t1", name="shell", arguments={"command": "ls"})],
            usage={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        ),
        call_err=None,
    )
    e1 = _read_entries(ws)["root"]
    assert e1["iteration_usages"] == [
        {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
    ]
    assert _count_iters(e1["conversation"]) == 0

    # 第 2 小轮：历史里已有 assistant+tool，再要最终回复
    messages = [
        Message(role=MessageRole.USER, content="hi"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="t1", name="shell", arguments={"command": "ls"})],
        ),
        Message(role=MessageRole.TOOL_RESULT, name="shell", tool_call_id="t1", content="ok"),
    ]
    log_llm_call(
        ws,
        Meta(session_id=sid, agent_id="root", agent_kind="主会话", llm_call=2, user_input="hi"),
        system_prompt="sys",
        messages=messages,
        model="m",
        max_tokens=1,
        temperature=0.0,
        tools=[],
        response=LLMResponse(
            content="done",
            finish_reason="stop",
            usage={"input_tokens": 200, "output_tokens": 20, "total_tokens": 220},
        ),
        call_err=None,
    )
    e2 = _read_entries(ws)["root"]
    assert len(e2["iteration_usages"]) == 2
    assert e2["iteration_usages"][0]["prompt_tokens"] == 100
    assert e2["iteration_usages"][1]["prompt_tokens"] == 200
    # 历史小轮已戳到 conversation；当前 response 仍是最新 usage
    hist = e2["conversation"][0]["iterations"][0]
    assert hist["usage"]["prompt_tokens"] == 100
    assert e2["response"]["usage"]["prompt_tokens"] == 200


def _count_iters(conversation: list) -> int:
    return sum(len(r.get("iterations") or []) for r in conversation)
