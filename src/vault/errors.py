"""Vault-specific errors."""

from __future__ import annotations


class VaultError(Exception):
    """Base vault error."""


class VaultLockedError(VaultError):
    """Vault is locked; unlock required for sensitive operations."""


class VaultNotInitializedError(VaultError):
    """Vault has not been set up yet."""


class VaultAuthError(VaultError):
    """Incorrect vault password."""
