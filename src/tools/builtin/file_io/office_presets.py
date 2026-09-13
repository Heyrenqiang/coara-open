"""Typography and layout presets for Word / Excel builders."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WordPreset:
    """Built-in Word layout preset."""

    body_font: str
    body_size_pt: float
    title_font: str
    title_size_pt: float
    h1_font: str
    h1_size_pt: float
    h2_font: str
    h2_size_pt: float
    h3_font: str
    h3_size_pt: float
    h4_font: str
    h4_size_pt: float
    line_spacing_pt: float
    top_margin_mm: float
    bottom_margin_mm: float
    left_margin_mm: float
    right_margin_mm: float
    first_line_indent_chars: int = 2


WORD_PRESETS: dict[str, WordPreset] = {
    # General research / industry reports (default).
    "report": WordPreset(
        body_font="宋体",
        body_size_pt=12.0,
        title_font="黑体",
        title_size_pt=18.0,
        h1_font="黑体",
        h1_size_pt=16.0,
        h2_font="黑体",
        h2_size_pt=14.0,
        h3_font="楷体",
        h3_size_pt=12.0,
        h4_font="楷体",
        h4_size_pt=11.0,
        line_spacing_pt=22.0,
        top_margin_mm=25.4,
        bottom_margin_mm=25.4,
        left_margin_mm=31.7,
        right_margin_mm=31.7,
        first_line_indent_chars=2,
    ),
    # GB/T 9704-2012 common defaults (body + margins).
    "official": WordPreset(
        body_font="仿宋",
        body_size_pt=16.0,
        title_font="小标宋",
        title_size_pt=22.0,
        h1_font="黑体",
        h1_size_pt=16.0,
        h2_font="楷体",
        h2_size_pt=16.0,
        h3_font="仿宋",
        h3_size_pt=16.0,
        h4_font="仿宋",
        h4_size_pt=14.0,
        line_spacing_pt=28.0,
        top_margin_mm=37.0,
        bottom_margin_mm=35.0,
        left_margin_mm=28.0,
        right_margin_mm=26.0,
        first_line_indent_chars=2,
    ),
    # Academic papers / theses.
    "academic": WordPreset(
        body_font="宋体",
        body_size_pt=12.0,
        title_font="黑体",
        title_size_pt=16.0,
        h1_font="黑体",
        h1_size_pt=14.0,
        h2_font="黑体",
        h2_size_pt=12.0,
        h3_font="宋体",
        h3_size_pt=12.0,
        h4_font="宋体",
        h4_size_pt=11.0,
        line_spacing_pt=20.0,
        top_margin_mm=25.4,
        bottom_margin_mm=25.4,
        left_margin_mm=31.7,
        right_margin_mm=31.7,
        first_line_indent_chars=2,
    ),
}


@dataclass(frozen=True, slots=True)
class ExcelPreset:
    """Built-in Excel table preset."""

    title_font: str
    title_size_pt: float
    header_font: str
    header_size_pt: float
    body_font: str
    body_size_pt: float
    border_style: str  # official | sanxian


EXCEL_PRESETS: dict[str, ExcelPreset] = {
    "generic": ExcelPreset(
        title_font="黑体",
        title_size_pt=14.0,
        header_font="黑体",
        header_size_pt=11.0,
        body_font="宋体",
        body_size_pt=11.0,
        border_style="official",
    ),
    "official": ExcelPreset(
        title_font="黑体",
        title_size_pt=14.0,
        header_font="黑体",
        header_size_pt=12.0,
        body_font="仿宋",
        body_size_pt=12.0,
        border_style="official",
    ),
    "sanxian": ExcelPreset(
        title_font="黑体",
        title_size_pt=14.0,
        header_font="黑体",
        header_size_pt=11.0,
        body_font="宋体",
        body_size_pt=11.0,
        border_style="sanxian",
    ),
}
