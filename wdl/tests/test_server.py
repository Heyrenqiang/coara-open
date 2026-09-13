"""WdlWorkbenchServer handler 全链路测试：文件 CRUD、parse/emit、run/list/get/cancel、WS 事件转发。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wdl.engine import WdlEngine
from wdl.server import WdlWorkbenchServer

WDL_TWO_NODES = """\
name: demo
nodes:
  a:
    task: 第一步
  b:
    task: 第二步
    input: "{{steps.a.text}}"
edges:
  - from: a
    to: b
"""


@pytest.fixture
async def client(tmp_path):
    """真实引擎 + 假 LLM 节点执行器的服务端测试客户端。"""

    async def fake_executor(*, node_id: str, prompt: str, task: str) -> str:
        await asyncio.sleep(0)
        return f"{node_id}-output"

    def factory(graph_llm, node_llms, node_tools=None, tool_settings=None):
        return fake_executor

    files_root = tmp_path / "files"
    files_root.mkdir(parents=True, exist_ok=True)
    engine = WdlEngine(db_path=tmp_path / "instances.db", node_executor_factory=factory)
    server = WdlWorkbenchServer(port=0, root_dir=tmp_path / "files", engine=engine, static_dir=tmp_path / "no-dist")
    # 测试不起 TCP 监听：直接把 app 挂到 aiohttp 测试设施上，手工复现 start() 的引擎侧初始化
    server._register_routes()
    engine.on_event(server._on_engine_event)
    await engine.start()
    test_client = TestClient(TestServer(server.app))
    await test_client.start_server()
    try:
        yield test_client, tmp_path / "files"
    finally:
        await test_client.close()
        await engine.shutdown()


async def test_file_crud(client):
    cli, files_root = client

    resp = await cli.get("/api/files")
    assert resp.status == 200
    assert (await resp.json())["files"] == []

    resp = await cli.post("/api/files", json={"name": "flow-a"})
    assert resp.status == 201
    body = await resp.json()
    assert body["name"] == "flow-a.wdl"
    assert "name:" in body["wdl"]

    resp = await cli.post("/api/files", json={"name": "flow-a"})
    assert resp.status == 409

    resp = await cli.get("/api/files/flow-a.wdl")
    assert resp.status == 200

    resp = await cli.put("/api/files/flow-a.wdl", json={"wdl": WDL_TWO_NODES})
    assert resp.status == 200
    assert (await resp.json())["saved"] is True
    assert (files_root / "flow-a.wdl").read_text(encoding="utf-8") == WDL_TWO_NODES

    resp = await cli.get("/api/files")
    names = [f["name"] for f in (await resp.json())["files"]]
    assert names == ["flow-a.wdl"]

    resp = await cli.delete("/api/files/flow-a.wdl")
    assert resp.status == 200
    resp = await cli.get("/api/files/flow-a.wdl")
    assert resp.status == 404


async def test_file_name_guard(client):
    cli, _ = client
    resp = await cli.post("/api/files", json={"name": "../evil"})
    assert resp.status == 400
    resp = await cli.get("/api/files/..%2Fevil.wdl")
    assert resp.status in (400, 404)


async def test_wdl_parse_and_emit_roundtrip(client):
    cli, _ = client
    resp = await cli.post("/api/wdl/parse", json={"wdl": WDL_TWO_NODES})
    assert resp.status == 200
    document = (await resp.json())["document"]
    assert document["name"] == "demo"
    assert set(document["nodes"]) == {"a", "b"}
    assert document["edges"] == [{"from": "a", "to": "b"}]

    resp = await cli.post("/api/wdl/emit", json={"document": document})
    assert resp.status == 200
    emitted = (await resp.json())["wdl"]
    resp = await cli.post("/api/wdl/parse", json={"wdl": emitted})
    assert resp.status == 200
    assert (await resp.json())["document"]["nodes"].keys() == document["nodes"].keys()

    resp = await cli.post("/api/wdl/parse", json={"wdl": "not: [valid"})
    assert resp.status == 400


async def test_run_list_get_cancel(client):
    cli, files_root = client
    (files_root / "job.wdl").write_text(WDL_TWO_NODES, encoding="utf-8")

    resp = await cli.post("/api/instances/run", json={"file": "job.wdl"})
    assert resp.status == 200
    instance_id = (await resp.json())["instance_id"]

    # 等引擎跑完
    for _ in range(100):
        resp = await cli.get(f"/api/instances/{instance_id}")
        assert resp.status == 200
        view = await resp.json()
        if view["status"] in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(0.05)
    assert view["status"] == "completed"
    assert set(view["completed_steps"]) == {"a", "b"}

    resp = await cli.get("/api/instances")
    assert resp.status == 200
    instances = (await resp.json())["instances"]
    assert any(i["instance_id"] == instance_id for i in instances)

    resp = await cli.get("/api/instances/wf-no-such")
    assert resp.status == 404

    # 缺 wdl/file → 400；取消已完成实例 → 幂等 ok
    resp = await cli.post("/api/instances/run", json={})
    assert resp.status == 400
    resp = await cli.post(f"/api/instances/{instance_id}/cancel")
    assert resp.status == 200

    # 取消不存在实例 → 404
    resp = await cli.post("/api/instances/wf-no-such/cancel")
    assert resp.status == 404


async def test_ws_forwards_engine_events(client):
    cli, _ = client
    received: list[dict[str, Any]] = []
    ws = await cli.ws_connect("/ws")
    try:
        resp = await cli.post("/api/instances/run", json={"wdl": WDL_TWO_NODES})
        assert resp.status == 200
        instance_id = (await resp.json())["instance_id"]

        async def collect() -> None:
            async for msg in ws:
                if msg.type.name != "TEXT":
                    break
                data = json.loads(msg.data)
                if data.get("instance_id") == instance_id:
                    received.append(data)

        collector = asyncio.create_task(collect())
        for _ in range(100):
            if any(m["type"] == "workflow_completed" for m in received):
                break
            await asyncio.sleep(0.05)
        types = [m["type"] for m in received]
        assert types[0] == "workflow_started"
        assert types.count("node_started") == 2
        assert types.count("node_completed") == 2
        assert types[-1] == "workflow_completed"
        payload = received[-1]["payload"]
        assert payload["context"]["status"] == "completed"
        collector.cancel()
    finally:
        await ws.close()
