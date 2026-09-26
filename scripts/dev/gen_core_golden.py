"""生成 coara 自持 flow 内核的 golden 期望值。

背景：coara 的 ``src/workflow/core/`` 是从 ``wdl/src/wdl/core/`` 搬来的副本，两处
语义会静默漂移。原先的护栏拿 wdl 当 oracle，wdl-engine 没装就整模块 skip——
而 coara 独立运行时正常就是没装，于是用户机上等于零护栏。

改法：把「同一批图的正确行为」固化成 golden（本脚本生成，随仓库提交），测试随时可比。
装了 wdl-engine 时，测试再额外把 wdl 的结果对照同一份 golden，继续当漂移哨兵。

用法（语义有意变更时重新生成，并在提交信息里写明为何）：
    python scripts/dev/gen_core_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path

from src.workflow.core import (
    ActivationEngine,
    emit_graph,
    parse_graph,
    validate_graph,
)
from src.workflow.core.semantics import classify_edges

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "tests" / "data" / "core_golden.json"


def _cases() -> list[tuple[str, str]]:
    """一批覆盖关键语义的 (说明, WDL 文本)。"""
    return [
        (
            "线性两节点",
            "name: linear\nnodes:\n  a:\n    task: A\n  b:\n    task: B\nedges:\n  - from: a\n    to: b\n",
        ),
        (
            "扇出 + 扇入",
            "name: fan\nnodes:\n  s:\n    task: S\n  x:\n    task: X\n  y:\n    task: Y\n"
            "  e:\n    task: E\n"
            "edges:\n  - from: s\n    to: x\n  - from: s\n    to: y\n"
            "  - from: x\n    to: e\n  - from: y\n    to: e\n",
        ),
        (
            "one 路由",
            "name: branch\nnodes:\n  s:\n    task: S\n    routes: one\n  x:\n    task: X\n"
            "  y:\n    task: Y\n"
            "edges:\n  - from: s\n    to: x\n  - from: s\n    to: y\n",
        ),
        (
            "回边（循环）",
            "name: loop\nnodes:\n  a:\n    task: A\n  b:\n    task: B\n"
            "edges:\n  - from: a\n    to: b\n  - from: b\n    to: a\n",
        ),
        (
            "error 边（重试路由）",
            "name: retry\nnodes:\n  a:\n    task: A\n  b:\n    task: B\n"
            "edges:\n  - from: a\n    to: b\n  - from: b\n    to: a\n    on: error\n",
        ),
        (
            "模板隐式依赖 + 节点级上限 + tools",
            "name: tmpl\nnodes:\n  a:\n    task: A\n"
            "  b:\n    task: B\n    input: '{{steps.a.text}}'\n    max_activations: 5\n"
            "    tools:\n      - read_file\n"
            "    provider: p\n    model: m\n"
            "edges:\n  - from: a\n    to: b\n",
        ),
        (
            "图级 settings 与 provider/model",
            "name: full\nprovider: pp\nmodel: mm\nmax_activations: 7\n"
            "settings:\n  max_tool_rounds: 3\n  shell_timeout: 1.5\n"
            "schedule:\n  kind: cron\n  expr: '* * * * *'\n"
            "description: 描述\nnodes:\n  a:\n    task: A\nedges: []\n",
        ),
        (
            "非法：routes 非法 + 边引用不存在",
            "name: bad\nnodes:\n  a:\n    task: A\n    routes: nope\nedges:\n  - from: a\n    to: ghost\n",
        ),
    ]


def _activations(graph: object) -> list[list[object]]:
    """跑一轮确定性驱动，把引擎簿记序列化成可比对的形状。"""
    engine = ActivationEngine(graph)  # type: ignore[arg-type]
    trace: list[list[object]] = []
    for nid in sorted(graph.nodes):  # type: ignore[attr-defined]
        trace.append([nid, engine.is_ready(nid), engine.can_activate(nid), engine.cap_of(nid)])
    for nid in sorted(graph.nodes):  # type: ignore[attr-defined]
        act = engine.consume(nid)
        if act is not None:
            trace.append([nid, "consumed", act.round_index, act.arrivals])
            for target in graph.routes_to(nid):  # type: ignore[attr-defined]
                engine.record_arrival(nid, target, "r")
    for nid in sorted(graph.nodes):  # type: ignore[attr-defined]
        trace.append([nid, "after", engine.is_ready(nid), engine.activations_of(nid)])
    trace.append(["snapshot", engine.snapshot()])
    return trace


def _expected(wdl_text: str) -> dict[str, object]:
    graph = parse_graph(wdl_text)
    return {
        "nodes": list(graph.nodes),
        "edges": [[e.frm, e.to, e.on] for e in graph.edges],
        "name": graph.name,
        "max_activations": graph.max_activations,
        "settings": graph.settings,
        "schedule": graph.schedule,
        "emit": emit_graph(graph),
        "validate": [[i.level, i.message] for i in validate_graph(graph)],
        "classify": {str(k): v for k, v in classify_edges(graph).items()},
        "activations": _activations(graph),
    }


def main() -> None:
    payload = [{"label": label, "graph_text": text, "expected": _expected(text)} for label, text in _cases()]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO)} ({len(payload)} cases)")


if __name__ == "__main__":
    main()
