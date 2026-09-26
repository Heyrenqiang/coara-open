"""Product defaults shared by CLI / Matrix connect (end-user install contract)"""

from __future__ import annotations

# GoMatrix local homeserver (PC ↔ coara). Phone uses Dashboard QR② (tunnel).
MATRIX_SERVER_NAME = "coara.local"
MATRIX_PORT = 8008
MATRIX_HOMESERVER = f"http://127.0.0.1:{MATRIX_PORT}"
MATRIX_BOT_USER = f"@coara:{MATRIX_SERVER_NAME}"

# End-user providers.yaml default (templates/providers.yaml).
DEFAULT_PROVIDER = "deepseek"
