"""导出开源仓库快照：白名单复制 + 商业化痕迹剔除 + 残留扫查。

用法::

    python scripts/dev/export_open_source.py --target D:\\code_ws\\coara [--force]

做四件事

1. 按白名单把主仓库的目录树复制到目标目录（不含 .git、缓存、构建产物）
2. 剔除商业化内容：telemetry / account 两个包、android-app、license-server、
   软著材料、商业模式、发布链脚本与文档
3. 自动跳过引用了商业化包的测试文件，删掉指定的整节文档内容
4. 扫查残留商业化痕迹（私有域名、令牌、私有仓库地址、账户与试用措辞）

产物是一个可以直接 ``git init`` 的目录，不带任何 git 历史。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# 跟着开源走的顶层条目
INCLUDE_TOP = (
    "src",
    "tests",
    "skills",
    "docs",
    "scripts",
    "events",
    "examples",
    "wdl",
    "gomatrix",
    "deploy",
    "pyproject.toml",
    ".env.example",
    "config.yaml.example",
    "providers.yaml.example",
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
)

# 不进开源的目录（相对仓库根，前缀匹配）
EXCLUDE_DIRS = (
    "src/telemetry",
    "src/account",
    "docs/open-source",
    "scripts/soft-copyright",
    "scripts/tunnels",
    "deploy/gitee",
    "deploy/checklists",
    "deploy/homepage",
    "deploy/release",
    "deploy/android",
    "deploy/profiles",
    "src/ui/web/dist",
    "src/ui/web/node_modules",
)

# 不进开源的文件（相对仓库根）
EXCLUDE_FILES = (
    "deploy/MACHINES.md",
    "deploy/README.md",
    "docs/遥测.md",
    "docs/MATRIX_APP_LINK.md",
    "tests/test_tools/test_screenshot_tool.py",  # 录制依赖 standalone 下的自研组件，不随开源导出
    "README.md",  # 开源版 README 由本脚本另写
    "env.example",
)

# 测试文件引用这些内容时自动跳过（否则开源版收集阶段就 ImportError）：
# 商业化包，以及未随开源导出的模块（standalone 下的自研组件等）
SKIP_TEST_TOKENS = ("src.account", "src.telemetry", "makevideo", "standalone")

# 复制后按行净化：删掉含这些关键词的整行（抹掉 App 分发与私有域名段落）
LINE_FILTERS = {
    "gomatrix/README.md": ("xuan-note", "App 分发", "APK", "安装包", "官网主页"),
    "docs/系统术语表.md": ("TelemetryTracker", "license-server"),
}

# 复制后行内替换：把商业化措辞换成中性表述（保留行结构）
REPLACEMENTS = {
    "src/ui/web/src/views/PersonalView.tsx": (
        ('message="免费试用中（1 小时）"', 'message="账户状态"'),
        ('message="免费试用已结束"', 'message="需要登录"'),
        ('description={account.gate_reason || "试用期满后需登录账户"}', 'description={account.gate_reason || ""}'),
        ('description={account.gate_reason || "请登录后继续使用"}', 'description={account.gate_reason || ""}'),
        ('description={account.gate_reason || "需付费后继续使用（720h 免费额度）"}', 'description={account.gate_reason || ""}'),
    ),
    "docs/manual/16-CLI与WebUI.md": (
        ("用量（侧栏独立路由，登录后可见）", "用量（侧栏独立路由）"),
        ("配置（侧栏独立路由，登录后可见）", "配置（侧栏独立路由）"),
    ),
}

# 复制后整节删除：从该标题行删到下一个同级或更高级标题
SECTION_DROPS = {
    "docs/系统术语表.md": ("## license-server 与遥测",),
}

SKIP_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
    "node_modules",
    "dist",
    "build",
    ".venv",
}

# 残留扫查：命中即列入报告，人工判断删还是改
SCAN_PATTERNS = (
    (r"xuan-note|xuan_note", "私有域名"),
    (r"huang-renqiang", "Gitee 账号名"),
    (r"f8681f32", "发布令牌"),
    (r"coara-release", "私有发布仓库"),
    (r"license-server", "许可证服务"),
    (r"src\.telemetry|src\.account", "已剔除的商业化包引用"),
    (r"免费试用|需付费|付费后|登录后可见|配额已超", "商业化文案"),
    (r"TelemetryTracker|遥测上报", "遥测实现"),
)

SCAN_SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".ts",
    ".tsx",
    ".json",
    ".ps1",
    ".sh",
    ".css",
    ".html",
}
SCAN_SKIP_DIRS = {"node_modules", ".git", "dist", "__pycache__"}


def _is_excluded(rel: str) -> bool:
    return any(rel == d or rel.startswith(d + "/") for d in EXCLUDE_DIRS) or rel in EXCLUDE_FILES


def _should_skip_test(path: Path) -> bool:
    """测试文件引用了未导出的模块就跳过。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(token in text for token in SKIP_TEST_TOKENS)


def _copy_tree(src: Path, dst: Path) -> tuple[int, int]:
    """复制一个顶层条目，返回（文件数, 跳过数）。"""
    files = skipped = 0
    if src.is_file():
        if not _is_excluded(src.name):
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            files += 1
        return files, skipped
    for path in src.rglob("*"):
        rel_parts = path.relative_to(REPO).parts
        if any(part in SKIP_DIR_NAMES for part in rel_parts):
            continue
        rel = path.relative_to(REPO).as_posix()
        if _is_excluded(rel):
            if path.is_file():
                skipped += 1
            continue
        if path.is_file() and rel.startswith("tests/") and path.suffix == ".py" and _should_skip_test(path):
            skipped += 1
            continue
        target = dst / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        files += 1
    return files, skipped


def _apply_line_filters(root: Path) -> None:
    for rel, keywords in LINE_FILTERS.items():
        path = root / rel
        if not path.is_file():
            continue
        kept = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not any(keyword in line for keyword in keywords)
        ]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def _drop_sections(root: Path) -> None:
    for rel, titles in SECTION_DROPS.items():
        path = root / rel
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        output: list[str] = []
        dropping = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                level = len(stripped) - len(stripped.lstrip("#"))
                if dropping and level <= 2:
                    dropping = False
            if stripped in titles:
                dropping = True
                continue
            if not dropping:
                output.append(line)
        path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _apply_replacements(root: Path) -> None:
    for rel, pairs in REPLACEMENTS.items():
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for old, new in pairs:
            text = text.replace(old, new)
        path.write_text(text, encoding="utf-8")


def _scan(root: Path) -> list[tuple[str, str, int, str]]:
    hits: list[tuple[str, str, int, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SCAN_SUFFIXES:
            continue
        if any(part in SCAN_SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.name == "export_open_source.py":
            continue
        if path.as_posix().endswith("src/ext/__init__.py"):
            continue  # 接入位要写明实现包路径，不算残留
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern, label in SCAN_PATTERNS:
            match = re.search(pattern, text)
            if not match:
                continue
            line = text.count("\n", 0, match.start()) + 1
            rows = text.splitlines()
            snippet = rows[line - 1].strip()[:110] if rows else ""
            if "镜像仓" in snippet:
                continue  # README 有意保留的 Gitee 镜像说明，不算残留
            hits.append((path.relative_to(root).as_posix(), label, line, snippet))
    return hits


README = """# coara

[![CI](https://github.com/Heyrenqiang/coara-open/actions/workflows/ci.yml/badge.svg)](https://github.com/Heyrenqiang/coara-open/actions/workflows/ci.yml)

多智能体运行时，个人 AI 助手的内核。Python 3.11+ / asyncio。

> 镜像仓：[Gitee coara-open](https://gitee.com/huang-renqiang_admin/coara-open)（内容一致，主仓在 GitHub）

## 它是什么

一个无头内核，里面跑多路工作空间、多路会话，经统一输出路由连接多个显示端，
端可以是终端、浏览器或 Matrix 客户端。智能体在内核上创建并组织子智能体，
带工具、技能、可追踪执行与录像带式的会话存档。

- 内核：回合编排、会话生命周期、输出路由、端注册表
- 端：CLI、Web UI、Matrix 客户端
- 能力：内置工具、技能加载、子智能体委派、工作流、事件源、宝箱加密存储

## 快速开始

```
pip install -e ".[dev]"

coara            # 拉起常驻内核并接入终端
coara status     # 看运行时状态
```

首次启动若无可用 API key，会进入交互式配置向导。配置样例见
`config.yaml.example`、`providers.yaml.example` 与 `.env.example`。

## 目录

- `src/` 内核、端、工具、技能、领域服务
- `tests/` pytest 测试，默认档为冒烟集
- `skills/` 内置技能包
- `docs/` 架构与设计文档，入口 `docs/README.md`
- `wdl/` 独立工作流引擎与画布工作台
- `gomatrix/` 轻量 Matrix homeserver，Go 实现

## 开发

```
pytest tests/ -q          # 默认测试
ruff check . && ruff format .
```

架构约定见 `docs/架构契约.md`，开发总览见 `AGENTS.md`。

## 许可

MIT，见 `LICENSE`。
"""

CONTRIBUTING = """# 参与开发

欢迎提交 issue 与 pull request。

## 起步

1. 克隆仓库，`pip install -e ".[dev,office]"`
2. 跑 `pytest tests/ -q` 确认基线全绿
3. 改代码，保持 `ruff check .` 与 `ruff format .` 干净
4. 提交前跑一遍默认测试

## 约定

- 架构分层见 `docs/架构契约.md`，依赖只能向下
- 代码风格与命名见 `AGENTS.md`，行宽 120，模块以 `from __future__ import annotations` 开头
- 面向用户的文案中文优先、朴素、有信息量
- 新增或改动行为请补测试，测试放 `tests/` 对应子系统目录

## 提交信息

用简短的祈使句，能加范围前缀就加，例如 `fix(view) ...`、`feat(tools) ...`。
"""

LICENSE = """MIT License

Copyright (c) 2026 coara contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="导出白名单开源副本到目标目录")
    parser.add_argument("--target", required=True, help="目标目录，例如 D:\\code_ws\\coara")
    parser.add_argument("--force", action="store_true", help="目标已存在时先清空")
    args = parser.parse_args()

    target = Path(args.target).expanduser().resolve()
    if target == REPO:
        print("目标不能是主仓库本身")
        return 2
    if target.exists():
        if not args.force:
            print(f"目标已存在：{target}（要覆盖加 --force）")
            return 2
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    total = skipped = 0
    for name in INCLUDE_TOP:
        src = REPO / name
        if not src.exists():
            print(f"  [缺] {name}")
            continue
        files, skip = _copy_tree(src, target / name)
        total += files
        skipped += skip
        print(f"  [复制] {name}  文件 {files}  剔除 {skip}")

    _apply_line_filters(target)
    _apply_replacements(target)
    _drop_sections(target)

    readme_template = REPO / "docs" / "open-source" / "README.md"
    contributing_template = REPO / "docs" / "open-source" / "CONTRIBUTING.md"
    (target / "README.md").write_text(
        readme_template.read_text(encoding="utf-8") if readme_template.is_file() else README,
        encoding="utf-8",
    )
    (target / "CONTRIBUTING.md").write_text(
        contributing_template.read_text(encoding="utf-8") if contributing_template.is_file() else CONTRIBUTING,
        encoding="utf-8",
    )
    (target / "LICENSE").write_text(LICENSE, encoding="utf-8")

    print(f"\n共复制 {total} 个文件，剔除 {skipped} 个；已写入 README / CONTRIBUTING / LICENSE")

    hits = _scan(target)
    if hits:
        print(f"\n残留痕迹 {len(hits)} 处，需要人工判断：")
        for rel, label, line, snippet in hits:
            print(f"  {rel}:{line}  [{label}]  {snippet}")
    else:
        print("\n未发现残留商业化痕迹")
    return 0


if __name__ == "__main__":
    sys.exit(main())
