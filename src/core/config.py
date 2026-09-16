"""Configuration loading for coara."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from src.core.coara_home import (
    _coerce_existing_path,
    home_llm_preferences_path,
    iter_home_config_files,
    resolve_bootstrap_coara_home,
    resolve_config_home,
    system_dir_for_home,
    user_dir_for_home,
)
from src.core.errors import ConfigError, ProviderNotFoundError
from src.core.json_store import write_text_atomic
from src.core.logger import logger
from src.core.types import (
    CoaraConfig,
    ContextCompressionSettings,
    LLMProfileConfig,
    LLMProviderConfig,
    MatrixConfig,
    OutputTruncationConfig,
    RecordsConfig,
    RuntimeEnhancementsConfig,
    SessionConfig,
    SkillsConfig,
    ToolsConfig,
)


def _workspace_config_dir() -> Path:
    """Workspace-level config directory: ``<cwd>/.coara``."""
    return (Path.cwd() / ".coara").resolve()


def _repo_root() -> Path | None:
    """Return the coara source repository root (where ``pyproject.toml`` lives)."""
    import src

    current = Path(src.__file__).resolve().parent
    for _ in range(8):
        if (current / "pyproject.toml").is_file():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def _iter_env_file_paths(raw_config: dict[str, Any] | None = None) -> list[Path]:
    """``<coara_home>/system/.env``，开发机加 ``<repo_root>/.env``。

    repo 私货只在显式开发标记（``COARA_DEV=1``）下才加载——用户机若误落 repo
    布局（手动 clone / 装进含 pyproject.toml 的目录树），开发私货会以最高优先级
    静默覆盖用户在 WebUI 填的配置，排障极难定位（「改了不生效」）。开发机显式
    标记保持原行为；用户机默认只认 coara_home，杜绝遮蔽。
    """
    home = resolve_config_home(raw_config)
    paths: list[Path] = []
    sys_env = system_dir_for_home(home) / ".env"
    paths.append(sys_env)
    root = _repo_root()
    if root is not None and _is_dev_environment():
        root_env = root / ".env"
        if root_env not in paths:
            paths.append(root_env)
    return paths


def _is_dev_environment() -> bool:
    """开发机标记：显式 ``COARA_DEV=1`` 才加载 repo 私货配置（防用户机被遮蔽）。"""
    import os

    return os.environ.get("COARA_DEV", "").strip() in ("1", "true", "True", "yes")


def _iter_config_yaml_paths(raw_config: dict[str, Any] | None = None) -> list[Path]:
    """YAML loading order (later = higher priority):

    ``<coara_home>/system/`` → ``<repo_root>/``（仅开发机）→ user overrides.

    repo 私货同样只在显式开发标记下加载，理由同 ``_iter_env_file_paths``。
    """
    home = resolve_config_home(raw_config)
    repo_files: list[Path] = []
    root = _repo_root()
    if root is not None and _is_dev_environment():
        repo_dir = root
        for name in ("providers.yaml", "config.yaml"):
            path = repo_dir / name
            if path.is_file():
                repo_files.append(path)
    return [*iter_home_config_files(home), *repo_files]


def _iter_llm_preference_paths(raw_config: dict[str, Any] | None = None) -> list[Path]:
    home = resolve_config_home(raw_config)
    return [home_llm_preferences_path(home)]


def _find_writable_config_yaml(raw_config: dict[str, Any] | None = None) -> Path:
    """Find a writable config.yaml that will be read on next startup.

    Priority (must match _iter_config_yaml_paths read order, highest first):
    1. ``<repo_root>/config.yaml`` — repo-level config (developer override)
    2. ``<coara_home>/users/default/config.yaml`` — user-level override
    3. ``<coara_home>/system/config.yaml`` — canonical user config
    4. ``<cwd>/.coara/config.yaml`` — fallback (creates parent dir)

    注意：写入按**读序最高**层落盘（repo > users/default > system），否则
    WebUI 保存的设置会被更高优先级层遮蔽、重启即回退——审计 P0 #4。
    ``<repo_root>/`` 层仅开发机存在（用户机无 repo），用户机上最热层是
    users/default，与读取端 iter_home_config_files 的覆盖语义一致。

    Returning a path that isn't on the read path would silently break
    settings toggles (e.g. ``cli.theme`` saved but never loaded).
    """
    home = resolve_config_home(raw_config)

    root = _repo_root()
    if root is not None:
        root_config = root / "config.yaml"
        if root_config.exists():
            return root_config

    user_config = user_dir_for_home(home) / "config.yaml"
    if user_config.exists():
        return user_config

    sys_config = system_dir_for_home(home) / "config.yaml"
    if sys_config.exists():
        return sys_config

    path = _workspace_config_dir() / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _find_writable_providers_yaml(raw_config: dict[str, Any] | None = None) -> Path:
    """Find a writable providers.yaml that will be read on next startup.

    Priority (must match _iter_config_yaml_paths read order, highest first):
    1. ``<repo_root>/providers.yaml`` — repo-level config (developer override)
    2. ``<coara_home>/system/providers.yaml`` — canonical user config
       (读序中 users/default 层只读 config.yaml，不读 providers.yaml，故
       providers 无 user 级覆盖层，与 iter_home_config_files 一致)
    3. ``<cwd>/.coara/providers.yaml`` — fallback (creates parent dir)

    Returning a path that isn't on the read path would silently break
    provider edits (saved but never loaded on next start).
    """
    home = resolve_config_home(raw_config)

    # repo 私货只在开发机生效（与读序 _iter_config_yaml_paths 对齐）——用户机
    # 上 repo/providers.yaml 读不到，却会被这里当可写目标，配置写进去下次启动
    # 又读不回（「改了不生效」）。
    root = _repo_root()
    if root is not None and _is_dev_environment():
        root_providers = root / "providers.yaml"
        if root_providers.exists():
            return root_providers

    sys_providers = system_dir_for_home(home) / "providers.yaml"
    if sys_providers.exists():
        return sys_providers

    path = _workspace_config_dir() / "providers.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class ConfigManager:
    """Load and merge user, workspace, and provider configuration."""

    def __init__(self):
        self._config: CoaraConfig | None = None
        self._raw_config: dict[str, Any] = {}
        self._providers: dict[str, LLMProviderConfig] = {}
        self._llm_profiles: dict[str, LLMProfileConfig] = {}
        # 配置加载错误收集器：YAML 解析失败/字段值非法时记录，启动后向用户上报。
        self._load_errors: list[str] = []

    def _iter_yaml_load_paths(self, config_paths: list[Path] | None = None) -> list[Path]:
        if config_paths is not None:
            return list(config_paths)
        paths = _iter_config_yaml_paths(None)
        # 趟2 降载：只为取出可能存在的 coara_home 键（鸡生蛋问题），
        # 不做全量深合并——单键覆盖与深合并对取该键语义等价
        coara_home_raw: Any = None
        for path in paths:
            try:
                with open(path, encoding="utf-8") as handle:
                    data = yaml.safe_load(handle) or {}
                if "coara_home" in data:
                    coara_home_raw = data["coara_home"]
            except Exception:
                continue
        merged: dict[str, Any] = {"coara_home": coara_home_raw} if coara_home_raw is not None else {}
        extra = _iter_config_yaml_paths(merged)
        ordered: list[Path] = []
        seen: set[Path] = set()
        for path in [*paths, *extra]:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            ordered.append(path)
        return ordered

    async def reload(self, config_paths: list[Path] | None = None) -> CoaraConfig:
        """Force reload configuration from disk."""
        self._config = None
        return await self.load(config_paths)

    async def load(self, config_paths: list[Path] | None = None) -> CoaraConfig:
        self._raw_config = {}
        self._providers = {}
        self._llm_profiles = {}

        self._load_env()
        loaded: set[Path] = set()
        for path in self._iter_yaml_load_paths(config_paths=config_paths):
            resolved = path.resolve()
            if resolved in loaded:
                continue
            loaded.add(resolved)
            try:
                with open(path, encoding="utf-8") as handle:
                    data = yaml.safe_load(handle) or {}
                self._merge_config(data)
            except FileNotFoundError:
                continue
            except Exception as exc:
                self._load_errors.append(f"配置文件 {path} 解析失败：{exc}")
                logger.warning(f"Failed to load config from {path}: {exc}")

        if config_paths is None:
            self._require_config_layout()

        self._load_llm_preferences_files()

        await self._load_providers()
        self._load_llm_profiles()

        default_provider = self._raw_config.get("default_provider", "")
        if not default_provider and len(self._providers) == 1:
            default_provider = next(iter(self._providers))

        default_model = self._raw_config.get("default_model", "")
        if not default_model and default_provider and default_provider in self._providers:
            default_model = self._providers[default_provider].models.get("default", "")

        context_compression = self._safe_validate(
            "context_compression",
            lambda raw: ContextCompressionSettings(**(raw or {})),
            ContextCompressionSettings(),
        )
        matrix_raw = self._raw_config.get("matrix", {})
        try:
            matrix_port = int(matrix_raw.get("port", 8008))
        except (TypeError, ValueError):
            self._load_errors.append(f"matrix.port 值非法（{matrix_raw.get('port')!r}），已回退默认 8008")
            matrix_port = 8008
        matrix_config = MatrixConfig(
            homeserver=matrix_raw.get("homeserver", ""),
            user=matrix_raw.get("user", ""),
            password=matrix_raw.get("password", ""),
            notify_room_id=str(matrix_raw.get("notify_room_id") or ""),
            server_name=matrix_raw.get("server_name", "coara.local"),
            port=matrix_port,
        )

        skills_raw = self._raw_config.get("skills", {})
        skills_config = SkillsConfig(
            default_include=list(skills_raw.get("default_include") or []),
            default_exclude=list(skills_raw.get("default_exclude") or []),
        )

        output_truncation_config = self._safe_validate(
            "output_truncation",
            OutputTruncationConfig.model_validate,
            OutputTruncationConfig(),
        )
        runtime_enhancements_config = self._safe_validate(
            "runtime_enhancements",
            RuntimeEnhancementsConfig.model_validate,
            RuntimeEnhancementsConfig(),
        )
        session_config = self._safe_validate(
            "session",
            SessionConfig.model_validate,
            SessionConfig(),
        )
        records_config = self._safe_validate(
            "records",
            RecordsConfig.model_validate,
            RecordsConfig(),
        )

        default_profile = str(self._raw_config.get("default_profile") or "agent.main")

        self._config = CoaraConfig(
            providers=self._providers,
            llm_profiles=self._llm_profiles,
            default_profile=default_profile,
            default_provider=default_provider,
            default_model=default_model,
            workspace_dir=Path.cwd(),
            coara_home=self._resolve_coara_home_config(),
            vault_enabled=self._raw_config.get("vault_enabled", True),
            skills_enabled=self._raw_config.get("skills_enabled", True),
            skills=skills_config,
            background_task_timeout_seconds=self._raw_config.get("background_task_timeout_seconds"),
            log_level=self._raw_config.get("log_level", "INFO"),
            context_compression=context_compression,
            matrix=matrix_config,
            output_truncation=output_truncation_config,
            runtime_enhancements=runtime_enhancements_config,
            session=session_config,
            records=records_config,
            tools=self._safe_validate("tools", ToolsConfig.model_validate, ToolsConfig()),
        )
        from src.context.window import context_window_manager

        context_window_manager.configure(
            compression_threshold=context_compression.threshold,
            compression_preserve_ratio=context_compression.preserve_ratio,
            min_compressible_fraction=context_compression.min_compressible_fraction,
        )
        # 执行沙箱的装配移至 bootstrap（core 不依赖 tools）——此处仅加载配置，
        # 沙箱实例由 runtime_bootstrap 依据 get_security_config() 配置。

        logger.info(f"Configuration loaded: {len(self._providers)} providers")
        return self._config

    def _require_config_layout(self) -> None:
        """Fail fast when canonical config is missing (avoid silent 0-provider startup)."""
        home = resolve_config_home(self._raw_config)
        if (system_dir_for_home(home) / "providers.yaml").is_file():
            return
        root = _repo_root()
        if root is not None and (root / "providers.yaml").is_file():
            return
        raise ConfigError(
            "No LLM configuration found. "
            f"Expected {system_dir_for_home(home) / 'providers.yaml'} or "
            f"<repo_root>/providers.yaml. "
            "Copy providers.yaml.example to one of these locations."
        )

    def _resolve_coara_home_config(self) -> Path | None:
        coerced = _coerce_existing_path(self._raw_config.get("coara_home"))
        if coerced is not None:
            return coerced
        root = _repo_root()
        # repo fallback 只用于「找到配置」，不作为 home——否则 traces/logs/vault
        # 等运行时数据会写进源码仓库（开发机 v8 根有 providers.yaml 时该分支
        # 生效过，仓库里堆满运行时产物）。home 仍走 COARA_HOME / cwd 解析。
        if root is not None and (root / "providers.yaml").is_file() and _is_dev_environment():
            logger.info("[config] repo-root providers.yaml found (dev only); home resolved separately, not repo root")
        bootstrap = resolve_bootstrap_coara_home()
        if bootstrap is not None:
            return bootstrap
        home = resolve_config_home(self._raw_config)
        if (system_dir_for_home(home) / "providers.yaml").is_file():
            return home
        return None

    def _load_env(self) -> None:
        for env_path in _iter_env_file_paths(None):
            if env_path.exists():
                load_dotenv(env_path, override=True)
                logger.debug(f"Loaded environment from {env_path}")

    @staticmethod
    def _deep_merge_dict(target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                ConfigManager._deep_merge_dict(target[key], value)
            else:
                target[key] = value

    def _merge_config(self, data: dict[str, Any]) -> None:
        self._deep_merge_dict(self._raw_config, data)

    def merge_config(self, data: dict[str, Any]) -> None:
        """Merge a config patch into the in-memory raw config (public wrapper)."""
        self._merge_config(data)

    def _load_llm_preferences_files(self) -> None:
        # Paths for persisted /model selection (loaded last; highest priority).
        for prefs_path in _iter_llm_preference_paths(self._raw_config):
            if not prefs_path.exists():
                continue
            try:
                with open(prefs_path, encoding="utf-8") as handle:
                    data = yaml.safe_load(handle) or {}
                self._merge_config(data)
                logger.debug(f"Loaded LLM preferences from {prefs_path}")
            except Exception as exc:
                logger.warning(f"Failed to load LLM preferences from {prefs_path}: {exc}")

    def _rebuild_providers_cache(self) -> None:
        """Rebuild typed ``_providers`` from ``_raw_config['providers']``."""
        providers_data = dict(self._raw_config.get("providers", {}))
        for name, provider_data in providers_data.items():
            if not isinstance(provider_data, dict):
                continue
            try:
                models = dict(provider_data.get("models") or {})
                default_model = str(provider_data.get("default_model") or models.get("default") or "")
                if default_model and "default" not in models:
                    models["default"] = default_model
                provider = LLMProviderConfig(
                    name=name,
                    base_url=provider_data.get("base_url", ""),
                    api_key_env=provider_data.get("api_key_env", ""),
                    driver=str(provider_data.get("driver") or ""),
                    models=models,
                    max_tokens=provider_data.get("max_tokens"),
                    default_model=default_model,
                    api_key=str(provider_data.get("api_key") or ""),
                    enabled=bool(provider_data.get("enabled", True)),
                )
            except ValidationError as exc:
                logger.warning(f"Invalid provider config for '{name}': {exc}")
                continue

            self._providers[name] = provider

    async def _load_providers(self) -> None:
        self._rebuild_providers_cache()

    def _load_llm_profiles(self) -> None:
        profiles_data = dict(self._raw_config.get("llm_profiles") or {})
        for profile_name, profile_data in profiles_data.items():
            if not isinstance(profile_data, dict):
                continue
            try:
                self._llm_profiles[profile_name] = LLMProfileConfig(
                    provider=str(profile_data.get("provider") or ""),
                    model=str(profile_data.get("model") or ""),
                    max_tokens=profile_data.get("max_tokens"),
                    temperature=profile_data.get("temperature"),
                    inherit=profile_data.get("inherit"),
                )
            except ValidationError as exc:
                logger.warning(f"Invalid llm_profile '{profile_name}': {exc}")

    def get_llm_profile(self, name: str) -> LLMProfileConfig:
        if name not in self._llm_profiles:
            raise ProviderNotFoundError(f"LLM profile not found: {name}")
        return self._llm_profiles[name]

    def list_llm_profiles(self) -> list[str]:
        return list(self._llm_profiles.keys())

    @property
    def load_errors(self) -> list[str]:
        """配置加载过程中收集的非致命错误（YAML 解析失败/字段值非法回退默认）。"""
        return list(self._load_errors)

    def _safe_validate(self, section: str, validator: Any, fallback: Any) -> Any:
        """model_validate 容错包装：单个 section 非法值回退默认 记 _load_errors 不阻断启动。"""
        raw = self._raw_config.get(section) or {}
        try:
            return validator(raw)
        except Exception as exc:
            self._load_errors.append(f"配置节 {section} 值非法（{exc}），已回退默认值")
            return fallback

    @property
    def config(self) -> CoaraConfig:
        if self._config is None:
            raise ConfigError("Configuration not loaded. Call load() first.")
        return self._config

    def get_provider(self, name: str) -> LLMProviderConfig:
        if name not in self._providers:
            raise ProviderNotFoundError(name)
        return self._providers[name]

    def get_security_config(self) -> dict[str, Any]:
        """Get the security configuration (sandbox, call_policy, owner_matrix_ids)."""
        return dict(self._raw_config.get("security", {}))

    def get_api_key(self, provider_name: str) -> str:
        provider = self.get_provider(provider_name)
        # Inline api_key (from Web UI / providers.yaml) wins over the env var.
        if provider.api_key:
            return provider.api_key
        if not provider.api_key_env:
            raise ConfigError(f"No api_key configured for provider '{provider_name}'")

        api_key = os.getenv(provider.api_key_env, "")
        if not api_key:
            raise ConfigError(f"Environment variable '{provider.api_key_env}' not set")
        return api_key

    def list_providers(self) -> list[str]:
        return list(self._providers.keys())

    def set_tools_disabled(self, disabled: list[str]) -> None:
        """Persist tools.disabled to config.yaml and update in-memory config."""
        self.save_config_yaml({"tools": {"disabled": list(disabled)}})
        if self._config is not None:
            self._config.tools.disabled = list(disabled)

    def get_raw_config(self) -> dict[str, Any]:
        """Return the merged raw config dict as loaded from disk.

        Safe to call after ``load()`` has been invoked at least once.
        """
        return dict(self._raw_config)

    def save_config_yaml(self, data: dict[str, Any]) -> None:
        """Merge `data` into the writable config.yaml and persist.

        Never persists ``providers`` / ``llm_profiles`` here — those belong in
        ``providers.yaml`` (see ``save_providers_yaml``). Keeping them in
        config.yaml would shadow providers.yaml on the next load. Any copies
        already present from a previous buggy save are removed on write.
        """
        path = _find_writable_config_yaml(self._raw_config)
        if not path.exists():
            raise ConfigError("No config.yaml found")

        with open(path, encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}

        # coara_home 切换检测：落待迁移标记到旧 home（同步写入函数不做交互/迁移）。
        # 下次启动早期检测到标记后确认并迁移全套数据到新 home。
        if "coara_home" in data:
            old_home = _coerce_existing_path(existing.get("coara_home"))
            new_home = _coerce_existing_path(data.get("coara_home"))
            if old_home is not None and new_home is not None and old_home != new_home:
                try:
                    from src.core.home_migration import home_has_data, mark_pending_migration

                    if home_has_data(old_home):
                        mark_pending_migration(old_home, new_home)
                except Exception as exc:
                    logger.warning(f"Failed to mark coara_home migration ({old_home} -> {new_home}): {exc}")

        merged = dict(existing)
        self._deep_merge_dict(merged, data)
        for key in ("providers", "llm_profiles"):
            merged.pop(key, None)

        write_text_atomic(
            path,
            yaml.dump(merged, allow_unicode=True, sort_keys=False, default_flow_style=False),
        )

        # Keep in-process config in sync (CLI picks up dashboard edits on next classify).
        self._merge_config(data)
        if self._config is not None and ("skills" in data or "skills" in self._raw_config):
            skills_raw = self._raw_config.get("skills", {})
            self._config.skills.default_include = list(skills_raw.get("default_include") or [])
            self._config.skills.default_exclude = list(skills_raw.get("default_exclude") or [])

    def save_providers_yaml(self, data: dict[str, Any]) -> None:
        """Merge ``providers`` into providers.yaml without wiping sibling keys.

        ``providers.yaml`` also holds ``default_profile`` / ``default_provider`` /
        ``default_model`` / ``llm_profiles`` / ``security.call_policy``. Replacing
        the whole file with ``{providers: …}`` would erase those on every Web save.
        """
        path = _find_writable_providers_yaml(self._raw_config)
        providers = data.get("providers")
        if not isinstance(providers, dict):
            raise ConfigError("save_providers_yaml requires data['providers'] as a dict")

        existing: dict[str, Any] = {}
        if path.is_file():
            try:
                with open(path, encoding="utf-8") as handle:
                    loaded = yaml.safe_load(handle) or {}
                if isinstance(loaded, dict):
                    existing = loaded
            except Exception as exc:
                # 读失败时按空合并会原子写回抹掉全部兄弟键（default_profile/
                # llm_profiles/security）——fail-closed：拒绝写，与 TypedJsonStore 同策略
                raise ConfigError(f"providers.yaml 读取失败，拒绝覆盖写入（先修复或备份该文件）：{exc}") from exc

        merged = dict(existing)
        merged["providers"] = providers

        write_text_atomic(
            path,
            yaml.dump(merged, allow_unicode=True, sort_keys=False, default_flow_style=False),
        )

        # Keep in-process raw + typed provider cache aligned with the new map.
        self._raw_config["providers"] = dict(providers)
        self._providers = {}
        self._rebuild_providers_cache()


config_manager = ConfigManager()


def reset_config_manager_for_tests() -> None:
    """Clear cached config between pytest cases (prevents leaking developer COARA_HOME)."""
    config_manager._config = None
    config_manager._raw_config = {}
    config_manager._providers = {}
    config_manager._llm_profiles = {}


# ---------------------------------------------------------------------------
# Secret masking utility for dashboard config exposure
# ---------------------------------------------------------------------------

_SECRET_KEYS: set[str] = {
    "password",
    "api_key",
    "token",
    "secret",
    "auth",
    "access_token",
    "refresh_token",
    "client_secret",
}


def _is_secret_key(key: str) -> bool:
    """键名是否为敏感键：确切命中，或以已知敏感词为**词边界后缀**。

    子串匹配会误伤 ``author``（含 auth）与 ``max_tokens``（含 token）这类
    普通键；只认「键 == 敏感词」或「键以 _key/_token/_secret/_password/
    /authorization 结尾（或无分隔符直接以其结尾，如 apitoken）」。
    """
    lowered = key.lower()
    if lowered in _SECRET_KEYS:
        return True
    suffixes = (
        "_key",
        "key",
        "_token",
        "token",
        "_secret",
        "secret",
        "_password",
        "password",
        "authorization",
    )
    for suffix in suffixes:
        if lowered.endswith(suffix):
            stem = lowered[: -len(suffix)]
            # 词边界：前一个字符必须是分隔符（_ 或 -），或敏感词就是整个键
            if not stem or stem.endswith(("_", "-")):
                return True
    return False


def mask_secrets(obj: Any) -> Any:
    """Recursively mask sensitive string values in a JSON-serializable object.

    Keys matching a secret keyword (exact key name, or key ending in a known
    sensitive suffix at a word boundary) have their string values replaced
    with ``"***"``.  Non-string values and nested structures are preserved.
    """
    if isinstance(obj, dict):
        masked: dict[str, Any] = {}
        for key, value in obj.items():
            if isinstance(value, str) and _is_secret_key(str(key)):
                masked[key] = "***"
            else:
                masked[key] = mask_secrets(value)
        return masked
    if isinstance(obj, list):
        return [mask_secrets(item) for item in obj]
    return obj


async def get_config(force_reload: bool = False) -> CoaraConfig:
    """Convenience helper.

    Args:
        force_reload: If True, discard cached config and reload from disk.
    """
    if config_manager._config is None or force_reload:
        await config_manager.load()
    return config_manager.config
