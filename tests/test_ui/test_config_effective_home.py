"""Test for the effective_coara_home field in the config envelope."""

from __future__ import annotations

from pathlib import Path

from src.core.coara_home import resolve_coara_home
from src.ui.control_plane import build_config_envelope


def test_build_config_envelope_includes_effective_coara_home() -> None:
    payload = build_config_envelope({})
    effective = payload["effective_coara_home"]
    assert isinstance(effective, str)
    assert effective
    # 与权威解析函数同值（config.coara_home 为空时回落到 cwd/.coara 或 COARA_HOME）
    assert Path(effective) == resolve_coara_home(Path.cwd(), None)
