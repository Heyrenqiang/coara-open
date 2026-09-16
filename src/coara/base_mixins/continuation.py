"""Continuation input mixin.

宿主为 CoaraBase，属性在宿主 __init__ 初始化（mixin 不设 __init__，只做方法容器）。
"""

from __future__ import annotations

from typing import Any

from src.core.types import ContinuationInput


class ContinuationMixin:
    """接续输入：submit_continuation_input / drain / deferred remote ctx"""

    def submit_continuation_input(
        self,
        text: str,
        image_blocks: list[dict[str, Any]] | None = None,
        *,
        source: str = "",
        agent_origin: str = "",
        agent_origin_channel: str = "",
        client_msg_id: str = "",
    ) -> None:
        """Buffer a user message to be injected mid-turn.

        ``image_blocks`` carries optional Vision attachments so a follow-up
        pasted mid-turn keeps its images (Plan B: multimodal continuation).
        ``source`` tags the follow-up's origin (``cli``/``matrix``/``web``);
        required for correct CLI mirroring when the active turn is unrelated
        (e.g. phone message during a ``background`` awaken turn).
        ``agent_origin`` / ``agent_origin_channel`` tag a *subagent result* with
        the delegate-call user input origin + matrix room (never clears the
        plan lock — it is system injection).
        """
        # Windows 控制台/粘贴可能产生 UTF-16 代理项（无法 UTF-8 编码），
        # 进入事件流会让显示订阅者编码失败——入口统一清洗
        from src.utils.text_utils import sanitize_surrogates

        text = sanitize_surrogates(text)
        origin = str(source or "").strip()
        agent_src = str(agent_origin or "").strip()
        origin_ch = str(agent_origin_channel or "").strip()
        from src.coara.turn_context import get_turn_channel_id

        _origin_channel_id = str(get_turn_channel_id() or "")
        # 带来源的接续输入 = 用户中途介入：解除 plan_mode submit 的待批准锁。
        # 系统注入（delegate 结果等）不带 source，不误清。
        if origin:
            self._plan_pending_approval = False
        # 入队只记 source 在队列项上，不刷新「最近一次输入端」——那是注入语义，
        # 归属到 turn loop 迭代头真正注入上下文那一刻（开新段时才更新）。
        # 入队≠注入：跟话入队后 LLM 仍在跑上一段的输出，端归属不能提前切换。
        self._continuation_inputs.append(
            ContinuationInput(
                text=text,
                image_blocks=image_blocks,
                source=origin,
                agent_origin=agent_src,
                agent_origin_channel=origin_ch,
                channel_id=_origin_channel_id,
                deferred_remote_ctx=self._pending_deferred_remote_ctx,
                client_msg_id=str(client_msg_id or "").strip(),
            )
        )
        # Stamp moves onto the queue item; do not leave a shared slot for the next end.
        self._pending_deferred_remote_ctx = None
        self._deferred_remote_ctx = None
        self._continuation_event.set()
        self._emit_trace(
            "continuation_input_received",
            f"Continuation input received: {text[:80]}",
            payload={
                "text": text,
                "image_count": len(image_blocks or []),
                "source": origin,
                "agent_origin": agent_src,
                "agent_origin_channel": origin_ch,
            },
        )

    def drain_continuation_inputs(self) -> list[ContinuationInput]:
        """Pop all buffered continuation inputs."""
        if not self._continuation_inputs:
            return []
        items = self._continuation_inputs[:]
        self._continuation_inputs.clear()
        if not self._continuation_inputs:
            self._continuation_event.clear()
        return items

    def set_deferred_remote_ctx(
        self, room_id, send_text, interaction_channel, *, source: str = "", actor: str = ""
    ) -> None:
        """Stamp remote turn context for the *next* ``submit_continuation_input``.

        Called by the ingress defer path (busy turn) so the turn loop can
        re-apply ``turn`` ContextVars when it consumes that specific input
        （审批/询问走远端通道）。Stamp is per queue item — a later end cannot
        overwrite an earlier follow-up's approval channel.

        正文输出归属由 **segment**（最近一次用户注入端）经 EndRegistry 路由——
        各端在跟话注入时已登记自己的流式通道，不再设收尾补发镜像。
        """
        ctx = (room_id, send_text, interaction_channel, source, actor)
        self._pending_deferred_remote_ctx = ctx
        # Compat mirror until submit attaches the stamp to the queue item.
        self._deferred_remote_ctx = ctx

    def clear_deferred_remote_ctx(self) -> None:
        """Drop pending/compat deferred remote context (turn cleanup)."""
        self._deferred_remote_ctx = None
        self._pending_deferred_remote_ctx = None
