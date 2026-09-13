"""In-process vault unlock session — idle deadline from last activity."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.vault.errors import VaultLockedError

# Idle after last activity *inside* the open/ boundary; then VaultService seals+wipes.
DEFAULT_IDLE_TTL_SECONDS = 120.0


@dataclass(slots=True)
class VaultSession:
    """Holds DEK in memory; expiry is checked by VaultService (full lock/seal)."""

    ttl_seconds: float = DEFAULT_IDLE_TTL_SECONDS
    _dek: bytearray | None = field(default=None, repr=False)
    _deadline: float = 0.0

    def unlock(self, dek: bytes, *, persistent: bool = False) -> None:
        """Unlock and start/refresh idle deadline (persistent disables auto-idle)."""
        # Copy into a mutable buffer so lock() can zero the stored key material.
        self._dek = bytearray(dek)
        if persistent:
            self._deadline = float("inf")
        else:
            self._deadline = time.monotonic() + self.ttl_seconds

    def lock(self) -> None:
        if self._dek is not None:
            self._dek[:] = b"\x00" * len(self._dek)
            self._dek = None
        self._deadline = 0.0

    def has_dek(self) -> bool:
        return self._dek is not None

    def is_expired(self) -> bool:
        if self._dek is None:
            return True
        if self._deadline == float("inf"):
            return False
        return time.monotonic() > self._deadline

    def touch(self) -> None:
        """Refresh idle deadline from last vault-folder activity."""
        if self._dek is None:
            return
        if self._deadline == float("inf"):
            return
        self._deadline = time.monotonic() + self.ttl_seconds

    def get_dek(self) -> bytes | None:
        return bytes(self._dek) if self._dek is not None else None

    def require_dek(self) -> bytes:
        if self._dek is None or self.is_expired():
            raise VaultLockedError("vault is locked")
        self.touch()
        return bytes(self._dek)
