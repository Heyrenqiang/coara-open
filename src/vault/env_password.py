"""Read vault master password from environment (same pattern as API keys)."""

from __future__ import annotations

import os

COARA_VAULT_PASSWORD_ENV = "COARA_VAULT_PASSWORD"


def vault_password_from_env() -> str | None:
    value = os.environ.get(COARA_VAULT_PASSWORD_ENV, "").strip()
    return value or None
