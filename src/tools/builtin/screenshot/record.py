"""屏幕录制（连续捕捉）——截屏套薄适配层，引擎在 standalone/makevideo。

与截图同族：截图是单帧捕捉，录制是连续捕捉，都不改环境。
voice/subtitle/actuate 是具身器官（行为类），不在此层，仍走 CLI。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_STANDALONE = Path(__file__).resolve().parents[4] / "standalone"


def _session_module():
    if str(_STANDALONE) not in sys.path:
        sys.path.insert(0, str(_STANDALONE))
    from makevideo.engine import session

    return session


def record_start(workspace: str | Path, fps: int = 30, area: str | None = None) -> str:
    """开录：启动 ffmpeg gdigrab 连续捕捉屏幕。返回会话信息 JSON。"""
    session = _session_module()
    if session.active_session(workspace):
        raise RuntimeError("已有活动录制会话，先 record_stop")
    r = session.new_session(workspace, fps=int(fps or 30), region=area or None)
    return json.dumps({"ok": True, "session": r["session"], "video": r["video"]}, ensure_ascii=False)


def record_stop(workspace: str | Path) -> str:
    """停录：优雅收尾，voice 留底自动混流进原片。返回结果 JSON。"""
    session = _session_module()
    active = session.active_session(workspace)
    if not active:
        raise RuntimeError("无活动录制会话")
    r = session.stop_session(workspace, active)
    return json.dumps(r, ensure_ascii=False, default=str)


def record_status(workspace: str | Path) -> str:
    """录制状态：{"recording": bool, "session"?, "pid"?}。"""
    session = _session_module()
    active = session.active_session(workspace)
    if not active:
        return json.dumps({"recording": False}, ensure_ascii=False)
    try:
        meta = json.loads((Path(active) / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    return json.dumps({"recording": True, "session": str(active), "pid": meta.get("pid")}, ensure_ascii=False)
