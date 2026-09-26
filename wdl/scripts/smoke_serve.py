"""wdl serve 手动冒烟：REST 断言 + WS 事件序列断言。

用法（在 wdl/ 下）：
    .venv/Scripts/python scripts/smoke_serve.py

步骤：
1. 子进程起 `wdl serve --port 8177 --root <临时目录>`（WDL_HOME 指向临时目录）
2. curl 语义断言：GET /api/instances → 空列表；GET / 返回 index.html
3. 把 examples/hello.wdl 拷进根目录，POST /api/instances/run {"file": ...}
4. aiohttp WS 客户端连 /ws，断言收到 workflow_started → node_started×2 →
   node_completed×2 → workflow_completed 事件序列
5. 断言 GET /api/instances 里该实例 completed
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import aiohttp
from aiohttp import web as aiohttp_web

ROOT = Path(__file__).resolve().parent.parent
PORT = 18179
BASE = f"http://127.0.0.1:{PORT}"

# 假 LLM 网关：OpenAI 兼容 /chat/completions 直接回固定文本
FAKE_REPLY = "WDL 是面向智能体编排的工作流定义语言。"


async def fake_llm_gateway(port: int) -> aiohttp_web.AppRunner:
    async def chat(request: aiohttp_web.Request) -> aiohttp_web.Response:
        return aiohttp_web.json_response(
            {
                "choices": [{"message": {"content": FAKE_REPLY}}],
            }
        )

    app = aiohttp_web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    runner = aiohttp_web.AppRunner(app)
    await runner.setup()
    await aiohttp_web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


def http_json(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wdl-smoke-"))
    wdl_home = tmp / "home"
    wdl_home.mkdir(parents=True)
    llm_port = 18177
    (wdl_home / "providers.yaml").write_text(
        "default: fake\n"
        "providers:\n"
        "  fake:\n"
        "    type: openai\n"
        f"    base_url: http://127.0.0.1:{llm_port}/v1\n"
        "    api_key: fake\n"
        "    models:\n"
        "      - fake-model\n",
        encoding="utf-8",
    )
    shutil.copy(ROOT / "examples" / "hello.wdl", tmp / "hello.wdl")

    env = dict(os.environ, WDL_HOME=str(wdl_home))
    proc = subprocess.Popen(
        [sys.executable, "-m", "wdl.cli", "serve", "--port", str(PORT), "--root", str(tmp)],
        cwd=ROOT,
        env=env,
    )
    gateway = await fake_llm_gateway(llm_port)
    try:
        # 等服务就绪（同时确认子进程没有提前退出）
        for _ in range(50):
            if proc.poll() is not None:
                print(f"FAIL：serve 子进程已退出，exit={proc.returncode}")
                return 1
            try:
                code, _ = http_json("GET", f"{BASE}/api/instances")
                if code == 200:
                    break
            except OSError:
                pass
            await asyncio.sleep(0.2)
        else:
            print("FAIL：服务未在 10s 内就绪")
            return 1

        code, body = http_json("GET", f"{BASE}/api/instances")
        assert code == 200 and body.get("instances") == [], f"实例列表应为空：{body}"
        print("✓ GET /api/instances → 空列表")

        with urllib.request.urlopen(f"{BASE}/", timeout=10) as resp:
            html = resp.read().decode()
        assert '<div id="root">' in html, "index.html 未正确托管"
        print("✓ GET / → workbench dist index.html")

        async with aiohttp.ClientSession() as session, session.ws_connect(f"ws://127.0.0.1:{PORT}/ws") as ws:
            code, body = http_json("POST", f"{BASE}/api/instances/run", {"file": "hello.wdl"})
            assert code == 200 and body.get("instance_id"), f"run 失败：{body}"
            instance_id = body["instance_id"]
            print(f"✓ POST /api/instances/run → {instance_id}")

            events: list[dict] = []
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = json.loads(msg.data)
                if data.get("instance_id") != instance_id:
                    continue
                events.append(data)
                if data["type"] in ("workflow_completed", "workflow_failed"):
                    break

        types = [e["type"] for e in events]
        if "workflow_failed" in types or "node_failed" in types:
            for e in events:
                print("  事件：", e["type"], e.get("payload", {}).get("step_id"), e.get("payload", {}).get("error"))
        assert types[0] == "workflow_started", f"首事件应为 workflow_started：{types}"
        assert types.count("node_started") == 2, f"应两个 node_started：{types}"
        assert types.count("node_completed") == 2, f"应两个 node_completed：{types}"
        assert types[-1] == "workflow_completed", f"末事件应为 workflow_completed：{types}"
        started = [e["payload"]["step_id"] for e in events if e["type"] == "node_started"]
        assert started == ["draft", "polish"], f"节点顺序：{started}"
        print(f"✓ WS 事件序列：{' → '.join(types)}")

        code, body = http_json("GET", f"{BASE}/api/instances/{instance_id}")
        assert code == 200 and body["status"] == "completed", f"实例终态异常：{body}"
        print("✓ GET /api/instances/{id} → completed")

        code, body = http_json("GET", f"{BASE}/api/instances")
        assert any(i["instance_id"] == instance_id and i["status"] == "completed" for i in body["instances"])
        print("✓ GET /api/instances → 含已完成实例")

        print("\n冒烟通过")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        await gateway.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
