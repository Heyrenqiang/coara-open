"""Shared typed models for coara.

coara v8: single-process asyncio runtime with workspace sessions and
subagents. WDL workflow execution lives in the standalone wdl/ subproject.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from src.core.time import now_iso
from src.utils.text_utils import sanitize_json_payload, sanitize_surrogates

# ============================================================================
# coara Identity (lightweight metadata)
# ============================================================================


class CoaraPersona(BaseModel):
    """Persona description for an agent."""

    name: str
    role: str
    system_prompt_template: str | None = None
    expertise_areas: list[str] = Field(default_factory=list)
    yaml_config: Any | None = None


class CoaraIdentity(BaseModel):
    """Identity metadata for the Root agent.

    Lightweight metadata only; capabilities live on the runtime instance, not on identity.
    Root is the sole intelligent agent; subagents are temporary IN_PROCESS threads.
    """

    coara_id: str
    name: str
    user_facing: bool = True  # Root output goes to CLI/dashboard
    is_owner_context: bool = True  # Original user-facing context
    persona: CoaraPersona
    workspace_dir: Path | None = None
    created_at: str = ""


class CoaraStatus(StrEnum):
    """coara runtime status."""

    CREATED = "created"
    RUNNING = "running"
    IDLE = "idle"
    COMPLETED = "completed"
    FAILED = "failed"
    TERMINATED = "terminated"


class SubagentStatus(StrEnum):
    """Subagent instance lifecycle status for persistence and UI."""

    RUNNING_FOREGROUND = "running_foreground"
    RUNNING_BACKGROUND = "running_background"
    IDLE = "idle"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ============================================================================
# Messaging
# ============================================================================


class MessageRole(StrEnum):
    """Chat message role."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"
    TOOL_RESULT = "tool_result"


class ToolCall(BaseModel):
    """Tool invocation."""

    id: str
    name: str
    arguments: dict[str, Any]


class Message(BaseModel):
    """Conversation message."""

    role: MessageRole
    content: str | list[dict[str, Any]]
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    # MiMo OpenAI-compatible: parsed from responses and echoed back on tool-call turns.
    reasoning_content: str | None = None
    # MiniMax Anthropic wire-only thinking blocks — merged at API send, not conversation content.
    provider_wire_blocks: list[dict[str, Any]] | None = None

    @field_validator("content", "reasoning_content", mode="before")
    @classmethod
    def _strip_lone_surrogates(cls, value: Any) -> Any:
        """Drop lone UTF-16 surrogates so tokenizers / UTF-8 JSON never see them."""
        if isinstance(value, str):
            return sanitize_surrogates(value)
        if isinstance(value, list):
            return sanitize_json_payload(value)
        return value


@dataclass
class ContinuationInput:
    """A mid-turn follow-up buffered for injection into the running turn.

    ``text`` is the user follow-up body (裸文本，来源标签已废弃；当前端由
    情境后缀承载); ``image_blocks`` carries optional Vision attachments that ride
    along when the follow-up is injected as a multimodal user message (Plan B:
    mid-turn image continuation).

    ``source`` is the *origin of this follow-up* (``cli`` / ``matrix`` / ``web`` / …),
    not the active turn's source. Phone/Web mid-turn messages must keep their own
    source so CLI mirroring does not mis-label them as ``[后台]`` when they land
    inside a background-awakened turn.
    """

    text: str
    image_blocks: list[dict[str, Any]] | None = None
    source: str = ""
    # 子智能体结果的派发来源（delegate 调用时用户输入端）。与 ``source`` 不同：
    # 这是系统注入（delegate 收官回投）的专用通道，不参与 plan 锁清理，也不
    # 被当作「用户中途介入」——结果路由按它归位（谁派发、结果给谁看）。
    agent_origin: str = ""
    # matrix 派发时的房间 id（与 ``_subagent_origin`` 快照第二项同源）；收官
    # 直推手机用它，不依赖 turn ContextVar。
    agent_origin_channel: str = ""
    # 端内连接标识（attach conn_id / matrix room_id）：同端多连接并存时输出
    # 路由按它精确归位，空串=端级兜底（旧内核/无连接语义场景）。
    channel_id: str = ""
    # mid-turn 远端 defer 时绑定的审批/回投上下文（room/send/channel/source/actor）。
    # 按条挂在队列项上，禁止单槽覆盖——同回合 web+matrix 先后跟话各跟各的门。
    deferred_remote_ctx: tuple[Any, ...] | None = None


def unpack_continuation_item(item: str | ContinuationInput) -> tuple[str, list[dict[str, Any]] | None]:
    """Normalize a drained continuation queue entry to ``(text, image_blocks)``."""
    if isinstance(item, ContinuationInput):
        return item.text, item.image_blocks
    return str(item or ""), None


class UnifiedMessage(BaseModel):
    """Unified message format for scheduler/queue communication."""

    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=now_iso)

    source_id: str
    target_id: str
    task_id: str | None = None

    content: Any = None
    msg_type: str = "command"
    metadata: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# Skills & Tools
# ============================================================================


class SkillDefinition(BaseModel):
    """Skill definition loaded from SKILL.md."""

    name: str
    description: str
    location: str
    body: str
    disabled: bool = False
    references: list[str] = Field(default_factory=list)
    # False = 不进 Root prompt 的技能名清单（自动生成的未策展技能），
    # 经 skill(action="search") 发现、activate 激活
    listed: bool = True


class ToolDefinition(BaseModel):
    """Tool definition exposed to the model."""

    name: str
    description: str
    parameters: dict[str, Any]
    owner_only: bool = False
    category: str = "general"
    display_name: str = ""
    kind: str = "other"


# ============================================================================
# Memory
# ============================================================================


class VaultStatus(BaseModel):
    """Encrypted vault status."""

    initialized: bool = False
    locked: bool = True
    entries: int = 0
    vault_dir: str = ""


# ============================================================================
# Configuration
# ============================================================================


class LLMProviderConfig(BaseModel):
    """LLM provider connection configuration (API endpoint + credentials)."""

    name: str
    base_url: str
    api_key_env: str
    driver: str = ""
    models: dict[str, Any] = Field(default_factory=dict)
    max_tokens: int | None = None
    default_model: str = ""
    # Inline API key takes precedence over api_key_env; masked as "***" in Web/API.
    api_key: str = ""
    # Whether this provider participates in /model listing & first-run; disabled stays configured but hidden.
    enabled: bool = True


class LLMProfileConfig(BaseModel):
    """Routing entry: maps a logical profile to a provider + model."""

    provider: str = ""
    model: str = ""
    max_tokens: int | None = None
    temperature: float | None = None
    inherit: str | None = None


class ContextCompressionSettings(BaseModel):
    """Configurable context compression knobs."""

    threshold: float = 0.80
    preserve_ratio: float = 0.30
    min_compressible_fraction: float = 0.05


class MatrixConfig(BaseModel):
    """Matrix 接入层配置：gomatrix 由 coara 托管（married sidecar）。

    定位见 docs/接入层-gomatrix.md——gomatrix 是 coara 的内嵌接入层，核心
    职责是连接手机与本地 coara；外部智能体可接入，但只是访客。coara 负责
    gomatrix 的拉起/看护/关停，数据目录归 `<coara_home>/matrix/`。
    """

    homeserver: str = ""
    user: str = ""
    password: str = ""
    # Room for outbound event reports (webhook auto_run). Falls back to last remote channel.
    notify_room_id: str = ""
    # GoMatrix server settings (used for default connection URL)
    server_name: str = "coara.local"
    port: int = 8008
    # gomatrix 托管开关与二进制覆盖：默认开启（手机连接是核心功能）；
    # 端口上已有健康实例时接入之，不动其生命周期（adopt）。
    host_enabled: bool = True
    host_binary: str = ""
    # 访客（非 owner 发送者，含外部智能体）房间白名单：["*"] 全部放行（默认）；
    # 空列表 = 全面拒绝访客；否则按房间 ID 精确匹配。访客消息一律走
    # untrusted 沙箱与审批链，owner 名单成员不受此限制。
    guest_rooms: list[str] = Field(default_factory=lambda: ["*"])
    # 隧道收编：gomatrix [tunnel] 由 coara 配置统管，spawn 时写入数据目录 toml。
    # None/空 = 该键以 toml 现状为准（不碰）；显式设置后以 coara 配置为准。
    tunnel_enabled: bool | None = None
    tunnel_mode: str = ""  # quick | named
    tunnel_token: str = ""
    tunnel_public_url: str = ""


class SkillsConfig(BaseModel):
    """Default skills injected into Root system prompt (when agent YAML include is empty)."""

    default_include: list[str] = Field(
        default_factory=list,
        description='Skill names to preload; use ["*"] for all discovered skills',
    )
    default_exclude: list[str] = Field(default_factory=list)


class OutputTruncationConfig(BaseModel):
    """LLM output truncation detection and recovery policy."""

    enabled: bool = True
    default_policy: str = "force_tool"  # force_tool | auto_continue | warn_only
    max_continuations: int = Field(default=3, ge=0)
    long_text_threshold_chars: int = Field(default=4000, ge=0)
    output_token_ratio: float = Field(default=0.98, ge=0.5, le=1.0)


class ToolOutputStoreConfig(BaseModel):
    """Large tool result spill to coara Home (layered budgets — see spill_policy.py)."""

    enabled: bool = True
    spill_threshold_bytes: int = Field(
        default=25_000,
        ge=0,
        description="Default per-tool threshold when no named override applies",
    )
    batch_budget_bytes: int = Field(
        default=200_000,
        ge=0,
        description="Max combined model-facing tool output per LLM tool-call batch; 0=disable",
    )
    preview_head_chars: int = Field(default=2_000, ge=0)
    preview_tail_chars: int = Field(default=8_000, ge=0)
    tool_thresholds: dict[str, int] = Field(
        default_factory=dict,
        description="Optional per-tool threshold overrides (tool name → bytes)",
    )
    retention_days: int = Field(default=30, ge=1)


class RulesGlobConfig(BaseModel):
    """Path-glob matched rules injection."""

    enabled: bool = True
    max_total_chars: int = Field(default=8000, ge=0)
    max_rule_chars: int = Field(default=2000, ge=0)


class ShellNotifyConfig(BaseModel):
    """Shell output pattern wake defaults."""

    enabled: bool = True
    default_debounce_ms: int = Field(default=5000, ge=0)
    default_max_notifications: int = Field(default=3, ge=1)


class RuntimeEnhancementsConfig(BaseModel):
    """Runtime enhancements (tool spill, rules glob, shell wake)."""

    tool_output_store: ToolOutputStoreConfig = Field(default_factory=ToolOutputStoreConfig)
    rules_glob: RulesGlobConfig = Field(default_factory=RulesGlobConfig)
    shell_notify: ShellNotifyConfig = Field(default_factory=ShellNotifyConfig)


class SessionConfig(BaseModel):
    """User session lifecycle settings."""

    # Keep default in sync with Android ``SessionInactivityTracker.AUTO_NEW_SESSION_MS``
    # (2h session gap — last conversation / last user message).
    idle_timeout_seconds: float = Field(
        default=7200.0,
        ge=0.0,
        description="Start a new session after this many seconds without a user message; 0 = disable",
    )
    idle_check_interval_seconds: float = Field(
        default=60.0,
        ge=1.0,
        description="How often to check for session idle timeout",
    )
    janitor_min_interval_seconds: float = Field(
        default=600.0,
        ge=0.0,
        description=(
            "Min seconds between janitor dispatches per workspace; triggers inside the "
            "window are deferred (latest snapshot wins), 0 = no cooldown"
        ),
    )


class RecordsConfig(BaseModel):
    """Local records subsystem (agent notes + digests; tool on janitor/daily only).

    ``enabled`` 是 agent 写入总开关（含 daily）：false 时不建 agent 子树、不写笔记。
    由配置助手改 config.yaml 的 records.enabled，改完需重启内核。不再有 pause 工具动作。
    """

    enabled: bool = True
    default_ttl_days: int | None = 180
    daily_curator_enabled: bool = True
    curator_timezone: str = "Asia/Shanghai"
    daily_provider: str | None = None
    daily_model: str | None = None


class ToolsConfig(BaseModel):
    """主会话工具开关：disabled 列表内的工具不注入 prompt 且执行被拒（实验/裁剪用）。

    auto_approve：免审批的磁盘工具包名单（自建工具默认每次调用都需审批）。
    """

    disabled: list[str] = Field(default_factory=list)
    auto_approve: list[str] = Field(default_factory=list)


class CoaraConfig(BaseModel):
    """Global coara configuration."""

    providers: dict[str, LLMProviderConfig] = Field(default_factory=dict)
    llm_profiles: dict[str, LLMProfileConfig] = Field(default_factory=dict)
    default_profile: str = "agent.main"
    default_provider: str = ""
    default_model: str = ""
    workspace_dir: Path | None = None
    coara_home: Path | None = None
    vault_enabled: bool = True
    skills_enabled: bool = True
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    # 后台 bash 任务硬上限（秒）的配置投影；0/负值关闭兜底。注意 bash_runner 实际直接读 raw
    # config（resolve_background_task_timeout），不经此字段
    background_task_timeout_seconds: float | None = None
    log_level: str = "INFO"
    context_compression: ContextCompressionSettings = Field(default_factory=ContextCompressionSettings)
    matrix: MatrixConfig = Field(default_factory=MatrixConfig)
    output_truncation: OutputTruncationConfig = Field(default_factory=OutputTruncationConfig)
    runtime_enhancements: RuntimeEnhancementsConfig = Field(default_factory=RuntimeEnhancementsConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    records: RecordsConfig = Field(default_factory=RecordsConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
