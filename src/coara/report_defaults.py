"""Built-in developer report endpoint (shipped with coara; not a user setting).

Before release, set both constants to a **named** Cloudflare tunnel URL + secret.
Quick tunnels (``*.trycloudflare.com``) change on restart — do not ship them.

Local override (dev only): ``COARA_REPORT_WEBHOOK_URL`` / ``COARA_REPORT_WEBHOOK_SECRET``.
"""

from __future__ import annotations

# Empty = channel not ready until filled for a release (or overridden by env).
DEFAULT_REPORT_WEBHOOK_URL = ""
DEFAULT_REPORT_WEBHOOK_SECRET = ""
