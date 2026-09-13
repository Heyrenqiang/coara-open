"""端到端演示：单进程内 fake LLM server + 真实 wdl.cli 全链路。

fake server 与 CLI 跑在同一 asyncio loop（aiohttp AppRunner 随机端口），
CLI 事件打印走 on_event 全量透传，最后断言 note.txt 落盘与 ✓ 工具行输出。

用法：.venv\\Scripts\\python.exe demo_tool_loop.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

WDL_DIR = Path(__file__).resolve().parent


async def _start_fake_llm() -> tuple[object, int, dict]:
    from aiohttp import web

    state = {"calls": 0}

    async def chat(request: web.Request) -> web.Response:
        await request.json()
        state["calls"] += 1
        if state["calls"] == 1:
            body = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": json.dumps(
                                            {"path": "note.txt", "content": "图即工作流。"}, ensure_ascii=False
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        else:
            body = {"choices": [{"message": {"role": "assistant", "content": "已写入 note.txt：图即工作流。"}}]}
        return web.json_response(body)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port, state


async def main() -> int:
    os.environ["PYTHONIOENCODING"] = "utf-8"
    workdir = (WDL_DIR / ".demo_out").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    for stale in ("note.txt", "instances.db"):
        with contextlib.suppress(OSError):
            (workdir / stale).unlink()

    runner, port, state = await _start_fake_llm()
    try:
        (workdir / "providers.yaml").write_text(
            "default: fake\n"
            "providers:\n"
            "  fake:\n"
            "    type: openai\n"
            f"    base_url: http://127.0.0.1:{port}/v1\n"
            "    api_key: fake-key\n"
            "    models: [fake-model]\n",
            encoding="utf-8",
        )
        os.environ["WDL_HOME"] = str(workdir)
        wdl_file = workdir / "write_note.wdl"
        wdl_file.write_text((WDL_DIR / "examples" / "write_note.wdl").read_text(encoding="utf-8"), encoding="utf-8")

        # 直接调 cli 的 async 入口（与 `wdl run <file>` 同一链路）
        sys.path.insert(0, str(WDL_DIR / "src"))
        from wdl.cli import _cmd_run

        rc = await _cmd_run(wdl_file, {})
    finally:
        await runner.cleanup()

    note = (workdir / "note.txt").read_text(encoding="utf-8")
    assert note == "图即工作流。", note
    assert state["calls"] == 2, f"fake LLM 被调 {state['calls']} 次（期望 2 拍）"
    print(f"✓ 演示通过：fake LLM 两拍（tool_calls → 终稿），write_file 落盘 {workdir / 'note.txt'}")
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
