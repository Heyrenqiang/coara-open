"""仓库代码量统计 (cloc 风格简易实现).

用法: python scripts/count_loc.py [--json out.json]
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 目录名剪枝(依赖 / 构建产物 / 缓存)
PRUNE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".gradle",
    ".idea", "build", "dist", "target", ".next", "out",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "egg-info", "staticfiles", "coverage", "htmlcov",
}
# 相对路径剪枝
PRUNE_REL = {
    "deploy/release/cache",
    "deploy/release/out",
    "deploy/release/wheelhouse",
    "android-app/design-preview",
    # 运行时 / 工作目录 / 用户上传(非仓库源码)
    ".coara",
    ".workbuddy",
    ".codeartsdoer",
    "uploads",
    "MagicMock",
}
# 第三方目录(单独统计, 不计入自有代码)
THIRD_PARTY_REL = {"android-app/third_party"}

# 扩展名 -> 语言, 注释行前缀/块
LANGS = {
    ".py": ("Python", ("#",), (('"""', '"""'), ("'''", "'''"))),
    ".pyi": ("Python", ("#",), ()),
    ".ts": ("TypeScript", ("//",), (("/*", "*/"),)),
    ".tsx": ("TypeScript", ("//",), (("/*", "*/"),)),
    ".js": ("JavaScript", ("//",), (("/*", "*/"),)),
    ".jsx": ("JavaScript", ("//",), (("/*", "*/"),)),
    ".mjs": ("JavaScript", ("//",), (("/*", "*/"),)),
    ".cjs": ("JavaScript", ("//",), (("/*", "*/"),)),
    ".vue": ("Vue", ("//",), (("/*", "*/"), ("<!--", "-->"))),
    ".go": ("Go", ("//",), (("/*", "*/"),)),
    ".kt": ("Kotlin", ("//",), (("/*", "*/"),)),
    ".kts": ("Kotlin", ("//",), (("/*", "*/"),)),
    ".java": ("Java", ("//",), (("/*", "*/"),)),
    ".cs": ("C#", ("//",), (("/*", "*/"),)),
    ".c": ("C", ("//",), (("/*", "*/"),)),
    ".h": ("C/C++ Header", ("//",), (("/*", "*/"),)),
    ".cpp": ("C++", ("//",), (("/*", "*/"),)),
    ".hpp": ("C++", ("//",), (("/*", "*/"),)),
    ".cc": ("C++", ("//",), (("/*", "*/"),)),
    ".rs": ("Rust", ("//",), (("/*", "*/"),)),
    ".swift": ("Swift", ("//",), (("/*", "*/"),)),
    ".dart": ("Dart", ("//",), (("/*", "*/"),)),
    ".scala": ("Scala", ("//",), (("/*", "*/"),)),
    ".proto": ("Protobuf", ("//",), (("/*", "*/"),)),
    ".php": ("PHP", ("//", "#"), (("/*", "*/"),)),
    ".rb": ("Ruby", ("#",), ()),
    ".sh": ("Shell", ("#",), ()),
    ".bash": ("Shell", ("#",), ()),
    ".zsh": ("Shell", ("#",), ()),
    ".ps1": ("PowerShell", ("#",), (("<#", "#>"),)),
    ".bat": ("Batch", ("REM", "rem", "::"), ()),
    ".cmd": ("Batch", ("REM", "rem", "::"), ()),
    ".sql": ("SQL", ("--",), (("/*", "*/"),)),
    ".yaml": ("YAML", ("#",), ()),
    ".yml": ("YAML", ("#",), ()),
    ".toml": ("TOML", ("#",), ()),
    ".ini": ("INI", (";", "#"), ()),
    ".cfg": ("INI", (";", "#"), ()),
    ".properties": ("Properties", ("#", "!"), ()),
    ".json": ("JSON", (), ()),
    ".xml": ("XML", (), (("<!--", "-->"),)),
    ".html": ("HTML", (), (("<!--", "-->"),)),
    ".htm": ("HTML", (), (("<!--", "-->"),)),
    ".css": ("CSS", (), (("/*", "*/"),)),
    ".scss": ("SCSS", ("//",), (("/*", "*/"),)),
    ".less": ("LESS", ("//",), (("/*", "*/"),)),
    ".md": ("Markdown", (), (("<!--", "-->"),)),
    ".rst": ("reStructuredText", ("..",), ()),
    ".gradle": ("Gradle", ("//",), (("/*", "*/"),)),
    ".cmake": ("CMake", ("#",), ()),
    ".dockerfile": ("Dockerfile", ("#",), ()),
}

# 按扩展名判断是否"代码"(可用于计算代码行)
CODE_EXTS = set(LANGS)

DOCKERFILE_NAMES = {"Dockerfile", "dockerfile", "Containerfile"}


def classify(rel_parts: tuple[str, ...], name: str) -> str | None:
    if name in DOCKERFILE_NAMES:
        return ".dockerfile"
    if name in ("Makefile", "makefile", "GNUmakefile"):
        return ".py" if False else "make"
    ext = os.path.splitext(name)[1].lower()
    if ext in LANGS:
        return ext
    return None


def count_file(path: Path, ext: str) -> tuple[int, int, int, int]:
    """返回 (total, code, comment, blank)."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return (0, 0, 0, 0)
    lines = text.splitlines()
    if ext == "make":
        line_c, block_c = ("#",), ()
    else:
        _, line_c, block_c = LANGS[ext]
    total = code = comment = blank = 0
    in_block: str | None = None
    for raw in lines:
        total += 1
        s = raw.strip()
        if not s:
            blank += 1
            continue
        if in_block:
            comment += 1
            if in_block in s:
                in_block = None
            continue
        if s.startswith(line_c) if line_c else False:
            comment += 1
            continue
        matched = False
        for open_c, close_c in block_c:
            idx = s.find(open_c)
            if idx != -1:
                rest = s[idx + len(open_c):]
                if close_c in rest:
                    # 同行闭合
                    before = s[:idx]
                    if before.strip():
                        code += 1
                    else:
                        comment += 1
                else:
                    in_block = close_c
                    before = s[:idx]
                    if before.strip():
                        code += 1
                    else:
                        comment += 1
                matched = True
                break
        if matched:
            continue
        code += 1
    return (total, code, comment, blank)


def main() -> None:
    json_out = None
    if "--json" in sys.argv:
        json_out = sys.argv[sys.argv.index("--json") + 1]

    by_lang: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])  # files,total,code,comment,blank
    by_mod_lang: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: defaultdict(lambda: [0, 0, 0, 0, 0])
    )
    third_by_lang: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])

    for dirpath, dirnames, filenames in os.walk(ROOT):
        d = Path(dirpath)
        rel = d.relative_to(ROOT)
        rel_str = rel.as_posix() if rel != Path(".") else ""

        # 剪枝
        dirnames[:] = [x for x in dirnames if x not in PRUNE_DIRS]
        skip = False
        for prune in PRUNE_REL:
            if rel_str == prune or rel_str.startswith(prune + "/"):
                skip = True
                break
        if skip:
            dirnames[:] = []
            continue

        is_third = any(
            rel_str == tp or rel_str.startswith(tp + "/") for tp in THIRD_PARTY_REL
        )
        # 顶层模块
        top = rel.parts[0] if rel.parts else "(root)"

        for fn in filenames:
            ext = classify(tuple(rel.parts), fn)
            if ext is None:
                continue
            res = count_file(d / fn, ext)
            if res[0] == 0:
                continue
            lang = LANGS[ext][0] if ext != "make" else "Makefile"
            tgt = third_by_lang if is_third else by_lang
            tgt[lang][0] += 1
            for i in range(4):
                tgt[lang][i + 1] += res[i]
            if not is_third:
                ml = by_mod_lang[top][lang]
                ml[0] += 1
                for i in range(4):
                    ml[i + 1] += res[i]

    def fmt(rows: dict[str, list[int]], title: str) -> str:
        out = [f"\n### {title}"]
        out.append(f"{'语言':<20}{'文件':>8}{'总行':>10}{'代码':>10}{'注释':>10}{'空行':>10}")
        order = sorted(rows.items(), key=lambda kv: -kv[1][2])
        tf = tt = tc = tm = tb = 0
        for k, v in order:
            out.append(f"{k:<20}{v[0]:>8}{v[1]:>10}{v[2]:>10}{v[3]:>10}{v[4]:>10}")
            tf += v[0]
            tt += v[1]
            tc += v[2]
            tm += v[3]
            tb += v[4]
        out.append(f"{'合计':<20}{tf:>8}{tt:>10}{tc:>10}{tm:>10}{tb:>10}")
        return "\n".join(out)

    print("=" * 72)
    print("coara v8 仓库代码量统计")
    print("=" * 72)
    print(fmt(by_lang, "按语言 (自有代码, 已排除依赖/构建产物/第三方)"))
    print(fmt(third_by_lang, "第三方 vendored (android-app/third_party)"))

    # 分类汇总: 代码 / 文档 / 配置数据
    doc_kinds = {"Markdown", "reStructuredText"}
    data_kinds = {"JSON", "XML", "YAML", "TOML", "INI", "Properties"}
    cat = {"源码 (编程语言)": [0, 0, 0, 0, 0], "文档": [0, 0, 0, 0, 0],
           "配置/数据": [0, 0, 0, 0, 0]}
    for lang, v in by_lang.items():
        k = "文档" if lang in doc_kinds else ("配置/数据" if lang in data_kinds else "源码 (编程语言)")
        for i in range(5):
            cat[k][i] += v[i]
    print("\n### 分类汇总 (自有)")
    print(f"{'类别':<20}{'文件':>8}{'总行':>10}{'代码':>10}{'注释':>10}{'空行':>10}")
    for k, v in cat.items():
        print(f"{k:<20}{v[0]:>8}{v[1]:>10}{v[2]:>10}{v[3]:>10}{v[4]:>10}")
    tot = [sum(cat[c][i] for c in cat) for i in range(5)]
    print(f"{'合计':<20}{tot[0]:>8}{tot[1]:>10}{tot[2]:>10}{tot[3]:>10}{tot[4]:>10}")

    print("\n### 按顶层模块 (代码行)")
    mods = []
    for mod, langs in by_mod_lang.items():
        tf = sum(v[0] for v in langs.values())
        tt = sum(v[1] for v in langs.values())
        tc = sum(v[2] for v in langs.values())
        mods.append((mod, tf, tt, tc, langs))
    mods.sort(key=lambda x: -x[3])
    print(f"{'模块':<20}{'文件':>8}{'总行':>10}{'代码行':>10}")
    for mod, tf, tt, tc, _langs in mods:
        print(f"{mod:<20}{tf:>8}{tt:>10}{tc:>10}")
    print(f"\n顶层模块数: {len(mods)}")

    print("\n### src/ 各子系统 (代码行)")
    # 单独统计 src 下一级
    src_dir = ROOT / "src"
    src_sys: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    if src_dir.is_dir():
        for sub_d in sorted(src_dir.iterdir()):
            if not sub_d.is_dir():
                continue
            if sub_d.name in PRUNE_DIRS:
                continue
            agg = [0, 0, 0, 0, 0]
            for dp, dn, fns in os.walk(sub_d):
                dn[:] = [x for x in dn if x not in PRUNE_DIRS]
                for fn in fns:
                    ext = classify((), fn)
                    if ext is None or ext == "make":
                        continue
                    r = count_file(Path(dp) / fn, ext)
                    if r[0] == 0:
                        continue
                    agg[0] += 1
                    for i in range(4):
                        agg[i + 1] += r[i]
            src_sys[sub_d.name] = agg
    print(f"{'子系统':<20}{'文件':>8}{'总行':>10}{'代码行':>10}")
    for k, v in sorted(src_sys.items(), key=lambda kv: -kv[1][2]):
        print(f"{k:<20}{v[0]:>8}{v[1]:>10}{v[2]:>10}")

    if json_out:
        data = {
            "by_lang": dict(by_lang),
            "third_party": dict(third_by_lang),
            "by_module": {m: {"files": tf, "total": tt, "code": tc} for m, tf, tt, tc, _ in mods},
            "src_subsystems": src_sys,
        }
        Path(json_out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 已写入 {json_out}")


if __name__ == "__main__":
    main()
