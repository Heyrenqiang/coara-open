"""media 工具：文生图 / 图生图 / 文生视频 / 图生视频（Agnes），结果保存到本地。

Provider：仅 agnes（AGNES_API_KEY）。

视频生成走全局 FIFO 队列（``video_queue.VideoQueue``），由常驻
``video_scheduler.VideoScheduler`` **单飞**执行（同时只跑一条：提交→轮询→落盘，
再按 60s 节拍接下一条）；提交/生成失败一律重排队尾，重试超限判最终失败；
完成后自动落盘并通知。对 LLM 只暴露一个工具 ``media``，队列的查看
（status）与取消（cancel，会打断进行中的轮询）是它的内部 ``action``。
"""

from __future__ import annotations

import base64
import random
import string
import time
from pathlib import Path
from typing import Any

import httpx

from src.core.tool_base import ToolKind, ToolResult
from src.tools.builtin.file_io.workspace_tool_base import (
    WorkspaceBoundTool,
    WorkspaceBoundToolInvocation,
)
from src.tools.builtin.media import providers
from src.tools.builtin.media.video_queue import VideoQueue
from src.tools.builtin.media.video_scheduler import VideoScheduler

_EXT_BY_KIND = {"image": ".png", "video": ".mp4"}


def _auto_out_path(workspace_root: Path | None, out_dir: str | None, kind: str, provider: str) -> Path:
    base = Path(out_dir).expanduser() if out_dir else (workspace_root or Path.cwd()) / "media"
    if not base.is_absolute() and workspace_root is not None:
        base = workspace_root / base
    stamp = time.strftime("%Y%m%d_%H%M%S")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return base.resolve() / f"{kind}_{provider}_{stamp}_{rand}{_EXT_BY_KIND[kind]}"


def _parse_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class MediaToolInvocation(WorkspaceBoundToolInvocation):
    def __init__(
        self,
        params: dict[str, Any],
        workspace_root: Path | None = None,
        available_providers: set[str] | None = None,
    ):
        super().__init__(params, workspace_root)
        self.action = str(params.get("action") or "generate").strip().lower() or "generate"
        if self.action not in ("generate", "status", "cancel"):
            raise ValueError("action 仅支持 generate / status / cancel")
        self.task_id = str(params.get("task_id") or "").strip()

        # 通用字段（status 无需；cancel 只需 task_id）
        self.kind = str(params.get("kind") or "").strip().lower()
        self.prompt = str(params.get("prompt") or "").strip()
        self.provider = "agnes"
        self.out = None
        self.out_dir = str(params.get("out_dir") or "").strip() or None
        self.model = str(params.get("model") or "").strip() or None
        self.image_size = str(params.get("size") or "1024x1024").strip()
        raw_image = params.get("image")
        if isinstance(raw_image, list):
            self.images = [str(p) for p in raw_image]
        else:
            self.images = [str(raw_image)] if raw_image else []
        self.seconds = str(params.get("seconds") or "5").strip()
        self.aspect_ratio = str(params.get("aspect_ratio") or "16:9").strip()
        self.video_mode = str(params.get("mode") or "").strip() or None
        self.seed = params.get("seed")
        self.negative_prompt = params.get("negative_prompt")
        self.steps = _parse_int(params.get("steps"))
        self.guidance = params.get("guidance")
        self.timeout = float(params.get("timeout") or 600.0)
        self.return_image = bool(params.get("return_image", False))

        if self.action == "generate":
            if self.kind not in ("image", "video"):
                raise ValueError("参数 kind 必填：image 或 video")
            if not self.prompt:
                raise ValueError("参数 prompt 必填")
            avail = set(available_providers) if available_providers else None
            if avail is not None and "agnes" not in avail:
                raise ValueError("媒体生成仅支持 agnes，但未检测到 AGNES_API_KEY")
            out_raw = str(params.get("out") or "").strip()
            out = Path(out_raw).expanduser() if out_raw else None
            if out is not None and not out.is_absolute():
                out = (self._workspace_root / out).resolve() if self._workspace_root is not None else out.resolve()
            self.out = out

    def get_description(self) -> str:
        if self.action == "status":
            return "查看媒体生成队列"
        if self.action == "cancel":
            return f"取消媒体任务: {self.task_id}"
        return f"生成{('图片' if self.kind == 'image' else '视频')}: {self.prompt[:40]}"

    def _origin_source(self) -> str:
        from src.coara.turn_source import normalize_launch_source

        # executor 会在 invocation 上注入 origin_source（当前回合来源）；
        # 缺省时 normalize 为 cli——手机发起必须被记成 matrix，否则唤醒回不到手机。
        return normalize_launch_source(
            getattr(self, "origin_source", None) or getattr(self, "source", None)
        )

    def _coara_id(self) -> str:
        return getattr(self, "coara_id", "") or ""

    async def execute(self, signal=None) -> ToolResult:
        try:
            if self.action == "status":
                return self._queue_status()
            if self.action == "cancel":
                return self._queue_cancel()
            async with httpx.AsyncClient() as client:
                if self.kind == "image":
                    return await self._run_image(client)
                return self._run_video()
        except InterruptedError:
            return ToolResult.cancelled()
        except ValueError as exc:
            return ToolResult.error(str(exc))
        except httpx.HTTPError as exc:
            return ToolResult.error(f"网络请求失败: {exc}")

    # ── queue introspection / cancel ─────────────────────────────────────────

    def _queue_status(self) -> ToolResult:
        VideoScheduler().start()
        rows = VideoQueue().status()
        if not rows:
            return ToolResult.success("媒体生成队列为空。")
        lines = [f"媒体生成队列（{len(rows)} 条）："]
        for r in rows:
            line = (
                f"- {r['task_id']} [{r['status']}] 尝试 {r['attempts']}/{r['max_attempts']}"
                + (f" → {r['out']}" if r["out"] else "")
            )
            if r["error"]:
                line += f"（{r['error'][:60]}）"
            lines.append(line)
        return ToolResult.success("\n".join(lines), metadata={"kind": "video_queue"})

    def _queue_cancel(self) -> ToolResult:
        if not self.task_id:
            return ToolResult.error("action=cancel 缺少必填参数: task_id")
        # Scheduler cancel marks the queue + aborts in-flight poll HTTP loops.
        ok = VideoScheduler().cancel(self.task_id)
        if not ok:
            return ToolResult.error(f"未找到可取消的任务 '{self.task_id}'（或已终态）。")
        return ToolResult.success(f"已取消媒体任务 {self.task_id}。", metadata={"task_id": self.task_id})

    # ── image generation (Agnes only) ────────────────────────────────────────

    async def _run_image(self, client: httpx.AsyncClient) -> ToolResult:
        resolved = self.model or providers.DEFAULT_AGNES_IMAGE_MODEL
        url, b64 = await providers.agnes_image(
            client,
            prompt=self.prompt,
            model=resolved,
            size=self.image_size,
            images=self.images or None,
            timeout=self.timeout,
        )
        raw = base64.b64decode(b64) if b64 else None
        out = self.out or _auto_out_path(self._workspace_root, self.out_dir, "image", "agnes")
        metadata = await self._save_image_result(client, url=url, raw=raw, out=out, model=resolved)
        text = f"图片已生成并保存: {out}（{metadata['bytes']} 字节, agnes/{resolved}）"
        content: Any = text
        if self.return_image and raw is not None:
            try:
                from src.utils.multimodal_content import image_bytes_blocks_with_notice

                blocks = image_bytes_blocks_with_notice(raw, detail="high")
                if blocks:
                    content = [{"type": "text", "text": text}, *blocks]
            except Exception:
                from src.core.logger import logger

                logger.warning("media 生成图回注失败，仅回传文本", exc_info=True)
        return ToolResult.success(content, metadata=metadata)

    async def _save_image_result(
        self,
        client: httpx.AsyncClient,
        *,
        url: str | None,
        raw: bytes | None,
        out: Path,
        model: str,
    ) -> dict[str, Any]:
        if raw is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(raw)
            size_bytes = out.stat().st_size
        else:
            size_bytes = await providers.download_file(client, url or "", out)
        return {
            "kind": "image",
            "provider": "agnes",
            "model": model,
            "saved": str(out),
            "bytes": size_bytes,
            "url": url,
        }

    # ── video generation (queue + scheduler) ─────────────────────────────────

    def _run_video(self) -> ToolResult:
        model = self.model or providers.DEFAULT_AGNES_VIDEO_MODEL
        out = self.out or _auto_out_path(self._workspace_root, self.out_dir, "video", "agnes")
        task_id = VideoQueue().enqueue(
            self.prompt,
            {
                "model": model,
                "seconds": self.seconds,
                "aspect_ratio": self.aspect_ratio,
                "mode": self.video_mode,
                "images": self.images or None,
                "seed": self.seed,
                "timeout": self.timeout,
            },
            out=str(out),
            origin_source=self._origin_source(),
            description=f"视频生成: {self.prompt[:40]}",
            coara_id=getattr(self, "coara_id", "") or "",
            session_id=getattr(self, "session_id", "") or "",
        )
        VideoScheduler().start()
        return ToolResult.success(
            (
                f"视频已加入生成队列（task_id={task_id}），完成后自动落盘并通知。"
                f"保存位置: {out}。请等待，不要用 shell/curl 自行重试；"
                f"取消用 media(action=\"cancel\", task_id=\"{task_id}\")。"
            ),
            metadata={
                "kind": "video",
                "provider": "agnes",
                "model": model,
                "task_id": task_id,
                "background": True,
                "saved": None,
            },
        )


class MediaTool(WorkspaceBoundTool):
    name = "media"
    description = """Agnes 媒体生成，文生图 / 图生图 / 文生视频 / 图生视频，结果自动保存到本地。
内部全自动，你只需三个动作。

动作
- action=generate——提交生成任务。kind=image|video 必填，prompt 必填。
  提交后在后台执行，完成后自动落盘并通知，无需轮询、无需查进度。
- action=status ——查看生成队列实情（排队/进行中/取消）。仅在用户询问队列时才用，不要主动查。
- action=cancel ——取消某个排队/进行中的任务。需传 task_id，来自 status 或此前提交的返回。

generate 可选参数
- image：参考图（图生图/图生视频），本地路径或 URL
- out：输出文件绝对路径；缺省自动命名到 工作区/media/
- size：图片尺寸，默认 1024x1024
- seconds/aspect_ratio/mode：视频参数（Agnes Video 2.5 Flash）；
  seconds 4–12 默认 5；画幅默认 16:9；720P 固定；
  mode 为 text/keyframe/reference，有参考图时自动推断
- return_image：默认 false——生成图仅落盘、不回注给 LLM；true 才压缩回注供继续查看/编辑

返回保存路径与元数据。视频生成完成会自动通知，等通知即可，不要用 shell/curl 自行重试。"""
    kind = ToolKind.FETCH
    category = "media"
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["generate", "status", "cancel"],
                "description": "generate=生成；status=查看队列，仅用户询问时用；cancel=取消任务",
                "default": "generate",
            },
            "kind": {"type": "string", "enum": ["image", "video"], "description": "生成类型，generate 必填"},
            "prompt": {"type": "string", "description": "生成描述，generate 必填"},
            "task_id": {"type": "string", "description": "目标任务 ID，cancel 必填"},
            "image": {
                "type": "array",
                "items": {"type": "string"},
                "description": "参考图（图生图/图生视频），本地路径或 URL",
            },
            "out": {"type": "string", "description": "输出文件绝对路径，缺省自动命名"},
            "out_dir": {"type": "string", "description": "自动命名时的输出目录"},
            "return_image": {
                "type": "boolean",
                "default": False,
                "description": "默认false，生成图仅落盘、不回注给LLM；显式true才压缩回注供模型查看/编辑",
            },
            "size": {"type": "string", "description": "图片尺寸，默认 1024x1024"},
            "seconds": {"type": "string", "description": "视频时长（秒），4–12，默认 5"},
            "aspect_ratio": {
                "type": "string",
                "description": "视频画幅，默认 16:9；支持 21:9/16:9/4:3/1:1/3:4/9:16",
            },
            "mode": {
                "type": "string",
                "enum": ["text", "keyframe", "reference"],
                "description": "视频生成模式；有参考图时可省略并自动推断",
            },
            "seed": {"type": "integer", "description": "随机种子"},
            "negative_prompt": {"type": "string", "description": "负面提示"},
            "steps": {"type": "integer", "description": "推理步数"},
            "guidance": {"type": "number", "description": "引导强度"},
            "timeout": {"type": "number", "description": "生成超时秒数"},
        },
        "required": ["action"],
    }
    invocation_class = MediaToolInvocation

    def __init__(
        self,
        workspace_root: Path | None = None,
        available_providers: set[str] | None = None,
    ):
        super().__init__(workspace_root=workspace_root)
        self._available_providers = set(available_providers) if available_providers else None

    def create_invocation(self, params: dict[str, Any]) -> MediaToolInvocation:
        return self.invocation_class(params, self._workspace_root, self._available_providers)

    def get_execution_timeout(self, default_timeout: float, args: dict | None = None) -> float | None:
        # 视频走队列+调度器（后台），status/cancel 即时。生成图可能耗时，交给工具内部超时。
        return None
