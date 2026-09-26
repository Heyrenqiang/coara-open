"""具身器官适配层：手 actuate / 嘴巴 voice（字幕协同） / 章节 mark。

调用即真实发生（真实键鼠 / 真实放声+同文烧字），引擎在 standalone/makevideo。
有活动录制会话时动作自动写入时间轴；voice 放声同时 Tee 留底，停录自动混流。
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

_STANDALONE = Path(__file__).resolve().parents[4] / "standalone"


def _engine(name: str):
    if str(_STANDALONE) not in sys.path:
        sys.path.insert(0, str(_STANDALONE))
    return importlib.import_module(f"makevideo.engine.{name}")


def actuate_run(workspace: str | Path, kind: str, **kw) -> str:
    """手：执行一次真实键鼠动作。"""
    actuate = _engine("actuate")
    session = _engine("session")
    rec = actuate.actuate(kind, **kw)
    logged = session.log_action(workspace, rec["action"], **{k: v for k, v in rec.items() if k != "action"})
    return json.dumps(
        {"ok": True, "t": logged.get("t") if logged else None, **rec},
        ensure_ascii=False,
        default=str,
    )


def voice_say(
    workspace: str | Path,
    text: str,
    engine: str = "edge",
    speaker: str | None = None,
    caption: bool = True,
    wait: bool = True,
) -> str:
    """嘴巴：讲一句话。caption=True 时同文字幕协同上屏、说完即隐。

    wait=True 阻塞到说完（留底/时间轴精准）；wait=False 后台讲（边说边做），
    由 CLI 子进程完成协同/留底/记轴，立即返回。录制中留底停录自动混流进原片音轨。
    """
    if not wait:
        cmd = [
            sys.executable,
            "-m",
            "makevideo",
            "-w",
            str(workspace),
            "voice",
            "--text",
            text,
            "--engine",
            engine,
        ]
        if speaker:
            cmd += ["--voice", speaker]
        if not caption:
            cmd += ["--no-caption"]
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": str(_STANDALONE),
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(cmd, **kwargs)
        return json.dumps({"ok": True, "background": True, "text": text}, ensure_ascii=False)
    narrate = _engine("narrate")
    session = _engine("session")
    active = session.active_session(workspace)
    res = narrate.narrate(text, engine=engine, speaker=speaker, capture_dir=active, with_caption=caption)
    capture = None
    if active is not None and res["capture"] is not None:
        t0 = session.session_t0(active)
        if t0 is not None:
            src = Path(res["capture"])
            capture = active / f"voice_{res['started_at'] - t0:.3f}{src.suffix}"
            src.replace(capture)
    logged = session.log_action(
        workspace,
        "voice",
        at=res["started_at"],
        text=text,
        engine=res["engine"],
        caption=caption,
        capture=str(capture) if capture else None,
    )
    return json.dumps(
        {
            "ok": True,
            "engine": res["engine"],
            "caption": caption,
            "t": logged.get("t") if logged else None,
            "capture": str(capture) if capture else None,
        },
        ensure_ascii=False,
    )


def mark_scene(workspace: str | Path, scene: str) -> str:
    """章节边界：写进动作时间轴（需在录制中）。"""
    session = _engine("session")
    logged = session.log_action(workspace, "mark", scene=scene)
    if not logged:
        raise RuntimeError("无活动录制会话，无法 mark")
    return json.dumps({"ok": True, "t": logged.get("t")}, ensure_ascii=False)
