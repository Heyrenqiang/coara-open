"""Vault directory layout under coara Home.

Locked at rest::

    vault/
      vault.meta.json
      sealed/<uuid>.cvault   # encrypted file payloads (opaque)

Unlocked working tree (plaintext, wiped on lock)::

    vault/open/**            # LLM uses normal read/write/edit here
"""

from __future__ import annotations

from pathlib import Path

from src.core.coara_home import user_dir_for_home

ENTRY_SUFFIX = ".cvault"
SEALED_DIRNAME = "sealed"
OPEN_DIRNAME = "open"


def resolve_vault_dir(coara_home: Path) -> Path:
    home = Path(coara_home).expanduser().resolve()
    return user_dir_for_home(home) / "assets" / "vault"


def vault_meta_path(vault_dir: Path) -> Path:
    return vault_dir / "vault.meta.json"


def vault_sealed_dir(vault_dir: Path) -> Path:
    return vault_dir / SEALED_DIRNAME


def vault_open_dir(vault_dir: Path) -> Path:
    return vault_dir / OPEN_DIRNAME


def ensure_vault_layout(vault_dir: Path) -> None:
    vault_dir.mkdir(parents=True, exist_ok=True)
    vault_sealed_dir(vault_dir).mkdir(parents=True, exist_ok=True)
