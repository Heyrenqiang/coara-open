"""COARA_HOME 系统区升级：出厂目录 rebase + 退役清理，用户密钥与自定义保留。

契约（装 / 卸 / 再装循环的稳健性）：

- **程序目录**（``%LOCALAPPDATA%\\coara``）：安装器整包替换，与本模块无关
- **用户资产**（``.env`` 密钥、工作空间内容、会话带、Matrix 凭证、自加 provider）：
  永不删除密钥与空间数据；若偏好/空间绑定仍指向**已退役**出厂名，只清空绑定（回落全局默认）
- **出厂目录**（模板声明的 stock provider / 配套 llm_profiles / ``env.example``）：
  每次产品升级按模板 rebase；已从模板退役的 stock 名从 ``providers.yaml`` 摘掉
- **用户自加 provider**（不在出厂名单、也不在退役名单）：原样保留

安装器只「缺失才拷」会把旧 ``providers.yaml`` 冻死在机上（Agnes 幽灵等）。
本模块在 **安装后** 与 **内核启动** 双入口幂等执行，消除该冻死态。
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.core.json_store import write_text_atomic

# 调高 = 强制再跑一遍迁移规则（例如退役名单变更）。
HOME_SCHEMA_VERSION = 2

# 曾随出厂模板下发、现已撤出模板的 stock 名。有 key 也摘 yaml——密钥仍留在 .env，
# 用户若还要用可自行加回自定义 provider；产品目录不再冒充「出厂支持」。
RETIRED_STOCK_PROVIDERS: frozenset[str] = frozenset({"agnes"})

_SCHEMA_FILENAME = "home_schema.yaml"
_PROVIDERS_NAME = "providers.yaml"
_CONFIG_NAME = "config.yaml"
_ENV_EXAMPLE_NAME = "env.example"


@dataclass
class UpgradeReport:
    """一次升级的可观测结果（测试与安装日志用）。"""

    schema_from: int = 0
    schema_to: int = HOME_SCHEMA_VERSION
    product_version: str = ""
    rebased_providers: list[str] = field(default_factory=list)
    retired_providers: list[str] = field(default_factory=list)
    preserved_custom: list[str] = field(default_factory=list)
    providers_written: bool = False
    env_example_written: bool = False
    default_provider_cleared: str = ""
    preferences_scrubbed: bool = False
    workspace_bindings_cleared: list[str] = field(default_factory=list)
    skipped: bool = False
    reason: str = ""


def resolve_product_templates_dir() -> Path | None:
    """定位随包 templates/（安装根或开发仓）。"""
    from src.core.coara_home import _install_template_dir

    installed = _install_template_dir()
    if installed is not None:
        return installed
    # 开发机：仓内 deploy/official/templates
    repo_templates = Path(__file__).resolve().parents[2] / "deploy" / "official" / "templates"
    if (repo_templates / _PROVIDERS_NAME).is_file():
        return repo_templates
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _read_schema(system: Path) -> dict[str, Any]:
    return _load_yaml(system / _SCHEMA_FILENAME)


def _write_schema(system: Path, *, product_version: str) -> None:
    payload = {
        "version": HOME_SCHEMA_VERSION,
        "product_version": product_version or "",
        "upgraded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    write_text_atomic(system / _SCHEMA_FILENAME, _dump_yaml(payload))


def _model_id(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        return str(entry.get("id") or "").strip()
    return ""


def _rebase_stock_provider(user_entry: dict[str, Any] | None, template_entry: dict[str, Any]) -> dict[str, Any]:
    """出厂块以模板为准；保留用户 enabled；用户自加的模型 id 追加在末尾。"""
    out = copy.deepcopy(template_entry)
    out.pop("api_key", None)
    if isinstance(user_entry, dict) and "enabled" in user_entry:
        out["enabled"] = user_entry.get("enabled")

    tmpl_models = out.get("models") if isinstance(out.get("models"), dict) else {}
    tmpl_available = list(tmpl_models.get("available") or [])
    tmpl_ids = {_model_id(x) for x in tmpl_available if _model_id(x)}

    extras: list[Any] = []
    if isinstance(user_entry, dict):
        user_models = user_entry.get("models") if isinstance(user_entry.get("models"), dict) else {}
        for item in user_models.get("available") or []:
            mid = _model_id(item)
            if mid and mid not in tmpl_ids:
                extras.append(copy.deepcopy(item))
                tmpl_ids.add(mid)
    if extras:
        models = dict(tmpl_models) if isinstance(tmpl_models, dict) else {}
        models["available"] = list(tmpl_available) + extras
        out["models"] = models
    return out


def _merge_llm_profiles(
    user_profiles: dict[str, Any],
    template_profiles: dict[str, Any],
    stock_providers: set[str],
) -> dict[str, Any]:
    """模板里的 profile 覆盖同名；退役 stock 相关 profile 删除；其余用户 profile 保留。"""
    merged = dict(user_profiles) if isinstance(user_profiles, dict) else {}
    for name, body in (template_profiles or {}).items():
        merged[name] = copy.deepcopy(body)

    drop: list[str] = []
    for name, body in list(merged.items()):
        if not isinstance(body, dict):
            continue
        provider = str(body.get("provider") or "").strip()
        if provider in RETIRED_STOCK_PROVIDERS:
            drop.append(name)
            continue
        # agent.<stock> 且 stock 已不在出厂名单 → 删（避免幽灵 agent.agnes）
        if name.startswith("agent.") and provider and provider not in stock_providers:
            if provider in RETIRED_STOCK_PROVIDERS or name.removeprefix("agent.") in RETIRED_STOCK_PROVIDERS:
                drop.append(name)
    for name in drop:
        merged.pop(name, None)
    # 显式清掉退役名对应的 agent.*（即使 body 不规范）
    for retired in RETIRED_STOCK_PROVIDERS:
        merged.pop(f"agent.{retired}", None)
    return merged


def upgrade_providers_yaml(
    user_path: Path,
    template_path: Path,
) -> tuple[bool, UpgradeReport]:
    """Rebase ``providers.yaml``；返回 (是否写盘, 报告片段)。"""
    report = UpgradeReport()
    template = _load_yaml(template_path)
    tmpl_providers = template.get("providers")
    if not isinstance(tmpl_providers, dict) or not tmpl_providers:
        report.reason = "template providers missing"
        report.skipped = True
        return False, report

    stock_ids = {str(k) for k in tmpl_providers}
    user = _load_yaml(user_path) if user_path.is_file() else {}
    user_providers = user.get("providers") if isinstance(user.get("providers"), dict) else {}

    new_providers: dict[str, Any] = {}
    # 出厂顺序跟模板
    for name, tmpl_body in tmpl_providers.items():
        if not isinstance(tmpl_body, dict):
            continue
        user_body = user_providers.get(name)
        new_providers[name] = _rebase_stock_provider(
            user_body if isinstance(user_body, dict) else None,
            tmpl_body,
        )
        report.rebased_providers.append(str(name))

    for name, body in user_providers.items():
        key = str(name)
        if key in stock_ids:
            continue
        if key in RETIRED_STOCK_PROVIDERS:
            report.retired_providers.append(key)
            continue
        new_providers[key] = copy.deepcopy(body)
        report.preserved_custom.append(key)

    new_doc: dict[str, Any] = {}
    # 顶层键：模板优先，再补用户有、模板没有的（除 providers/llm_profiles 已单独处理）
    for key, value in template.items():
        if key in ("providers", "llm_profiles"):
            continue
        new_doc[key] = copy.deepcopy(value)
    for key, value in user.items():
        if key in ("providers", "llm_profiles"):
            continue
        if key not in new_doc:
            new_doc[key] = copy.deepcopy(value)

    # default_provider 指向退役名 → 清空（启动 heal 会再对齐到有 key 的出厂序）
    default_provider = str(new_doc.get("default_provider") or user.get("default_provider") or "").strip()
    if default_provider in RETIRED_STOCK_PROVIDERS or (
        default_provider and default_provider not in new_providers
    ):
        if default_provider:
            report.default_provider_cleared = default_provider
        new_doc["default_provider"] = ""
        if "default_model" in new_doc:
            new_doc["default_model"] = ""
    elif "default_provider" not in new_doc and user.get("default_provider") is not None:
        new_doc["default_provider"] = user.get("default_provider")
        if "default_model" in user:
            new_doc["default_model"] = user.get("default_model")

    tmpl_profiles = template.get("llm_profiles") if isinstance(template.get("llm_profiles"), dict) else {}
    user_profiles = user.get("llm_profiles") if isinstance(user.get("llm_profiles"), dict) else {}
    new_doc["llm_profiles"] = _merge_llm_profiles(user_profiles, tmpl_profiles, stock_ids)
    new_doc["providers"] = new_providers

    # 稳定键序：常用顶层在前
    ordered: dict[str, Any] = {}
    for key in (
        "default_profile",
        "default_provider",
        "default_model",
        "providers",
        "llm_profiles",
        "security",
    ):
        if key in new_doc:
            ordered[key] = new_doc.pop(key)
    ordered.update(new_doc)

    new_text = _dump_yaml(ordered)
    old_text = user_path.read_text(encoding="utf-8") if user_path.is_file() else ""
    # 语义比较：解析后再 dump，避免空白差异误写
    if _dump_yaml(_load_yaml(user_path) if user_path.is_file() else {}) == new_text and user_path.is_file():
        # 再比一轮有序文档——上面 ordered 可能与旧文件键序不同仍应写盘一次拉齐
        old_parsed = _load_yaml(user_path)
        if old_parsed.get("providers") == ordered.get("providers") and old_parsed.get("llm_profiles") == ordered.get(
            "llm_profiles"
        ):
            # default_provider 清理仍可能需要写
            if str(old_parsed.get("default_provider") or "") == str(ordered.get("default_provider") or ""):
                report.skipped = True
                report.reason = "providers already aligned"
                return False, report

    user_path.parent.mkdir(parents=True, exist_ok=True)
    # 升级前备份一份，便于排障（只保留最近一份）
    if user_path.is_file() and old_text.strip():
        bak = user_path.with_suffix(".yaml.bak-upgrade")
        write_text_atomic(bak, old_text)
    write_text_atomic(user_path, new_text)
    report.providers_written = True
    return True, report


def _live_provider_names(providers_yaml: Path) -> set[str]:
    data = _load_yaml(providers_yaml)
    providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
    return {str(k) for k in providers}


def _scrub_preferences(home: Path, live_providers: set[str]) -> bool:
    """清空指向退役/已不存在厂商的全局默认与 agent.main 绑定。不删其它偏好。"""
    from src.core.coara_home import home_llm_preferences_path, user_dir_for_home

    candidates = [
        home_llm_preferences_path(home),
        user_dir_for_home(home) / "llm_preferences.yaml",
    ]
    # cwd 偏好可能与 home 不同；升级时两边都扫
    changed = False
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.is_file():
            continue
        data = _load_yaml(resolved)
        if not data:
            continue
        dirty = False
        default_p = str(data.get("default_provider") or "").strip()
        if default_p and (default_p in RETIRED_STOCK_PROVIDERS or default_p not in live_providers):
            data["default_provider"] = ""
            data["default_model"] = ""
            dirty = True
        profiles = data.get("llm_profiles")
        if isinstance(profiles, dict):
            main = profiles.get("agent.main")
            if isinstance(main, dict):
                mp = str(main.get("provider") or "").strip()
                if mp and (mp in RETIRED_STOCK_PROVIDERS or mp not in live_providers):
                    main.pop("provider", None)
                    main.pop("model", None)
                    dirty = True
        if dirty:
            write_text_atomic(resolved, _dump_yaml(data))
            changed = True
    return changed


def _scrub_workspace_llm_bindings(home: Path, live_providers: set[str]) -> list[str]:
    """空间绑定指向退役/失踪厂商时清空，回落全局默认。返回被清理的空间名。"""
    from src.workspace.registry import registry_path

    path = registry_path(home)
    if not path.is_file():
        return []
    data = _load_yaml(path)
    workspaces = data.get("workspaces")
    if not isinstance(workspaces, dict):
        return []
    cleared: list[str] = []
    dirty = False
    for _wid, entry in workspaces.items():
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        if not provider:
            continue
        if provider in RETIRED_STOCK_PROVIDERS or provider not in live_providers:
            entry["provider"] = None
            entry["model"] = None
            cleared.append(str(entry.get("name") or _wid))
            dirty = True
    if dirty:
        write_text_atomic(path, _dump_yaml(data))
    return cleared


def upgrade_home_system(
    home: Path,
    *,
    templates_dir: Path | None = None,
    product_version: str = "",
) -> UpgradeReport:
    """对 ``<home>/system`` 执行幂等升级。``.env`` 与用户工作空间内容一律不碰。"""
    from src.core.coara_home import system_dir_for_home

    home = Path(home).expanduser().resolve()
    system = system_dir_for_home(home)
    system.mkdir(parents=True, exist_ok=True)

    templates = Path(templates_dir) if templates_dir else resolve_product_templates_dir()
    report = UpgradeReport(product_version=product_version)
    if templates is None or not (templates / _PROVIDERS_NAME).is_file():
        report.skipped = True
        report.reason = "no product templates"
        return report

    schema = _read_schema(system)
    report.schema_from = int(schema.get("version") or 0)

    # 缺文件时先从模板补齐（与 ensure_system_config_templates 同语义）
    for name in (_PROVIDERS_NAME, _CONFIG_NAME):
        dst = system / name
        src = templates / name
        if src.is_file() and not dst.is_file():
            write_text_atomic(dst, src.read_text(encoding="utf-8"))

    providers_dst = system / _PROVIDERS_NAME
    providers_src = templates / _PROVIDERS_NAME
    written, prov_report = upgrade_providers_yaml(providers_dst, providers_src)
    report.rebased_providers = prov_report.rebased_providers
    report.retired_providers = prov_report.retired_providers
    report.preserved_custom = prov_report.preserved_custom
    report.providers_written = written
    report.default_provider_cleared = prov_report.default_provider_cleared
    if prov_report.reason and not written:
        report.reason = prov_report.reason

    live = _live_provider_names(providers_dst)
    try:
        report.preferences_scrubbed = _scrub_preferences(home, live)
    except Exception:
        report.preferences_scrubbed = False
    try:
        report.workspace_bindings_cleared = _scrub_workspace_llm_bindings(home, live)
    except Exception:
        report.workspace_bindings_cleared = []

    # env.example 纯参考：始终用模板覆盖（不含密钥）
    env_example_src = templates / _ENV_EXAMPLE_NAME
    env_example_dst = system / _ENV_EXAMPLE_NAME
    if env_example_src.is_file():
        text = env_example_src.read_text(encoding="utf-8")
        if (not env_example_dst.is_file()) or env_example_dst.read_text(encoding="utf-8") != text:
            write_text_atomic(env_example_dst, text)
            report.env_example_written = True

    if not product_version:
        product_version = _detect_product_version()
        report.product_version = product_version
    _write_schema(system, product_version=product_version)
    report.schema_to = HOME_SCHEMA_VERSION
    return report


def _detect_product_version() -> str:
    templates = resolve_product_templates_dir()
    if templates is not None:
        version_txt = templates.parent / "version.txt"
        if version_txt.is_file():
            return version_txt.read_text(encoding="utf-8").strip()
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version("coara")
    except Exception:
        return os.environ.get("COARA_VERSION", "").strip()


def main(argv: list[str] | None = None) -> int:
    """``python -m src.core.home_upgrade --home <path>``（安装器调用）。"""
    import argparse

    parser = argparse.ArgumentParser(description="Upgrade COARA_HOME system catalog")
    parser.add_argument("--home", required=True, help="COARA_HOME path")
    parser.add_argument("--templates", default="", help="Override templates dir")
    parser.add_argument("--product-version", default="", help="Installed product version stamp")
    args = parser.parse_args(argv)
    templates = Path(args.templates) if args.templates.strip() else None
    report = upgrade_home_system(
        Path(args.home),
        templates_dir=templates,
        product_version=args.product_version.strip(),
    )
    parts = []
    if report.providers_written:
        parts.append("providers rebased")
    if report.retired_providers:
        parts.append("retired=" + ",".join(report.retired_providers))
    if report.preserved_custom:
        parts.append("custom=" + ",".join(report.preserved_custom))
    if report.default_provider_cleared:
        parts.append(f"cleared_default={report.default_provider_cleared}")
    if report.preferences_scrubbed:
        parts.append("preferences scrubbed")
    if report.workspace_bindings_cleared:
        parts.append("ws_bindings=" + ",".join(report.workspace_bindings_cleared))
    if report.env_example_written:
        parts.append("env.example refreshed")
    if report.skipped and not parts:
        parts.append(f"skipped:{report.reason or 'noop'}")
    print("home_upgrade: " + ("; ".join(parts) if parts else "ok"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
