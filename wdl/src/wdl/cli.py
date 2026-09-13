"""wdl 命令行入口。

- ``wdl run <file.wdl> [--inputs k=v ...]``：解析并执行一个 WDL 文件，
  打印节点进度与最终结果
- ``wdl validate <file.wdl>``：只解析校验
- ``wdl serve [--port 8177] [--root DIR]``：起画布工作台 Web 服务
  （静态托管 workbench/dist + REST API + WS 事件转发）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import yaml

from wdl.core.semantics import validate_graph
from wdl.core.serde import parse_graph
from wdl.engine import WdlEngine
from wdl.logging import setup_logger


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wdl", description="独立 WDL 执行引擎")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="解析并执行一个 WDL 文件")
    p_run.add_argument("file", help="WDL 文件路径")
    p_run.add_argument(
        "--inputs",
        nargs="*",
        default=[],
        metavar="KEY=VALUE",
        help="运行输入（值按 YAML 标量解析）",
    )

    p_val = sub.add_parser("validate", help="只解析校验")
    p_val.add_argument("file", help="WDL 文件路径")

    p_serve = sub.add_parser("serve", help="起画布工作台 Web 服务（REST + WS + 静态站点）")
    p_serve.add_argument("--port", type=int, default=8177, help="监听端口（默认 8177）")
    p_serve.add_argument(
        "--root",
        default=None,
        help="WDL 文件浏览/读写根目录（默认当前目录）",
    )
    return parser


def _parse_inputs(pairs: list[str]) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--inputs 项缺少 '='：{pair!r}")
        key, _, raw = pair.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"--inputs 项 key 为空：{pair!r}")
        inputs[key] = yaml.safe_load(raw)
    return inputs


def _cmd_validate(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读取失败：{exc}")
        return 1
    try:
        graph = parse_graph(text)
    except ValueError as exc:
        print(f"解析失败：{exc}")
        return 1
    issues = validate_graph(graph)
    errors = [i for i in issues if i.level == "error"]
    for issue in issues:
        print(str(issue))
    if errors:
        print(f"校验失败：{len(errors)} 个错误")
        return 1
    print(f"校验通过：{graph.name}（{len(graph.nodes)} 节点 / {len(graph.edges)} 边）")
    return 0


async def _cmd_run(path: Path, inputs: dict[str, Any]) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读取失败：{exc}")
        return 1
    try:
        graph = parse_graph(text)  # 先解析，失败直接退出不建实例
    except ValueError as exc:
        print(f"解析失败：{exc}")
        return 1
    issues = validate_graph(graph)
    errors = [i.message for i in issues if i.level == "error"]
    if errors:
        for e in errors:
            print(f"[error] {e}")
        print("校验失败，未执行")
        return 1

    done = asyncio.Event()
    final: dict[str, Any] = {}
    task_id_box: list[str] = []

    async def on_event(event_type: str, payload: dict[str, Any]) -> None:
        tid = str(payload.get("task_id") or payload.get("instance_id") or "")
        if task_id_box and tid and tid != task_id_box[0]:
            return  # 只关注本次提交的实例（scanner 可能并发恢复别的实例）
        if event_type == "node_started":
            print(f"→ 节点开始：{payload.get('step_id')}")
        elif event_type == "tool_start":
            print(f"  ⚙ 工具调用：{payload.get('tool')} — {payload.get('arguments_summary')}")
        elif event_type == "tool_result":
            mark = "✓" if payload.get("ok") else "✗"
            print(f"  {mark} 工具结果：{payload.get('tool')} — {payload.get('result_summary')}")
        elif event_type == "node_completed":
            print(f"✓ 节点完成：{payload.get('step_id')}")
        elif event_type == "node_failed":
            print(f"✗ 节点失败：{payload.get('step_id')} — {payload.get('error')}")
        elif event_type == "workflow_completed" or event_type == "workflow_failed":
            final.update(payload)
            done.set()

    engine = WdlEngine(workdir=path.resolve().parent)
    engine.on_event(on_event)
    await engine.start()
    try:
        task_id = await engine.start_workflow(text, inputs)
        task_id_box.append(task_id)
        print(f"▶ 工作流开始：{graph.name}（实例 {task_id}）")
        await done.wait()
    finally:
        await engine.shutdown()

    if "error" in final and "context" not in final:
        print(f"✗ 工作流失败：{final.get('error')}")
        return 1
    context = final.get("context") or {}
    node_map = context.get("context") if isinstance(context.get("context"), dict) else context
    status = str(context.get("status") or "completed")
    print()
    for nid, value in node_map.items():
        if not isinstance(value, dict) or "text" not in value:
            continue
        print(f"── {nid} ──")
        print(str(value.get("text") or "").strip())
        print()
    if status == "completed":
        print(f"✓ 工作流完成：{graph.name}")
        return 0
    print(f"✗ 工作流结束状态：{status}")
    return 1


def _cmd_serve(port: int, root: str | None) -> int:
    from wdl.server import serve

    try:
        asyncio.run(serve(port=port, root_dir=root))
    except KeyboardInterrupt:
        print("已停止")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logger(verbose=args.verbose)
    if args.command == "serve":
        return _cmd_serve(args.port, args.root)
    path = Path(args.file)
    if args.command == "validate":
        return _cmd_validate(path)
    try:
        inputs = _parse_inputs(args.inputs)
    except ValueError as exc:
        print(str(exc))
        return 1
    try:
        return asyncio.run(_cmd_run(path, inputs))
    except KeyboardInterrupt:
        print("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
