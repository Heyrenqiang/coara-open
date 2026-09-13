"""Orchestrator Tool — 编排：以 agentic 节点为单位织工作流。

从 delegate 拆出的编排工具：spawn 登记 flow 节点（停车 coaras）、run 点火、
wait 收尾、status 查看；草案 CRUD（save/run/result/delete）
与内存图落盘（save/load）也在此。激活 workflow 技能后才注册（延迟加载），
delegate 回归纯委派语义。

节点模型：智能体 + 任务 + 数据进 + 数据出 + 控制流（depends_on/routes_to）。
一组 spawn 调用唯一确定一张 flow 图。
"""

from __future__ import annotations

from typing import Any

from src.coara.base import CoaraBase
from src.core.logger import logger
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult

_ORCHESTRATOR_DESCRIPTION = """工作流编排，在需要多智能体协作完成复杂任务时使用。

搞清楚目标，创建子节点运行实现目标。

图模型（唯一核心）：工作流就是一张图——节点是干活的智能体，边是干活的先后顺序。
- 节点只有一种：智能体（id 即名字，task 是给它的完整任务指令）。没有 if/for/while 这类控制节点，分支循环全靠连边
- 边是唯一事实：谁依赖谁、结果流向谁，只看边；不允许另立一套依赖或路由表
- 常见形状怎么搭：并行=一个节点连出多条边（扇出）；汇总=多条边连进一个节点（扇入）；
  二选一=上游 routes="one" 运行时只挑一条走；循环=把下游连回上游（回边）；重试=连一条 on=error 的兜底边
- 一组 spawn 唯一确定一张图；跑起来后随时 update/edge/remove 改图（没跑到的地方生效，跑过的就是事实）
- 序列化规则固定：键序固定、默认值省略、节点按 id 排、边按 (from,to,on) 排——同一张图输出逐字节相同

从需求到图，一个完整小例——用户要「三路并行调研，最后汇总成报告」
- 拆节点：research-a/b/c（三路调研，互不依赖）、汇总（要等三路都完成）
- 建图，spawn 的 depends_on/routes_to 与 edge action 是同一张图的等价写法，边就从这来
  orchestrator(spawn, flow=调研, node_id=research-a, description=调研A, prompt=A 的完整指令)
  orchestrator(spawn, flow=调研, node_id=research-b, description=调研B, prompt=B 的完整指令)
  orchestrator(spawn, flow=调研, node_id=research-c, description=调研C, prompt=C 的完整指令)
  orchestrator(spawn, flow=调研, node_id=汇总, description=汇总报告,
               depends_on=[research-a, research-b, research-c], prompt=汇总指令)
  → 图自动成形：a/b/c 并行扇出，汇总 三条 wait 边入，凑齐三份结果才开跑（人齐开会）
- 跑通：run(flow=调研) 点火 → wait(flow=调研) 收尾

边分两类，运行时自动判定
- wait 边（普通先后边）：不成环且 on=success。节点开跑的前提是每条 wait 边的上游都交了活——到齐才动，像等人齐开会
- kick 边（打回/兜底边）：成环的回边或 on=error 边。不参与就绪判定，上游一完成就送来一次到达，节点有机会就再跑一轮

跑起来之后全自动推进：节点跑完结果沿边流向下游，你只管 run 点火、wait 收尾、status 看进度。
循环有硬上限：每节点最多激活 max_activations 次（默认 100，节点级可覆写），一定停得下来。

固化入库：编排即写穿——每个改图动作（spawn/update/edge/remove）都会自动把图投影落盘到绑定草案
并推送 WebUI 编辑器实时显示，无需手动 save；save 仅作幂等确认。草案存系统草案库（与工作空间无关）。

数据流：上游结果自动以【来自 xx】段落注入下游 prompt；节点 input 可写 {{steps.上游.text}} 显式引用上游结果
（即隐式依赖，忘了画边也能追到），或写字面量种子。不需要全局输入声明。"""


# 已知 provider 特性一句话简介（生成编排工具描述的 provider 清单用）。
# 只写给 LLM 决策有用的差异点；没条目的 provider 只列名字与默认模型。
_PROVIDER_TRAITS: dict[str, str] = {
    "deepseek": "便宜量大，峰谷定价（9-12、14-18 点高峰贵，空闲半价），适合批量文本节点",
    "kimi": "Kimi k 系列，长上下文，适合长文档读写",
    "minimax": "MiniMax 系列，长上下文",
    "zhipu": "智谱 GLM 系列",
    "qwen": "通义千问系列",
    "openai": "OpenAI GPT 系列",
    "anthropic": "Anthropic Claude 系列",
    "gemini": "Google Gemini 系列",
    "doubao": "豆包系列",
    "grok": "xAI Grok 系列",
    "agnes": "媒体生成见长（文生图/图生图/文生视频），节点要做图或视频时选它",
    "longcat": "LongCat 系列",
}


def _build_provider_guide() -> str:
    """生成工具描述尾部的 provider 清单：当前已启用 provider + 特性 + 使用原则。

    配置读不到（未加载/异常）时返回空串，只影响描述丰富度，不影响工具功能。
    """
    try:
        from src.core.config import config_manager

        names = [
            name
            for name in config_manager.list_providers()
            if getattr(config_manager.get_provider(name), "enabled", True)
        ]
    except Exception:  # noqa: BLE001
        return ""
    if not names:
        return ""
    lines = [
        "",
        "",
        "节点 provider 清单（高级选项，默认不要动）：",
        "- 默认留空：节点统一跟随全局「节点模型」配置（WebUI 工作流页可改），与主会话模型互不影响",
        "- 仅当某个节点有特殊模型需求时才设 provider，例如该节点要生成图片/视频（选媒体能力强的），"
        "或任务简单量大想换便宜模型",
        "- 当前已配置可用：",
    ]
    for name in names:
        trait = _PROVIDER_TRAITS.get(name, "")
        lines.append(f"  - {name}" + (f"：{trait}" if trait else ""))
    lines.append("- 只设 provider 时，节点模型自动取该 provider 的默认模型；WebUI 画布上点节点也可改")
    return "\n".join(lines)


class OrchestratorTool(BaseTool):
    """编排工具：flow 图级操作入口（挂起工具，tool(activate) 揭示后可用）。"""

    name = "orchestrator"
    summary = "工作流编排，多智能体协作完成复杂任务时使用"
    description = _ORCHESTRATOR_DESCRIPTION
    display_name = "Orchestrator"
    category = "agent"
    kind = ToolKind.EXECUTE
    owner_only = False
    should_defer = True  # 挂起：平时不占工具面，经 tool(activate) 揭示
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "spawn",
                    "run",
                    "wait",
                    "status",
                    "save",
                    "load",
                    "result",
                    "delete",
                    "update",
                    "edge",
                    "remove",
                    "rerun",
                    "resume",
                    "cancel",
                ],
                "description": (
                    "spawn=登记 flow 节点; run=运行工作流，flow 运行当前图、draft_id 运行已保存的文件; "
                    "wait=等 flow 收尾; status=查看节点输出; "
                    "save=固化入库，save(flow=…) 从会话内已编排的图自动生成定义并保存；"
                    "也可 save(definition=…) 直接存文本；"
                    "load=把保存的文件载回会话; result=查实例结果，引擎已剥离，仅历史实例; delete=删草案; "
                    "update=改节点任务/路由模式; edge=加/删边; remove=删未运行节点; "
                    "rerun=单节点重跑，cascade=true 连下游; resume=断点续跑，失败节点重置后继续; "
                    "cancel=取消运行实例，引擎已剥离，仅历史实例"
                ),
            },
            "flow": {
                "type": "string",
                "description": "工作流名，中文或英文均可",
            },
            "node_id": {
                "type": "string",
                "description": "工作流节点名，中文或英文均可，spawn 时必填",
            },
            "description": {
                "type": "string",
                "description": "一句话节点名，spawn 时必填，简短概括做什么",
            },
            "prompt": {
                "type": "string",
                "description": "spawn 时的完整节点指令，写清任务目标、范围、方法、规则、验收、回报格式",
            },
            "subagent_type": {
                "type": "string",
                "description": "节点智能体类型，默认 coaras",
            },
            "provider": {
                "type": "string",
                "description": (
                    "节点专属 LLM provider，高级，默认留空不要动=跟随全局「节点模型」配置。"
                    "仅当该节点有特殊模型需求时才设置，例如需要图片/视频生成能力、"
                    "或该节点任务简单想换更便宜的模型。只给 provider 时模型自动取该 provider 的默认模型。"
                    "可选值与特性见工具描述末尾的 provider 清单。"
                ),
            },
            "depends_on": {
                "type": "array",
                "items": {"type": "string"},
                "description": "依赖的节点，fan-in 等齐后启动",
            },
            "routes_to": {
                "type": "array",
                "items": {"type": "string"},
                "description": "结果路由到的下游节点，fan-out 广播",
            },
            "task": {
                "type": "string",
                "description": "update action 的新任务指令，与 routes_mode 二选一",
            },
            "routes_mode": {
                "type": "string",
                "enum": ["all", "one"],
                "description": "all=广播给全部 routes_to；one=节点运行期用 deliver(next=...) 选一个后继",
                "default": "all",
            },
            "input": {
                "type": "string",
                "description": "入口节点的工作流输入，即种子数据",
            },
            "auto_run": {
                "type": "boolean",
                "description": "spawn 是否立即启动该节点，默认 false 表示停车，等 run 统一启动",
                "default": False,
            },
            "schedule": {
                "type": "object",
                "description": "定时意图，仅记录，内部不生效；保存成文件后由外部引擎/触发器承载",
            },
            "frm": {
                "type": "string",
                "description": "edge action 的源节点",
            },
            "to": {
                "type": "string",
                "description": "edge action 的目标节点",
            },
            "on": {
                "type": "string",
                "enum": ["success", "error"],
                "description": "edge action 的边触发条件，success=正常投递；error=失败兜底路由，默认 success",
                "default": "success",
            },
            "remove": {
                "type": "boolean",
                "description": "edge action 传 true 表示删边，默认 false 表示加边",
                "default": False,
            },
            "draft_id": {
                "type": "string",
                "description": "草案 ID（draft-xxxxxxxx），save 时表示覆盖；run 时与 definition 二选一；delete 时必填",
            },
            "instance_id": {
                "type": "string",
                "description": "工作流运行实例 ID（wf-xxxxxxxx），result 时必填",
            },
            "definition": {
                "type": "string",
                "description": (
                    "工作流定义文本，save 时与 flow 二选一——"
                    "有 flow 则从会话内图自动固化，不必手写；"
                    "无 flow 时才需要贴完整 definition。"
                    "run 时与 draft_id 二选一，校验后提示用独立 WDL 软件执行。格式为内核投影（见本工具描述）"
                ),
            },
            "cascade": {
                "type": "boolean",
                "description": "rerun action 传 true 时连同该节点全部下游一起重置重跑，默认 false",
                "default": False,
            },
        },
        "required": ["action"],
    }

    def __init__(self, parent_coara: CoaraBase | None = None):
        self._parent = parent_coara
        # 描述尾部附上当前已启用 provider 清单与特性（随用户配置动态生成，
        # 进程内静态——providers 配置改动要新会话才反映到工具面）。
        self.description = _ORCHESTRATOR_DESCRIPTION + _build_provider_guide()

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return OrchestratorToolInvocation(params, self._parent)

    @staticmethod
    def requires_approval(args: dict[str, Any]) -> bool:
        """Prompt for workflow draft deletion (irreversible)."""
        action = str(args.get("action", "") or "").strip() if isinstance(args, dict) else ""
        return action == "delete"

    def get_execution_timeout(self, default_timeout: float, args: dict | None = None) -> float | None:
        # 编排节点是完整子智能体运行；wait 阻塞到真实事件。均不应被通用超时切断。
        return None


class OrchestratorToolInvocation(ToolInvocation):
    """编排工具调用实例。"""

    def __init__(self, params: dict[str, Any], parent_coara: CoaraBase | None = None):
        super().__init__(params)
        self._parent = parent_coara
        self.action = str(params.get("action") or "spawn").strip().lower() or "spawn"
        self.flow = str(params.get("flow") or "").strip() or None
        self.node_id = str(params.get("node_id") or "").strip() or None
        self.task_id = str(params.get("task_id") or "").strip()

        def _str_list_param(name: str) -> list[str]:
            raw = params.get(name)
            if raw is None or raw == "":
                return []
            if isinstance(raw, str):
                return [raw]
            if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
                return list(raw)
            raise ValueError(
                f"参数 {name} 需为字符串或字符串列表（传单个节点名时也按列表处理），当前类型: {type(raw).__name__}"
            )

        self.depends_on = _str_list_param("depends_on")
        self.routes_to = _str_list_param("routes_to")
        self.routes_mode = str(params.get("routes_mode") or "all").strip() or "all"
        self.flow_input = str(params.get("input") or "").strip() or None
        self.auto_run = bool(params.get("auto_run", False))
        self.schedule = params.get("schedule") if isinstance(params.get("schedule"), dict) else None
        self.draft_id = str(params.get("draft_id") or "").strip() or None
        self.instance_id = str(params.get("instance_id") or "").strip() or None
        self.definition_text = str(params.get("definition") or "").strip() or None
        self.description = str(params.get("description") or "").strip()
        self.frm = str(params.get("frm") or "").strip() or None
        self.to = str(params.get("to") or "").strip() or None
        self.edge_on = str(params.get("on") or "success").strip() or "success"
        self.edge_remove = bool(params.get("remove", False))
        self.cascade = bool(params.get("cascade", False))
        # update action：未显式传的调整项为 None（不改动）
        _task = params.get("task")
        self.new_task = str(_task) if _task is not None else None
        _rm = params.get("routes_mode") if self.action == "update" else None
        self.new_routes_mode = str(_rm).strip() if _rm is not None else None

        known_actions = (
            "spawn",
            "run",
            "wait",
            "status",
            "save",
            "load",
            "result",
            "delete",
            "update",
            "edge",
            "remove",
            "rerun",
            "resume",
            "cancel",
        )
        if self.action not in known_actions:
            raise ValueError(f"未知 action: {self.action}（支持 {' / '.join(known_actions)}）")

        if self.action in ("update", "remove", "rerun") and not self.flow:
            raise ValueError(f"action={self.action} 需要 flow")
        if self.action == "remove" and not self.node_id:
            raise ValueError("action=remove 需要 node_id")
        if self.action == "rerun" and not self.node_id:
            raise ValueError("action=rerun 需要 node_id")
        if self.action == "resume" and not self.flow:
            raise ValueError("action=resume 需要 flow")
        if self.action == "edge" and (not self.flow or not self.frm or not self.to):
            raise ValueError("action=edge 需要 flow、frm、to")

        if self.action == "spawn":
            if not self.flow:
                raise ValueError("spawn 缺少必填参数: flow（工作流名）")
            if not self.node_id:
                raise ValueError("spawn 缺少必填参数: node_id（节点名）")
            if not self.description:
                raise ValueError("spawn 缺少必填参数: description（一句话节点名）")
            if not str(params.get("prompt") or "").strip():
                raise ValueError("spawn 缺少必填参数: prompt（完整节点指令）")
            self.prompt = str(params["prompt"])
            self.subagent_type = str(params.get("subagent_type") or "coaras").strip() or "coaras"
            self.provider = str(params.get("provider") or "").strip()
            return

        self.prompt = ""
        self.subagent_type = ""
        self.provider = ""

        if self.action == "save" and not self.definition_text and not self.flow:
            raise ValueError("action=save 需要 flow=...（从会话内已编排的图自动固化）或 definition=...")
        if self.action == "load" and not self.flow and not self.draft_id:
            raise ValueError("action=load 需要 flow=...（按名载回最新保存的文件）或 draft_id=...")
        if self.action == "run" and not self.flow and not self.definition_text and not self.draft_id:
            raise ValueError("action=run 需要 flow（内存图点火）或 draft_id/definition（校验已存草案）")
        if self.action == "result" and not self.instance_id:
            raise ValueError("action=result 缺少必填参数: instance_id")
        if self.action == "cancel" and not self.instance_id:
            raise ValueError("action=cancel 缺少必填参数: instance_id")
        if self.action == "delete" and not self.draft_id:
            raise ValueError("action=delete 缺少必填参数: draft_id")

    def _resolve_save_definition(self) -> str:
        """save：有 definition 用文本；否则从会话内 flow 图自动投影。"""
        if self.definition_text:
            return self.definition_text
        if not self.flow:
            raise ValueError("action=save 需要 flow=... 或 definition=...")
        try:
            return self._coord().export_wdl(self.flow)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    def get_description(self) -> str:
        if self.action == "spawn" and self.flow:
            return f"Orchestrator [{self.flow}/{self.node_id}]: {self.description}"
        if self.action in ("update", "edge", "remove") and self.flow:
            target = self.node_id or f"{self.frm}→{self.to}"
            return f"Orchestrator {self.action} [{self.flow}/{target}]"
        return f"Orchestrator {self.action}: {self.flow or self.draft_id or self.instance_id or ''}".rstrip(": ")

    async def execute(self, signal=None) -> ToolResult:
        if self.action == "spawn":
            return await self._execute_flow_spawn()
        if self.action == "run":
            if self.flow:
                return await self._execute_flow_run()
            return await self._execute_draft_run()
        if self.action == "wait":
            return await self._execute_flow_wait()
        if self.action == "status":
            return self._execute_flow_status()
        if self.action == "update":
            return await self._execute_flow_update()
        if self.action == "edge":
            return await self._execute_flow_edge()
        if self.action == "remove":
            return await self._execute_flow_remove()
        if self.action == "rerun":
            return await self._execute_flow_rerun()
        if self.action == "resume":
            return await self._execute_flow_resume()
        if self.action == "save":
            return await self._execute_draft_save()
        if self.action == "load":
            return self._execute_flow_load()
        if self.action == "result":
            return await self._execute_flow_result()
        if self.action == "cancel":
            return await self._execute_flow_cancel()
        if self.action == "delete":
            return await self._execute_flow_delete()
        return ToolResult.error(f"未知 action: {self.action}")

    # ── 内存 flow 图（会话内编排） ──────────────────────────────────────────

    def _coord(self) -> Any:
        """编排协调器：跟随父实例（主体隔离），无父上下文时回退进程级兜底单例。"""
        if self._parent is not None:
            return self._parent.flow_coordinator
        from src.coara.flow_coordinator import get_flow_coordinator

        return get_flow_coordinator()

    def _root(self) -> Any:
        """父实例所属的 Root（WS 推送/引擎桥接用）；取不到则父实例本身。"""
        if self._parent is None:
            return None
        root = getattr(self._parent, "_root_coara", None) or getattr(self._parent, "root_coara", None)
        return root if root is not None else self._parent

    def _merge_into_existing_draft(self, store: Any, draft_id: str, wdl: str) -> str | None:
        """复用当前打开的草案时，把本流合并进草案现有内容（共同编辑，不覆盖）。

        返回合并后的投影文本；草案不存在返回 None（调用方回退新建）。
        空画布（工作台占位草案：节点全部无任务无输入且无边）直接采用本流内容；
        有内容则按节点 id 并入、边去重——一份草案画布可共存多张流。
        草案名随最新流（画布标题与运行实例名一致，避免分叉）。
        """
        try:
            existing = store.get(draft_id)
        except Exception:  # noqa: BLE001
            return None
        if existing is None:
            return None
        from src.workflow.core.serde import emit_graph, parse_graph

        try:
            base = parse_graph(existing.wdl)
        except Exception:  # noqa: BLE001 — 草案内容损坏时以本流为准
            return wdl
        blank = not base.edges and all(not n.task.strip() and not n.input.strip() for n in base.nodes.values())
        if blank:
            return wdl
        try:
            incoming = parse_graph(wdl)
        except Exception:  # noqa: BLE001
            return None
        base.nodes.update(incoming.nodes)
        seen = {(e.frm, e.to, e.on) for e in base.edges}
        for e in incoming.edges:
            if (e.frm, e.to, e.on) not in seen:
                base.edges.append(e)
        # 草案名随最新流：画布标题与运行实例名一致，避免名字分叉
        if incoming.name:
            base.name = incoming.name
        return emit_graph(base)

    async def _write_through_draft(self, *, open_editor: bool = False) -> None:
        """编排写穿：内存图投影 → 绑定草案落盘（系统草案库）+ WS 实时推送。

        草案是工作流唯一活模型：每次改图都写穿，WebUI 编辑器即时可见。
        全程 best-effort——落盘/推送失败不影响编排动作本身。
        """
        if self._parent is None or not self.flow:
            return
        coord = self._coord()
        try:
            wdl = coord.export_wdl(self.flow)
        except (ValueError, AttributeError):
            return
        from src.workflow.draft_store import WorkflowDraftStore

        store = WorkflowDraftStore()
        bound_id = coord.draft_id_for(self.flow) if hasattr(coord, "draft_id_for") else None
        draft_id = bound_id
        reused_active_draft = False
        if draft_id is None:
            # 工作台/编辑器当前正打开一份草案时，写穿复用它（用户盯着的就是画布），
            # 而不是每次编排都新开一份草案再跳过去
            root0 = self._root()
            ws0 = getattr(root0, "_web_server", None) if root0 is not None else None
            active = getattr(ws0, "active_workflow_draft_id", None) if ws0 is not None else None
            if active:
                draft_id = str(active)
                reused_active_draft = True
        if reused_active_draft:
            # 共同编辑：草案是共享活模型。复用他人草案时把本流合并进现有内容
            # （节点按 id 并入、边去重），一份草案画布上可以共存多张流；
            # 草案不存在（已被删）则回退新建
            merged = self._merge_into_existing_draft(store, draft_id, wdl)
            if merged is None:
                reused_active_draft = False
                draft_id = None
            else:
                wdl = merged
        try:
            draft = store.save(
                wdl,
                draft_id=draft_id,
                source_subagent=str(getattr(self._parent.identity, "name", "") or "root"),
                flow_name=self.flow,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Orchestrator write-through save failed ({self.flow}): {exc}")
            return
        if bound_id is None and hasattr(coord, "bind_draft"):
            coord.bind_draft(self.flow, draft.draft_id)
        note_sync = getattr(coord, "note_draft_sync", None)
        if callable(note_sync):
            note_sync(self.flow, draft.updated_at)
        root = self._root()
        web_server = getattr(root, "_web_server", None) if root is not None else None
        if web_server is None:
            return
        try:
            await web_server.registry.send_to_active(
                {
                    "type": "workflow_draft_updated",
                    "draft_id": draft.draft_id,
                    "workflow_name": self.flow,
                }
            )
            if open_editor and not reused_active_draft:
                await web_server.registry.send_to_active(
                    {
                        "type": "open_workflow_editor",
                        "draft_id": draft.draft_id,
                        "workflow_name": self.flow,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Orchestrator write-through WS push skipped: {exc}")

    async def _refresh_if_stale(self) -> None:
        """UI 侧改过绑定草案（stale）：运行前从草案重建干净图。"""
        if self._parent is None or not self.flow:
            return
        coord = self._coord()
        if not hasattr(coord, "is_stale") or not coord.is_stale(self.flow):
            return
        draft_id = coord.draft_id_for(self.flow)
        if not draft_id:
            return
        from src.workflow.draft_store import WorkflowDraftStore

        wdl = WorkflowDraftStore().load_wdl(draft_id)
        if not wdl or not hasattr(coord, "rebuild_from_projection"):
            return
        try:
            coord.rebuild_from_projection(self._parent, flow=self.flow, wdl=wdl)
            logger.info(f"Flow {self.flow} rebuilt from draft {draft_id} (stale refresh)")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Flow {self.flow} stale rebuild failed: {exc}")

    async def _rebase_before_mutation(self) -> None:
        """共同编辑保护：编排动作前检测草案被外部（画布/其他会话）改过。

        外部版本更新 → 先从草案重建内存图（对方的修改并进图），再应用本次
        编排动作——会话写穿是全文投影，不 rebase 就会把对方的修改静默抹掉。
        """
        if self._parent is None or not self.flow:
            return
        await self._refresh_if_stale()
        coord = self._coord()
        draft_id_fn = getattr(coord, "draft_id_for", None)
        sync_fn = getattr(coord, "draft_sync", None)
        note_fn = getattr(coord, "note_draft_sync", None)
        if not (callable(draft_id_fn) and callable(sync_fn) and callable(note_fn)):
            return
        if not hasattr(coord, "rebuild_from_projection"):
            return
        draft_id = draft_id_fn(self.flow)
        if not draft_id:
            return
        from src.workflow.draft_store import WorkflowDraftStore

        try:
            draft = WorkflowDraftStore().get(draft_id)
        except Exception:  # noqa: BLE001
            return
        if draft is None:
            return
        last = sync_fn(self.flow)
        if last is None:
            note_fn(self.flow, draft.updated_at)
            return
        if draft.updated_at == last:
            return
        try:
            coord.rebuild_from_projection(self._parent, flow=self.flow, wdl=draft.wdl)
            note_fn(self.flow, draft.updated_at)
            logger.info(f"Flow {self.flow} rebased onto externally-modified draft {draft_id}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Flow {self.flow} rebase failed: {exc}")

    async def _execute_flow_spawn(self) -> ToolResult:
        """编排 spawn：节点带拓扑参数织入 flow 图（停车 cooras，auto_run 时立即启动）。"""
        if self._parent is None:
            return ToolResult.error("编排 spawn 需要父 Coara 上下文")
        coord = self._coord()
        get_flow = getattr(coord, "get_flow", None)
        is_new_flow = get_flow(self.flow) is None if callable(get_flow) else False
        await self._rebase_before_mutation()
        spawn_kwargs: dict[str, Any] = {}
        if self.provider:
            spawn_kwargs["provider"] = self.provider
        msg = await coord.spawn_node(
            self._parent,
            flow=self.flow,
            node_id=self.node_id,
            task=self.prompt,
            seed=self.flow_input or "",
            depends_on=self.depends_on,
            routes_to=self.routes_to,
            routes_mode=self.routes_mode,
            subagent_type=self.subagent_type or "coaras",
            auto_run=self.auto_run,
            schedule=self.schedule,
            **spawn_kwargs,
        )
        if not msg.endswith("跳过重复 spawn"):
            # 写穿：首个节点顺手让编辑器打开（web 模式）
            await self._write_through_draft(open_editor=is_new_flow)
        return ToolResult.success(msg)

    async def _execute_flow_run(self) -> ToolResult:
        """内部启动 flow：所有就绪节点（pending 且依赖满足）开始运行。"""
        if not self.flow:
            return ToolResult.error("run 需要 flow 参数")
        await self._refresh_if_stale()
        return ToolResult.success(self._coord().run(self.flow))

    async def _execute_flow_wait(self) -> ToolResult:
        """等整个 flow 收尾（节点由 flow_coordinator 调度）。"""
        if not self.flow:
            return ToolResult.error("wait 需要 flow 参数")
        await self._refresh_if_stale()
        return ToolResult.success(await self._coord().wait(self.flow))

    def _execute_flow_status(self) -> ToolResult:
        """查看 flow 图当前状态与各节点输出（含草案绑定与拓扑全览）。"""
        if not self.flow:
            return ToolResult.error("status 需要 flow 参数")
        coord = self._coord()
        msg = coord.status(self.flow)
        snapshot_fn = getattr(coord, "snapshot", None)
        snap = snapshot_fn(self.flow) if callable(snapshot_fn) else None
        if snap is not None:
            extras: list[str] = []
            draft_id_fn = getattr(coord, "draft_id_for", None)
            draft_id = draft_id_fn(self.flow) if callable(draft_id_fn) else None
            if draft_id:
                extras.append(f"绑定草案：{draft_id}（系统草案库，WebUI 编辑器实时同步）")
            edges = snap.get("edges") or []
            if edges:
                topo = "；".join(
                    f"{e['from']} → {e['to']}" + (f" (on={e['on']})" if e.get("on") != "success" else "") for e in edges
                )
                extras.append(f"拓扑：{topo}")
            if extras:
                msg = msg + "\n\n" + "\n".join(extras)
        return ToolResult.success(msg)

    async def _execute_flow_update(self) -> ToolResult:
        """运行中调整：改节点任务/路由模式，下次激活生效。"""
        if self._parent is None:
            return ToolResult.error("update 需要父 Coara 上下文")
        if not self.flow or not self.node_id:
            return ToolResult.error("update 需要 flow 与 node_id")
        if self.new_task is None and self.new_routes_mode is None:
            return ToolResult.error("update 需要 task 或 routes_mode 之一")
        await self._rebase_before_mutation()
        msg = self._coord().update_node(
            self._parent,
            flow=self.flow,
            node_id=self.node_id,
            task=self.new_task,
            routes_mode=self.new_routes_mode,
        )
        await self._write_through_draft()
        return ToolResult.success(msg)

    async def _execute_flow_edge(self) -> ToolResult:
        """运行中调整：加/删边。"""
        if self._parent is None:
            return ToolResult.error("edge 需要父 Coara 上下文")
        await self._rebase_before_mutation()
        coord = self._coord()
        if self.edge_remove:
            msg = coord.remove_flow_edge(self._parent, flow=self.flow, frm=self.frm, to=self.to, on=self.edge_on)
        else:
            msg = coord.add_flow_edge(self._parent, flow=self.flow, frm=self.frm, to=self.to, on=self.edge_on)
        await self._write_through_draft()
        return ToolResult.success(msg)

    async def _execute_flow_remove(self) -> ToolResult:
        """运行中调整：删未运行节点（已运行的是实录，不可删）。"""
        if self._parent is None:
            return ToolResult.error("remove 需要父 Coara 上下文")
        if not self.flow or not self.node_id:
            return ToolResult.error("remove 需要 flow 与 node_id")
        await self._rebase_before_mutation()
        msg = self._coord().remove_flow_node(self._parent, flow=self.flow, node_id=self.node_id)
        await self._write_through_draft()
        return ToolResult.success(msg)

    async def _execute_flow_rerun(self) -> ToolResult:
        """调试：单节点重跑（重置后点火）；cascade=true 连同全部下游。"""
        if self._parent is None:
            return ToolResult.error("rerun 需要父 Coara 上下文")
        msg = await self._coord().rerun_node(
            self._parent, flow=self.flow or "", node_id=self.node_id or "", cascade=self.cascade
        )
        return ToolResult.success(msg)

    async def _execute_flow_resume(self) -> ToolResult:
        """调试：断点续跑——失败节点（连同下游）重置后继续推进。"""
        if self._parent is None:
            return ToolResult.error("resume 需要父 Coara 上下文")
        msg = await self._coord().resume_flow(self._parent, flow=self.flow or "")
        return ToolResult.success(msg)

    def _execute_flow_load(self) -> ToolResult:
        """把保存的文件载回会话（干净图，可 update/edge 调整后重新 run）。"""
        if self._parent is None:
            return ToolResult.error("load 需要父 Coara 上下文")
        from src.workflow.draft_store import WorkflowDraftStore

        store = WorkflowDraftStore()
        draft = None
        if self.draft_id:
            draft = store.get(self.draft_id)
            if draft is None:
                return ToolResult.error(f"找不到工作流草案：{self.draft_id}")
        else:
            draft = store.find_by_name(self.flow)
            if draft is None:
                return ToolResult.error(f"没有名为 {self.flow} 的保存文件（先 save(flow=…) 保存）")
        name = self._coord().load({"graph": draft.wdl})
        coord = self._coord()
        if hasattr(coord, "bind_draft"):
            coord.bind_draft(name, draft.draft_id)
        note_sync = getattr(coord, "note_draft_sync", None)
        if callable(note_sync):
            note_sync(name, draft.updated_at)
        if hasattr(coord, "bind_parent"):
            coord.bind_parent(name, self._parent)
        return ToolResult.success(f"已从文件 {draft.draft_id} 载回 flow {name}，可调整后重新 run")

    # ── 草案与外部引擎 ─────────────────────────────────────────────────

    async def _execute_draft_save(self) -> ToolResult:
        """保存工作流草案：有 flow 则从图自动投影；校验 → canonical → 落盘 → 触发器 → 编辑器。

        编排动作本已写穿绑定草案，本 action 是幂等确认（复用绑定 draft_id 覆盖）。
        """
        if self._parent is None:
            return ToolResult.error("save 需要父 Coara 上下文")
        from src.workflow.draft_service import normalize_projection, validate_projection
        from src.workflow.draft_store import WorkflowDraftStore

        try:
            definition_text = self._resolve_save_definition()
        except ValueError as exc:
            return ToolResult.error(str(exc))
        errors, warnings = validate_projection(definition_text)
        if errors:
            return ToolResult.error("工作流校验失败（save）：" + "；".join(errors))
        canonical_wdl = normalize_projection(definition_text)
        subagent_name = str(self._parent.identity.name or "root")
        store = WorkflowDraftStore()
        # 会话内 flow 已绑定草案时覆盖同一份（编排写穿的延续），不产生重复草案
        coord = self._coord()
        draft_id = self.draft_id
        draft_id_fn = getattr(coord, "draft_id_for", None)
        if draft_id is None and self.flow and callable(draft_id_fn):
            draft_id = draft_id_fn(self.flow)
        draft = store.save(
            canonical_wdl,
            draft_id=draft_id,
            source_subagent=subagent_name,
            flow_name=self.flow or None,
        )
        if self.flow and hasattr(coord, "bind_draft"):
            coord.bind_draft(self.flow, draft.draft_id)
        if self.flow:
            note_sync = getattr(coord, "note_draft_sync", None)
            if callable(note_sync):
                note_sync(self.flow, draft.updated_at)
        from src.workflow.draft_service import parse_projection_or_none

        draft_path = store.path_for(draft.draft_id)
        # 用户正盯着同一份草案（工作台/编辑器当前打开的就是它）就不再跳转——
        # 画布随写穿实时刷新，跳转只会把用户从工作台拽到另一个编辑器外壳页。
        # 只有用户没在看这份草案时（CLI 触发、或打开的是别的草案）才推导航。
        web_server = getattr(self._root(), "_web_server", None)
        already_viewing = (
            web_server is not None and getattr(web_server, "active_workflow_draft_id", None) == draft.draft_id
        )
        editor_url = f"/workflow/editor/{draft.draft_id}"
        if not already_viewing and web_server is not None:
            # Web UI 在线时推 WS 导航，让浏览器打开该草案的编辑器页
            _graph = parse_projection_or_none(draft.wdl) if draft.wdl else None
            workflow_name = (_graph.name if _graph else "") or draft.draft_id
            try:
                await web_server.registry.send_to_active(
                    {
                        "type": "open_workflow_editor",
                        "draft_id": draft.draft_id,
                        "workflow_name": workflow_name,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Workflow editor WS navigation skipped: {exc}")
        content_lines = [
            f"工作流草案已保存（内核投影），draft_id: {draft.draft_id}。",
            f"编辑器: {editor_url}",
            f"文件: {draft_path}",
        ]
        if warnings:
            content_lines.append("")
            content_lines.append("保存时发现的建议项：")
            content_lines.extend(f"- {w}" for w in warnings)
        return ToolResult.success(
            content="\n".join(content_lines),
            metadata={
                "draft_id": draft.draft_id,
                "draft_path": str(draft_path),
                "editor_url": editor_url,
                "status": "saved",
                "updated_at": draft.updated_at,
                "source_subagent": draft.source_subagent,
                "warnings": warnings,
            },
        )

    async def _execute_draft_run(self) -> ToolResult:
        """校验投影文本并提示用 WDL 软件执行。

        WDL 执行层已剥离为独立软件（2026-09-08）：coara 只做 .wdl 的生产者
        与文件宿主，run 动作不再提交内核引擎，而是校验后指明执行入口。
        """
        if self._parent is None:
            return ToolResult.error("run 需要父 Coara 上下文")
        from src.workflow.draft_service import validate_projection

        if not self.definition_text:
            from src.workflow.draft_store import WorkflowDraftStore

            loaded = WorkflowDraftStore().load_wdl(self.draft_id)
            if not loaded:
                return ToolResult.error(f"找不到工作流草案：{self.draft_id}")
            self.definition_text = loaded
        errors, warnings = validate_projection(self.definition_text)
        if errors:
            return ToolResult.error("工作流校验失败（run）：" + "；".join(errors))
        content = (
            "工作流校验通过。WDL 执行引擎已独立于 coara（独立 WDL 软件），"
            "coara 不再代跑：请用 WDL 软件执行该 .wdl 文件（wdl run）。"
            "若还没保存，先用 orchestrator(action=save) 落盘。"
        )
        if self.draft_id:
            content += f"\n草案：{self.draft_id}"
        if warnings:
            content += "\n\n校验建议：\n" + "\n".join(f"- {w}" for w in warnings)
        return ToolResult.success(
            content=content,
            metadata={
                "status": "validated",
                "draft_id": self.draft_id,
                "warnings": warnings,
            },
        )

    async def _execute_flow_result(self) -> ToolResult:
        """历史实例结果查询（引擎剥离后内核侧无实例台账）。"""
        if not self.instance_id:
            return ToolResult.error("result 需要 instance_id")
        return ToolResult.error(
            "WDL 执行引擎已剥离出 coara（独立 WDL 软件）："
            "内核不再保存运行实例与结果，请在 WDL 软件中查询该实例"
        )

    async def _execute_flow_delete(self) -> ToolResult:
        """删除工作流草案。"""
        if not self.draft_id:
            return ToolResult.error("delete 需要 draft_id")
        if self._parent is None:
            return ToolResult.error("删除工作流草案缺少父 Coara 上下文")
        from src.workflow.draft_service import parse_projection_or_none
        from src.workflow.draft_store import WorkflowDraftStore

        store = WorkflowDraftStore()
        draft = store.get(self.draft_id)
        if draft is None:
            return ToolResult.error(f"找不到工作流草案: {self.draft_id}")
        graph = parse_projection_or_none(draft.wdl) if draft.wdl else None
        name = graph.name if graph else "workflow"
        deleted = store.delete(self.draft_id)
        if not deleted:
            return ToolResult.error(f"无法删除工作流草案: {self.draft_id}")
        # 草案没了，会话内绑定一并解除
        unbind = getattr(self._coord(), "unbind_draft", None)
        if callable(unbind):
            unbind(self.draft_id)
        logger.info(f"Workflow draft {self.draft_id} ({name}) deleted")
        return ToolResult.success(
            content=f"已删除工作流草案 {self.draft_id}（{name}）",
            metadata={"draft_id": self.draft_id, "name": name, "deleted": True},
        )

    async def _execute_flow_cancel(self) -> ToolResult:
        """历史实例取消（引擎剥离后内核侧无可取消实例）。"""
        if not self.instance_id:
            return ToolResult.error("cancel 需要 instance_id")
        return ToolResult.error(
            "WDL 执行引擎已剥离出 coara（独立 WDL 软件）："
            "内核没有可取消的运行实例，请在 WDL 软件中停止该实例"
        )
