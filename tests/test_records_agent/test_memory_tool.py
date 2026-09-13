"""Legacy config gate tests; tool coverage lives in test_records/."""

from __future__ import annotations

from src.core.types import RecordsConfig


def test_records_config_defaults_on():
    cfg = RecordsConfig()
    assert cfg.enabled is True
    assert cfg.daily_curator_enabled is True


def test_records_enabled_gate_for_lifecycle():
    """Document the gate used by root_lifecycle: agent subtree when enabled."""
    off = RecordsConfig(enabled=False)
    assert not (off is not None and getattr(off, "enabled", False))
    on = RecordsConfig(enabled=True)
    assert on.enabled is True
