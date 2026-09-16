"""Syntax highlighting helpers for diff/read CLI output."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pygments.lexers import TextLexer, get_lexer_by_name, guess_lexer_for_filename
from pygments.token import Token
from rich.text import Text

from src.core.logger import logger

_EXT_LEXER = {
    "py": "python",
    "js": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "jsx": "javascript",
    "md": "markdown",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "rs": "rust",
    "go": "go",
    "java": "java",
    "kt": "kotlin",
    "sql": "sql",
    "sh": "bash",
    "bash": "bash",
    "html": "html",
    "css": "css",
    "xml": "xml",
}


def _rich_style(token_type) -> str | None:
    while token_type:
        name = token_type.__class__.__name__
        if token_type is Token:
            break
        if name == "Keyword":
            return "bold cyan"
        if name in {"Name", "Name_Builtin", "Name_Function", "Name_Class"}:
            return "cyan"
        if name in {"String", "String_Affix", "String_Escape"}:
            return "green"
        if name in {"Number", "Number_Integer", "Number_Float"}:
            return "magenta"
        if name in {"Comment", "Comment_Single", "Comment_Multiline"}:
            return "dim"
        if name in {"Operator", "Punctuation"}:
            return "white"
        token_type = token_type.parent
    return None


@lru_cache(maxsize=64)
def _lexer_for_path(path: str):
    ext = Path(path).suffix.lstrip(".").lower()
    if ext in _EXT_LEXER:
        try:
            return get_lexer_by_name(_EXT_LEXER[ext])
        except Exception:
            logger.debug(f"pygments lexer {_EXT_LEXER[ext]!r} unavailable; fall back to guess")
    try:
        return guess_lexer_for_filename(path, "")
    except Exception:
        return TextLexer()


def highlight_code(text: str, path: str) -> Text:
    """Highlight a single line (or short fragment) for diff panels."""
    if not text:
        return Text("")
    lexer = _lexer_for_path(path)
    rich = Text()
    for token_type, value in lexer.get_tokens(text):
        rich.append(value, style=_rich_style(token_type))
    # HTML/XML lexers treat each diff line as an incomplete document and append
    # a trailing newline, which Rich tables render as a blank row per line.
    trailing_newlines = len(rich.plain) - len(rich.plain.rstrip("\n"))
    if trailing_newlines:
        rich.right_crop(trailing_newlines)
    return rich


def highlight_lines(block_text: str, path: str) -> list[Text] | None:
    """整块词法分析后按行切分（一次 lexer 调用，parser 状态跨行保留）。

    比逐行 ``highlight_code`` 快（无重复初始化开销），且多行字符串/块注释
    高亮更准。返回每行一个 ``Text``；行数无法与输入对齐（个别 lexer 对
    残缺文档的 token 行为）时返回 ``None``，调用方回退逐行/纯文本。
    """
    if not block_text:
        return []
    lexer = _lexer_for_path(path)
    lines: list[Text] = []
    current = Text()
    for token_type, value in lexer.get_tokens(block_text):
        if "\n" in value:
            parts = value.split("\n")
            for i, part in enumerate(parts):
                if i > 0:
                    lines.append(current)
                    current = Text()
                if part:
                    current.append(part, style=_rich_style(token_type))
        else:
            current.append(value, style=_rich_style(token_type))
    if current.plain:
        lines.append(current)
    return lines
