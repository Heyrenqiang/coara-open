"""coara 自持 flow 内核的语义护栏（golden，不依赖 wdl-engine）。

背景（2026-09-10）：coara 的内嵌 flow 能力原先直接 import ``wdl.core``，而 wdl-engine
是**独立软件**（自带 CLI、独立发布），对 coara 应为可选；于是把
``wdl/src/wdl/core/{model,semantics,serde}.py`` 搬进 ``src/workflow/core/``，代价是同一份
图语义分叉成两份。图语义静默漂移比信封协议漂移危险——后者漏了能看见，前者只会在运行时
表现成「某个工作流偶尔卡住不往下走」。

护栏分两层：

1. **golden（始终生效）**：``tests/data/core_golden.json`` 固化了一批图的正确行为
   （解析结构、canonical 序列化、校验结果、边分类、激活引擎簿记）。它不依赖
   wdl-engine —— coara 独立运行（用户机常态）时照样有护栏。语义有意变更时用
   ``python scripts/dev/gen_core_golden.py`` 重新生成，并在提交信息里写明缘由。
2. **oracle（装了 wdl-engine 才有）**：把同一批图的 wdl 结果对照**同一份** golden，
   不一致则 fail，提示人工判断该同步哪一边。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from src.workflow.core import (
    ActivationEngine,
    emit_graph,
    parse_graph,
    validate_graph,
)
from src.workflow.core.semantics import classify_edges

REPO = Path(__file__).resolve().parents[2]
GOLDEN_PATH = REPO / "tests" / "data" / "core_golden.json"
GOLDEN: list[dict] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
_CASES = [(case["label"], case["graph_text"], case["expected"]) for case in GOLDEN]
_IDS = [case[0] for case in _CASES]


def _activations(graph: object) -> list[list[object]]:
    """一轮确定性驱动，把引擎簿记序列化出来比对。

    与 ``scripts/dev/gen_core_golden.py`` 里的同名函数必须同构——改一处就得改另一处。
    """
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


def _observe(parse, emit, validate, classify, graph_text: str) -> dict[str, object]:
    """把一份图的全部可观察行为收成一个 dict（golden 的形状）。"""
    graph = parse(graph_text)
    return {
        "nodes": list(graph.nodes),
        "edges": [[e.frm, e.to, e.on] for e in graph.edges],
        "name": graph.name,
        "max_activations": graph.max_activations,
        "settings": graph.settings,
        "schedule": graph.schedule,
        "emit": emit(graph),
        "validate": [[i.level, i.message] for i in validate(graph)],
        "classify": {str(k): v for k, v in classify(graph).items()},
        "activations": _activations(graph),
    }


def _canon(obj: object) -> object:
    """经一次 JSON 往返再比较：golden 是 JSON（tuple 已落成 list），而实测的引擎
    簿记里仍有 tuple（如 snapshot 的 queues 值），不归一就永远比不平。"""
    return json.loads(json.dumps(obj, ensure_ascii=False))


@pytest.mark.parametrize("label,graph_text,expected", _CASES, ids=_IDS)
def test_self_hosted_core_matches_golden(label: str, graph_text: str, expected: dict) -> None:
    """自持内核必须复现 golden —— 这一层不依赖 wdl-engine，任何环境都跑。"""
    observed = _observe(parse_graph, emit_graph, validate_graph, classify_edges, graph_text)
    assert _canon(observed) == expected, label


def _wdl_modules():
    """装了 wdl-engine 才返回模块，否则 None。"""
    if importlib.util.find_spec("wdl.core") is None:
        return None
    if importlib.util.find_spec("wdl.core.semantics") is None:
        return None
    import wdl.core
    import wdl.core.semantics

    return wdl.core, wdl.core.semantics


@pytest.mark.parametrize("label,graph_text,expected", _CASES, ids=_IDS)
def test_wdl_engine_matches_same_golden(label: str, graph_text: str, expected: dict) -> None:
    """有 oracle 时，同一批图的 wdl 结果也要对上同一份 golden（漂移哨兵）。"""
    mods = _wdl_modules()
    if mods is None:
        pytest.skip("wdl-engine 未安装 —— golden 层已覆盖，本层仅在有 oracle 时生效")
    wdl_core, wdl_semantics = mods
    observed = _observe(
        wdl_core.parse_graph,
        wdl_core.emit_graph,
        wdl_core.validate_graph,
        wdl_semantics.classify_edges,
        graph_text,
    )
    assert _canon(observed) == expected, label


def test_self_hosted_core_has_no_wdl_dependency() -> None:
    """coara 的 src/ 下不得再出现任何 wdl import（解耦不变量）。"""
    hits = subprocess.run(
        ["grep", "-rn", "--include=*.py", "-E", r"^\s*(from|import)\s+wdl\b", "src"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert hits == "", f"src/ 仍依赖 wdl：\n{hits}"
