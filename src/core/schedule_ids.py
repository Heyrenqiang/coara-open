"""ID helpers for cron/event schedulers (reminders)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime


def generate_scheduled_id(prefix: str, *entropy_parts: str) -> str:
    """Build a time-stamped, collision-resistant scheduler record id."""
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    joined = ":".join(str(part) for part in entropy_parts if str(part).strip())
    digest = hashlib.sha256(joined.encode()).hexdigest()[:8] if joined else "00000000"
    return f"{prefix}_{ts}_{digest}_{uuid.uuid4().hex[:6]}"
