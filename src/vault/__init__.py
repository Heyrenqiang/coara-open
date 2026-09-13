"""Encrypted personal vault (locked folder → open/plaintext working tree)."""

from __future__ import annotations

from src.vault.service import VaultService, get_vault_service

__all__ = [
    "VaultService",
    "get_vault_service",
]
